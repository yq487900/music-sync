"""队列实例与任务执行器：把「队列 / 下载 / 上传」串起来。

main.py 与 scheduler.py 只依赖本模块，避免业务模块互相缠绕。
"""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

import app.cloud as cloud
import app.cloud_index as cloud_index
import app.config as config
import app.organize as organize
from app.db.models import SessionLocal, Track
from app.ncm import Ncm, NcmError
from app.nfo import write_song_nfo
from app.queue import Queue, Task
from app.sync.netease import (_meta_of, _ncm, _now, download_track,
                              plan_downloads, save_result)


# ------------------------------------------------- 上传后校准（以歌单数据为准）
CALIBRATE_LOG = Path("/data/calibrate.jsonl")      # 每次校准改了什么，都记一行
_FIELD_CN = {"title": "歌名", "artist": "歌手", "album": "专辑"}
# 校准力度（cloud.calibrate_mode）：
#   off  一个字节都不动本地文件（云盘那边照样匹配正式曲目）
#   fill 只补空：本地缺歌名/歌手/专辑/封面/歌词才补上，**已有的值一律不覆盖**（默认）
#   full 按网易云官方信息逐项核对并纠正（会覆盖你手改过的值）
CALIBRATE_MODES = ("off", "fill", "full")
_MODE_CN = {"off": "不改本地", "fill": "只补空", "full": "全量纠正"}


def calibrate_mode(cfg: Dict[str, Any]) -> str:
    """读「上传后自动校准」的力度；老配置只写了布尔 calibrate 时按它换算"""
    cloud = cfg.get("cloud") or {}
    mode = str(cloud.get("calibrate_mode") or "").strip().lower()
    if mode in CALIBRATE_MODES:
        return mode
    return "full" if bool(cloud.get("calibrate", True)) else "off"


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _same_text(a: str, b: str) -> bool:
    """比对两段文本（歌词）：忽略所有空白差异，只看内容一不一样"""
    norm = lambda s: "".join(str(s or "").split())      # noqa: E731
    return norm(a) == norm(b)


async def _cover_from_playlist(url: str) -> Optional[bytes]:
    """下载歌单里那张封面；占位图 / 太小 / 不是图片一律不要（返回 None）"""
    if not url:
        return None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    return None
                got = await r.read()
    except Exception:  # noqa: BLE001
        return None
    # 占位图（灰底红音符 ~6 KB）和 HTML 错误页都挡在这里
    if len(got) < organize.NO_COVER_MAX_BYTES or not organize.is_image(got):
        return None
    return got


def _log_calibration(sid: str, title: str, changes: List[str], mode: str = "") -> None:
    """把这次校准改了哪些信息写进 /data/calibrate.jsonl（排查 / 回看用）"""
    try:
        with open(CALIBRATE_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": _now(), "sid": sid, "title": title,
                                 "mode": mode or "full", "changes": changes},
                                ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- 执行器
async def _download_runner(task: Task, cfg: Dict[str, Any]) -> None:
    db = SessionLocal()
    try:
        tr = db.query(Track).filter_by(id=task.track_id).first()
        if tr is None:
            task.fail("曲目记录不存在")
            return
        res = await download_track(cfg, tr, task)
        save_result(task.track_id, res)
        if not res.get("ok"):
            task.fail(res.get("reason") or "下载失败")
            return
        task.done_with(source=res.get("source") or "", level=res.get("level") or "")
        # 元数据已齐全（下载时已写入官方 歌名/歌手/专辑/封面/歌词 + 网易云 id）
        # → 自动归档进「整理后」，不用再手动刮削一次
        if bool(((cfg.get("library") or {}).get("auto_archive", True))):
            fp = str(res.get("file_path") or "")
            if fp and organize.is_meta_complete(Path(fp)):
                dest = organize.move_into_library(Path(fp), cfg)
                if dest:
                    db.refresh(tr)
                    tr.file_path = dest
                    db.commit()
                    _task_note(task, "元数据齐全，已自动归档")
    finally:
        db.close()
    # 全局 auto_upload，或本次请求勾了「下载并转存云盘」
    if bool((cfg.get("cloud") or {}).get("auto_upload")) or getattr(task, "to_cloud", False):
        enqueue_upload(task.track_id)


