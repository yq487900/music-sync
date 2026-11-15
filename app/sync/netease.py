"""网易云歌单同步：歌单拉取 → 音质协商 → 下载 → 打标签 → NFO。

所有网易云请求都经自建 ncm-api（见 app/ncm.py），不直连 music.163.com。
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import aiohttp

import app.config as cfgmod
from app import lxsource
from app.db.models import Playlist, SessionLocal, Track
from app.downloader import (DownloadError, download, looks_like_preview,
                            move_into, read_duration, sniff_ext, verify)
from app.ncm import Ncm, NcmError
from app.nfo import write_album_nfo, write_song_nfo
from app.quality import chain_from, pick_level, rank
from app.queue import Canceled
from app.tagger import read_track_number, song_relative_path, tag_file
from app.verify import log_report, verify_cfg, verify_file

TMP_DIR = Path("/data/tmp")
Progress = Optional[Callable[[str, str, Optional[int], Optional[int]], None]]

# 音源偏好取值
SRC_AUTO = ""            # 网易云官方优先，拿不到完整直链再用第三方
SRC_OFFICIAL = "netease"  # 只用网易云官方

# 「临时故障」标记：写在 last_error 前缀，plan_downloads 据此改用**短退避**。
# 场景：音源沙箱（lx-runner）不可用 —— 沙箱恢复后重试通常就能成功，
# 不该像「这首歌真下不到」那样等 fail_backoff × backoff_hours（默认 24 小时）。
TRANSIENT_TAG = "[临时故障] "
TRANSIENT_BACKOFF_MIN = 10      # 临时故障的重试间隔（分钟）


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _as_int(value: Any, default: int = 0) -> int:
    """宽松转 int：非数字（如自测用的伪 id）不抛异常"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _cookie(cfg: Dict[str, Any]) -> str:
    return str(((cfg.get("platforms") or {}).get("netease") or {}).get("cookie") or "")


def _ncm(cfg: Dict[str, Any]) -> Ncm:
    delay = float(((cfg.get("limits") or {}).get("api_delay")) or 0.35)
    return Ncm(cookie=_cookie(cfg), delay=max(0.0, delay))


def _meta_of(sid: int, detail: Dict[str, Any], member: Optional[tuple] = None) -> Dict[str, Any]:
    """把 ncm-api 的歌曲详情转成内部元数据"""
    artists = [a.get("name") for a in (detail.get("ar") or detail.get("artists") or []) if a.get("name")]
    al = detail.get("al") or detail.get("album") or {}
    year = ""
    if detail.get("publishTime"):
        try:
            year = str(datetime.datetime.fromtimestamp(int(detail["publishTime"]) / 1000).year)
        except (ValueError, OSError, OverflowError):
            year = ""
    disc = 0
    if detail.get("cd"):
        m = re.match(r"\d+", str(detail["cd"]))
        disc = int(m.group()) if m else 0
    album = str(al.get("name") or "未知专辑")
    return {
        "title": str(detail.get("name") or sid),
        "artist": " / ".join(artists) or "未知歌手",
        "album_artist": (artists[0] if artists else "") or "未知歌手",
        "album": album,
        "pic_url": str(al.get("picUrl") or ""),
        "track": int(detail.get("no") or 0),
        "disc": disc,
        "date": year,
        "playlist": (member[0] if member else album),
        "pos": (member[1] if member else 0),
        "sid": sid,
    }


def _meta_of_track(tr: Track) -> Dict[str, Any]:
    """从库里的曲目行还原元数据（详情缺失时的兜底）"""
    sid = _as_int(tr.platform_track_id)
    detail = tr.track_metadata if isinstance(tr.track_metadata, dict) else {}
    if detail:
        return _meta_of(sid, detail, detail.get("_playlist"))
    return {
        "title": tr.title or str(sid), "artist": tr.artist or "未知歌手",
        "album": tr.album or "未知专辑", "album_artist": tr.artist or "",
        "pic_url": tr.pic_url or "", "track": tr.track_no or 0, "disc": tr.disc or 0,
        "date": "", "playlist": tr.album or "未命名歌单", "pos": 0, "sid": sid,
    }


def _ref_artists(detail: Dict[str, Any], meta: Dict[str, Any]) -> List[str]:
    """网易云的歌手名列表（一致性校验的基准）；详情缺失时退回拼接串"""
    out: List[str] = []
    src = (detail.get("ar") or detail.get("artists") or []) if isinstance(detail, dict) else []
    for a in src:
        if isinstance(a, dict) and a.get("name"):
            out.append(str(a["name"]))
    if not out:
        out = [x.strip() for x in str(meta.get("artist") or "").split(" / ") if x.strip()]
    return out


