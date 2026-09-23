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
from app.ncm import NcmError
from app.nfo import write_song_nfo
from app.queue import Queue, Task
from app.sync.netease import (_meta_of, _ncm, _now, download_track,
                              plan_downloads, save_result)


# ------------------------------------------------- 上传后校准（以歌单数据为准）
CALIBRATE_LOG = Path("/data/calibrate.jsonl")      # 每次校准改了什么，都记一行
_FIELD_CN = {"title": "歌名", "artist": "歌手", "album": "专辑"}


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


def _log_calibration(sid: str, title: str, changes: List[str]) -> None:
    """把这次校准改了哪些信息写进 /data/calibrate.jsonl（排查 / 回看用）"""
    try:
        with open(CALIBRATE_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": _now(), "sid": sid, "title": title,
                                 "changes": changes}, ensure_ascii=False) + "\n")
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
    finally:
        db.close()
    # 全局 auto_upload，或本次请求勾了「下载并转存云盘」
    if bool((cfg.get("cloud") or {}).get("auto_upload")) or getattr(task, "to_cloud", False):
        enqueue_upload(task.track_id)


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
        if tr.cloud_sid:
            task.done_with(source="网易云云盘")
            return

        data = await cloud.upload(cookie, path)
        # 网易云偶发 409「音频解析失败」（与文件本身无关，实测同样文件再传即可过）→ 重试
        for attempt in (1, 2):
            if cloud.upload_ok(data) or int(data.get("code") or 0) != 409:
                break
            print(f"[cloud] {path.name} 被网易云判 409（音频解析失败），第 {attempt} 次重传…",
                  flush=True)
            await asyncio.sleep(3 * attempt)
            data = await cloud.upload(cookie, path)
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
        task.done_with(source="网易云云盘")

        # 上传后：匹配曲库 → 再按「歌单里的数据」校准本地封面 / 专辑 / 歌词 / NFO
        await _enrich_after_upload(cfg, cookie, tr, sid, db)
    finally:
        db.close()


async def _enrich_after_upload(cfg: Dict[str, Any], cookie: str, tr: Track,
                               sid: str, db) -> None:
    """上传完成后：① 让云盘条目匹配到正式曲目 ② 按「歌单里的数据」校准本地文件

    ① 匹配：网易云不保存上传文件里的封面，云盘条目**只有匹配到正式曲目才有封面**。

    ② 校准：以**歌单里这首歌**（platform_track_id，也就是他在歌单里看到的那条）的
       网易云官方信息为准，逐项核对并纠正本地文件的
       歌名 / 歌手 / 专辑 / 封面 / 歌词，并刷新 NFO 与整理页缓存。

    硬规则（都是实测踩出来的，别再放宽）：
      * 占位图（< 8 KB 灰底红音符）一律不写进本地文件；
      * 网易云没有的字段绝不写空（沿用本地现值）；
      * 只有确实不一致才动文件，每处改动都打进日志与 /data/calibrate.jsonl。
    """
    if not bool((cfg.get("cloud") or {}).get("calibrate", True)):
        return                                   # 配置页把「上传后自动校准」关了
    target = str(tr.platform_track_id or "")
    if not target.isdigit():
        return                                   # 没有网易云歌曲 id（外来文件），无从校准
    ncm = _ncm(cfg)
    try:
        uid = str(((cfg.get("platforms") or {}).get("netease") or {}).get("user_id") or "")
        # ① 云盘条目 → 正式曲目
        if uid and sid and str(sid) != target:
            try:
                await cloud.match(cookie, uid, sid, target)
            except NcmError:
                pass
        # ② 歌单标准：这首歌在网易云的官方信息（= 他歌单里那条的数据）
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

        # 歌名 / 歌手 / 专辑：以歌单为准
        for key in ("title", "artist", "album"):
            want = str(std.get(key) or "").strip()
            have = str(local.get(key) or "").strip()
            if want and want != have:
                changes.append(f"{_FIELD_CN[key]}：{have or '（空）'} → {want}")

        # 封面：本地没有 → 补上；和歌单那张不是同一张 → 换成歌单那张
        cover = None
        std_cover = await _cover_from_playlist(std.get("pic_url"))
        if std_cover:
            cur = organize.current_cover(path)
            if not cur:
                changes.append(f"封面：本地没有 → 用歌单封面（{len(std_cover) // 1024} KB）")
                cover = std_cover
            elif _md5(cur) != _md5(std_cover):
                changes.append(f"封面：本地那张和歌单的不是同一张 → 换成歌单封面"
                               f"（{len(cur) // 1024} KB → {len(std_cover) // 1024} KB）")
                cover = std_cover

        # 歌词：以歌单为准（网易云没有歌词时保留本地的，不清空）
        lrc = ""
        try:
            lrc = await ncm.lyric(int(target)) or ""
        except NcmError:
            lrc = ""
        lrc_out: Optional[str] = None            # None = 不动歌词
        if lrc.strip():
            have_lrc = organize.current_lyrics(path)
            if not _same_text(lrc, have_lrc):
                changes.append("歌词：" + ("本地没有" if not have_lrc.strip()
                                          else "与歌单的不一样")
                               + f" → 写入 {len(lrc.splitlines())} 行")
                lrc_out = lrc

        if not changes:
            print(f"[calibrate] {tr.title or target}：与歌单数据一致，未改动文件", flush=True)
            return
        # 确实有变化：写标签（空字段用文件现值兜底，绝不写空）
        meta = {
            "title": str(std.get("title") or local.get("title") or ""),
            "artist": str(std.get("artist") or local.get("artist") or ""),
            "album": str(std.get("album") or local.get("album") or ""),
            "album_artist": str(std.get("album_artist") or local.get("album_artist") or ""),
            "track": int(std.get("track") or local.get("track") or 0),
            "disc": int(std.get("disc") or local.get("disc") or 0),
            "date": str(std.get("date") or local.get("date") or ""),
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
        print(f"[calibrate] {meta['title']}（{target}）已按歌单数据校准："
              + "；".join(changes), flush=True)
        _log_calibration(target, meta["title"], changes)
    except Exception as e:  # noqa: BLE001
        print(f"[calibrate] 校准失败: {type(e).__name__}: {e}", flush=True)
    finally:
        await ncm.close()


# ---------------------------------------------------------------- 整理队列
_ORGANIZE_SEQ = itertools.count(1)


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
            local = bool(tr is not None and tr.status == "ok" and tr.file_path)
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


def enqueue_upload(track_id: int) -> Task:
    db = SessionLocal()
    try:
        tr = db.query(Track).filter_by(id=int(track_id)).first()
        if tr is None:
            raise LookupError("曲目不存在")
        return UPLOADS.add(Task("upload", tr.id, title=tr.title or "",
                                artist=tr.artist or "", album=tr.album or ""))
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