async def add_to_playlists(cfg: Dict[str, Any], sids: List[str],
                           pids: List[str]) -> Dict[str, Any]:
    """把歌曲（网易云正式曲目 id）加进若干歌单。

    sids 必须是**正式曲目 id**（云盘里没匹配到正式曲目的条目拿不到歌单可用的 id）。
    「上传到云盘并加入歌单」和云盘页的「添加到歌单」共用这一份实现。
    """
    cookie = str(((cfg.get("platforms") or {}).get("netease") or {}).get("cookie") or "")
    uid = str(((cfg.get("platforms") or {}).get("netease") or {}).get("user_id") or "")
    if not cookie:
        return {"added_to": [], "failed": [{"pid": p, "msg": "还没登录网易云"} for p in pids]}
    ncm = Ncm(cookie=cookie)
    added: List[str] = []
    failed: List[Dict[str, str]] = []
    try:
        for pid in pids:
            try:
                # 这个 ncm-api（@neteasecloudmusicapienhanced）里正确、且用网易云**新版**上游
                # 接口的路由是 /playlist/tracks：op=add/del + pid + tracks（逗号分隔）。
                # 踩过的坑：/playlist/manipulate/tracks 这个路由压根不存在（返回空 body）；
                # /playlist/track/add 存在但用的是网易云已废弃的旧接口（恒回 401 无权限操作歌单）
                r = await ncm.get("/playlist/tracks", op="add", pid=pid,
                                  tracks=",".join(str(x) for x in sids))
                code = str((r or {}).get("status") or (r or {}).get("code") or "")
                if code in ("200", "201") or (r or {}).get("body"):
                    added.append(str(pid))
                else:
                    failed.append({"pid": str(pid),
                                   "msg": str((r or {}).get("message") or f"code={code}")})
            except NcmError as e:
                failed.append({"pid": str(pid), "msg": str(e)})
    finally:
        await ncm.close()
    if added and uid:
        try:      # 歌单变了 → 把歌单索引刷一遍（云盘页的「在歌单」状态跟着更新）
            from app.playlist_index import index as pl_index
            pl_index.ensure_async(cookie, uid, force=True)
        except Exception:  # noqa: BLE001
            pass
    return {"added_to": added, "failed": failed, "count": len(added)}


def _task_note(task: Task, text: str) -> None:
    """把一句结果附到任务的「来源」上（队列列表里能看到），已写过的同句不重复加"""
    text = str(text or "").strip()
    if not text or text in str(task.source or ""):
        return
    task.source = (f"{task.source} · {text}" if task.source else text)


# 上传失败后「退到队尾、稍后再来」的等待节奏（秒）。两类原因各自独立计数、各有各的预算：
#   parse  —— 网易云偶发 409「音频解析失败」（与文件无关，实测原样重传就过）→ 退避短
#   server —— 5xx（网关 / 上游抖动）或连接被掐、读超时
# 注意：等待期间任务会**退回排队状态、不占住队列**（Queue.requeue 把它挪到队尾），
# 后面的歌照常上传，所以这里的数值只决定「这首歌自己多久后再试」，不再拖累别人。
# 想更快就调这个元组；次数 = 元组长度（各 3 次重试，连首次共 4 次尝试）。
UPLOAD_RETRY_PLAN = {"parse": (2, 4, 8), "server": (3, 6, 12)}


async def _upload_runner(task: Task, cfg: Dict[str, Any]) -> None:
    cookie = str(((cfg.get("platforms") or {}).get("netease") or {}).get("cookie") or "")
    if not cookie:
        task.fail("还没登录网易云，无法上传云盘")
        return

    db = SessionLocal()
    try:
        tr = db.query(Track).filter_by(id=task.track_id).first()
        if tr is None or not tr.file_path:
            task.fail("没有可上传的本地文件")
            return
        path = Path(tr.file_path)
        if not path.exists():
            tr.cloud_state = "failed"
            tr.cloud_error = "本地文件不存在"
            db.commit()
            task.fail("本地文件不存在")
            return
        ci = cloud_index.index
        sid = str(tr.cloud_sid or "")
        mode = str(getattr(task, "cloud_mode", "") or "")
        # 曲目库记着「传过」还不够，必须跟云盘核对一次：那条可能已经被删掉了
        # （用户在网易云 App 删的、或换了账号）。不核对就会死锁 —— 云盘里明明没有，
        # 这里却永远跳过上传，页面上又显示「在云盘」不给点（2026-09-24 用户实测踩到）。
        gone = bool(sid) and ci.ready and sid not in ci.entry_ids
        if gone:
            print(f"[cloud] {path.name}：曲目库记的云盘条目 {sid} 已不在云盘，清掉记录重新上传",
                  flush=True)
            tr.cloud_sid, tr.cloud_state, tr.cloud_error = "", "", ""
            db.commit()
        elif sid and mode == "":
            # 确实还在云盘、也没要求重传：不用再传，但如果这次要的是「加入歌单」，歌单还得加上
            _task_note(task, "网易云云盘")
            await _add_task_to_playlists(cfg, tr, task)
            return
        elif sid and mode == "replace":
            # 「替换」：先删掉云盘上旧的那一条，再传新的。删失败不中断上传，只是退化成新增一份
            try:
                res = await cloud.delete(cookie, sid)
                code = str((res or {}).get("code") or (res or {}).get("status") or "")
                if code in ("200", "201"):
                    print(f"[cloud] {path.name}：已删除云盘上的旧条目 {sid}，准备重传", flush=True)
                else:
                    _task_note(task, f"云盘旧条目没删掉（返回 code={code}），已改为新增一份")
            except NcmError as e:
                _task_note(task, f"云盘旧条目没删掉（{e}），已改为新增一份")
            tr.cloud_sid, tr.cloud_state, tr.cloud_error = "", "", ""
            db.commit()
        # mode == "again"：直接往下走，再传一份新的（传成功后 cloud_sid 会被新的覆盖）

        # 上传一次；失败且值得重传 → 退回队尾稍后再来（不占队列），否则按失败处理
        err: Optional[cloud.UploadError] = None
        data: Dict[str, Any] = {}
        try:
            data = await cloud.upload(cookie, path)
        except cloud.UploadError as e:
            err = e
        if err is not None:
            kind = "server" if err.retryable else ""
            why = f"HTTP 异常：{err}"
        elif cloud.upload_ok(data):
            kind, why = "", ""
        else:
            kind = cloud.upload_retry_kind(data)
            why = f"接口返回 code={data.get('code')}"

        if kind:
            plan = UPLOAD_RETRY_PLAN.get(kind) or ()
            n = int(task.retries.get(kind, 0))
            if n < len(plan):
                task.retries[kind] = n + 1
                wait = plan[n]
                print(f"[cloud] {path.name} 上传失败（{why}），退到队尾 {wait}s 后重试"
                      f"（不占队列，后面的歌先走）", flush=True)
                UPLOADS.requeue(task, wait, f"第 {n + 1} 次重试：{why}")
                return
        if err is not None:         # 重试耗尽 / 不该重试的错 → 真失败
            tr.cloud_state = "failed"
            tr.cloud_error = str(err)[:300]
            db.commit()
            task.fail(str(err))
            return
        code = data.get("code")
        sid = cloud.uploaded_song_id(data)
        if not cloud.upload_ok(data) or not sid:
            msg = str(data.get("msg") or data.get("message")
                      or f"上传失败（接口返回 code={code}）")
            tr.cloud_state = "failed"
            tr.cloud_error = msg[:300]
            db.commit()
            task.fail(msg)
            return

        tr.cloud_sid = sid
        tr.cloud_state = "uploaded"
        tr.cloud_at = _now()
        tr.cloud_error = ""
        db.commit()
        # 立刻登记进云盘索引：否则最多 10 分钟内页面会把这歌显示成「不在云盘」
        cloud_index.index.note_upload(sid, tr.title or "", tr.artist or "")
        # 用 _task_note 而不是 done_with(source=…)：后者会覆盖掉前面「旧条目没删掉」这类提示
        _task_note(task, "网易云云盘")

        # 上传后：匹配曲库 → 再按「歌单里的数据」校准本地封面 / 专辑 / 歌词 / NFO
        await _enrich_after_upload(cfg, cookie, tr, sid, db)

        # 「上传到云盘并加入歌单」→ 上传成功后再把它加进用户选的那几个歌单
        await _add_task_to_playlists(cfg, tr, task)

        # 上传后删除本地（可选）：开关开启 → 本地文件连同封面/歌词/NFO 一起移进回收站。
        # 放在校准/加歌单之后 —— 校准要写本地文件，删早了就没得写了。
        if bool(((cfg.get("cloud") or {}).get("delete_local_after_upload", False))):
            fp = str(tr.file_path or "")
            if fp:
                p = Path(fp)
                if p.exists():
                    res = organize.to_trash([fp], str(organize.music_base(cfg)))
                    if res.get("count"):
                        organize.drop_cache_entry(fp)
                        tr.status = "new"
                        tr.downloaded_at = ""
                        db.commit()
                        _task_note(task, "已删除本地文件（入回收站）")
    finally:
        db.close()


