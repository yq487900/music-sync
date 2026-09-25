from fastapi import FastAPI, Form, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import asyncio
import base64
import hashlib
import io
import ipaddress
import json
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import aiohttp
from sqlalchemy import text

import app.cloud as cloud
import app.cloud_index as cloud_index
import app.config as config
import app.playlist_index as playlist_index
import app.lxsource as lxsource
import app.organize as organize
import app.platforms as platforms
from app.auth.netease_qr import NeteaseQR
from app.db.models import Playlist, SessionLocal, Track
from app.ncm import QUALITY_CHAIN, Ncm, NcmError
from app.runner import (DOWNLOADS, ORGANIZE, UPLOADS, _enrich_after_upload, add_to_playlists,
                        apply_config, enqueue_backfill, enqueue_download_one, enqueue_downloads,
                        enqueue_upload, enqueue_upload_paths, enqueue_uploads)
from app.scheduler import reschedule, start, start_scheduler, status
from app.sync.netease import SRC_OFFICIAL, source_label
from app.tagger import LAYOUT_LABELS
from jinja2 import Environment, FileSystemLoader

# ---------------------------------------------------------------- 缓存
# 账号歌单 / 歌单曲目都是低频变化的远端数据，缓存几分钟避免翻页反复请求
_CACHE: dict = {}
_SONG_CACHE: dict = {}   # 歌曲详情缓存（独立，避免占用 _CACHE 的 24 键配额）
_TTL_PLAYLISTS = 120
_TTL_TRACKS = 600
_TTL_CLOUD = 30
_TTL_SONG_DETAIL = 3600    # 歌曲详情（标题/歌手/封面）变化极低频，缓存 1 小时


def _cache_get(key: str, ttl: int):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def _cache_set(key: str, value):
    _CACHE[key] = (time.time(), value)
    if len(_CACHE) > 24:
        oldest = sorted(_CACHE.items(), key=lambda kv: kv[1][0])[0][0]
        _CACHE.pop(oldest, None)


def _hit(item: Dict[str, Any], kw: str, fields) -> bool:
    """搜索：按空格分词，每个词都出现在指定字段里才算命中（例如「周杰伦 屋顶」）"""
    if not kw:
        return True
    hay = " ".join(str(item.get(f) or "") for f in fields).lower()
    return all(part in hay for part in kw.lower().split())


def _page_args(page: int, size: int, default: int, cap: int) -> tuple:
    size = max(1, min(cap, int(size or default)))
    page = max(1, int(page or 1))
    return page, size


def _slice(items: list, page: int, size: int) -> dict:
    total = len(items)
    pages = max(1, math.ceil(total / size)) if total else 1
    page = min(page, pages)
    start = (page - 1) * size
    return {"items": items[start:start + size], "total": total,
            "page": page, "pages": pages, "size": size}


def _ensure_indexes() -> None:
    """启动时确保常用查询列有索引。

    tracks.platform_track_id 是热点查询列（歌单页一次要按它查 2000+ 个值，
    下载/整理路径也用它定位曲目），而这张表原本一个索引都没有 —— 每次都是全表扫描。
    SQLite 建索引幂等且是毫秒级，放启动时兜底：即使换了新库（create_all 不会
    给已存在的表补索引）也能自动带上。
    """
    try:
        db = SessionLocal()
        try:
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_tracks_ptid ON tracks(platform_track_id)"))
            db.commit()
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        print(f"[startup] 建立索引跳过：{e}", flush=True)


MUSIC_ROOT = "/music"        # 三个目录（download / musics / _trash）的共同父目录


def _migrate_music_paths(cfg: Dict[str, Any]) -> int:
    """老库路径迁移：把挂在 /music 根上的老曲库路径改到新的「整理后曲库」目录下

    目录拆成 /music/download（待整理）/ /music/musics（整理后）/ /music/_trash（回收站）
    之后，原先直接放在 /music 下的那份曲库，在新结构里位于 /music/musics 里。
    曲目库里存的还是老路径（/music/aespa/x.flac），不改的话所有歌都会被判成
    「本地没有」、云盘待上传列表还会把它们当成文件已丢而重置成未下载。

    只在**老路径确实不存在、新路径确实存在**时才改 ——
    挂载方式没变（或用户另有映射）时什么都不会动，重复执行也安全。
    """
    lib = str(cfg.get("library_dir") or "/music/musics").rstrip("/")
    dl = str(cfg.get("download_dir") or "/music/download").rstrip("/")
    trash = str(cfg.get("trash_dir") or "/music/_trash").rstrip("/")
    keep = (lib + "/", dl + "/", trash + "/")
    n = 0
    try:
        db = SessionLocal()
        try:
            rows = (db.query(Track)
                    .filter(Track.file_path.like(MUSIC_ROOT + "/%")).all())
            for tr in rows:
                p = str(tr.file_path or "")
                if not p or p.startswith(keep) or Path(p).exists():
                    continue
                cand = lib + p[len(MUSIC_ROOT):]
                if Path(cand).exists():
                    tr.file_path = cand
                    n += 1
            if n:
                db.commit()
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        print(f"[startup] 迁移老曲库路径跳过：{e}", flush=True)
    if n:
        print(f"[startup] 已把 {n} 条曲目记录迁移到 {lib} 下", flush=True)
    return n


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_indexes()
    _migrate_music_paths(config.load())
    start_scheduler()
    yield
    from app.scheduler import scheduler
    scheduler.shutdown()


app = FastAPI(title="MusicSync", lifespan=lifespan)
# 页面 HTML 约 50~90KB、列表接口 JSON 也不小，手机端（微信 WebView）下载+解析都吃力；
# gzip 后通常只剩 1/5（实测 /cloud 73.6KB → 约 12KB），菜单切换明显更快。
app.add_middleware(GZipMiddleware, minimum_size=800)
app.mount("/static", StaticFiles(directory="/app/app/static"), name="static")


