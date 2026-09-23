"""定时任务与后台任务运行器。"""
from __future__ import annotations

import asyncio
import datetime
from typing import Any, Callable, Dict, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import app.config as cfgmod

scheduler = AsyncIOScheduler()

# 批量任务运行状态（网页轮询用）；单曲的进度看 queues
RUN: Dict[str, Any] = {
    "running": False, "kind": "", "stage": "", "detail": "",
    "done": 0, "total": 0, "recent": [], "last": {}, "started": "", "finished": "",
}


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def status() -> Dict[str, Any]:
    """批量任务状态 + 两个队列的实时快照"""
    from app import runner
    data = dict(RUN)
    try:
        data["queues"] = runner.snapshot()
    except Exception:  # noqa: BLE001
        data["queues"] = {}
    return data


def _progress(stage: str, detail: str, done: Optional[int] = None, total: Optional[int] = None) -> None:
    RUN["stage"] = stage
    RUN["detail"] = detail
    if done is not None:
        RUN["done"] = done
    if total is not None:
        RUN["total"] = total


async def _guard(kind: str, fn: Callable[[], Any]) -> None:
    if RUN["running"]:
        return
    RUN.update(running=True, kind=kind, stage="准备中", detail="", done=0, total=0,
               recent=[], started=_now(), finished="")
    try:
        summary = await fn()
        RUN["last"] = {"kind": kind, "ok": True, "summary": summary, "finished": _now()}
        print(f"[{kind}] 完成: {summary}", flush=True)
    except Exception as e:  # noqa: BLE001
        RUN["last"] = {"kind": kind, "ok": False, "error": f"{type(e).__name__}: {e}",
                       "finished": _now()}
        print(f"[{kind}] 异常: {type(e).__name__}: {e}", flush=True)
    finally:
        RUN.update(running=False, stage="", detail="", finished=_now())


def _fetch_coro(cfg: Dict[str, Any]):
    from app.sync.netease import fetch
    return fetch(cfg, _progress)


def _download_coro(cfg: Dict[str, Any]):
    from app.runner import download_all
    return download_all(cfg, _progress)


def _upload_coro(cfg: Dict[str, Any]):
    from app.runner import upload_all
    return upload_all(cfg, selected_only=True)


_CORO = {"sync": _fetch_coro, "download": _download_coro, "upload": _upload_coro}


async def run_sync() -> bool:
    """拉取歌单曲目（前台等待版，定时任务用）"""
    if RUN["running"]:
        return False
    await _guard("sync", lambda: _fetch_coro(cfgmod.load()))
    return True


async def run_download() -> bool:
    """下载待办曲目（前台等待版，定时任务用）"""
    if RUN["running"]:
        return False
    await _guard("download", lambda: _download_coro(cfgmod.load()))
    return True


async def run_upload() -> bool:
    """把本地已下载未上传的曲目补传到云盘（定时任务用）"""
    if RUN["running"]:
        return False
    await _guard("upload", lambda: _upload_coro(cfgmod.load()))
    return True


def start(kind: str, **kwargs: Any) -> bool:
    """网页按钮用：扔到后台跑，立刻返回"""
    if kind not in _CORO:
        return False
    if RUN["running"]:
        return False
    cfg = cfgmod.load()
    make = _CORO[kind]
    coro = make(cfg, **kwargs) if kwargs else make(cfg)
    asyncio.create_task(_guard(kind, lambda: coro))
    RUN.update(kind=kind, stage="启动中", detail="")
    return True