async def _add_task_to_playlists(cfg: Dict[str, Any], tr: Track, task: Task) -> None:
    """上传成功 → 把这首歌加进任务里带的歌单（整理页那两个上传选项里的第二个）

    加歌单只能用**正式曲目 id**：外来的本地歌如果没有这个 id，就只能传上云盘、
    加不了歌单（网易云不认它的音频时绑不上正式曲目），要如实告诉用户，别假装成功。
    """
    pids = [str(x) for x in (getattr(task, "to_playlists", None) or [])]
    if not pids:
        return
    sid = str(tr.platform_track_id or "")
    if not sid.isdigit():
        _task_note(task, "已传云盘，但加不了歌单：这首歌还没对上网易云的正式曲目")
        print(f"[upload] {tr.title or ''}：没有正式曲目 id，跳过加入歌单", flush=True)
        return
    try:
        res = await add_to_playlists(cfg, [sid], pids)
    except Exception as e:  # noqa: BLE001
        _task_note(task, f"加入歌单失败：{type(e).__name__}")
        return
    if res.get("added_to"):
        _task_note(task, f"已加入 {len(res['added_to'])} 个歌单")
    if res.get("failed"):
        _task_note(task, f"{len(res['failed'])} 个歌单没加成：{res['failed'][0].get('msg')}")