@app.middleware("http")
async def no_store_api(request: Request, call_next):
    """接口返回实时状态 → no-store；页面 HTML 也必须禁缓存。

    页面里的 JS 是内联的，一旦被浏览器缓存，修复/新增功能就“看得到却用不到”，
    排查时极容易误判成“改了没生效”。
    """
    resp = await call_next(request)
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/static/"):
        # 静态资源：带 ?v= 的是「版本化」资源（改了必换号）→ 可以长期强缓存，
        # 这样手机端每次切页不用再重新下载 CSS（56KB）；不带版本的（图标/manifest）
        # 只短缓存，避免改了看不到。
        if request.query_params.get("v"):
            resp.headers["Cache-Control"] = "public, max-age=2592000, immutable"
        else:
            resp.headers["Cache-Control"] = "public, max-age=600"
    elif str(resp.headers.get("content-type") or "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp

templates_env = Environment(
    loader=FileSystemLoader("/app/app/templates"),
    cache_size=0,
    auto_reload=True,
)


def render_template(template_name: str, context: dict) -> HTMLResponse:
    return HTMLResponse(content=templates_env.get_template(template_name).render(context))


def page_ctx(request: Request, active: str, **extra) -> dict:
    ctx = {"request": request, "cfg": config.load(), "active": active}
    ctx.update(extra)
    return ctx


def _cookie(cfg: dict) -> str:
    return str(((cfg.get("platforms") or {}).get("netease") or {}).get("cookie") or "")


def _uid(cfg: dict) -> str:
    return str(((cfg.get("platforms") or {}).get("netease") or {}).get("user_id") or "")


def _sources_view(cfg: dict) -> dict:
    """音源下拉框用：id -> 名称"""
    return {str(s.get("id")): str(s.get("name") or s.get("id"))
            for s in lxsource.list_sources(cfg)}


# ------------------------------------------------------------- 远端数据
async def _account_playlists(cfg: dict) -> dict:
    cached = _cache_get("account_playlists", _TTL_PLAYLISTS)
    if cached is not None:
        return cached
    ncm = Ncm(cookie=_cookie(cfg))
    try:
        uid = await ncm.user_id()
        if not uid:
            raise NcmError("登录凭证已失效，请重新扫码登录")
        items = []
        for p in await ncm.user_playlists(uid):
            try:
                pid = int(p.get("id"))
            except (TypeError, ValueError):
                continue
            items.append({
                "id": pid,
                "name": p.get("name") or str(pid),
                "total": int(p.get("trackCount") or 0),
                "cover": p.get("coverImgUrl") or "",
            })
        data = {"uid": uid, "playlists": items}
        _cache_set("account_playlists", data)
        return data
    finally:
        await ncm.close()


# 歌单批量拉详情的并发度。实测（2098 首全量）：
#   并发 8 / 分块 300 -> 1.46s；并发 12 -> 0.97s；并发 16 -> 0.89s。
# 取 12：收益已经到位，同时给网易云留出余量（再高收益递减、风控风险上升）。
_PL_CONCURRENCY = 12


def _song_to_row(s: dict) -> dict:
    """网易云 song 对象 → 面板用的曲目行。

    `/song/detail` 的 songs[] 与 `/playlist/detail` 里内嵌的 tracks[] 是**同一种结构**，
    所以两处可以共用一个转换函数。字段不完整时返回 {}。
    """
    try:
        sid = int(s["id"])
    except (KeyError, TypeError, ValueError):
        return {}
    ar = [a.get("name") for a in (s.get("ar") or s.get("artists") or []) if a.get("name")]
    al = s.get("al") or s.get("album") or {}
    return {
        "sid": sid,
        "title": str(s.get("name") or sid),
        "artist": " / ".join(ar) or "未知歌手",
        "album": str(al.get("name") or ""),
        "cover": str(al.get("picUrl") or ""),
        "duration": int((s.get("dt") or 0) // 1000),
        # al.id=0 = 已下架（网易云官方还留着元数据、但没有专辑信息，版权也没了）。
        # 歌单页据此打「已下架」角标并支持筛选。注意不能用 copyright：
        # 实测大量正常老歌 copyright 也是 0，只有 al.id=0 才是下架歌的稳定信号。
        "al_id": int(al.get("id") or 0),
    }


def _song_cache_get(sid) -> Optional[dict]:
    """读歌曲详情缓存（超 TTL 视为没有）。返回副本，调用方随意改动。"""
    hit = _SONG_CACHE.get(str(sid))
    if hit and time.time() - hit[0] < _TTL_SONG_DETAIL:
        return dict(hit[1])
    return None


def _song_cache_put(row: dict) -> None:
    """写歌曲详情缓存。超过上限时先清过期项，仍超再丢最早的，避免长期运行内存无界增长。"""
    if not row:
        return
    if len(_SONG_CACHE) > 20000:
        now = time.time()
        for k in [k for k, v in list(_SONG_CACHE.items()) if now - v[0] > _TTL_SONG_DETAIL]:
            _SONG_CACHE.pop(k, None)
        if len(_SONG_CACHE) > 20000:
            for k in sorted(_SONG_CACHE, key=lambda x: _SONG_CACHE[x][0])[:10000]:
                _SONG_CACHE.pop(k, None)
    _SONG_CACHE[str(row["sid"])] = (time.time(), row)


async def _playlist_tracks(pid: int, cfg: dict) -> dict:
    """歌单全部曲目（带封面）。歌单 id 列表短缓存；歌曲详情长缓存。

    三次提速叠加（思路参考 SPlayer-Next 的 fetchPlaylist）：
      ① 白拿：`/playlist/detail` 的响应里本来就带前 ~1000 首**完整 song**，
         直接拿来用（零额外请求），而不是只取 trackIds 后对这 1000 首再请求一遍。
      ② 长缓存：详情（标题/歌手/封面）几乎不变，缓存 1 小时；刷新时只有
         新加入歌单的歌未命中 → 增量拉取。
      ③ 动态分块并发：未命中的部分按并发数切分（批数 = 并发、每批最小）并行请求。
    """
    key = f"tracks:{pid}"
    cached = _cache_get(key, _TTL_TRACKS)
    if cached is not None:
        return cached
    ncm = Ncm(cookie=_cookie(cfg), delay=0.0, concurrency=_PL_CONCURRENCY)
    try:
        pl = await ncm.playlist_detail(pid)
        if not pl:
            raise NcmError("歌单不存在或不可见")
        name = str(pl.get("name") or pid)
        cover = str(pl.get("coverImgUrl") or "")

        # ① 白拿 detail 内嵌的完整曲目（约前 1000 首），同时回填长缓存
        for s in (pl.get("tracks") or []):
            _song_cache_put(_song_to_row(s))

        ids = await ncm.playlist_track_ids(pid, pl)

        # ② 命中缓存（含上一步刚回填的）直接用，未命中的才需要请求
        rows = []
        missing = []
        for sid in ids:
            hit = _song_cache_get(sid)
            if hit is not None:
                rows.append(hit)
            else:
                missing.append(sid)

        # ③ 只补缺的部分：动态分块，批数 = 并发数（每批尽量小 → 单批越快）
        if missing:
            chunk = max(1, -(-len(missing) // _PL_CONCURRENCY))
            _chunks = [missing[i:i + chunk] for i in range(0, len(missing), chunk)]
            _batches = await asyncio.gather(*[ncm.song_detail(c) for c in _chunks])
            for _b in _batches:
                for s in _b:
                    row = _song_to_row(s)
                    if not row:
                        continue
                    _song_cache_put(row)
                    rows.append(row)
        order = {sid: i for i, sid in enumerate(ids)}
        rows.sort(key=lambda r: order.get(r["sid"], 10 ** 9))
        data = {"id": pid, "name": name, "cover": cover, "tracks": rows}
        _cache_set(key, data)
        return data
    finally:
        await ncm.close()


# ------------------------------------------------------------- 页面
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    cfg = config.load()
    return render_template("index.html",
                           page_ctx(request, "home", logged_in=bool(_cookie(cfg))))


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    return render_template("config.html",
                           page_ctx(request, "config", layouts=LAYOUT_LABELS,
                                    default_chain=QUALITY_CHAIN))


@app.get("/playlists", response_class=HTMLResponse)
async def playlists_page(request: Request):
    return render_template("playlists.html", page_ctx(request, "playlists"))


@app.get("/playlists/{pid}", response_class=HTMLResponse)
async def playlist_detail_page(request: Request, pid: int):
    cfg = config.load()
    return render_template("playlist_detail.html",
                           page_ctx(request, "playlists", pid=pid,
                                    configured=pid in config.playlist_ids(cfg)))


@app.get("/download", response_class=HTMLResponse)
async def download_page(request: Request):
    return render_template("download.html", page_ctx(request, "download"))


@app.get("/cloud", response_class=HTMLResponse)
async def cloud_page(request: Request):
    cfg = config.load()
    return render_template("cloud.html",
                           page_ctx(request, "cloud", logged_in=bool(_cookie(cfg))))


@app.get("/login/netease")
async def legacy_login_redirect():
    """登录已并入配置页顶部，保留旧链接跳转"""
    return RedirectResponse("/config#login", status_code=307)


# ------------------------------------------------------------- 配置
@app.post("/config")
async def save_config(
    download_dir: str = Form(...),
    library_dir: str = Form("/music/musics"),
    trash_dir: str = Form("/music/_trash"),
    layout: str = Form("album"),
    naming: str = Form(""),
    auto_archive: bool = Form(True),
    chain: str = Form(""),
    upgrade_existing: bool = Form(True),
    lrc: bool = Form(True),
    embed: bool = Form(True),
    nfo: bool = Form(True),
    concurrency: int = Form(3),
    api_delay: float = Form(0.35),
    max_per_run: int = Form(0),
    fail_backoff: int = Form(3),
    backoff_hours: int = Form(24),
    auto_sync: bool = Form(False),
    auto_download: bool = Form(False),
    auto_upload: bool = Form(False),
    delete_local_after_upload: bool = Form(False),
    calibrate: bool = Form(True),      # 老表单字段（已由 calibrate_mode 取代），保留兼容
    calibrate_mode: str = Form(""),    # off / fill / full；空 = 保持原值
    monitor_on: bool = Form(False),
    monitor_mode: str = Form("new"),
    monitor_batch: int = Form(20),
    monitor_interval: int = Form(0),    # 检查间隔（分钟）；0/非法 = 保持原值
    monitor_token: str = Form(""),      # 触发密钥。/api/monitor/run 要校验它
    sync_time: str = Form("02:00"),
):
    cfg = config.load()
    # 三个目录：待整理（下载）/ 整理后曲库 / 回收站 —— 各自可以映射到不同的宿主机路径
    cfg["download_dir"] = download_dir.strip() or "/music/download"
    cfg["library_dir"] = library_dir.strip() or "/music/musics"
    cfg["trash_dir"] = trash_dir.strip() or "/music/_trash"
    cfg["library"] = {"layout": layout if layout in LAYOUT_LABELS else "album",
                      "naming": naming.strip(),
                      "auto_archive": bool(auto_archive)}
    levels = [s.strip() for s in chain.split(",") if s.strip()]
    cfg["quality"] = {"chain": levels or list(QUALITY_CHAIN),
                      "upgrade_existing": bool(upgrade_existing)}
    cfg["lyrics"] = {"lrc": bool(lrc), "embed": bool(embed)}
    cfg["nfo"] = bool(nfo)
    cfg["limits"] = {
        "download_concurrency": max(1, min(8, int(concurrency))),
        "api_delay": max(0.0, float(api_delay)),
        "max_per_run": max(0, int(max_per_run)),
        "fail_backoff": max(0, int(fail_backoff)),
        "backoff_hours": max(0, int(backoff_hours)),
    }
    # 「上传后自动校准」的力度：off 不碰本地文件 / fill 只补空（默认）/ full 全量纠正。
    # 云盘条目匹配正式曲目不受它影响 —— 那是让云盘上有封面、显示成正式曲目。
    old_cloud = cfg.get("cloud") or {}
    mode = str(calibrate_mode or "").strip().lower()
    if mode not in ("off", "fill", "full"):
        mode = str(old_cloud.get("calibrate_mode") or "").strip().lower()
    if mode not in ("off", "fill", "full"):
        mode = "full" if bool(old_cloud.get("calibrate", True)) else "off"
    cfg["cloud"] = {"auto_upload": bool(auto_upload),
                    "delete_local_after_upload": bool(delete_local_after_upload),
                    "calibrate": mode != "off",       # 老字段跟着档位走，别再有两份真相
                    "calibrate_mode": mode}
    # 歌单监控：开启「从现在开始」时记录起算时间；选「全量扫描补齐」时先补齐再自动转监控
    # 注意 interval / token 必须一起带上：以前这里重建 mon 时漏了它们，
    # 结果**每次保存配置都会把检查间隔重置成 2 分钟、把触发密钥清空**（2026-09-24 修）。
    old_mon = dict(cfg.get("monitor") or {})
    mode = monitor_mode if monitor_mode in ("new", "full") else "new"
    try:
        interval = int(monitor_interval or 0)
    except (TypeError, ValueError):
        interval = 0
    mon = {"on": bool(monitor_on), "mode": mode, "batch": max(1, min(500, int(monitor_batch))),
           "interval": interval if interval > 0 else max(1, int(old_mon.get("interval") or 2)),
           "token": str(monitor_token or "").strip(),
           "since": float(old_mon.get("since") or 0),
           "last_run": old_mon.get("last_run") or "", "last_result": old_mon.get("last_result") or {}}
    if mon["on"]:
        if mode == "new":
            mon["since"] = time.time() if old_mon.get("mode") != "new" or not mon["since"] else mon["since"]
        elif float(old_mon.get("since") or 0) and old_mon.get("mode") != "full":
            mon["since"] = float(old_mon.get("since") or 0)
    else:
        mon["since"] = 0.0
    cfg["monitor"] = mon
    cfg.setdefault("scheduler", {})
    cfg["scheduler"].update(auto_sync=bool(auto_sync), auto_download=bool(auto_download),
                            time=sync_time)
    config.save(cfg)                 # 音源列表由音源卡片单独管理
    apply_config(cfg)
    try:
        jobs = reschedule()
    except Exception:  # noqa: BLE001
        jobs = []
    return JSONResponse({"status": "ok", "jobs": jobs})


# ------------------------------------------------------------- 登录
@app.post("/api/login/netease/qr")
async def netease_qr():
    try:
        info = await NeteaseQR.create()
    except Exception as e:  # noqa: BLE001
        print(f"[netease] 创建二维码失败: {type(e).__name__}: {e}", flush=True)
        return JSONResponse({"error": "获取二维码失败（请检查接口服务是否正常）"}, status_code=502)
    if not info.get("key"):
        return JSONResponse({"error": "获取二维码失败（接口服务未返回 unikey）"}, status_code=502)
    return {"key": info["key"], "qr_url": info["qr_url"]}


@app.get("/api/login/netease/qr.png")
async def netease_qr_png(key: str):
    try:
        png = NeteaseQR.qr_png(f"https://music.163.com/login?codekey={key}")
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "二维码渲染失败"}, status_code=500)
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=300"})


@app.post("/api/login/netease/poll")
async def netease_poll(key: str = Form(...)):
    res = await NeteaseQR.check(key)
    code = res.get("code")
    if code != 801:
        print(f"[netease] 扫码状态 code={code} msg={res.get('message')}", flush=True)
    if code == 803:
        cookie = res.get("cookie") or ""
        if "MUSIC_U" not in cookie:
            return {"status": "error", "code": code,
                    "message": res.get("message") or "拿不到登录凭证"}
        cfg = config.load()
        plats = cfg.setdefault("platforms", {}).setdefault("netease", {})
        plats["cookie"] = cookie
        user_id = res.get("user_id") or ""
        if user_id:
            plats["user_id"] = user_id
        config.save(cfg)
        _CACHE.clear()
        print(f"[netease] 登录成功 user_id={user_id or '(未取到)'}", flush=True)
        return {"status": "success", "code": code, "user_id": user_id}
    if code == 800:
        return {"status": "expired", "code": code}
    if code == 802:
        return {"status": "scanned", "code": code}
    return {"status": "pending", "code": code}


@app.post("/api/login/netease/cookie")
async def save_netease_cookie(cookie: str = Form(...)):
    cookie = (cookie or "").strip().replace("\n", "")
    if not cookie:
        return JSONResponse({"error": "Cookie 不能为空"}, status_code=400)
    if "MUSIC_U" not in cookie:
        return JSONResponse({"error": "Cookie 里必须包含 MUSIC_U"}, status_code=400)
    if "os=pc" not in cookie:
        cookie = f"{cookie}; os=pc"
    cfg = config.load()
    plats = cfg.setdefault("platforms", {}).setdefault("netease", {})
    plats["cookie"] = cookie
    user_id = ""
    try:
        user_id = await NeteaseQR.get_user_id(cookie)
    except Exception:  # noqa: BLE001
        user_id = ""
    if user_id:
        plats["user_id"] = user_id
    config.save(cfg)
    _CACHE.clear()
    return JSONResponse({"status": "ok", "user_id": user_id})


# ------------------------------------------------------------- 歌单
@app.get("/api/account/playlists")
async def api_account_playlists(page: int = 1, size: int = 10):
    cfg = config.load()
    if not _cookie(cfg):
        return JSONResponse({"error": "还没登录网易云，请先扫码登录"}, status_code=401)
    page, size = _page_args(page, size, 10, 100)
    try:
        data = await _account_playlists(cfg)
    except NcmError as e:
        return JSONResponse({"error": f"获取歌单失败：{e}"}, status_code=502)
    configured = set(config.playlist_ids(cfg))
    items = [dict(p, configured=p["id"] in configured) for p in data["playlists"]]
    out = _slice(items, page, size)
    return {"uid": data["uid"], **out}


@app.get("/api/playlists")
async def api_playlists():
    cfg = config.load()
    db = SessionLocal()
    try:
        rows = {str(p.playlist_id): p for p in db.query(Playlist).all()}
        out = []
        for p in cfg.get("playlists") or []:
            row = rows.get(str(p.get("id")))
            out.append({"id": p.get("id"),
                        "name": p.get("name") or (row.title if row is not None else ""),
                        "total": (row.track_count if row is not None else 0),
                        "last_sync": (row.last_sync if row is not None else "")})
        return {"playlists": out}
    finally:
        db.close()


@app.post("/api/playlists")
async def api_add_playlist(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "请求格式错误"}, status_code=400)
    try:
        pid = int(str(body.get("id")).strip())
    except (TypeError, ValueError, AttributeError):
        return JSONResponse({"error": "歌单 ID 必须是数字"}, status_code=400)
    name = str(body.get("name") or "").strip()
    cfg = config.load()
    pls = cfg.setdefault("playlists", [])
    for p in pls:
        try:
            if int(p.get("id")) == pid:
                return JSONResponse({"error": "该歌单已添加"}, status_code=409)
        except (TypeError, ValueError):
            continue
    pls.append({"id": pid, **({"name": name} if name else {})})
    config.save(cfg)
    return {"added": pid, "sync_started": start("sync")}


@app.delete("/api/playlists/{pid}")
async def api_remove_playlist(pid: int):
    cfg = config.load()
    found = False
    left = []
    for p in cfg.get("playlists") or []:
        try:
            same = int(p.get("id")) == pid
        except (TypeError, ValueError):
            same = False
        if same:
            found = True
            continue
        left.append(p)
    if not found:
        return JSONResponse({"error": "歌单不存在"}, status_code=404)
    cfg["playlists"] = left
    config.save(cfg)     # 已下载文件保留，仅解除关联
    _CACHE.pop(f"tracks:{pid}", None)
    return {"removed": pid}


def _local_map(sids: list) -> dict:
    """sid -> Track 行（用于判断本地是否已下载）"""
    db = SessionLocal()
    try:
        out = {}
        for i in range(0, len(sids), 500):
            batch = [str(x) for x in sids[i:i + 500]]
            for row in db.query(Track).filter(Track.platform_track_id.in_(batch)).all():
                try:
                    out[int(row.platform_track_id)] = row
                except (TypeError, ValueError):
                    continue
        return out
    finally:
        db.close()


@app.get("/api/playlists/{pid}/tracks")
async def api_playlist_tracks(pid: int, page: int = 1, size: int = 20, q: str = "",
                              local: str = "all", cloud: str = "all",
                              delisted: str = "all",
                              refresh: int = 0, sort: str = "", order: str = "asc"):
    """歌单曲目。筛选：local=all|yes|no、cloud=all|in|out、delisted=all|yes|no
    （先筛再分页，页码才准）

    sort=""=歌单原顺序；title=歌名；artist=歌手；time=加入歌单先后（用歌单内序号近似）。
    注：SQLite/Python 对中文按 Unicode 码位排序，不是拼音序。
    """
    cfg = config.load()
    page, size = _page_args(page, size, 20, 500)
    if refresh:
        # 点「刷新」时连歌单曲目缓存也丢掉 —— 否则拿到的还是缓存里的旧列表（他在手机上
        # 加进歌单的歌，要等缓存过期才出现，看着就像「刷新没用」）
        _CACHE.pop(f"tracks:{pid}", None)
    try:
        data = await _playlist_tracks(pid, cfg)
    except NcmError as e:
        return JSONResponse({"error": f"获取歌单曲目失败：{e}"}, status_code=502)

    # 云盘存在性：读常驻索引。**每打开一次歌单就强制重新索引一遍**（他要求打开时尽量是最新的），
    # 后台跑、页面不等它；跑完页面自己会刷新一次。带 15 秒防抖，连点不会重复刷。
    ci = cloud_index.index
    # 打开歌单时（refresh=1）强制重索引一遍；页面自己的自动刷新带 refresh=0，
    # 否则「刷新 → 又触发重索引 → 还在刷新 → 再刷新」会变成死循环
    ci.ensure_async(_cookie(cfg), force=bool(refresh))

    localmap = _local_map([t["sid"] for t in data["tracks"]])
    items = []
    for i, t in enumerate(data["tracks"]):
        row = localmap.get(t["sid"])
        items.append({
            **t,
            "index": i + 1,
            "track_id": (row.id if row is not None else 0),
            "local": bool(row is not None and row.status == "ok" and _is_local_file(row.file_path)),
            "status": (row.status if row is not None else ""),
            "level": (row.level if row is not None else ""),
            "error": (row.last_error if row is not None else ""),
            "selected": (bool(row.selected) if row is not None else False),
            "source_pref": (row.source_pref or "" if row is not None else ""),
            "source_used": (row.source_used or "" if row is not None else ""),
            "cloud_state": (row.cloud_state or "" if row is not None else ""),
            # True=在云盘 / False=不在 / None=索引还没建好（不确定）
            "in_cloud": ci.lookup(t["sid"], t.get("title"), t.get("artist"),
                                  (row.cloud_sid if row is not None else "")),
            # al_id=0 = 已下架（官方还留着元数据、但没有专辑/版权）
            "delisted": bool(t.get("al_id") == 0),
        })
    # 搜索：先过滤再分页，页码才准确（字段名是 title，别写成 name——那样歌名搜不到）
    items = [x for x in items if _hit(x, q, ("title", "artist", "album"))]
    # 各筛选档的数量（先按关键词过滤后统计，和筛选片对得上）
    counts = {"total": len(items),
              "local_yes": sum(1 for x in items if x["local"]),
              "local_no": sum(1 for x in items if not x["local"]),
              "cloud_in": sum(1 for x in items if x["in_cloud"] is True),
              "cloud_out": sum(1 for x in items if x["in_cloud"] is False),
              "delisted_yes": sum(1 for x in items if x["delisted"]),
              "delisted_no": sum(1 for x in items if not x["delisted"])}
    if local == "yes":
        items = [x for x in items if x["local"]]
    elif local == "no":
        items = [x for x in items if not x["local"]]
    if cloud == "in":
        items = [x for x in items if x["in_cloud"] is True]
    elif cloud == "out":
        items = [x for x in items if x["in_cloud"] is False]
    if delisted == "yes":
        items = [x for x in items if x["delisted"]]
    elif delisted == "no":
        items = [x for x in items if not x["delisted"]]
    # 排序放在筛选之后、分页之前，否则页码会错
    _srt = (sort or "").lower()
    _rev = (order or "asc").lower() == "desc"
    if _srt == "title":
        items.sort(key=lambda x: (x.get("title") or "").lower(), reverse=_rev)
    elif _srt == "artist":
        items.sort(key=lambda x: (x.get("artist") or "").lower(), reverse=_rev)
    elif _srt == "time":
        items.sort(key=lambda x: x.get("index") or 0, reverse=_rev)
    out = _slice(items, page, size)
    return {"id": data["id"], "name": data["name"], "cover": data["cover"],
            "local_count": counts["local_yes"], "counts": counts,
            "sources": _sources_view(cfg), "q": q, "cloud_index": ci.status(), **out}


@app.post("/api/playlists/{pid}/fill")
async def api_playlist_fill(pid: int, request: Request):
    """单独扫描补齐**这一个歌单**：云盘里没有的歌 → 本地有就补传云盘、本地没有就下载

    body: {"dry": true} 只算计划、不排队（页面先给计划，确认后再真跑）
    """
    body = await _body(request)
    dry = bool(body.get("dry"))
    cfg = config.load()
    try:
        data = await _playlist_tracks(pid, cfg)
    except NcmError as e:
        return JSONResponse({"error": f"获取歌单曲目失败：{e}"}, status_code=502)
    ci = cloud_index.index
    ci.ensure_async(_cookie(cfg), force=True)        # 补齐前先把云盘索引刷到最新
    tracks = data.get("tracks") or []
    localmap = _local_map([t["sid"] for t in tracks])
    plan_down: list = []
    plan_up: list = []
    in_cloud = 0
    for t in tracks:
        row = localmap.get(t["sid"])
        local = bool(row is not None and row.status == "ok" and _is_local_file(row.file_path))
        got = ci.lookup(t["sid"], t.get("title"), t.get("artist"),
                        (row.cloud_sid if row is not None else ""))
        if got is True:
            in_cloud += 1
            continue
        (plan_up if local else plan_down).append(t)
    title_of = lambda t: f"{t.get('title') or t['sid']}"          # noqa: E731
    if dry:
        return {"dry": True, "name": data.get("name"), "total": len(tracks),
                "in_cloud": in_cloud, "download": len(plan_down), "upload": len(plan_up),
                "download_titles": [title_of(t) for t in plan_down[:20]],
                "upload_titles": [title_of(t) for t in plan_up[:20]],
                "cloud_index": ci.status()}
    cap = int(((cfg.get("monitor") or {}).get("batch") or 20))
    db = SessionLocal()
    try:
        rows = {str(t.platform_track_id): t for t in db.query(Track).all()}
        n_down = n_up = 0
        for t in plan_down[:cap]:
            sid = str(t["sid"])
            row = rows.get(sid)
            if row is None:                       # 库里还没有 → 先建行（元数据就用歌单里的）
                row = Track(platform="netease", platform_track_id=sid,
                            title=str(t.get("title") or ""), artist=str(t.get("artist") or ""),
                            album=str(t.get("album") or ""), status="new", selected=True)
                db.add(row)
                rows[sid] = row
        db.commit()
        for t in plan_down[:cap]:
            row = rows.get(str(t["sid"]))
            if row is None or row.id is None:
                continue
            if row.status == "ok" and row.file_path:
                continue
            if enqueue_download_one(row.id, cfg):
                n_down += 1
        left_down = max(0, len(plan_down) - n_down)
        for t in plan_up[:max(1, cap - n_down)]:
            row = rows.get(str(t["sid"]))
            if row is None or row.id is None:
                continue
            if enqueue_upload(row.id):
                n_up += 1
    finally:
        db.close()
    return {"ok": True, "name": data.get("name"), "total": len(tracks), "in_cloud": in_cloud,
            "download": n_down, "upload": n_up, "left": left_down,
            "cloud_index": ci.status()}


# ------------------------------------------------------------- 曲目 / 队列
def _track_view(t: Track, cfg: dict) -> dict:
    return {
        "id": t.id, "sid": t.platform_track_id, "title": t.title, "artist": t.artist,
        "album": t.album, "status": t.status or "new", "level": t.level or "",
        "error": t.last_error or "", "size": int(t.size or 0),
        "selected": bool(t.selected), "source_pref": t.source_pref or "",
        "source_used": t.source_used or "", "cloud_state": t.cloud_state or "",
        "cloud_sid": t.cloud_sid or "", "file": (t.file_path or ""),
        "downloaded": bool(t.downloaded),
        # 本地有没有这个文件（下载成功且有落盘路径）
        "local": bool(t.status == "ok" and _is_local_file(t.file_path)),
    }


@app.post("/api/playlists/{pid}/select")
async def api_playlist_select(pid: int, request: Request):
    """只勾选/取消「这个歌单」里的曲目（不是全部曲目）"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    want = bool(body.get("selected", True))
    cfg = config.load()
    try:
        data = await _playlist_tracks(pid, cfg)
    except NcmError as e:
        return JSONResponse({"error": f"获取歌单曲目失败：{e}"}, status_code=502)

    sids = [str(t["sid"]) for t in data["tracks"]]
    updated = 0
    db = SessionLocal()
    try:
        for i in range(0, len(sids), 500):
            batch = sids[i:i + 500]
            for row in db.query(Track).filter(Track.platform_track_id.in_(batch)).all():
                row.selected = want
                updated += 1
        db.commit()
    finally:
        db.close()
    return {"updated": updated, "total": len(sids), "selected": want,
            "playlist": data["name"]}


@app.get("/api/tracks")
async def api_tracks(page: int = 1, size: int = 20, filter: str = "all", q: str = "",
                     cloud: str = "all", sort: str = "time", order: str = "desc"):
    """曲目列表。filter=all|selected|pending|ok|failed；cloud=all|in|out；
    sort=time(加入顺序)|title(歌名)|artist(歌手)，order=asc|desc"""
    cfg = config.load()
    page, size = _page_args(page, size, 20, 200)
    ci = cloud_index.index
    ci.ensure_async(_cookie(cfg))
    db = SessionLocal()
    try:
        ok = db.query(Track).filter(Track.status == "ok").count()
        failed = db.query(Track).filter(Track.status == "failed").count()
        pending = db.query(Track).filter(Track.status.isnot("ok")).count()
        selected = db.query(Track).filter(Track.selected.isnot(False)).count()
        query = db.query(Track)
        if filter == "ok":
            query = query.filter(Track.status == "ok")
        elif filter == "failed":
            query = query.filter(Track.status == "failed")
        elif filter == "pending":
            query = query.filter(Track.status.isnot("ok"))
        elif filter == "selected":
            query = query.filter(Track.selected.isnot(False))
        # 排序：time 用自增 id 代表「加入歌单的先后」（表里没有单独的加入时间列）。
        # 注：SQLite 对中文按 Unicode 码位排序，不是拼音序。
        _col = {"time": Track.id, "title": Track.title,
                "artist": Track.artist}.get((sort or "time").lower(), Track.id)
        rows = query.order_by(_col.asc() if (order or "desc").lower() == "asc" else _col.desc()).all()
        items = [_track_view(t, cfg) for t in rows]
    finally:
        db.close()
    for x in items:
        x["in_cloud"] = ci.lookup(x["sid"], x.get("title"), x.get("artist"),
                                  x.get("cloud_sid") or "")
    items = [x for x in items if _hit(x, q, ("title", "artist", "album", "sid"))]
    # 筛选片上的数字要和「搜索后的这一批」对得上（不能再用全局统计，否则搜完
    # 会出现「全部 1 / 已下载 2」这种自相矛盾）
    counts = {"total": len(items),
              "local_yes": sum(1 for x in items if x["local"]),
              "local_no": sum(1 for x in items if not x["local"]),
              "failed": sum(1 for x in items if x.get("status") == "failed"),
              "selected": sum(1 for x in items if x.get("selected")),
              "cloud_in": sum(1 for x in items if x["in_cloud"] is True),
              "cloud_out": sum(1 for x in items if x["in_cloud"] is False)}
    if cloud == "in":
        items = [x for x in items if x["in_cloud"] is True]
    elif cloud == "out":
        items = [x for x in items if x["in_cloud"] is False]
    out = _slice(items, page, size)
    return {"pending": pending, "downloaded": ok, "failed": failed,
            "selected": selected, "counts": counts, "cloud_index": ci.status(),
            "sources": _sources_view(cfg), "q": q, **out}


@app.post("/api/tracks/select")
async def api_select_tracks(request: Request):
    """勾选 / 取消勾选曲目：{ids:[...], selected:bool}；ids 为空且 all=true 时全量操作"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "请求格式错误"}, status_code=400)
    ids = body.get("ids") or []
    want = bool(body.get("selected", True))
    db = SessionLocal()
    try:
        if body.get("all"):
            if body.get("filter") == "ok":
                q = db.query(Track).filter(Track.status == "ok")
            elif body.get("filter") == "failed":
                q = db.query(Track).filter(Track.status == "failed")
            else:
                q = db.query(Track)
            n = q.update({Track.selected: want}, synchronize_session=False)
        else:
            n = 0
            for tid in ids:
                row = db.query(Track).filter_by(id=int(tid)).first()
                if row is not None:
                    row.selected = want
                    n += 1
        db.commit()
    except (TypeError, ValueError):
        db.rollback()
        return JSONResponse({"error": "曲目 ID 不合法"}, status_code=400)
    finally:
        db.close()
    return {"updated": n, "selected": want}


@app.post("/api/tracks/{tid}/source")
async def api_set_track_source(tid: int, request: Request):
    """设置单曲音源偏好："" 自动（网易云优先）/ netease 官方 / 音源 id"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "请求格式错误"}, status_code=400)
    pref = str(body.get("source") or "").strip()
    cfg = config.load()
    if pref not in ("", "auto", SRC_OFFICIAL) and lxsource.find_source(cfg, pref) is None:
        return JSONResponse({"error": "指定的音源不存在"}, status_code=400)
    if pref == "auto":
        pref = ""
    db = SessionLocal()
    try:
        row = db.query(Track).filter_by(id=int(tid)).first()
        if row is None:
            return JSONResponse({"error": "曲目不存在"}, status_code=404)
        row.source_pref = pref
        db.commit()
    finally:
        db.close()
    return {"track_id": tid, "source_pref": pref, "label": source_label(cfg, pref)}


@app.post("/api/tracks/{tid}/download")
async def api_download_track(tid: int, request: Request):
    """单首下载；body.source 可临时指定音源（同时记住该选择）；body.to_cloud=true 则下载完自动转存云盘"""
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    source = body.get("source")
    to_cloud = bool(body.get("to_cloud"))
    cfg = config.load()
    try:
        task = enqueue_download_one(int(tid), cfg,
                                    source_pref=(str(source).strip() if source is not None else None),
                                    to_cloud=to_cloud)
    except LookupError:
        return JSONResponse({"error": "曲目不存在"}, status_code=404)
    return {"queued": task.snapshot()}


@app.post("/api/tracks/download-selected")
async def api_download_selected():
    cfg = config.load()
    info = enqueue_downloads(cfg)
    if not info["added"]:
        return JSONResponse({"error": "没有需要下载的曲目（检查勾选与失败退避）"}, status_code=400)
    return {"status": "started", **info}


@app.get("/api/queue")
async def api_queue():
    return {"download": DOWNLOADS.snapshot(), "upload": UPLOADS.snapshot()}


@app.post("/api/queue/{tid}/{action}")
async def api_queue_action(tid: int, action: str):
    """单任务：pause / resume / cancel"""
    task = DOWNLOADS.get(tid) or UPLOADS.get(tid)
    if task is None:
        return JSONResponse({"error": "任务不存在或已结束"}, status_code=404)
    if action == "pause":
        task.pause()
    elif action == "resume":
        task.resume()
    elif action == "cancel":
        task.cancel()
    else:
        return JSONResponse({"error": "未知操作"}, status_code=400)
    return {"task": task.snapshot()}


@app.post("/api/queue/{action}")
async def api_queue_bulk(action: str):
    """整体：pause-all / resume-all / cancel-all / clear"""
    if action == "pause-all":
        n = DOWNLOADS.pause_all() + UPLOADS.pause_all()
    elif action == "resume-all":
        n = DOWNLOADS.resume_all() + UPLOADS.resume_all()
    elif action == "cancel-all":
        n = DOWNLOADS.cancel_all() + UPLOADS.cancel_all()
    elif action == "clear":
        n = DOWNLOADS.clear_finished() + UPLOADS.clear_finished()
    else:
        return JSONResponse({"error": "未知操作"}, status_code=400)
    return {"affected": n}


# ------------------------------------------------------------- 云盘
_ACCOUNT_CACHE: Dict[str, Any] = {"at": 0.0, "data": {}}
_TTL_ACCOUNT = 600


async def _account_info(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """已登录账号的信息（昵称 / 头像 / 会员 / 歌单数 / 等级）—— 缓存 10 分钟"""
    now = time.time()
    if _ACCOUNT_CACHE["data"] and now - _ACCOUNT_CACHE["at"] < _TTL_ACCOUNT:
        return _ACCOUNT_CACHE["data"]
    cookie = _cookie(cfg)
    if not cookie:
        return {}
    out: Dict[str, Any] = {"logged_in": True}
    ncm = Ncm(cookie=cookie)
    try:
        try:
            prof = ((await ncm.get("/user/account")) or {}).get("profile") or {}
            out.update({"nickname": prof.get("nickname") or "",
                        "avatar": prof.get("avatarUrl") or "",
                        "uid": str(prof.get("userId") or "")})
        except NcmError:
            pass
        try:
            sub = await ncm.get("/user/subcount") or {}
            out.update({"created_playlists": int(sub.get("createdPlaylistCount") or 0),
                        "sub_playlists": int(sub.get("subPlaylistCount") or 0),
                        "artists": int(sub.get("artistCount") or 0)})
        except NcmError:
            pass
        try:
            vip = ((await ncm.get("/vip/info")) or {}).get("data") or {}
            pkg = vip.get("musicPackage") or {}
            asso = vip.get("associator") or {}
            out.update({"vip_level": int(vip.get("redVipLevel") or 0),
                        "vip_expire": int(pkg.get("expireTime") or asso.get("expireTime") or 0)})
        except NcmError:
            pass
        try:
            lv = ((await ncm.get("/user/level")) or {}).get("data") or {}
            out.update({"level": int(lv.get("level") or 0),
                        "play_count": int(lv.get("nowPlayCount") or 0)})
        except NcmError:
            pass
    finally:
        await ncm.close()
    try:
        q = await cloud.quota(cookie)
        out.update({"cloud_count": q.get("count") or 0, "cloud_size": q.get("size") or 0,
                    "cloud_max": q.get("max_size") or 0})
    except NcmError:
        pass
    _ACCOUNT_CACHE.update({"at": now, "data": out})
    return out


@app.get("/api/home")
async def api_home():
    """首页汇总：账号信息 + 本地库/云盘/索引/监控/队列 的状态"""
    cfg = config.load()
    db = SessionLocal()
    try:
        total = db.query(Track).count()
        ok = db.query(Track).filter(Track.status == "ok").count()
        failed = db.query(Track).filter(Track.status == "failed").count()
        selected = db.query(Track).filter(Track.selected.isnot(False)).count()
        uploaded = db.query(Track).filter(Track.cloud_state == "uploaded").count()
        synced_playlists = db.query(Playlist).count()
    finally:
        db.close()
    ci = cloud_index.index
    ci_status = ci.status()
    items = ci.items
    pj = playlist_index.index
    pj_sids = pj.sids
    cloud_total = len(items)
    cloud_cover_no = sum(1 for x in items if not x.get("has_cover"))
    cloud_in_pl = sum(1 for x in items if str(x.get("sid") or "") in pj_sids) if pj_sids else 0
    # 回收站（只数文件，不读标签，图快）
    trash_count = trash_size = 0
    try:
        if organize.trash_root().exists():
            for f in organize.trash_root().rglob("*"):
                if f.is_file() and f.suffix.lower() not in (".lrc", ".nfo"):
                    trash_count += 1
                    trash_size += f.stat().st_size
    except OSError:
        pass
    lib_files = len(organize.load_cache() or {})
    mon = cfg.get("monitor") or {}
    st = status()
    return {
        "account": await _account_info(cfg),
        "stats": {
            "tracks": total, "pending": max(0, total - ok), "downloaded": ok, "failed": failed,
            "selected": selected, "uploaded": uploaded, "waiting_upload": max(0, ok - uploaded),
            "synced_playlists": synced_playlists,
            "playlists_configured": len(config.playlist_ids(cfg)),
        },
        "cloud": {"total": cloud_total, "cover_no": cloud_cover_no, "in_playlist": cloud_in_pl,
                  "index": ci_status},
        "playlist_index": pj.status(),
        "lib": {"files": lib_files},
        "trash": {"count": trash_count, "size": trash_size},
        "monitor": {"on": bool(mon.get("on")), "mode": mon.get("mode") or "new",
                    "last_run": mon.get("last_run") or "",
                    "last_result": mon.get("last_result") or {},
                    "batch": int(mon.get("batch") or 20),
                    "since": mon.get("since") or 0},
        "queues": st.get("queues") or {},
        "running": st.get("running") or [],
        "sources_ready": await lxsource.LxRunner.ready(),
    }


@app.post("/api/playlists/add-tracks")
async def api_playlists_add_tracks(request: Request):
    """把歌加进一个或多个歌单（云盘页那个「不在歌单」图标点开就能用）"""
    body = await _body(request)
    pids = [str(x) for x in (body.get("pids") or [])]
    sids = [str(x) for x in (body.get("sids") or [])]
    if not pids or not sids:
        return JSONResponse({"error": "请选择要加入的歌单"}, status_code=400)
    # 只有匹配到正式曲目的条目才有真正的「歌曲 id」；未匹配条目的 sid 是云盘记录 id，
    # 拿它去加歌单必然失败（实测过）
    ci = cloud_index.index
    bad = [x for x in sids if not (x.isdigit() and (not ci.ready or x in ci.sids))]
    if bad:
        return JSONResponse({"error": "这首歌在云盘里还没匹配到正式曲目（网易云没认出它的音频），"
                                      "没有可用的歌曲 ID，加不了歌单；先在手机 App / 电脑客户端"
                                      "重新上传一次试试"}, status_code=400)
    cfg = config.load()
    cookie = _cookie(cfg)
    if not cookie:
        return JSONResponse({"error": "还没登录网易云，请先扫码登录"}, status_code=401)
    res = await add_to_playlists(cfg, sids, pids)
    return {"ok": True, "added_to": res["added_to"], "failed": res["failed"],
            "count": res["count"]}


@app.get("/api/cloud/quota")
async def api_cloud_quota():
    cfg = config.load()
    if not _cookie(cfg):
        return JSONResponse({"error": "还没登录网易云"}, status_code=401)
    try:
        q = await cloud.quota(_cookie(cfg))
    except NcmError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    db = SessionLocal()
    try:
        uploaded = db.query(Track).filter(Track.cloud_state == "uploaded").count()
        local_ok = db.query(Track).filter(Track.status == "ok").count()
    finally:
        db.close()
    return {**q, "uploaded_by_us": uploaded, "local_downloaded": local_ok,
            "waiting": max(0, local_ok - uploaded)}


_CLOUD_ALL: Dict[str, Any] = {"at": 0.0, "items": []}
_TTL_CLOUD_ALL = 300


async def _cloud_all(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把整个云盘列表拉下来（带 5 分钟缓存），供全局搜索用"""
    now = time.time()
    if _CLOUD_ALL["items"] and now - _CLOUD_ALL["at"] < _TTL_CLOUD_ALL:
        return _CLOUD_ALL["items"]
    cookie = _cookie(cfg)
    items: List[Dict[str, Any]] = []
    offset, total = 0, None
    while True:
        data = await cloud.list_songs(cookie, limit=100, offset=offset)
        batch = data.get("data") or []
        items.extend(cloud.simplify(it) for it in batch)
        if total is None:
            total = int(data.get("count") or 0)
        offset += len(batch)
        if not batch or offset >= (total or 0) or offset >= 20000:
            break
        await asyncio.sleep(0.12)      # 别把接口打太急
    _CLOUD_ALL.update({"at": now, "items": items})
    return items


@app.get("/api/cloud")
async def api_cloud(page: int = 1, size: int = 20, q: str = "", cover: str = "all",
                    album: str = "all", pl: str = "all", local: str = "all",
                    delisted: str = "all",
                    fresh: int = 0, sort: str = "", order: str = "asc",
                    want_sids: int = 0):
    """云盘歌曲列表：直接用云盘索引（全量在内存），支持搜索 + 筛选 + 状态图标

    筛选（all|yes|no）：cover=有没有封面（云盘条目的封面只能来自「匹配到正式曲目」，
    所以「没封面」＝网易云数据库里没有这首歌的信息）；album=有没有正式专辑信息；
    pl=在不在歌单；local=在不在本地；delisted=是不是已下架。
    fresh=1 时强制重新索引一遍（打开页面用）。
    """
    cfg = config.load()
    cookie = _cookie(cfg)
    if not cookie:
        return JSONResponse({"error": "还没登录网易云，请先扫码登录"}, status_code=401)
    page, size = _page_args(page, size, 20, 100)
    uid = str(((cfg.get("platforms") or {}).get("netease") or {}).get("user_id") or "")
    ci = cloud_index.index
    pj = playlist_index.index
    ci.ensure_async(cookie, force=bool(fresh))     # 打开云盘页 → 重新索引一遍
    pj.ensure_async(cookie, uid)                   # 在不在歌单里，用歌单索引

    items = list(ci.items)
    if not items:
        return {"items": [], "page": 1, "pages": 1, "total": 0, "size": size,
                "count": 0, "counts": {}, "q": q,
                "cloud_index": ci.status(), "playlist_index": pj.status()}

    # 本地已下载的歌：按「云盘条目 id」和「网易云歌曲 id」两套都建索引
    db = SessionLocal()
    try:
        local_sids, local_cloud = set(), set()
        for row in db.query(Track).filter(Track.status == "ok", Track.file_path != "").all():
            if not _is_local_file(row.file_path):
                continue
            if row.platform_track_id:
                local_sids.add(str(row.platform_track_id))
            if row.cloud_sid:
                local_cloud.add(str(row.cloud_sid))
        # 「本工具上传过」：DB 登记过 cloud_sid 就算 —— 开了「上传后删除本地」后文件已进
        # 回收站（status=new、本地没了），不能因此把徽章丢了；歌确实是用本工具传上去的。
        uploaded_by_us = {str(r[0]) for r in
                          db.query(Track.cloud_sid).filter(Track.cloud_sid != "").all() if r[0]}
    finally:
        db.close()

    rows = []
    for x in items:
        sid = str(x.get("sid") or "")
        row = dict(x)
        row["in_local"] = bool(sid and (sid in local_sids or sid in local_cloud))
        row["from_us"] = bool(sid and sid in uploaded_by_us)
        # 只有**匹配到正式曲目**的条目才能判断在不在歌单（未匹配的 sid 是云盘记录 id，
        # 拿它当歌曲 id 去判断/去加歌单都是错的）→ 显示成「未知」
        row["in_playlist"] = (pj.in_playlist(sid)
                              if (sid and (x.get("matched") or x.get("al_id"))) else None)
        rows.append(row)

    kw = (q or "").strip()
    if kw:
        rows = [x for x in rows if _hit(x, kw, ("title", "artist", "album"))]
    counts = {
        "total": len(rows),
        "cover_yes": sum(1 for x in rows if x["has_cover"]),
        "cover_no": sum(1 for x in rows if not x["has_cover"]),
        "pl_yes": sum(1 for x in rows if x["in_playlist"] is True),
        "pl_no": sum(1 for x in rows if x["in_playlist"] is False),
        "local_yes": sum(1 for x in rows if x["in_local"]),
        "local_no": sum(1 for x in rows if not x["in_local"]),
        "delisted_yes": sum(1 for x in rows if x.get("delisted")),
        "delisted_no": sum(1 for x in rows if not x.get("delisted")),
    }
    def keep(x):
        if cover == "yes" and not x["has_cover"]:
            return False
        if cover == "no" and x["has_cover"]:
            return False
        if album == "yes" and not x["matched"]:
            return False
        if album == "no" and x["matched"]:
            return False
        if pl == "yes" and x["in_playlist"] is not True:
            return False
        if pl == "no" and x["in_playlist"] is not False:
            return False
        if local == "yes" and not x["in_local"]:
            return False
        if local == "no" and x["in_local"]:
            return False
        if delisted == "yes" and not x.get("delisted"):
            return False
        if delisted == "no" and x.get("delisted"):
            return False
        return True

    rows = [x for x in rows if keep(x)]
    # 排序：sort=""=云盘原顺序；time=加入云盘顺序（即原顺序）；title=歌名；artist=歌手
    _csrt = (sort or "").lower()
    if _csrt in ("title", "artist"):
        rows.sort(key=lambda x: str(x.get(_csrt) or "").lower(),
                  reverse=(order or "asc").lower() == "desc")
    elif _csrt == "time":
        rows.reverse() if (order or "asc").lower() == "desc" else None
    total = len(rows)
    pages = max(1, math.ceil(total / size)) if total else 1
    out = _slice(rows, page, size)
    ret = {"items": out["items"], "page": page, "size": size, "pages": pages,
           "total": total, "count": ci.count, "counts": counts, "q": q,
           "cloud_index": ci.status(), "playlist_index": pj.status()}
    if want_sids:      # 「勾选全部」用：当前筛选结果的全部 sid（不受分页限制）
        ret["all_sids"] = [str(x.get("sid") or "") for x in rows if x.get("sid")]
    return ret


@app.post("/api/cloud/download")
async def api_cloud_download(request: Request):
    """把云盘歌曲下载回本地（云盘页每行的「下载」/ 工具条的「下载勾选的」）

    body.sids = 云盘条目的 sid 列表。只有**匹配到正式曲目**（sid 就是网易云歌曲 id）
    的条目才取得到直链；未匹配条目的 sid 是云盘记录 id，得先「编辑 → 匹配」。
    本地已经有文件的会跳过。
    """
    body = await _body(request)
    sids = [str(x) for x in (body.get("sids") or []) if str(x)]
    if not sids:
        return JSONResponse({"error": "没有选中要下载的云盘歌曲"}, status_code=400)
    ci = cloud_index.index
    # 未匹配到正式曲目的条目**也照样下载**：它的 sid 是云盘条目自带的 songId，
    # 网易云对云盘文件同样能返回播放直链（只是元数据差、文件名可能不好听）。
    # 这里只统计数量，给前端做提示，不再拦。
    unmatched = [x for x in sids
                 if not (x.isdigit() and (not ci.ready or x in ci.sids))]
    meta = {str(x.get("sid")): x for x in ci.items}
    cfg = config.load()
    queued = []
    skipped = 0
    failed = 0
    db = SessionLocal()
    try:
        for sid in sids:
            tr = (db.query(Track)
                  .filter((Track.platform_track_id == sid) | (Track.cloud_sid == sid))
                  .first())
            if tr is None:      # 别处（手机/客户端）传的云盘歌：本地没记录，从云盘元数据建一条
                x = meta.get(sid) or {}
                tr = Track(platform="netease", platform_track_id=sid,
                           title=str(x.get("title") or ("云盘歌曲 " + sid)),
                           artist=str(x.get("artist") or "未知歌手"),
                           album=str(x.get("album") or ""),
                           duration=float(x.get("duration") or 0),
                           pic_url=str(x.get("cover") or ""),
                           cloud_sid=sid, cloud_state="uploaded",
                           status="new", downloaded=False)
                db.add(tr)
                db.commit()
                db.refresh(tr)
            if tr.status == "ok" and tr.file_path and Path(tr.file_path).exists():
                skipped += 1
                continue
            queued.append(int(tr.id))
    finally:
        db.close()
    for tid in queued:
        try:
            enqueue_download_one(tid, cfg)
        except Exception:  # noqa: BLE001
            failed += 1
    return {"ok": True, "queued": len(queued) - failed, "skipped": skipped,
            "failed": failed, "unmatched": len(unmatched)}


@app.get("/api/cloud/local")
async def api_cloud_local(page: int = 1, size: int = 20, q: str = "",
                          sort: str = "", order: str = "asc"):
    """本地已下载、待上传云盘的曲目"""
    page, size = _page_args(page, size, 20, 200)
    db = SessionLocal()
    try:
        rows = (db.query(Track)
                .filter(Track.status == "ok")
                # 注意：SQL 里 NULL != 'uploaded' 结果是 NULL（假），
                # 从没上传过的曲目 cloud_state 是 NULL，必须显式带上
                .filter((Track.cloud_state.is_(None)) | (Track.cloud_state != "uploaded"))
                .order_by(Track.id.desc()).all())
        items = [_track_view(t, config.load()) for t in rows]
    finally:
        db.close()
    # 列表里要显示文件真实封面：has_cover / mtime 走整理页的扫描缓存，
    # 不在缓存里（或缓存过期）就现读一次文件的标签
    # 文件已经不在音乐库里的（例如被移进回收站）不算「待上传」，
    # 顺手把曲目状态改回未下载，避免歌单页 / 下载页还显示「已下载」
    stale = [x for x in items if x.get("file") and not Path(str(x["file"])).exists()]
    if stale:
        ids = [int(x["id"]) for x in stale if x.get("id")]
        items = [x for x in items if x not in stale]
        if ids:
            db2 = SessionLocal()
            try:
                for tr in db2.query(Track).filter(Track.id.in_(ids)).all():
                    tr.status = "new"
                    tr.downloaded_at = ""
                db2.commit()
            finally:
                db2.close()
    cache = organize.load_cache()
    for x in items:
        p = str(x.get("file") or "")
        row = cache.get(p) if p else None
        if row is None and p:
            try:
                row = organize.inspect(Path(p))
            except Exception:  # noqa: BLE001
                row = None
        x["has_cover"] = bool((row or {}).get("has_cover"))
        x["mtime"] = int((row or {}).get("mtime") or 0)
    items = [x for x in items if _hit(x, q, ("title", "artist", "album"))]
    _lsrt = (sort or "").lower()
    if _lsrt in ("title", "artist"):
        items.sort(key=lambda x: str(x.get(_lsrt) or "").lower(),
                   reverse=(order or "asc").lower() == "desc")
    elif _lsrt == "time":
        items.sort(key=lambda x: int(x.get("id") or 0), reverse=(order or "asc").lower() == "desc")
    out = _slice(items, page, size)
    return {**out, "q": q}


@app.post("/api/cloud/upload")
async def api_cloud_upload(request: Request):
    """上传到云盘：{ids:[...]} 指定曲目；{all:true} 上传全部已下载未上传的"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    cfg = config.load()
    if not _cookie(cfg):
        return JSONResponse({"error": "还没登录网易云，无法上传"}, status_code=401)
    ids = [int(x) for x in (body.get("ids") or []) if str(x).strip().isdigit()]
    if body.get("all") or not ids:
        info = enqueue_uploads(cfg, selected_only=False)
    else:
        info = enqueue_uploads(cfg, only_ids=ids)
    if not info["added"]:
        return JSONResponse({"error": "没有需要上传的曲目"}, status_code=400)
    return {"status": "started", **info}


@app.post("/api/cloud/upload/{tid}")
async def api_cloud_upload_one(tid: int):
    cfg = config.load()
    if not _cookie(cfg):
        return JSONResponse({"error": "还没登录网易云，无法上传"}, status_code=401)
    try:
        task = enqueue_upload(int(tid), force=True)    # 用户主动点的：不等退避，立刻重排
    except LookupError:
        return JSONResponse({"error": "曲目不存在"}, status_code=404)
    return {"queued": task.snapshot()}


@app.delete("/api/cloud/{sid}")
async def api_cloud_delete(sid: str):
    cfg = config.load()
    if not _cookie(cfg):
        return JSONResponse({"error": "还没登录网易云"}, status_code=401)
    try:
        data = await cloud.delete(_cookie(cfg), sid)
    except NcmError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    if data.get("code") not in (200, 0, None):
        return JSONResponse({"error": str(data.get("message") or data.get("msg") or "删除失败")},
                            status_code=400)
    db = SessionLocal()
    try:
        for row in db.query(Track).filter(Track.cloud_sid == str(sid)).all():
            row.cloud_state = ""
            row.cloud_sid = ""
        db.commit()
    finally:
        db.close()
    _CACHE.clear()
    return {"removed": sid}


# ------------------------------------------------------------- 批量任务
@app.post("/api/sync/netease")
async def api_sync_netease():
    if not config.playlist_ids(config.load()):
        return JSONResponse({"error": "还没有勾选任何歌单，请先到「歌单」页选择"}, status_code=400)
    _CACHE.clear()
    return {"status": "started" if start("sync") else "busy"}


@app.post("/api/download/pending")
async def api_download_pending():
    """开始下载勾选的曲目"""
    if start("download"):
        return {"status": "started"}
    cfg = config.load()
    info = enqueue_downloads(cfg)
    if info["added"]:
        return {"status": "queued", **info}
    return {"status": "busy"}


@app.post("/api/upload/pending")
async def api_upload_pending():
    cfg = config.load()
    info = enqueue_uploads(cfg)
    if not info["added"]:
        return JSONResponse({"error": "没有需要上传的曲目"}, status_code=400)
    return {"status": "started", **info}


@app.get("/api/status")
async def api_status():
    return status()


# ------------------------------------------------------------- 音源
def _source_view(src: dict) -> dict:
    return {
        "id": src.get("id"), "name": src.get("name"), "url": src.get("url") or "",
        "enabled": bool(src.get("enabled")), "ok": bool(src.get("ok")),
        "note": src.get("note") or "", "version": src.get("version") or "",
        "platforms": src.get("platforms") or [], "qualities": src.get("qualities") or [],
        "checked_at": src.get("checked_at") or "",
        "has_script": bool((src.get("script") or "").strip()),
    }


@app.get("/api/sources")
async def api_sources():
    cfg = config.load()
    return {"runner": await lxsource.LxRunner.ready(),
            "sources": [_source_view(s) for s in lxsource.list_sources(cfg)]}


@app.post("/api/sources")
async def api_add_source(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "请求格式错误"}, status_code=400)
    name = str(body.get("name") or "").strip()
    url = str(body.get("url") or "").strip()
    script = str(body.get("script") or "").strip()
    if not script and not url:
        return JSONResponse({"error": "请填写音源链接或粘贴脚本内容"}, status_code=400)
    if url and not script:
        try:
            script = await lxsource.fetch_script(url)
        except lxsource.LxError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    if not name:
        name = (url.rsplit("/", 1)[-1] or "未命名音源") if url else "未命名音源"
    cfg = config.load()
    entry = lxsource.add_source(cfg, name, script, url)
    data = await lxsource.LxRunner.check(script)
    lxsource.mark_checked(entry, data)
    config.save(cfg)
    return {"added": entry["id"], "source": _source_view(entry)}


@app.get("/api/sources/{sid}")
async def api_get_source(sid: str):
    """取单个音源（含脚本内容），供「编辑」使用"""
    cfg = config.load()
    entry = lxsource.find_source(cfg, sid)
    if entry is None:
        return JSONResponse({"error": "音源不存在"}, status_code=404)
    return {"source": {**_source_view(entry), "script": entry.get("script") or ""}}


@app.patch("/api/sources/{sid}")
async def api_patch_source(sid: str, request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "请求格式错误"}, status_code=400)
    cfg = config.load()
    entry = lxsource.find_source(cfg, sid)
    if entry is None:
        return JSONResponse({"error": "音源不存在"}, status_code=404)

    if "enabled" in body:
        entry["enabled"] = bool(body["enabled"])

    # 编辑：名称 / 链接 / 脚本内容；内容有变动就重新检测
    if any(k in body for k in ("name", "url", "script")):
        name = str(body.get("name") or "").strip()
        url = str(body.get("url") or "").strip()
        script = str(body.get("script") or "").strip()
        if name:
            entry["name"] = name
        entry["url"] = url
        if not script and url:
            try:
                script = await lxsource.fetch_script(url)
            except lxsource.LxError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
        if not script:
            return JSONResponse({"error": "请填写音源链接或粘贴脚本内容"}, status_code=400)
        entry["script"] = script
        data = await lxsource.LxRunner.check(script)
        lxsource.mark_checked(entry, data)

    config.save(cfg)
    return {"ok": True, "source": _source_view(entry)}


@app.delete("/api/sources/{sid}")
async def api_delete_source(sid: str):
    cfg = config.load()
    if not lxsource.remove_source(cfg, sid):
        return JSONResponse({"error": "音源不存在"}, status_code=404)
    config.save(cfg)
    return {"removed": sid}


@app.post("/api/sources/{sid}/check")
async def api_check_source(sid: str):
    cfg = config.load()
    entry = lxsource.find_source(cfg, sid)
    if entry is None:
        return JSONResponse({"error": "音源不存在"}, status_code=404)
    script = str(entry.get("script") or "")
    if not script.strip():
        return JSONResponse({"error": "该音源没有脚本内容"}, status_code=400)
    data = await lxsource.LxRunner.check(script)
    lxsource.mark_checked(entry, data)
    config.save(cfg)
    return {"source": _source_view(entry), "logs": data.get("logs") or []}


@app.post("/api/sources/{sid}/update")
async def api_update_source(sid: str):
    """按记录的音源链接重新拉取脚本并检测"""
    cfg = config.load()
    entry = lxsource.find_source(cfg, sid)
    if entry is None:
        return JSONResponse({"error": "音源不存在"}, status_code=404)
    url = str(entry.get("url") or "")
    if not url:
        return JSONResponse({"error": "该音源没有记录链接，无法更新"}, status_code=400)
    try:
        entry["script"] = await lxsource.fetch_script(url)
    except lxsource.LxError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    data = await lxsource.LxRunner.check(entry["script"])
    lxsource.mark_checked(entry, data)
    config.save(cfg)
    return {"source": _source_view(entry)}


@app.post("/api/monitor/run")
async def api_monitor_run(request: Request):
    """手动跑一次歌单监控（配置页「立即检查一次」；也可给手机自动化 / 外网调用）。

    密钥（可选）：配置页填了「触发密钥」后，必须带上
    —— 请求头 `X-Token: <密钥>` 或查询参数 `?token=<密钥>`；
    不填就不校验（局域网内直接可用）。
    先强制刷一次歌单索引，否则刚加进歌单的歌手在内存索引里还没有。
    """
    from app.runner import monitor_playlists
    from app.scheduler import refresh_playlist_index
    cfg = config.load()
    mon = cfg.get("monitor") or {}
    want = str(mon.get("token") or "").strip()
    if want:
        given = (request.headers.get("X-Token") or request.query_params.get("token") or "").strip()
        if given != want:
            return JSONResponse({"error": "触发密钥不对"}, status_code=403)
    if not bool(mon.get("on")):
        return JSONResponse({"error": "「歌单监控」开关是关的，先在配置页打开"}, status_code=400)
    try:
        await refresh_playlist_index()
    except Exception:  # noqa: BLE001
        pass          # 索引刷不动也继续，用现有索引跑一次总比不跑强
    try:
        return await monitor_playlists(cfg_in=cfg)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": "%s: %s" % (type(e).__name__, e)}, status_code=500)


@app.post("/api/sources/check-all")
async def api_check_sources_all():
    """逐个检测所有音源（顺序执行，避免并发压垮 JS 沙箱）；结果写回配置"""
    cfg = config.load()
    entries = lxsource.list_sources(cfg)
    ok, failed = 0, []
    for entry in entries:
        name = str(entry.get("name") or entry.get("id") or "?")
        script = str(entry.get("script") or "")
        if not script.strip():
            failed.append({"name": name, "msg": "没有脚本内容"})
            continue
        try:
            data = await lxsource.LxRunner.check(script)
            lxsource.mark_checked(entry, data)
            if entry.get("ok"):
                ok += 1
            else:
                failed.append({"name": name, "msg": str(entry.get("note") or "检测未通过")})
        except Exception as e:  # noqa: BLE001
            failed.append({"name": name, "msg": "%s: %s" % (type(e).__name__, e)})
    config.save(cfg)
    return {"total": len(entries), "ok": ok, "failed": failed}


@app.post("/api/sources/update-all")
async def api_update_sources_all():
    """挨个按记录的音源链接重新拉脚本并检测；没记链接的跳过"""
    cfg = config.load()
    entries = lxsource.list_sources(cfg)
    updated, skipped, failed = 0, 0, []
    for entry in entries:
        name = str(entry.get("name") or entry.get("id") or "?")
        url = str(entry.get("url") or "")
        if not url:
            skipped += 1
            continue
        try:
            entry["script"] = await lxsource.fetch_script(url)
            data = await lxsource.LxRunner.check(entry["script"])
            lxsource.mark_checked(entry, data)
            updated += 1
        except Exception as e:  # noqa: BLE001
            failed.append({"name": name, "msg": "%s: %s" % (type(e).__name__, e)})
    config.save(cfg)
    return {"total": len(entries), "updated": updated, "skipped": skipped, "failed": failed}


# ------------------------------------------------------------- 其它
@app.get("/api/health")
async def api_health():
    ncm = Ncm()
    try:
        ncm_ok = await ncm.ready()
    finally:
        await ncm.close()
    return {"ncm_api": ncm_ok, "lx_runner": await lxsource.LxRunner.ready()}


@app.get("/api/config")
def api_config():
    return config.load()


# ------------------------------------------------------------- 曲库整理
def _is_local_file(file_path) -> bool:
    """本地已存在 = 文件在整理后目录 /music/musics 下且确实存在（不以 download 为准）"""
    if not file_path:
        return False
    p = Path(str(file_path))
    if not p.exists():
        return False
    lib = Path(str(config.load().get("library_dir") or "/music/musics")).resolve()
    try:
        return p.resolve().is_relative_to(lib)
    except (OSError, ValueError):
        return False


def _safe_music_path(raw: str) -> Optional[Path]:
    """把客户端传来的路径限制在音乐库目录内（download + musics），防止越权读写库外文件"""
    if not raw:
        return None
    cfg = config.load()
    roots = [Path(str(cfg.get("download_dir") or "/music/download")).resolve(),
             Path(str(cfg.get("library_dir") or "/music/musics")).resolve()]
    try:
        p = Path(raw).resolve()
    except OSError:
        return None
    for root in roots:
        if p == root or root in p.parents:
            return p
    return None


async def _body(request: Request) -> Dict[str, Any]:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


@app.get("/organize", response_class=HTMLResponse)
async def organize_page(request: Request):
    cfg = config.load()
    return render_template("organize.html",
                           page_ctx(request, "organize", logged_in=bool(_cookie(cfg)),
                                    download_dir=str(cfg.get("download_dir") or "/music/download"),
                                    library_dir=str(cfg.get("library_dir") or "/music/musics"),
                                    trash_dir=str(cfg.get("trash_dir") or "/music/_trash")))


@app.get("/trash", response_class=HTMLResponse)
async def trash_page(request: Request):
    """回收站：只放整理里移出的歌"""
    return render_template("trash.html", page_ctx(request, "trash"))


@app.get("/api/organize/list")
async def api_org_list(page: int = 1, size: int = 20, q: str = "",
                       tag: str = "all", cover: str = "all", lyric: str = "all",
                       cloud: str = "all", delisted: str = "all",
                       section: str = "musics"):
    """曲库列表（默认 20 首/页）+ 体检统计；未变化的文件走缓存不重复读标签

    筛选：tag / cover / lyric 各取 all|yes|no（yes=有，no=缺）；
          cloud 取 all|in|out（in=已在云盘，out=不在云盘）；
          delisted 取 all|yes|no（yes=已下架）
    section：download=待整理目录（歌单/云盘下载、本地原有、别处搜集来的）
             musics=整理后目录（已补齐封面/专辑/歌词/标签）
    """
    page, size = _page_args(page, size, 20, 200)
    section = section if section in ("download", "musics") else "musics"
    data = organize.scan_music()
    files = {k: v for k, v in data["files"].items() if v.get("section") == section}
    # 云盘状态分三档（筛选/分页前就要算好，否则「是否在云盘」这一档筛不了）：
    #   cloud_paths —— 铁证在云盘：库里记的云盘条目 id 还在云盘，或这首的正式曲目 id
    #                  能对上云盘里的条目
    #   cloud_maybe —— 只是歌名/歌手像（云盘索引里有个同名条目，但不一定是这个版本）
    # 以前把「歌名像」也当「在云盘」，结果云盘里查不到的歌在整理页显示「在云盘」、
    # 云朵图标还不能点 → 用户根本传不上去（2026-09-24 实测踩到，判定必须收紧）。
    db = SessionLocal()
    known, cloud_paths = set(), set()
    ci = cloud_index.index
    ci_ready = bool(ci.ready)
    ci_entry_ids = ci.entry_ids if ci_ready else set()
    ci_sids = ci.sids if ci_ready else set()
    try:
        for r in db.query(Track).filter(Track.file_path.isnot(None)).all():
            known.add(r.file_path)
            sid, tid = str(r.cloud_sid or ""), str(r.platform_track_id or "")
            if (sid and sid in ci_entry_ids) or (tid and tid in ci_sids):
                cloud_paths.add(r.file_path)
        if not ci_ready:
            # 云盘索引还没建好：没法核对，退回「库里说传过就算在云盘」
            # （宁可显示成在云盘，也别误报「不在云盘」让人重复传一份）
            cloud_paths = {r.file_path for r in db.query(Track.file_path)
                           .filter(Track.status == "ok", Track.cloud_state == "uploaded",
                                   Track.file_path.isnot(None)).all()}
    finally:
        db.close()
    cloud_maybe = set()
    # 云盘条目按文件名索引：既用于判断「上传后本地是否又被改过」，也用于判断「是否下架」
    # （同名云盘条目若已下架，本地这份大概率是同一首下架歌）
    cloud_by_name: Dict[str, Dict[str, Any]] = {}
    if ci_ready:
        for x in cloud_index.index.items:
            fn = str(x.get("file_name") or "")
            if fn and fn not in cloud_by_name:
                cloud_by_name[fn] = x
        for key, val in files.items():
            if key in cloud_paths:
                continue
            if cloud_index.index.has_song(val.get("title") or "", val.get("artist") or ""):
                cloud_maybe.add(key)
    # 已下架：本地文件标签里的网易云歌曲 id 能对上云盘里「已下架」的条目（同一首歌）。
    # 用 sid 精确关联 —— 文件名不可靠（整理后文件名带了「歌手 - 」前缀，和云盘
    # file_name 对不上，按文件名关联会漏）。
    # 顺带把每行的网易云歌曲 id 也带出来（页面显示「ID xxx」、点击复制，手动匹配用）。
    path_sid: Dict[str, str] = {}
    delisted_paths = set()
    delisted_sids = {str(x.get("sid") or "") for x in ci.items
                     if x.get("delisted")} if ci_ready else set()
    for key in files:
        sid = organize.read_netease_id(Path(key))
        if not sid:
            continue
        path_sid[key] = sid
        if sid in delisted_sids:
            delisted_paths.add(key)
    listed = organize.list_items(files, q, page, size,
                                 {"tag": tag, "cover": cover, "lyric": lyric,
                                  "cloud": cloud, "delisted": delisted},
                                 cloud_paths=cloud_paths,
                                 delisted_paths=delisted_paths)
    # 文件 md5 校准：云盘条目自带文件 md5（原文件字节的 md5），本地也算过就能确认
    # 「云盘上那份 == 本地这份」。这是最硬的证据 —— 名字/id 都可能骗人，md5 不会。
    #   cloud_same = True  本地这份文件确实在云盘（内容一致）
    #   cloud_same = False 算过了，云盘里没有这份内容 → 云盘上那条是别的版本
    #   cloud_same = None  还没算过（前端会按需批量校验一次，不拖慢列表）
    rows_out = []
    for r in listed["items"]:
        md5 = organize.md5_state(r["path"]) or ""
        hit_item = cloud_index.index.find_by_md5(md5) if (ci_ready and md5) else None
        hit = hit_item is not None
        # 云盘上「这首歌」那条的时间：md5 命中就用命中那条，否则按同名文件找。
        # 前端拿它跟本地 mtime 比 —— 本地更晚 = 上传之后本地又被改过（校准写了标签），
        # 本地更早 = 云盘那份本来就是别的版本。
        c_it = hit_item or cloud_by_name.get(str(r.get("name") or "")) or {}
        rows_out.append({
            **r,
            "in_db": r["path"] in known,
            # md5 命中 = 这份文件确实在云盘（比按 id / 歌名认更硬），并入 in_cloud
            "in_cloud": (r["path"] in cloud_paths) or hit,
            "cloud_maybe": r["path"] in cloud_maybe,
            "file_md5": md5,
            "cloud_same": (hit if md5 else None),
            "file_mtime": int(r.get("mtime") or 0),
            "cloud_add_time": int(c_it.get("add_time") or 0),
            "cloud_md5": str(c_it.get("md5") or ""),
            "delisted": r["path"] in delisted_paths,
            "sid": path_sid.get(r["path"], ""),
            "section": section,
        })
    listed["items"] = rows_out
    cfg = config.load()
    root = str(cfg.get("library_dir") or "/music/musics") if section == "musics" \
        else str(cfg.get("download_dir") or "/music/download")
    return {"root": root, "section": section, "scanned": len(files),
            "audit": organize.audit(files), **listed}


@app.post("/api/organize/scan")
async def api_org_scan():
    """重新扫描曲库（强制重读标签）"""
    data = organize.scan_music(refresh=True)
    return {"ok": True, "scanned": data["total"], "changed": data["changed"],
            "audit": organize.audit(data["files"])}


@app.post("/api/organize/verify")
async def api_org_verify(request: Request):
    """按 md5 校验「云盘上那份」与「本地这份」是不是同一个文件

    body: {"paths": [...]}  指定文件；或 {"section": "musics", "refresh": false} 批量。
    批量默认**只算还没算过的**（结果按 (大小, 修改时间) 缓存在 /data/file_hash.json，
    同一文件不会重复算）。算 md5 走线程池，不阻塞事件循环。
    """
    body = await _body(request)
    cfg = config.load()
    wanted = [str(x) for x in (body.get("paths") or [])]
    if wanted:
        paths = [str(q) for q in (_safe_music_path(x) for x in wanted) if q is not None]
    else:
        section = str(body.get("section") or "musics")
        files = organize.scan_music()["files"]
        paths = [k for k, v in files.items() if v.get("section") == section]
        if not body.get("refresh"):
            paths = [p for p in paths if organize.md5_state(p) is None]
    paths = paths[:300]                       # 一次最多 300 首，别把请求拖太久
    if not paths:
        return {"ok": True, "checked": 0, "same": 0, "diff": 0, "items": [],
                "note": "都已经校验过了（要重算请带 refresh）"}

    def work() -> List[Dict[str, Any]]:
        ci_md5 = set(cloud_index.index.md5_item)
        out = []
        for p in paths:
            md5 = organize.file_md5(p)
            out.append({"path": p, "md5": md5, "same": bool(md5) and md5 in ci_md5})
        return out

    rows = await asyncio.to_thread(work)
    return {"ok": True, "checked": len(rows),
            "same": sum(1 for r in rows if r["same"]),
            "diff": sum(1 for r in rows if r["md5"] and not r["same"]),
            "failed": sum(1 for r in rows if not r["md5"]),
            "items": rows}


@app.get("/api/organize/queue")
async def api_org_queue():
    return {"organize": ORGANIZE.snapshot()}


@app.post("/api/organize/queue/{tid}/{action}")
async def api_org_queue_action(tid: int, action: str):
    """单个整理任务：pause / resume / cancel"""
    task = ORGANIZE.get(tid)
    if task is None:
        return JSONResponse({"error": "任务不存在或已结束"}, status_code=404)
    if action == "pause":
        task.pause()
    elif action == "resume":
        task.resume()
    elif action == "cancel":
        task.cancel()
    else:
        return JSONResponse({"error": "未知操作"}, status_code=400)
    return {"task": task.snapshot()}


@app.post("/api/organize/queue/{action}")
async def api_org_queue_bulk(action: str):
    """整体：pause-all / resume-all / cancel-all / clear"""
    if action == "pause-all":
        n = ORGANIZE.pause_all()
    elif action == "resume-all":
        n = ORGANIZE.resume_all()
    elif action == "cancel-all":
        n = ORGANIZE.cancel_all()
    elif action == "clear":
        n = ORGANIZE.clear_finished()
    else:
        return JSONResponse({"error": "未知操作"}, status_code=400)
    return {"ok": True, "count": n}


@app.post("/api/organize/backfill")
async def api_org_backfill(request: Request):
    """一键批量刮削：把缺标签/封面/歌词的文件排进整理队列"""
    body = await _body(request)
    cfg = config.load()
    limit = int(body.get("limit") or (cfg.get("organize") or {}).get("batch") or 20)
    return enqueue_backfill(cfg, limit=limit, keyword=str(body.get("q") or ""))


@app.get("/api/organize/item")
async def api_org_item(path: str):
    """编辑界面需要的信息：标签 + 当前封面 + 当前歌词"""
    p = _safe_music_path(path)
    if p is None:
        return JSONResponse({"error": "路径不在音乐库目录内"}, status_code=400)
    if not p.exists():
        return JSONResponse({"error": "文件不存在"}, status_code=404)
    return {"item": organize.detail(p)}


@app.post("/api/organize/search")
async def api_org_search(request: Request):
    """按关键词搜候选（可选平台：wy / tx / kw / kg / mg）"""
    body = await _body(request)
    kw = str(body.get("q") or "").strip()
    if not kw:
        return {"items": []}
    plat = str(body.get("platform") or "wy")
    items = await organize.search_candidates(config.load(), kw,
                                             int(body.get("limit") or 12), plat)
    if plat == "wy":
        items = sorted(items, key=lambda c: -organize.score_candidate(c, kw, ""))
    # 标记「这条已经在你的云盘里了」：网易云的搜索会把用户自己的云盘记录也混进来
    # （未匹配的记录 id、以及已经绑定的正式曲目 id 都在索引里），点它去匹配必然被拒
    ci = cloud_index.index
    for x in items:
        sid = str(x.get("id") or "")
        x["in_cloud"] = bool(sid) and (sid in ci.entry_ids)
    return {"items": items, "platform": plat}


@app.get("/api/organize/platforms")
async def api_org_platforms():
    """可选的刮削来源（界面上的下拉框）"""
    return {"platforms": [{"id": k, "name": v} for k, v in platforms.PLATFORMS.items()]}


@app.post("/api/organize/apply")
async def api_org_apply(request: Request):
    """把手动选中的候选项写进文件（标签 + 封面 + 歌词）"""
    body = await _body(request)
    p = _safe_music_path(str(body.get("path") or ""))
    if p is None or not p.exists():
        return JSONResponse({"error": "文件不存在或不在音乐库目录内"}, status_code=400)
    cand = body.get("candidate")
    if not isinstance(cand, dict) or not cand.get("name"):
        return JSONResponse({"error": "没有选择候选项"}, status_code=400)
    res = await organize.apply_candidate(
        config.load(), p, cand,
        lyrics=(None if body.get("lyrics") is None else str(body["lyrics"])))
    if not res.get("ok"):
        return JSONResponse({"error": res.get("error") or res.get("warning") or "写入失败"},
                            status_code=400)
    return res


@app.post("/api/organize/lyric")
async def api_org_lyric(request: Request):
    """按「歌名 歌手」去网易云找一首真有歌词的，返回歌词（非网易云来源补歌词用）"""
    body = await _body(request)
    name = str(body.get("name") or "").strip()
    artist = str(body.get("artist") or "").strip()
    if not name:
        return {"lyric": ""}
    cfg = config.load()
    cands = await organize.search_candidates(cfg, f"{name} {artist}".strip(), 6, "wy")
    for c in cands:
        sid = str(c.get("id") or "")
        if sid.isdigit():
            ncm = organize._ncm(cfg)   # noqa: SLF001 复用统一的客户端构造
            try:
                t = await ncm.lyric(int(sid))
            except NcmError:
                t = None
            finally:
                await ncm.close()
            if t:
                return {"lyric": t, "from": c.get("name") or "", "id": sid}
    return {"lyric": ""}


@app.post("/api/organize/match")
async def api_org_match(request: Request):
    """按网易云歌曲 id 匹配（只返回信息供预览，不写文件）"""
    body = await _body(request)
    sid = str(body.get("id") or "").strip()
    song = await organize.fetch_song(config.load(), sid)
    if not song:
        return JSONResponse({"error": f"没找到 id={sid} 的歌曲"}, status_code=404)
    cover = await organize.download_image(
        str(song.get("cover") or song.get("pic_url") or ""))
    song["cover_data"] = (("data:image/jpeg;base64," + base64.b64encode(cover).decode())
                          if cover else "")
    return {"song": song}


@app.post("/api/organize/save")
async def api_org_save(request: Request):
    """保存手改的标签 / 封面 / 歌词"""
    body = await _body(request)
    p = _safe_music_path(str(body.get("path") or ""))
    if p is None or not p.exists():
        return JSONResponse({"error": "文件不存在或不在音乐库目录内"}, status_code=400)
    try:
        meta = {
            "title": str(body.get("title") or "").strip(),
            "artist": str(body.get("artist") or "").strip(),
            "album": str(body.get("album") or "").strip(),
            "album_artist": str(body.get("album_artist") or "").strip(),
            "track": int(body.get("track") or 0),
            "disc": int(body.get("disc") or 0),
            "date": str(body.get("date") or "").strip(),
            "sid": str(body.get("sid") or "").strip(),
        }
    except (TypeError, ValueError):
        return JSONResponse({"error": "音轨号 / 碟号必须是数字"}, status_code=400)
    cover = None
    if body.get("cover_data"):
        try:
            cover = base64.b64decode(str(body["cover_data"]).split(",", 1)[-1])
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "封面图片解码失败"}, status_code=400)
        if not organize.is_image(cover):
            return JSONResponse({"error": "封面不是有效的图片"}, status_code=400)
    elif body.get("cover_url"):
        # 候选项带来的封面地址（可能来自任意平台）—— 下载后一起写入
        cover = await organize.download_image(str(body["cover_url"]))
    lrc = body.get("lyrics")
    res = organize.save_meta(p, meta, cover=cover,
                             clear_cover=bool(body.get("clear_cover")),
                             lrc=(None if lrc is None else str(lrc)))
    if not res.get("ok"):
        return JSONResponse(
            {"error": res.get("error") or res.get("warning") or "保存失败"},
            status_code=400)
    organize.drop_cache_entry(str(p))
    return {"ok": True, "warning": res.get("warning") or ""}


@app.post("/api/organize/scrape")
async def api_org_scrape(request: Request):
    """刮削单个文件：传 id 走「按 ID 匹配」，否则按现有标签自动匹配"""
    body = await _body(request)
    p = _safe_music_path(str(body.get("path") or ""))
    if p is None or not p.exists():
        return JSONResponse({"error": "文件不存在或不在音乐库目录内"}, status_code=400)
    res = await organize.scrape_file(config.load(), p,
                                     song_id=str(body.get("id") or ""),
                                     keyword=str(body.get("q") or ""),
                                     dry_run=bool(body.get("preview")))
    if res.get("ok") and not body.get("preview"):
        organize.drop_cache_entry(str(p))
        if res.get("moved_to"):
            _update_track_path(str(p), res["moved_to"])
    return res


# ------------------------------------------------------------- 本地内嵌封面：缩略 + 落盘缓存
COVER_CACHE_DIR = Path("/data/cover_cache")
COVER_CACHE_KEEP = 3000        # 缓存文件数上限
COVER_PX = 200                 # 列表里封面只有 42~44px，2~3 倍屏下 200px 足够


def _cover_cache_trim() -> None:
    """缓存目录只保留最近 COVER_CACHE_KEEP 张，避免无限膨胀"""
    try:
        files = list(COVER_CACHE_DIR.glob("*.jpg"))
        if len(files) <= COVER_CACHE_KEEP:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for p in files[:len(files) - COVER_CACHE_KEEP]:
            p.unlink(missing_ok=True)
    except OSError:
        pass


def _cover_thumb(p: Path, px: int = COVER_PX) -> Optional[bytes]:
    """取内嵌封面 → 缩成小图 → 按 (路径+mtime+大小) 落盘缓存。

    为什么必须缩：内嵌封面是**原始大图**（实测单张 1.4MB），而列表里只显示 42~44px，
    云盘页 20 行本地封面一次就能是几十 MB —— 手机端「点菜单要等几秒」的主因。
    缩完通常 20KB 上下（差 50 倍以上）；文件一改 mtime 就变，缓存自然失效。
    """
    raw = organize.current_cover(p)
    if not raw:
        return None
    cache = None
    try:
        st = p.stat()
        key = hashlib.sha1(f"{p}|{int(st.st_mtime)}|{st.st_size}|{px}".encode()).hexdigest()
        cache = COVER_CACHE_DIR / (key + ".jpg")
        if cache.exists():                       # 命中缓存：不重复解码
            return cache.read_bytes()
    except OSError:
        cache = None
    try:
        from PIL import Image                    # 懒加载：没装也不影响其它功能
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        im.thumbnail((px, px), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=86, optimize=True)
        data = buf.getvalue()
    except Exception:  # noqa: BLE001 —— 缩图失败就发原图，别让封面直接消失
        return raw
    if cache is not None:
        try:
            COVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(data)
            _cover_cache_trim()
        except OSError:
            pass
    return data


@app.get("/api/organize/cover")
async def api_org_cover(path: str):
    """返回文件内嵌封面（列表缩略图用；已缩略 + 落盘缓存，见 _cover_thumb）"""
    p = _safe_music_path(path)
    if p is None or not p.exists():
        return Response(status_code=404)
    data = await asyncio.to_thread(_cover_thumb, p)
    if not data:
        return Response(status_code=404)
    return Response(content=data, media_type=("image/png" if data[:8] == b"\x89PNG\r\n\x1a\n"
                                              else "image/jpeg"))


@app.get("/cover")
async def local_cover(path: str, v: str = ""):
    """本地曲库封面（列表缩略图）—— 列表页面专用，可被浏览器长缓存。

    ★ 故意**不放在 /api/ 下**：`no_store_api` 中间件把 /api/ 全部强制 no-store，
    封面图会被「每次翻页重新下载」（实测内嵌封面单张 1.4MB，云盘页 20 行本地封面
    一次导航就要下几十 MB —— 手机端「点菜单要等几秒」的主因）。
    URL 里带 `v=<文件 mtime>`：文件一改 mtime 就变 → 天然版本化，
    所以这里可以直接 immutable 长缓存，切页时封面全部命中本地缓存、0 请求。
    """
    p = _safe_music_path(path)
    if p is None or not p.exists():
        return Response(status_code=404)
    data = await asyncio.to_thread(_cover_thumb, p)
    if not data:
        return Response(status_code=404)
    ct = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return Response(content=data, media_type=ct,
                    headers={"Cache-Control": "public, max-age=604800, immutable"})


# ------------------------------------------------------------- 外链图片代理
IMG_CACHE_DIR = Path("/data/img_cache")
IMG_MAX_BYTES = 8 * 1024 * 1024          # 单张图上限，防意外拉到大文件
IMG_CACHE_KEEP = 4000                    # 缓存文件数上限，超了删最旧的
IMG_HEADERS = {"Cache-Control": "public, max-age=1209600"}   # 让手机/电脑各自缓存两周
_IMG_UA = "Mozilla/5.0 (compatible; musicsync/1.0)"


def _img_cache_trim() -> None:
    """缓存目录只保留最近 IMG_CACHE_KEEP 张，避免无限膨胀"""
    try:
        files = list(IMG_CACHE_DIR.glob("*.bin"))
        if len(files) <= IMG_CACHE_KEEP:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for p in files[:len(files) - IMG_CACHE_KEEP]:
            p.unlink(missing_ok=True)
            p.with_suffix(".json").unlink(missing_ok=True)
    except OSError:
        pass


def _img_shrink(u: str, px: int = 240) -> str:
    """网易云 CDN 支持 ?param=WxH 取缩略图。

    实测同一张封面：原图 1.3MB，240×240 只有 128KB，180×180 只有 72KB。
    尺寸由调用方按显示大小给（列表封面只有 42px → 160 就够，大封面 240~320），
    统一 240 是浪费：列表一屏 20 张，每张多一半流量就是多几百 KB。
    """
    try:
        parts = urlsplit(u)
    except ValueError:
        return u
    host = (parts.hostname or "").lower()
    if not host.endswith(".music.126.net") or "param=" in (parts.query or ""):
        return u
    return u + ("&" if parts.query else "?") + f"param={px}y{px}"


def _canon_img_url(u: str) -> str:
    """把网易云图片 CDN 的**轮换主机**（p1~p4.music.126.net）统一成 p1。

    实测同一张封面两次请求可能给不同主机（p3 → p4）：URL 一变，浏览器缓存和本地磁盘
    缓存就都命中不了，手机端每切一次页都要重新下载几十上百 KB（页面上有几十张封面时
    就是几 MB）。这几个主机是等价镜像，统一后缓存才真正生效。
    """
    for host in ("p2", "p3", "p4"):
        for scheme in ("https://", "http://"):
            pre = f"{scheme}{host}.music.126.net/"
            if u.startswith(pre):
                return scheme + "p1.music.126.net/" + u[len(pre):]
    return u


@app.get("/img")
async def img_proxy(u: str, px: int = 240):
    """外链图片（封面）的本地代理：服务端取图 + 缩略 + 本地缓存

    为什么不让浏览器直连：网易云 CDN 给的封面是 `http://p*.music.126.net/...`，
    手机端经常加载不出来（系统「始终使用安全连接」、代理规则、防盗链都会中招），
    显示成破图/问号，电脑端却正常。这里由 NAS 去取图、页面只连本机 ——
    手机端不再依赖「手机能不能直连网易云 CDN」；顺带做缩略图 + 本地缓存，
    同一张封面只打一次外网，手机拿到的也是十几 KB 的小图。

    `px`：期望边长（前端按显示尺寸给：列表封面 160、大封面 320）。范围 64~480，
    缩略结果按 (URL+尺寸) 分别缓存，所以同一条目在不同场景可以用不同清晰度。
    """
    try:
        px = max(64, min(480, int(px)))
    except (TypeError, ValueError):
        px = 240
    raw = _canon_img_url(str(u or "").strip())
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return Response(status_code=400)
    # 防 SSRF：只允许公网地址（拒绝内网 / 回环 / 链路本地 / 保留段）
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parts.hostname, None)
    except OSError:
        return Response(status_code=404)
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]))
        except ValueError:
            return Response(status_code=404)
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return Response(status_code=403)

    fetch = _img_shrink(raw, px)
    key = hashlib.sha1(fetch.encode("utf-8")).hexdigest()
    bin_path = IMG_CACHE_DIR / (key + ".bin")
    meta_path = IMG_CACHE_DIR / (key + ".json")
    try:
        if bin_path.exists():                    # 命中缓存：直接返回，不打外网
            ct = "image/jpeg"
            try:
                ct = str(json.loads(meta_path.read_text("utf-8")).get("ct") or ct)
            except (OSError, ValueError):
                pass
            return Response(content=bin_path.read_bytes(), media_type=ct,
                            headers=IMG_HEADERS)
    except OSError:
        pass

    try:
        timeout = aiohttp.ClientTimeout(total=20, sock_connect=8)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(fetch, headers={"Referer": "https://music.163.com/",
                                             "User-Agent": _IMG_UA}) as r:
                if r.status != 200:
                    return Response(status_code=404)
                ct = str(r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                data = await r.read()
    except Exception:  # noqa: BLE001
        return Response(status_code=502)
    if not data or len(data) > IMG_MAX_BYTES:
        return Response(status_code=502)
    if not ct.startswith("image/"):
        ct = "image/jpeg"        # 有的 CDN 不给 Content-Type，封面按 jpeg 处理
    try:
        IMG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        bin_path.write_bytes(data)
        meta_path.write_text(json.dumps({"ct": ct, "u": raw}), "utf-8")
        _img_cache_trim()
    except OSError:
        pass
    return Response(content=data, media_type=ct, headers=IMG_HEADERS)


# ------------------------------------------------------------- 云盘歌曲纠错
@app.post("/api/cloud/songs/{sid}/match")
async def api_cloud_match(sid: str, request: Request):
    """把云盘歌曲手动匹配到网易云正式曲目（网易云自动匹配错了就用这个纠正）"""
    body = await _body(request)
    asid = str(body.get("asid") or "").strip()
    if not asid.isdigit():
        return JSONResponse({"error": "请填写网易云歌曲 ID（纯数字）"}, status_code=400)
    cfg = config.load()
    cookie = _cookie(cfg)
    uid = str(((cfg.get("platforms") or {}).get("netease") or {}).get("user_id") or "")
    if not (cookie and uid):
        return JSONResponse({"error": "还没登录网易云，请先扫码登录"}, status_code=401)
    try:
        res = await cloud.match(cookie, uid, str(sid), asid)
    except NcmError as e:
        return JSONResponse({"error": f"匹配失败：{e}"}, status_code=502)
    if res.get("code") not in (200, 201):
        msg = str(res.get("message") or f"网易云返回 code={res.get('code')}")
        if "不支持匹配" in msg:
            # 网易云对「没认出音频」的云盘条目一律拒绝绑定（实测 2026-09）
            msg = ("网易云没认出这首歌的音频（这条在云盘里是「未匹配」状态），"
                   "所以不允许绑定到正式曲目。本地文件不受影响；"
                   "这条云盘记录的信息/封面由网易云决定，我们改不了")
        elif "已在云盘存在" in msg or "已存在" in msg:
            # 实测：云盘里已经有同一首（已匹配、有封面）时，网易云会拒绝再绑一条重复的
            dup = []
            try:
                me = next((x for x in cloud_index.index.items
                           if str(x.get("sid")) == str(sid)), None)
                want = _norm_title_artist((me or {}).get("title"), (me or {}).get("artist"))
                for x in cloud_index.index.items:
                    if x.get("matched") and want and want == _norm_title_artist(
                            x.get("title"), x.get("artist")):
                        dup.append(f"《{x.get('title')}》{x.get('artist') or ''}"
                                   + (f" · {x.get('album')}" if x.get("album") else ""))
            except Exception:  # noqa: BLE001
                dup = []
            msg = ("网易云不允许：这样纠正后云盘里会出现两条一样的歌 —— "
                   "说明你云盘里已经有这首歌了。")
            if dup:
                msg += "云盘里已有的那条是：" + "、".join(dup[:3]) + "（已匹配、有封面）。"
            msg += ("这条留在云盘里只是少个封面，不影响听；"
                    "想干净点，可以用云盘页的「删除」把它删掉。")
            return JSONResponse({"error": msg, "already_matched": True}, status_code=409)
        elif "文件不存在" in msg:
            # 实测：第一次匹配成功后，云盘里会**换一条新记录**，旧条目 id 不再存在；
            # 页面如果还停在旧条目上，再点匹配就会收到这句「文件不存在」。
            msg = ("这个云盘条目已经不在了 —— 通常说明这首歌刚才已经匹配成功"
                   "（匹配成功后云盘里会换一条新记录，封面也一起补上了）。"
                   "刷新一下列表，那条新的才是最新的；如果它已经显示封面就不用再匹配。"
                   "文件没有丢，本地音乐库不受影响。")
            cloud_index.index.ensure_async(cookie, force=True)     # 顺手刷成最新
            return JSONResponse({"error": msg, "already_matched": True}, status_code=409)
        return JSONResponse({"error": msg}, status_code=400)
    # 匹配成功后云盘里会**换一条新记录**（旧条目被网易云合并掉了），
    # 所以必须立刻把索引刷成最新 —— 否则页面还显示旧条目 id，你再点一次就会报「文件不存在」
    for k in [k for k in _CACHE if str(k).startswith("cloud:")]:
        _CACHE.pop(k, None)
    cloud_index.index.ensure_async(cookie, force=True)
    # 如果这首歌是本工具上传的，顺手把新匹配的正式元数据回填到本地文件
    db = SessionLocal()
    try:
        row = db.query(Track).filter(Track.cloud_sid == str(sid)).first()
        if row is not None:
            await _enrich_after_upload(cfg, cookie, row, str(sid), db)
    except Exception:  # noqa: BLE001
        pass
    finally:
        db.close()
    song = await organize.fetch_song(cfg, asid)
    return {"ok": True, "song": song or {}}


@app.post("/api/organize/delete")
async def api_org_delete(request: Request):
    """移入回收站（/music/_trash，可找回；不是彻底删除）

    同时把曲目库里的「本地状态」清掉：删除后歌单页 / 下载页 / 云盘待上传 / 云盘歌曲
    列表里都不该再显示「已下载」。

    回收站里按「相对 /music 的路径」存（download/xxx、musics/yyy），
    这样恢复时能放回它原来所在的目录。
    """
    body = await _body(request)
    wanted = [str(x) for x in (body.get("paths") or [])]
    safe = [str(q) for q in (_safe_music_path(x) for x in wanted) if q is not None]
    if not safe:
        return JSONResponse({"error": "没有可删除的文件（或路径不在音乐库目录内）"},
                            status_code=400)
    cfg = config.load()
    root = str(organize.music_base(cfg))
    res = organize.to_trash(safe, root)
    moved_paths = [str(Path(root) / rel) for rel in (res.get("moved") or [])]
    for p in moved_paths:
        organize.drop_cache_entry(p)
    res["synced"] = _sync_local_state(cfg, moved_paths, mode="deleted")
    return res


@app.post("/api/organize/upload")
async def api_org_upload(request: Request):
    """把整理页里的本地歌曲直接上传到云盘（含「外来的」歌）

    body: {"paths": ["/music/download/xxx.flac", ...], "pids": [歌单 id, ...],
           "cloud_mode": "" | "again" | "replace"}
      pids 为空 → 只上传云盘；
      pids 非空 → 上传到云盘 + 上传成功后加进这些歌单（可多选）。
      cloud_mode：对「已经在云盘」的处理 —— "" 跳过上传只补歌单；"again" 再传一份新的；
                  "replace" 先删掉云盘上旧的那一条再传（改过元数据/换了文件时用）。

    **不判断这首歌在不在歌单里** —— 本地原有的歌本来就不一定在歌单里，
    用户要的就是「先把本地歌传上云盘，再让它进歌单」这条路；
    也不拦「已经在云盘」的 —— 界面会给重传入口，这里照单执行。
    """
    body = await _body(request)
    cfg = config.load()
    if not _cookie(cfg):
        return JSONResponse({"error": "还没登录网易云，无法上传"}, status_code=401)
    wanted = [str(x) for x in (body.get("paths") or [])]
    paths = [str(q) for q in (_safe_music_path(x) for x in wanted) if q is not None]
    if not paths:
        return JSONResponse({"error": "没有可上传的文件（或路径不在音乐库目录内）"},
                            status_code=400)
    pids = [str(x) for x in (body.get("pids") or []) if str(x).strip()]
    mode = str(body.get("cloud_mode") or "")
    if mode not in ("", "again", "replace"):
        mode = ""
    res = enqueue_upload_paths(cfg, paths, pids=pids, cloud_mode=mode)
    if not res["queued"] and res["failed"]:
        return JSONResponse({"error": res["failed"][0].get("error") or "上传入队失败"},
                            status_code=400)
    return res


def _norm_title_artist(title: Any, artist: Any) -> str:
    """歌名+第一个歌手，去括号/feat.，用来找云盘里的重复条目"""
    t = cloud_index._base_title(str(title or ""))
    a = (str(artist or "").split("/")[0].split("&")[0].split(",")[0].strip().lower())
    return f"{t}|{a}" if t else ""


def _update_track_path(old_path: str, new_path: str) -> int:
    """把曲目库里某文件的 file_path 更新到新路径（整理后移动到 musics 时用）"""
    db = SessionLocal()
    try:
        rows = db.query(Track).filter(Track.file_path == old_path).all()
        for tr in rows:
            tr.file_path = new_path
        if rows:
            db.commit()
        return len(rows)
    finally:
        db.close()


def _sync_local_state(cfg: Dict[str, Any], paths: List[str], mode: str) -> int:
    """把某几个文件的「本地状态」同步到曲目库

    mode: "deleted"（移入回收站 → 变成未下载）/ "restored"（恢复 → 变回已下载）/
          "purged"（彻底删除 → 清掉文件路径）
    返回受影响的行数。
    """
    if not paths:
        return 0
    root = str(cfg.get("download_dir") or "/music")
    db = SessionLocal()
    try:
        n = 0
        for raw in paths:
            p = str(raw)
            rel = ""
            try:
                rel = str(Path(p).relative_to(root))
            except ValueError:
                rel = ""
            cands = [p]
            if rel:
                cands.append(str(Path(root) / rel))
            rows = (db.query(Track)
                    .filter(Track.file_path.in_([c for c in cands if c])).all())
            for tr in rows:
                if mode == "deleted":
                    tr.status = "new"
                    tr.downloaded_at = ""
                elif mode == "restored":
                    tr.status = "ok"
                    tr.file_path = p
                    tr.downloaded_at = time.strftime("%Y-%m-%d %H:%M:%S")
                elif mode == "purged":
                    tr.status = "new"
                    tr.file_path = ""
                    tr.downloaded_at = ""
                n += 1
        if n:
            db.commit()
        return n
    finally:
        db.close()


@app.get("/api/trash")
async def api_trash_list(page: int = 1, size: int = 20, q: str = ""):
    """回收站列表（只放整理页删掉的歌）"""
    page, size = _page_args(page, size, 20, 200)
    items = organize.trash_items()
    summary = organize.trash_summary(items)
    kw = (q or "").strip().lower()
    if kw:
        items = [x for x in items
                 if kw in f"{x.get('title')} {x.get('artist')} {x.get('album')} "
                          f"{x.get('rel')}".lower()]
    out = _slice(items, page, size)
    out["all_paths"] = [str(x["path"]) for x in items]     # 全部勾选/全部恢复用
    return {"summary": summary, "q": q, **out}


@app.post("/api/trash/restore")
async def api_trash_restore(request: Request):
    """恢复回收站里的歌到音乐库（本地状态同步变回「已下载」）"""
    body = await _body(request)
    cfg = config.load()
    root = str(organize.music_base(cfg))
    paths = [str(x) for x in (body.get("paths") or [])]
    if not paths:
        return JSONResponse({"error": "没有要恢复的文件"}, status_code=400)
    res = organize.from_trash(paths, root)
    for p in res.get("restored") or []:
        organize.drop_cache_entry(p)
    res["synced"] = _sync_local_state(cfg, res.get("restored") or [], mode="restored")
    return res


def _trash_to_lib(path: str, root: str) -> str:
    """回收站里的路径 → 它原本在音乐库里的路径（彻底删除时用它清曲目记录）"""
    try:
        rel = Path(path).relative_to(organize.trash_root())
        parts = rel.parts[1:]                      # 第 0 段是时间戳批次目录
        if parts:
            return str(Path(root) / Path(*parts))
    except (ValueError, IndexError):
        pass
    return path


@app.post("/api/trash/purge")
async def api_trash_purge(request: Request):
    """彻底删除回收站里的歌（真实删除本地文件；删了就找不回来）"""
    body = await _body(request)
    cfg = config.load()
    root = str(organize.music_base(cfg))
    paths = [str(x) for x in (body.get("paths") or [])]
    if not paths:
        return JSONResponse({"error": "没有要清空的文件"}, status_code=400)
    res = organize.purge_trash(paths)
    # 彻底删掉后，曲目表里也要清掉文件路径（回收站路径 → 还原成音乐库路径再匹配）
    lib_paths = [_trash_to_lib(p, root) for p in (res.get("purged") or [])]
    res["synced"] = _sync_local_state(cfg, lib_paths, mode="purged")
    return res