def _drop_rejected(path: Path, sid: int, title: str, ext: str, keep: bool) -> None:
    """校验不通过的文件：默认删掉；keep=True 时留到 /data/verify_failed/ 供人工复查"""
    if keep:
        try:
            dst_dir = Path("/data/verify_failed")
            dst_dir.mkdir(parents=True, exist_ok=True)
            name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", f"{sid}_{title}")[:120]
            path.replace(dst_dir / f"{name}.{ext}")
            return
        except OSError:
            pass
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass



# ---------------------------------------------------------------- 歌单拉取
async def fetch(cfg: Dict[str, Any], on_progress: Progress = None) -> Dict[str, Any]:
    """拉取已配置歌单的曲目清单，写入数据库"""
    summary: Dict[str, Any] = {"playlists": [], "tracks": 0, "errors": []}
    pids = cfgmod.playlist_ids(cfg)
    if not pids:
        summary["errors"].append("还没有勾选任何歌单")
        return summary

    ncm = _ncm(cfg)
    db = SessionLocal()
    try:
        membership: Dict[int, tuple] = {}
        for pid in pids:
            if on_progress:
                on_progress("拉取歌单", str(pid), None, None)
            try:
                pl = await ncm.playlist_detail(pid)
            except NcmError as e:
                summary["errors"].append(f"歌单 {pid} 拉取失败：{e}")
                continue
            name = str(pl.get("name") or pid)
            ids = await ncm.playlist_track_ids(pid, pl)
            for i, sid in enumerate(ids):
                membership.setdefault(sid, (name, i + 1))

            row = db.query(Playlist).filter_by(platform="netease", playlist_id=str(pid)).first()
            if row is None:
                row = Playlist(platform="netease", playlist_id=str(pid))
                db.add(row)
            row.title = name
            row.track_count = len(ids)
            row.last_sync = _now()
            db.commit()
            summary["playlists"].append({"id": pid, "name": name, "total": len(ids)})

        all_ids = sorted(membership)
        summary["tracks"] = len(all_ids)
        if not all_ids:
            return summary

        # 批量补详情（每 100 首一批）
        details: Dict[int, Dict[str, Any]] = {}
        total = len(all_ids)
        for i in range(0, total, 100):
            batch = all_ids[i:i + 100]
            if on_progress:
                on_progress("拉取歌曲详情", f"{min(i + 100, total)}/{total}", min(i + 100, total), total)
            for s in await ncm.song_detail(batch):
                try:
                    details[int(s["id"])] = s
                except (KeyError, TypeError, ValueError):
                    continue

        for sid in all_ids:
            d = details.get(sid)
            if not d:
                continue
            meta = _meta_of(sid, d, membership.get(sid))
            tr = db.query(Track).filter_by(platform="netease", platform_track_id=str(sid)).first()
            if tr is None:
                tr = Track(platform="netease", platform_track_id=str(sid),
                           status="new", selected=True)
                db.add(tr)
            tr.title = meta["title"]
            tr.artist = meta["artist"]
            tr.album = meta["album"]
            tr.album_id = (d.get("al") or {}).get("id")
            tr.duration = float(d.get("dt") or 0) / 1000
            tr.track_no = meta["track"]
            tr.disc = meta["disc"]
            tr.pic_url = meta["pic_url"]
            if tr.selected is None:
                tr.selected = True
            # 原始详情（含 privilege 音质权限）+ 所属歌单信息（按歌单分类时用）
            raw = dict(d)
            raw["_playlist"] = [meta["playlist"], meta["pos"]]
            tr.track_metadata = raw
        db.commit()
    finally:
        db.close()
        await ncm.close()
    return summary