async def _enrich_after_upload(cfg: Dict[str, Any], cookie: str, tr: Track,
                               sid: str, db) -> None:
    """上传完成后：① 让云盘条目匹配到正式曲目 ② 按配置的力度校准本地文件

    ① 匹配：网易云不保存上传文件里的封面，云盘条目**只有匹配到正式曲目才有封面**。
       这一步**始终执行，不受校准开关影响** —— 它决定云盘上显示成哪首歌、有没有封面，
       跟「本地文件要不要被改」是两件独立的事（2026-09-24 解耦）。

    ② 校准本地文件，力度看 cloud.calibrate_mode：
       off  一个字节都不动；
       fill 只补空（本地缺的才补，已有的值一律不覆盖）—— 默认；
       full 按网易云官方信息逐项核对并纠正（会覆盖手改过的值）。
       标准信息取自 platform_track_id 那首歌的官方数据，并刷新 NFO 与整理页缓存。

    硬规则（都是实测踩出来的，别再放宽）：
      * 占位图（< 8 KB 灰底红音符）一律不写进本地文件；
      * 网易云没有的字段绝不写空（沿用本地现值）；
      * 只有确实要动才写文件，每处改动都打进日志与 /data/calibrate.jsonl。
    """
    target = str(tr.platform_track_id or "")
    if not target.isdigit():
        return                                   # 没有网易云歌曲 id（外来文件、又没刮削过）
    mode = calibrate_mode(cfg)
    ncm = _ncm(cfg)
    try:
        uid = str(((cfg.get("platforms") or {}).get("netease") or {}).get("user_id") or "")
        # ① 云盘条目 → 正式曲目（跟校准开关无关：不匹配的话云盘上没封面、也不是正式曲目）
        if uid and sid and str(sid) != target:
            try:
                await cloud.match(cookie, uid, sid, target)
            except NcmError:
                pass
        if mode == "off":
            print(f"[calibrate] {tr.title or target}：校准已关闭，本地文件不动", flush=True)
            return
        # ② 官方标准：这首歌在网易云的信息（= 他歌单里那条的数据）
        songs = await ncm.song_detail([int(target)])
        if not songs:
            print(f"[calibrate] {tr.title or target}：取不到网易云信息，跳过", flush=True)
            return
        detail = songs[0]
        al = detail.get("al") or {}
        if not int(al.get("id") or 0):
            print(f"[calibrate] {tr.title or target}：网易云没有这首歌的正式条目"
                  f"（al.id=0），跳过校准", flush=True)
            return
        raw = tr.track_metadata if isinstance(tr.track_metadata, dict) else {}
        std = _meta_of(int(target), detail, raw.get("_playlist"))   # 歌单口径的标准信息

        path = Path(tr.file_path) if tr.file_path else None
        if path is None or not path.exists():
            return
        local = organize.inspect(path)           # 现读文件里现有的信息
        changes: List[str] = []
        filling = (mode == "fill")               # 只补空：本地已有的一律不覆盖

        # 歌名 / 歌手 / 专辑：full = 以官方为准；fill = 只在本地为空时补
        for key in ("title", "artist", "album"):
            want = str(std.get(key) or "").strip()
            have = str(local.get(key) or "").strip()
            if not want or want == have:
                continue
            if not have:
                changes.append(f"{_FIELD_CN[key]}：（空） → {want}")
            elif not filling:
                changes.append(f"{_FIELD_CN[key]}：{have} → {want}")

        # 封面：本地没有 → 补上（两种模式都补，不覆盖任何东西）；
        # 本地有但是另一张 → 只有 full 才换（fill 尊重你自己放的那张）
        cover = None
        std_cover = await _cover_from_playlist(std.get("pic_url"))
        if std_cover:
            cur = organize.current_cover(path)
            if not cur:
                changes.append(f"封面：本地没有 → 用官方封面（{len(std_cover) // 1024} KB）")
                cover = std_cover
            elif _md5(cur) != _md5(std_cover) and not filling:
                changes.append("封面：本地那张和官方不是同一张 → 换成官方封面"
                               f"（{len(cur) // 1024} KB → {len(std_cover) // 1024} KB）")
                cover = std_cover

        # 歌词：本地没有 → 补上；与官方不同 → 只有 full 才覆盖。
        # 官方没有歌词时保留本地的，绝不清空。
        lrc = ""
        try:
            lrc = await ncm.lyric(int(target)) or ""
        except NcmError:
            lrc = ""
        lrc_out: Optional[str] = None            # None = 不动歌词
        if lrc.strip():
            have_lrc = organize.current_lyrics(path)
            if not have_lrc.strip():
                changes.append(f"歌词：本地没有 → 写入 {len(lrc.splitlines())} 行")
                lrc_out = lrc
            elif not _same_text(lrc, have_lrc) and not filling:
                changes.append(f"歌词：与官方的不一样 → 写入 {len(lrc.splitlines())} 行")
                lrc_out = lrc

        if not changes:
            print(f"[calibrate] {tr.title or target}：与官方数据一致，未改动文件"
                  f"（{_MODE_CN.get(mode, mode)}）", flush=True)
            return

        def _keep(key: str, cast=str):
            """取这一项要写进文件的值

            fill：本地已有就保留（只补空）；full：官方优先、官方空则退回本地。
            两种模式都**绝不写空**。
            """
            first, second = ((local, std) if filling else (std, local))
            a, b = first.get(key), second.get(key)
            if cast is int:
                try:
                    n = int(a or 0)
                except (TypeError, ValueError):
                    n = 0
                if n:
                    return n
                try:
                    return int(b or 0)
                except (TypeError, ValueError):
                    return 0
            return str(a or "").strip() or str(b or "").strip()

        # 确实要动：写标签（空字段用另一边的值兜底，绝不写空）
        meta = {
            "title": _keep("title"),
            "artist": _keep("artist"),
            "album": _keep("album"),
            "album_artist": _keep("album_artist"),
            "track": _keep("track", int),
            "disc": _keep("disc", int),
            "date": _keep("date"),
            "pic_url": str(al.get("picUrl") or std.get("pic_url") or ""),
            "sid": std.get("sid") or target,
        }
        res = organize.save_meta(path, meta, cover=cover, lrc=lrc_out)
        if not res.get("ok"):
            print(f"[calibrate] {tr.title or target}：写文件失败："
                  f"{res.get('error') or res.get('warning')}", flush=True)
            return
        organize.drop_cache_entry(str(path))     # 让整理页重新读这个文件

        tr.title = meta["title"] or tr.title
        tr.artist = meta["artist"] or tr.artist
        tr.album = meta["album"] or tr.album
        tr.pic_url = meta["pic_url"] or tr.pic_url
        tr.track_no = meta["track"] or tr.track_no
        tr.disc = meta["disc"] or tr.disc
        if detail.get("dt"):
            tr.duration = float(detail["dt"]) / 1000
        raw.update(detail)
        tr.track_metadata = raw
        db.commit()
        if cfg.get("nfo", True):
            try:
                write_song_nfo(path.with_suffix(".nfo"), meta, int(tr.duration or 0))
            except OSError:
                pass
        print(f"[calibrate] {meta['title']}（{target}）已按官方数据校准"
              f"（{_MODE_CN.get(mode, mode)}）：" + "；".join(changes), flush=True)
        _log_calibration(target, meta["title"], changes, mode)
    except Exception as e:  # noqa: BLE001
        print(f"[calibrate] 校准失败: {type(e).__name__}: {e}", flush=True)
    finally:
        await ncm.close()