def reschedule() -> list:
    """按最新配置重建定时任务，配置页保存后立即生效（无需重启）"""
    cfg = cfgmod.load()
    sch = cfg.get("scheduler") or {}
    cloud = cfg.get("cloud") or {}
    for job_id in ("auto_sync", "auto_download", "auto_upload"):
        try:
            scheduler.remove_job(job_id)
        except Exception:  # noqa: BLE001
            pass
    try:
        h, m = map(int, str(sch.get("time", "02:00")).split(":"))
    except (ValueError, TypeError):
        h, m = 2, 0
    if sch.get("auto_sync"):
        scheduler.add_job(run_sync, "cron", hour=h, minute=m, id="auto_sync")
    if sch.get("auto_download"):
        scheduler.add_job(run_download, "cron", hour=h, minute=(m + 5) % 60, id="auto_download")
        if cloud.get("auto_upload"):
            scheduler.add_job(run_upload, "cron", hour=h, minute=(m + 20) % 60, id="auto_upload")
    # 歌单索引 + 歌单监控：按配置的「检查间隔」跑（默认 2 分钟）。
    # 两者同频：索引负责发现「哪个歌单变了」，监控负责把新歌排队下载。
    # 云盘索引保持 10 分钟（它只管云盘列表，开销比歌单大）。
    mon = cfg.get("monitor") or {}
    try:
        iv = max(1, min(60, int(mon.get("interval") or 2)))
    except (TypeError, ValueError):
        iv = 2
    for job_id in ("playlist_monitor", "playlist_index", "cloud_index"):
        try:
            scheduler.remove_job(job_id)
        except Exception:  # noqa: BLE001
            pass
    now = datetime.datetime.now()
    scheduler.add_job(refresh_playlist_index, "interval", minutes=iv, id="playlist_index",
                      replace_existing=True, next_run_time=now + datetime.timedelta(seconds=5))
    scheduler.add_job(run_monitor, "interval", minutes=iv, id="playlist_monitor",
                      replace_existing=True, next_run_time=now + datetime.timedelta(seconds=15))
    scheduler.add_job(refresh_cloud_index, "interval", minutes=10, id="cloud_index",
                      replace_existing=True, next_run_time=now + datetime.timedelta(seconds=20))
    # next_run_time 仅在调度器启动后才有值
    return [(j.id, str(getattr(j, "next_run_time", None) or "-")) for j in scheduler.get_jobs()]


async def refresh_cloud_index() -> None:
    """刷新云盘索引：歌单页靠它判断「这首歌在不在云盘」

    容器刚起来时同容器里的 ncm-api 还没就绪，第一次会失败 —— 所以失败后退避重试。
    """
    from app.cloud_index import index
    cfg = cfgmod.load()
    cookie = str(((cfg.get("platforms") or {}).get("netease") or {}).get("cookie") or "")
    if not cookie:
        return
    for attempt in range(3):
        r = await index.refresh(cookie)
        print(f"[cloud_index] 第{attempt + 1}次 {r}", flush=True)
        if r.get("ok"):
            return
        await asyncio.sleep(30 * (attempt + 1))


async def refresh_playlist_index() -> None:
    """刷新歌单索引（他所有网易云歌单里的歌曲 id）

    只重拉「变过」的歌单，稳态下几乎不花请求；云盘页用它判断「这首歌在不在歌单里」，
    歌单监控也用它找新歌。同样带失败退避（容器刚起来时 ncm-api 还没就绪）。
    """
    from app.playlist_index import index
    cfg = cfgmod.load()
    platform_cfg = (cfg.get("platforms") or {}).get("netease") or {}
    cookie = str(platform_cfg.get("cookie") or "")
    uid = str(platform_cfg.get("user_id") or "")
    if not cookie or not uid:
        return
    for attempt in range(3):
        r = await index.refresh(cookie, uid)
        print(f"[playlist_index] 第{attempt + 1}次 {r}", flush=True)
        if r.get("ok"):
            return
        await asyncio.sleep(30 * (attempt + 1))


async def run_monitor() -> None:
    """歌单监控：歌单新歌 / 云盘缺歌 → 自动排队下载（开关关着时直接跳过）"""
    from app.runner import monitor_playlists
    try:
        r = await monitor_playlists()
        if not r.get("skipped"):
            print(f"[monitor] {r}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[monitor] 异常: {type(e).__name__}: {e}", flush=True)


def start_scheduler() -> None:
    from app.runner import apply_config
    apply_config(cfgmod.load())
    reschedule()
    if not scheduler.running:
        scheduler.start()
    # 云盘索引 / 歌单索引 / 歌单监控 的周期任务统一在 reschedule() 里注册，
    # 这样配置页改了「检查间隔」保存后立即生效，无需重启。