# ---------------------------------------------------------------- 取流
async def resolve_stream(cfg: Dict[str, Any], tr: Track, ncm: Ncm,
                         task: Any = None) -> Dict[str, Any]:
    """按音源偏好取直链；失败返回空字典。

    source_pref：
      ""/"auto"  网易云官方优先，官方只有试听或已下架时用第三方
      "netease"  只用网易云官方
      <音源 id>   只用指定的第三方音源（失败也可以改回自动）
    """
    sid = _as_int(tr.platform_track_id)
    detail = tr.track_metadata if isinstance(tr.track_metadata, dict) else {}
    pref = str(getattr(task, "source_pref", "") or tr.source_pref or "").strip()
    chain = cfgmod.quality_chain(cfg)
    want = pick_level(detail.get("privilege"), chain)

    if pref in (SRC_AUTO, "auto", SRC_OFFICIAL):
        for lvl in chain_from(want, chain):
            try:
                e = await ncm.song_url(sid, lvl)
            except NcmError:
                continue
            if e.get("url") and not e.get("freeTrialInfo"):
                return {
                    "url": e["url"], "level": str(e.get("level") or lvl),
                    "br": int(e.get("br") or 0), "md5": str(e.get("md5") or ""),
                    "size": int(e.get("size") or 0), "type": str(e.get("type") or ""),
                    "source": "网易云官方", "third_party": False,
                }
        if pref == SRC_OFFICIAL:
            return {}

    only = None if pref in (SRC_AUTO, "auto") else pref
    try:
        got = await lxsource.resolve_url(cfg, tr, only_id=only)
    except lxsource.LxUnavailable as e:
        # 沙箱整体不可用 —— 临时故障，交给 save_result 打标记 + 短退避自动重试
        return {"transient": True, "reason": "音源沙箱不可用，稍后会自动重试（%s）" % e}
    if got:
        return {
            "url": got["url"], "level": str(got.get("quality") or ""), "br": 0,
            "md5": "", "size": 0, "type": "",
            "source": str(got.get("source") or "第三方音源"),
            "source_id": str(got.get("source_id") or ""), "third_party": True,
            # 记录平台：直链失效时要靠它把整个平台一起换掉
            "platform": str(got.get("platform") or ""),
        }
    return {}


def source_label(cfg: Dict[str, Any], pref: str) -> str:
    """音源偏好的中文说明（失败提示用）"""
    if not pref or pref == "auto":
        return "自动（网易云优先）"
    if pref == SRC_OFFICIAL:
        return "网易云官方"
    src = lxsource.find_source(cfg, pref)
    return str(src.get("name")) if src else f"音源 {pref}"


# ---------------------------------------------------------------- 下载
async def _cover_bytes(session: aiohttp.ClientSession, url: str) -> Optional[bytes]:
    if not url:
        return None
    try:
        async with session.get(url) as r:
            if r.status == 200:
                return await r.read()
    except Exception:  # noqa: BLE001
        return None
    return None