# ---------------------------------------------------------------- 整理队列
_ORGANIZE_SEQ = itertools.count(1)


def _is_in_library(cfg, file_path) -> bool:
    """本地已存在 = 文件在整理后目录 /music/musics 下且存在"""
    if not file_path:
        return False
    p = Path(str(file_path))
    if not p.exists():
        return False
    lib = Path(str(cfg.get("library_dir") or "/music/musics")).resolve()
    try:
        return p.resolve().is_relative_to(lib)
    except (OSError, ValueError):
        return False


async def _organize_runner(task: Task, cfg: Dict[str, Any]) -> None:
    """刮削一个本地文件：标签 + 封面 + 歌词"""
    try:
        res = await organize.scrape_file(cfg, Path(task.path))
    except Exception as e:  # noqa: BLE001
        task.fail(f"{type(e).__name__}: {e}")
        return
    if not res.get("ok"):
        task.fail(str(res.get("reason") or "刮削失败"))
        return
    m = res.get("matched") or {}
    task.title = str(m.get("name") or task.title)
    task.artist = str(m.get("artist") or task.artist)
    marks = [k for k, v in (("封面", res.get("cover")), ("歌词", res.get("lyrics"))) if v]
    task.done_with(source="、".join(marks) or "仅标签")
    if res.get("moved_to"):
        db = SessionLocal()
        try:
            rows = db.query(Track).filter(Track.file_path == str(task.path)).all()
            for tr in rows:
                tr.file_path = res["moved_to"]
            if rows:
                db.commit()
        finally:
            db.close()
    if getattr(task, "to_upload", False):
        try:
            enqueue_upload(task.track_id)
        except LookupError:
            pass


ORGANIZE = Queue("organize", _organize_runner, config.load, concurrency=1)


def enqueue_backfill(cfg: Dict[str, Any], limit: int = 0,
                     keyword: str = "") -> Dict[str, Any]:
    """把「缺标签 / 缺封面 / 缺歌词」的文件加入整理队列（已齐全的自动跳过）"""
    import json

    files = organize.scan_music()["files"]
    batch = int(limit or (cfg.get("organize") or {}).get("batch") or 20)
    todo: List[Dict[str, Any]] = []
    for info in sorted(files.values(), key=lambda r: r.get("path") or ""):
        if keyword:
            hay = json.dumps(info, ensure_ascii=False).lower()
            if keyword.lower() not in hay:
                continue
        if (info.get("title") and info.get("artist") and info.get("has_cover")
                and info.get("has_lyrics")):
            continue
        todo.append(info)
        if len(todo) >= batch:
            break
    added = 0
    for info in todo:
        t = Task("organize", next(_ORGANIZE_SEQ),
                 title=info.get("title") or Path(info["path"]).name,
                 artist=info.get("artist") or "", album=info.get("album") or "")
        t.path = info["path"]
        if ORGANIZE.add(t) is t:
            added += 1
    return {"added": added, "candidates": len(todo), "total": len(files)}


def enqueue_organize_track(track_id: int, to_upload: bool = False) -> Task:
    """按曲目触发整理（刮削标签/封面/歌词）；to_upload=True 时整理完成后自动上传云盘"""
    db = SessionLocal()
    try:
        tr = db.query(Track).filter_by(id=int(track_id)).first()
        if tr is None or not tr.file_path:
            raise LookupError("曲目不存在或没有本地文件")
        t = Task("organize", tr.id, title=tr.title or "", artist=tr.artist or "",
                 album=tr.album or "")
        t.path = tr.file_path
        t.to_upload = bool(to_upload)
        return ORGANIZE.add(t)
    finally:
        db.close()


# ---------------------------------------------------------------- 歌单监控
async def monitor_playlists(dry: bool = False,
                           cfg_in: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """歌单监控：歌单里新加的 / 云盘里缺的歌 → 自动排队下载（下载完按开关自动上传）

    配置（config.monitor）：
      on     开关（配置页「歌单监控」）
      mode   new  从现在开始监控：只处理「加入歌单时间 >= 开启监控那一刻」的歌
             full 全量扫描补齐：歌单里所有不在云盘的歌都补齐，补完自动转成 new
      since  开启监控的时间戳（mode=new 的起算点）
      batch  每轮最多处理几首（默认 20，避免一次堆上千首）

    在云盘吗：查云盘索引（每 10 分钟刷，上传后立刻登记）。
    """
    from app.cloud_index import index as ci_index
    from app.playlist_index import index as pl_index

    cfg = cfg_in if isinstance(cfg_in, dict) else config.load()
    mon = dict(cfg.get("monitor") or {})
    if not bool(mon.get("on")):
        return {"ok": True, "skipped": "监控开关是关的"}
    platform_cfg = (cfg.get("platforms") or {}).get("netease") or {}
    cookie = str(platform_cfg.get("cookie") or "")
    uid = str(platform_cfg.get("user_id") or "")
    if not cookie or not uid:
        return {"ok": False, "error": "还没登录网易云"}

    mode = str(mon.get("mode") or "new")
    since = float(mon.get("since") or 0)
    batch = max(1, int(mon.get("batch") or 20))

    # ① 歌单索引（没变过的歌单不重拉，稳态只有 1 次请求）
    r = await pl_index.refresh(cookie, uid)
    if not r.get("ok"):
        print(f"[monitor] 歌单索引刷新失败：{r.get('error')}", flush=True)
        return r
    # ② 云盘索引：判断歌曲在不在云盘（过期就先补，用现有的也不阻塞）
    ci_index.ensure_async(cookie)
    ci = ci_index

    plan_down: List[str] = []          # 要下载的歌曲 id（云盘/本地都没有）
    plan_up: List[int] = []            # 本地有、云盘没有 → 整理后上传
    scanned = 0
    db = SessionLocal()
    try:
        rows = {str(t.platform_track_id): t for t in db.query(Track).all()}
        for sid in pl_index.candidates(mode, since):
            scanned += 1
            tr = rows.get(sid)
            local = bool(tr is not None and tr.status == "ok" and _is_in_library(cfg, tr.file_path))
            in_cloud = ci.lookup(sid, (tr.title if tr is not None else ""),
                                 (tr.artist if tr is not None else ""),
                                 (tr.cloud_sid if tr is not None else "") or "")
            if in_cloud is True:
                continue               # 云盘已有 → 不下载、不上传
            if local:
                plan_up.append(tr.id)  # 本地已有 → 整理 + 上传云盘（不重复下载）
                continue
            plan_down.append(sid)      # 云盘/本地都没有 → 下载
        # ③ 每轮上限：下载优先，剩下的下一轮继续
        left_down = max(0, len(plan_down) - batch)
        plan_down = plan_down[:batch]
        plan_up = plan_up[:max(1, batch - len(plan_down))]

        if dry:                                  # dry=True：只算计划，不建行、不排队
            return {"ok": True, "dry": True, "mode": mode, "scanned": scanned,
                    "download": len(plan_down), "upload": len(plan_up), "left": left_down,
                    "download_titles": [(rows[s].title if rows.get(s) is not None else s)
                                        for s in plan_down],
                    "upload_titles": [(rows[s].title if rows.get(s) is not None else s)
                                      for s in plan_up]}

        # ④ 缺曲目行的先建出来（批量取详情，一次 100 首以内）
        missing = [s for s in plan_down if s not in rows]
        if missing:
            ncm = _ncm(cfg)
            try:
                details = {str(s.get("id")): s for s in await ncm.song_detail([int(x) for x in missing])}
            except NcmError as e:
                details = {}
                print(f"[monitor] 取歌曲详情失败：{e}", flush=True)
            finally:
                await ncm.close()
            for sid in missing:
                d = details.get(sid)
                if not d:
                    continue
                meta = _meta_of(int(sid), d, (pl_index.pl_name.get(sid, "歌单监控"), 0))
                tr = Track(platform="netease", platform_track_id=sid, status="new", selected=True)
                db.add(tr)
                tr.title = meta["title"]
                tr.artist = meta["artist"]
                tr.album = meta["album"]
                tr.album_id = (d.get("al") or {}).get("id")
                tr.duration = float(d.get("dt") or 0) / 1000
                tr.track_no = meta["track"]
                tr.disc = meta["disc"]
                tr.pic_url = meta["pic_url"]
                raw = dict(d)
                raw["_playlist"] = [meta["playlist"], meta["pos"]]
                tr.track_metadata = raw
                rows[sid] = tr
            db.commit()
        queued_d = queued_u = 0
        for sid in plan_down:
            tr = rows.get(sid)
            if tr is None or tr.id is None:
                continue
            if tr.status == "ok" and tr.file_path:
                continue
            try:
                enqueue_download_one(tr.id, cfg)
                queued_d += 1
            except LookupError:
                continue
        for tid in plan_up:
            enqueue_organize_track(tid, to_upload=True)
            queued_u += 1
    finally:
        db.close()

    result = {"ok": True, "mode": mode, "scanned": scanned, "download": queued_d,
              "upload": queued_u, "left": left_down, "playlists": r.get("playlists")}
    print(f"[monitor] 模式={mode} 扫了 {scanned} 首 → 排队下载 {queued_d} / 上传 {queued_u}"
          + (f"（还有 {left_down} 首下一轮继续）" if left_down else ""), flush=True)

    # ⑤ full 模式补完了 → 自动转成「从现在开始监控」
    fresh = config.load()
    mon2 = dict(fresh.get("monitor") or {})
    mon2["last_run"] = _now()
    mon2["last_result"] = result
    if mode == "full" and queued_d == 0 and queued_u == 0 and left_down == 0:
        mon2["mode"] = "new"
        mon2["since"] = time.time()
        print("[monitor] 全量补齐完成，已转为「从现在开始监控」", flush=True)
    fresh["monitor"] = mon2
    config.save(fresh)
    return result


# ---------------------------------------------------------------- 队列实例
DOWNLOADS = Queue("download", _download_runner, config.load, concurrency=3)
UPLOADS = Queue("upload", _upload_runner, config.load, concurrency=1)


def apply_config(cfg: Dict[str, Any]) -> None:
    limits = cfg.get("limits") or {}
    DOWNLOADS.set_concurrency(int(limits.get("download_concurrency") or 3))


def snapshot() -> Dict[str, Any]:
    return {"download": DOWNLOADS.snapshot(), "upload": UPLOADS.snapshot(),
            "organize": ORGANIZE.snapshot()}


# ---------------------------------------------------------------- 入队
def enqueue_downloads(cfg: Dict[str, Any], only_ids: Optional[List[int]] = None,
                      include_unselected: bool = False,
                      source_pref: Optional[str] = None) -> Dict[str, Any]:
    """把待下载曲目加入队列；返回 {added, skipped, plan}

    only_ids 指定时只处理这些曲目（单首下载 / 批量下载勾选项）。
    """
    apply_config(cfg)
    plan = plan_downloads(cfg, only_ids=only_ids, include_unselected=include_unselected)
    limit = int((cfg.get("limits") or {}).get("max_per_run") or 0)
    truncated = 0
    if limit > 0 and len(plan) > limit:
        truncated = len(plan) - limit
        plan = plan[:limit]

    db = SessionLocal()
    added = 0
    try:
        for track_id, _kind in plan:
            tr = db.query(Track).filter_by(id=track_id).first()
            if tr is None:
                continue
            task = Task("download", track_id, title=tr.title or "", artist=tr.artist or "",
                        album=tr.album or "",
                        source_pref=source_pref if source_pref is not None else (tr.source_pref or ""))
            if DOWNLOADS.add(task) is task:      # 已有未完成任务时不重复计数
                added += 1
    finally:
        db.close()
    return {"added": added, "planned": len(plan), "truncated": truncated}


def enqueue_download_one(track_id: int, cfg: Dict[str, Any],
                         source_pref: Optional[str] = None,
                         to_cloud: bool = False) -> Task:
    """单首下载（不受勾选状态限制）；to_cloud=True 时下载完成后自动上传云盘"""
    apply_config(cfg)
    db = SessionLocal()
    try:
        tr = db.query(Track).filter_by(id=int(track_id)).first()
        if tr is None:
            raise LookupError("曲目不存在")
        pref = source_pref if source_pref is not None else (tr.source_pref or "")
        if source_pref is not None and source_pref != tr.source_pref:
            tr.source_pref = source_pref
            db.commit()
        return DOWNLOADS.add(Task("download", tr.id, title=tr.title or "",
                                  artist=tr.artist or "", album=tr.album or "",
                                  source_pref=pref, to_cloud=to_cloud))
    finally:
        db.close()


def ensure_local_track(path: str, cfg: Dict[str, Any],
                       info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """取（没有就建）某本地文件对应的曲目记录

    整理页里的「外来的」歌（从别处搜集来放进待整理目录的）在曲目库里没有记录，
    要上传云盘 / 加歌单就得先给它建一条 —— 建完它才算进了曲目库。

    另外补一道：老行**没有网易云 ID、但文件标签里现在有了**（用户后来在整理页
    按 ID 匹配 / 刮削过）→ 补上。否则「加歌单」会因为没有正式曲目 id 而失败。
    """
    p = Path(str(path))
    key = str(p)
    db = SessionLocal()
    try:
        tr = db.query(Track).filter(Track.file_path == key).first()
        created = False
        if tr is None:
            i = info if isinstance(info, dict) else organize.inspect(p)
            sid = organize.read_netease_id(p)
            tr = Track(platform="netease",
                       platform_track_id=(sid if sid.isdigit() else ""),
                       title=str(i.get("title") or p.stem),
                       artist=str(i.get("artist") or ""),
                       album=str(i.get("album") or ""),
                       track_no=int(i.get("track") or 0),
                       disc=int(i.get("disc") or 0),
                       duration=float(i.get("duration") or 0),
                       file_path=key, ext=p.suffix.lower().lstrip("."),
                       size=int(i.get("size") or 0), status="ok", downloaded=True,
                       selected=False, downloaded_at=_now())
            db.add(tr)
            db.commit()
            created = True
        elif not str(tr.platform_track_id or "").isdigit():
            sid_now = organize.read_netease_id(p)     # 只在缺 ID 时才读标签，别每次都读
            if sid_now.isdigit():
                tr.platform_track_id = sid_now
                db.commit()
        return {"id": int(tr.id), "created": created, "title": tr.title or "",
                "artist": tr.artist or "", "sid": str(tr.platform_track_id or "")}
    finally:
        db.close()


def enqueue_upload_paths(cfg: Dict[str, Any], paths: List[str],
                         pids: Optional[List[str]] = None,
                         cloud_mode: str = "") -> Dict[str, Any]:
    """把一批本地文件（整理页里的路径）排队上传云盘

    pids 非空 = 「上传到云盘并加入歌单」：上传成功后自动把这歌加进这些歌单。
    不判断歌曲在不在歌单里 —— 本地原有的歌本来就不一定在歌单里。
    cloud_mode：「已经在云盘也想重传」时的处理（"again" 再传一份 / "replace" 先删旧的再传）。
    """
    to_pl = [str(x) for x in (pids or []) if str(x).strip()]
    queued: List[Dict[str, Any]] = []
    failed: List[Dict[str, str]] = []
    for raw in paths:
        key = str(raw)
        try:
            rec = ensure_local_track(key, cfg)
        except Exception as e:  # noqa: BLE001
            failed.append({"path": key, "error": f"{type(e).__name__}: {e}"})
            continue
        try:
            enqueue_upload(int(rec["id"]), to_playlists=to_pl,
                           force=True, cloud_mode=cloud_mode)   # 用户在上传页主动点的
            queued.append({"path": key, "track_id": rec["id"],
                           "title": rec["title"], "artist": rec["artist"]})
        except Exception as e:  # noqa: BLE001
            failed.append({"path": key, "error": f"{type(e).__name__}: {e}"})
    return {"ok": True, "queued": len(queued), "count": len(queued),
            "items": queued, "failed": failed, "playlists": len(to_pl)}


def enqueue_upload(track_id: int, to_playlists: Optional[List[str]] = None,
                   force: bool = False, cloud_mode: str = "") -> Task:
    """把一首歌加入上传队列。

    force=True 用于「用户主动点的上传」：若它正在失败退避等待中，直接取消等待立刻重排
    （监控自动补传走默认 force=False，免得每轮都打断退避节奏）。
    cloud_mode：「已经在云盘也想重传」时的处理 —— "again"=再传一份；"replace"=先删旧的再传。
    """
    db = SessionLocal()
    try:
        tr = db.query(Track).filter_by(id=int(track_id)).first()
        if tr is None:
            raise LookupError("曲目不存在")
        task = Task("upload", tr.id, title=tr.title or "",
                    artist=tr.artist or "", album=tr.album or "")
        if to_playlists:
            task.to_playlists = [str(x) for x in to_playlists]
        task.cloud_mode = str(cloud_mode or "")
        out = UPLOADS.add(task, force_ready=force)
        if task.cloud_mode:
            out.cloud_mode = task.cloud_mode      # 队列里可能已有这首歌的任务，把模式补上
        return out
    finally:
        db.close()


def enqueue_uploads(cfg: Dict[str, Any], only_ids: Optional[List[int]] = None,
                    selected_only: bool = False) -> Dict[str, Any]:
    """把本地已下载但未上传的曲目加入上传队列"""
    db = SessionLocal()
    added = 0
    pending = 0
    try:
        q = db.query(Track).filter(Track.status == "ok")
        if only_ids:
            q = q.filter(Track.id.in_([int(x) for x in only_ids]))
        elif selected_only:
            q = q.filter(Track.selected.isnot(False))
        rows = q.all()
        for tr in rows:
            if tr.cloud_state == "uploaded" and tr.cloud_sid:
                continue
            if not tr.file_path or not Path(tr.file_path).exists():
                continue
            pending += 1
            task = Task("upload", tr.id, title=tr.title or "", artist=tr.artist or "",
                        album=tr.album or "")
            if UPLOADS.add(task) is task:
                added += 1
    finally:
        db.close()
    return {"added": added, "pending": pending}


# ---------------------------------------------------------------- 批量（定时任务 / 自测）
async def download_all(cfg: Dict[str, Any], on_progress=None) -> Dict[str, Any]:
    """把待下载曲目全部入队并等待完成（定时任务用）"""
    info = enqueue_downloads(cfg)
    await DOWNLOADS.drain()
    snap = DOWNLOADS.snapshot()
    levels: Dict[str, int] = {}
    failures: List[Dict[str, Any]] = []
    for t in snap["tasks"]:
        if t["state"] == "done":
            lvl = t["level"] or "?"
            levels[lvl] = levels.get(lvl, 0) + 1
        elif t["state"] == "failed":
            failures.append({"title": t["title"], "artist": t["artist"], "reason": t["error"]})
    return {
        "ok": snap["done"], "failed": snap["failed"], "levels": levels,
        "failures": failures, "added": info["added"], "upgraded": 0,
        "partial": 0, "recent": [], "errors": [], "canceled": snap["canceled"],
    }


async def upload_all(cfg: Dict[str, Any], only_ids: Optional[List[int]] = None,
                     selected_only: bool = False) -> Dict[str, Any]:
    info = enqueue_uploads(cfg, only_ids=only_ids, selected_only=selected_only)
    await UPLOADS.drain()
    snap = UPLOADS.snapshot()
    failures = [{"title": t["title"], "artist": t["artist"], "reason": t["error"]}
                for t in snap["tasks"] if t["state"] == "failed"]
    return {"ok": snap["done"], "failed": snap["failed"], "failures": failures,
            "added": info["added"], "pending": info["pending"]}