async def download_track(cfg: Dict[str, Any], tr: Track, task: Any = None) -> Dict[str, Any]:
    """下载并入库单曲；task 提供 进度 / 暂停 / 取消 / 音源偏好。

    返回 {"ok": bool, "level", "source", "reason", "warns", "file_path", ...}
    """
    sid = _as_int(tr.platform_track_id)
    detail = tr.track_metadata if isinstance(tr.track_metadata, dict) else {}
    meta = _meta_of_track(tr)
    pref = str(getattr(task, "source_pref", "") or tr.source_pref or "").strip()

    ncm = _ncm(cfg)
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
    tmp: Optional[Path] = None
    md5hex, size = "", 0
    try:
        entry = await resolve_stream(cfg, tr, ncm, task)
        if entry.get("transient"):
            return {"ok": False, "transient": True,
                    "title": meta["title"], "artist": meta["artist"],
                    "reason": entry.get("reason") or "音源沙箱不可用"}
        if not entry:
            if pref not in (SRC_AUTO, "auto"):
                reason = f"{source_label(cfg, pref)}没取到可用音源，可在歌曲右侧改回「自动」再试"
            else:
                reason = "无可用音源（网易云仅试听或已下架，且没有可用的第三方音源）"
            return {"ok": False, "title": meta["title"], "artist": meta["artist"], "reason": reason}

        if task is not None:
            task.done_with(source=entry["source"], level=entry["level"])
        if task is not None:
            await task.gate()

        def on_bytes(done: int, total: int) -> None:
            if task is not None:
                task.progress(done, total)

        tried: set = set()
        skip_plats: set = set()
        cur_plat = ""
        plat_tries = 0
        # 最多 8 次：**先在同一平台内换音源，换不动就把整个平台也跳掉**。
        # 实测存在这种曲目：网易云官方无资源、各音源的 wy 通道全 404，
        # 但同一首歌在酷狗/酷我上是有的 —— 只换音源永远跳不出 wy。
        for attempt in range(1, 9):
            try:
                tmp, md5hex, size = await download(entry["url"], TMP_DIR, meta["title"],
                                                   progress=on_bytes, control=task)
                # 有的音源给的「直链」其实是个 JSON 中转页（实测「全豆要」@kg 就是）。
                # 它会被当成文件下下来，把重试链直接截断 —— 所以在这里就先认出非音频。
                if entry.get("third_party"):
                    if not sniff_ext(tmp):
                        tmp.unlink(missing_ok=True)
                        raise DownloadError("返回的不是音频文件")
                    if looks_like_preview(tmp, float(tr.duration or 0)):
                        tmp.unlink(missing_ok=True)
                        raise DownloadError("只返回了试听片段")
                break
            except Canceled:
                raise
            except DownloadError as e:
                code = str(e)
                if entry.get("third_party") and attempt < 8:
                    pl = str(entry.get("platform") or "")
                    if pl != cur_plat:
                        cur_plat = pl
                        plat_tries = 0
                    plat_tries += 1
                    if plat_tries >= 2:
                        # 这个平台内已经换过音源了还是不行 → 整个平台跳过，
                        # 并**清空已排除音源**：某音源在此平台给坏链，不代表
                        # 它在别的平台上也不行（实测「全豆要」wy 404 但 kg 可用）。
                        skip_plats.add(pl)
                        tried = set()
                        plat_tries = 0
                    else:
                        tried.add(entry.get("source_id") or "")
                    try:
                        nxt = await lxsource.resolve_url(
                            cfg, tr, exclude=tried, skip_platforms=skip_plats)
                    except lxsource.LxUnavailable:
                        nxt = None       # 沙箱不可用：本轮没法换，走下面的失败分支
                    if nxt:
                        entry.update({
                            "url": nxt["url"], "level": str(nxt.get("quality") or ""),
                            "source": str(nxt.get("source") or "第三方音源"),
                            "source_id": str(nxt.get("source_id") or ""),
                            "platform": str(nxt.get("platform") or ""),
                            "md5": "", "size": 0, "type": "", "third_party": True,
                        })
                        if task is not None:
                            task.done_with(source=entry["source"], level=entry["level"])
                        continue
                if attempt == 1 and not entry["third_party"] and any(
                        x in code for x in ("403", "404", "410")):
                    chain = cfgmod.quality_chain(cfg)
                    want = pick_level(detail.get("privilege"), chain)
                    try:
                        e2 = await ncm.song_url(sid, str(entry.get("level") or want))
                        if e2.get("url") and not e2.get("freeTrialInfo"):
                            entry["url"] = e2["url"]
                            entry["md5"] = str(e2.get("md5") or "")
                            entry["size"] = int(e2.get("size") or 0)
                            entry["type"] = str(e2.get("type") or "")
                            continue
                    except NcmError:
                        pass
                return {"ok": False, "title": meta["title"], "artist": meta["artist"],
                        "reason": f"下载失败：{code}"}
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "title": meta["title"], "artist": meta["artist"],
                        "reason": f"下载失败：{type(e).__name__}"}
        if tmp is None:
            return {"ok": False, "title": meta["title"], "artist": meta["artist"],
                    "reason": "下载失败：重试后仍未成功"}

        if not verify(tmp, str(entry.get("md5") or ""), int(entry.get("size") or 0)):
            tmp.unlink(missing_ok=True)
            return {"ok": False, "title": meta["title"], "artist": meta["artist"],
                    "reason": "校验失败（大小或 MD5 不符）"}

        if entry["third_party"]:
            # 第三方音源：校验确实是音频，且不是几十秒的试听片段
            real_ext = sniff_ext(tmp)
            if not real_ext:
                tmp.unlink(missing_ok=True)
                return {"ok": False, "title": meta["title"], "artist": meta["artist"],
                        "reason": f"第三方音源（{entry['source']}）返回的不是音频文件"}
            if looks_like_preview(tmp, float(tr.duration or 0)):
                got_sec = int(read_duration(tmp) or 0)
                tmp.unlink(missing_ok=True)
                return {"ok": False, "title": meta["title"], "artist": meta["artist"],
                        "reason": f"第三方音源（{entry['source']}）只返回了试听片段"
                                  f"（{got_sec}s / 应为 {int(tr.duration or 0)}s）"}
            ext = real_ext
        else:
            ext = str(entry.get("type") or "mp3").lower()

        if task is not None:
            await task.gate()
        real = tmp.with_suffix(f".{ext}")
        tmp.replace(real)

        # ---------------- 一致性校验（下载与整理之间的强制关口） ----------------
        # 必须在 tag_file 之前：演唱者/专辑要比对的是**文件自带的原始标签**
        # （第三方音源在上游打的），打完我们自己的标签就变成「自己和自己比」，毫无意义。
        # 不通过 → 删文件、不落 download_dir、不自动归档、不上传云盘。
        vrep: Optional[Dict[str, Any]] = None
        vcfg = verify_cfg(cfg)
        if vcfg.get("enabled", True):
            ref_meta = {
                "sid": sid,
                "title": meta["title"],
                "duration": float(tr.duration or 0),
                "artists": _ref_artists(detail, meta),
                "album": meta["album"],
            }
            try:
                report = await verify_file(real, ref_meta, ncm, cfg,
                                           source_is_official=not entry.get("third_party"))
            except Exception as e:  # noqa: BLE001
                # 校验模块自己出问题（代码缺陷、环境缺件）不能把整条下载链路拖死：
                # 记为 skipped + 原因，照常往下走，事后能在 verify.jsonl / 任务备注里看到
                from app.verify import Report as _Report
                report = _Report(state="skipped",
                                 error=f"校验模块异常：{type(e).__name__}: {e}")
            vrep = report.to_dict()
            log_report(report, {"sid": sid, "title": meta["title"], "artist": meta["artist"],
                                "source": entry["source"], "level": str(entry.get("level") or ""),
                                "third_party": bool(entry.get("third_party")),
                                "file": str(real)})
            if not report.passed:
                _drop_rejected(real, sid, meta["title"], ext, bool(vcfg.get("keep_failed")))
                return {"ok": False, "verify_failed": True, "verify_report": vrep,
                        "title": meta["title"], "artist": meta["artist"],
                        "level": str(entry.get("level") or ""), "source": entry["source"],
                        "reason": report.summary()}

        # 网易云接口没给音轨号（no=0）时，用文件自带的编号兜底，
        # 否则会出现「文件名没编号、内嵌标签却有编号」的不一致
        if not meta.get("track"):
            tno, dno = read_track_number(real)
            if tno:
                meta["track"] = tno
                meta["disc"] = meta.get("disc") or dno

        # 歌词 / 封面 / 标签 / NFO
        warns: List[str] = []
        lyrics_cfg = cfg.get("lyrics") or {}
        lrc = ""
        if lyrics_cfg.get("lrc", True) or lyrics_cfg.get("embed", True):
            try:
                got = await ncm.lyric(sid)
            except NcmError:
                got = None
            if got is None:
                warns.append("歌词获取失败")
            else:
                lrc = got

        cover = await _cover_bytes(session, meta["pic_url"]) if meta["pic_url"] else None
        if meta["pic_url"] and cover is None:
            warns.append("封面获取失败")

        warn = tag_file(real, meta, cover, lrc if lyrics_cfg.get("embed", True) else None)
        if warn:
            warns.append(warn)

        layout = str((cfg.get("library") or {}).get("layout") or "album")
        naming = str((cfg.get("library") or {}).get("naming") or "")
        rel = song_relative_path(meta, layout, naming)
        final = Path(cfg["download_dir"]) / f"{rel}.{ext}"
        move_into(real, final)

        if lrc and lyrics_cfg.get("lrc", True):
            try:
                final.with_suffix(".lrc").write_text(lrc, encoding="utf-8")
            except OSError:
                warns.append("歌词文件写入失败")

        if cfg.get("nfo", True):
            try:
                write_song_nfo(final.with_suffix(".nfo"), meta, int((detail.get("dt") or 0) // 1000))
                if layout == "album":
                    write_album_nfo(final.parent / "album.nfo", meta)
                    if cover:
                        cover_path = final.parent / (
                            "cover.png" if cover[:8] == b"\x89PNG\r\n\x1a\n" else "cover.jpg")
                        if not cover_path.exists():
                            cover_path.write_bytes(cover)
            except OSError:
                warns.append("NFO 写入失败")

        return {"ok": True, "title": meta["title"], "artist": meta["artist"],
                "level": str(entry.get("level") or ""), "source": entry["source"],
                "warns": warns, "file_path": str(final), "ext": ext,
                "size": size, "md5": md5hex, "br": int(entry.get("br") or 0),
                "verify_report": vrep}
    finally:
        await session.close()
        await ncm.close()


def save_result(tr_id: int, res: Dict[str, Any]) -> None:
    """把下载结果写回数据库（失败也记录原因与次数）"""
    db = SessionLocal()
    try:
        row = db.query(Track).filter_by(id=tr_id).first()
        if row is None:
            return
        if res.get("ok"):
            row.status = "ok"
            row.file_path = res.get("file_path") or ""
            row.ext = res.get("ext") or ""
            row.size = int(res.get("size") or 0)
            row.md5 = res.get("md5") or ""
            row.level = res.get("level") or ""
            row.br = int(res.get("br") or 0)
            row.source_used = str(res.get("source") or "")
            row.downloaded = True
            row.fail_count = 0
            row.last_error = "".join(res.get("warns") or [])[:300]
            row.downloaded_at = _now()
            # 一致性校验明细（passed / skipped 都记下来，页面上能看「为什么没校验」）
            vr = res.get("verify_report")
            if isinstance(vr, dict) and vr:
                row.verify_state = str(vr.get("state") or "")
                row.verify_report = vr
                row.verify_at = _now()
        else:
            row.status = "failed"
            reason = str(res.get("reason") or "未知原因")[:300]
            if res.get("verify_failed"):
                # 一致性校验不通过：文件已删，走正常失败退避（不打「临时故障」标记）
                row.verify_state = "failed"
                row.verify_report = res.get("verify_report") or {}
                row.verify_at = _now()
            if res.get("transient"):
                # 临时故障：**不累加 fail_count**（不占 24 小时长退避的额度），
                # 只在 last_error 前面打标记，plan_downloads 看到标记就走短退避。
                row.last_error = (TRANSIENT_TAG + reason)[:300]
            else:
                row.last_error = reason
                row.fail_count = int(row.fail_count or 0) + 1
            row.downloaded = False
            row.downloaded_at = _now()
        db.commit()
    finally:
        db.close()


def plan_downloads(cfg: Dict[str, Any], only_ids: Optional[List[int]] = None,
                   include_unselected: bool = False) -> List[Tuple[int, str]]:
    """算出待下载清单：[(track_id, kind)]，kind 为 new / upgrade

    only_ids 指定时只考虑这些曲目（单首下载 / 勾选下载）；
    批量下载默认只看被勾选的曲目。
    """
    limits = cfg.get("limits") or {}
    chain = cfgmod.quality_chain(cfg)
    upgrade_on = bool((cfg.get("quality") or {}).get("upgrade_existing", True))
    backoff_n = int(limits.get("fail_backoff") or 0)
    backoff_h = int(limits.get("backoff_hours") or 0)
    now = datetime.datetime.now()

    db = SessionLocal()
    try:
        q = db.query(Track).filter_by(platform="netease")
        if only_ids is not None:
            q = q.filter(Track.id.in_([int(x) for x in only_ids]))
        elif not include_unselected:
            q = q.filter(Track.selected.isnot(False))
        rows = q.order_by(Track.id.asc()).all()
        plan: List[Tuple[int, str]] = []
        for tr in rows:
            exists = bool(tr.file_path) and Path(tr.file_path).exists()
            if exists and tr.status == "ok":
                if upgrade_on:
                    better = pick_level((tr.track_metadata or {}).get("privilege"), chain)
                    if rank(better) > rank(tr.level):
                        plan.append((tr.id, "upgrade"))
                continue
            # 临时故障（音源沙箱不可用这类）：**短退避**自动重试。
            # 沙箱一恢复通常很快就能补上，不该像「这首歌下不到」那样等 24 小时。
            if str(tr.last_error or "").startswith(TRANSIENT_TAG):
                if tr.downloaded_at:
                    try:
                        last = datetime.datetime.strptime(tr.downloaded_at, "%Y-%m-%d %H:%M:%S")
                        if (now - last).total_seconds() < TRANSIENT_BACKOFF_MIN * 60:
                            continue
                    except ValueError:
                        pass
            elif backoff_n and int(tr.fail_count or 0) >= backoff_n and tr.downloaded_at and backoff_h:
                try:
                    last = datetime.datetime.strptime(tr.downloaded_at, "%Y-%m-%d %H:%M:%S")
                    if (now - last).total_seconds() < backoff_h * 3600:
                        continue
                except ValueError:
                    pass
            plan.append((tr.id, "new"))
        return plan
    finally:
        db.close()
