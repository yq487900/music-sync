"""曲库整理：扫描本地目录 → 读标签 → 刮削（标签 / 封面 / 歌词）→ 回收站。

刮削策略参考 ToneCore（deltrivx/ToneCore）的实测结论：
  * 候选按「标题相似度 + 歌手匹配 - 版本噪声词」打分排序，并设分数下限，
    宁可报「找不到」也不给配到翻唱；
  * 歌词按分数逐个候选试，取**第一个真有歌词的**（最高分常是翻唱，翻唱多数没歌词）；
  * 封面必须走 song/detail 取（搜索结果的封面字段常为空），并且校验图片魔数，
    避免把 HTML 错误页当封面写进文件；超过 5MB 丢弃（防止标签膨胀）；
  * 单个文件失败绝不影响其它文件。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from mutagen import File as MutagenFile

import app.config as config
import app.platforms as platforms
from app.ncm import Ncm, NcmError
from app.tagger import tag_file

AUDIO_EXT = {".flac", ".mp3", ".m4a", ".wav", ".ape", ".ogg", ".opus"}
CACHE_PATH = Path("/data/organize_scan.json")


def _trash_dir() -> Path:
    return Path(str(config.load().get("trash_dir") or "/music/_trash"))


def trash_root() -> Path:
    """回收站根目录（配置里的 trash_dir，默认 /music/_trash）"""
    return _trash_dir()


def music_base(cfg: Optional[Dict[str, Any]] = None) -> Path:
    """download / musics 的共同父目录（通常就是 /music）。

    回收站里按「相对这个目录」的路径存文件，恢复/彻底删除时才知道它原本在
    待整理目录还是整理后目录 —— 如果只按 download_dir 存，整理后的歌恢复回来会跑错地方。
    """
    c = cfg if isinstance(cfg, dict) else config.load()
    dl = Path(str(c.get("download_dir") or "/music/download"))
    lib = Path(str(c.get("library_dir") or "/music/musics"))
    try:
        if dl.resolve().parent == lib.resolve().parent:
            return dl.resolve().parent
    except OSError:
        pass
    return dl


# 版本噪声词：命中则降权（让原唱排在翻唱前面）
NOISE_RE = re.compile(
    r"伴奏|remix|dj|翻唱|cover|纯音乐|消音|ktv|铃声|串烧|改编|慢摇|抖音|恶搞|搞笑|"
    r"鬼畜|喊麦|土味|电音|八音盒|童声|儿歌|合唱|清唱|demo|试听|片段|降调|升调|"
    r"变调|加速版|减速版|治愈版|深情版|男声版|女声版|live|现场|演唱会|演奏版|"
    r"钢琴版|吉他版|竖琴版|纯伴奏",
    re.I,
)


# ------------------------------------------------------------------ 匹配打分
def _clean(s: str) -> str:
    return re.sub(r"[\s\-_（）()《》〈〉·、,，.。!！?？'\"“”~～]", "", (s or "").lower())


def similarity(a: str, b: str) -> float:
    """清洗标点后：全等 1.0 / 包含 0.85 / 否则按字符交集比例给分"""
    x, y = _clean(a), _clean(b)
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    if x in y or y in x:
        return 0.85
    chars = set(y)
    hit = sum(1 for c in x if c in chars)
    return hit / max(len(x), len(y)) * 0.7


def strip_suffix(s: str) -> str:
    """剥掉括号后缀与破折号说明，便于与原曲名比对"""
    s = re.sub(r"[（(\[【][^）)\]】]*[）)\]】]", "", s or "")
    s = re.sub(r"\s*[-–—]\s*.*$", "", s)
    return s.strip()


def score_candidate(cand: Dict[str, Any], title: str, artist: str = "") -> float:
    """候选打分；低于 0.5 的标题相似度直接判为不相干"""
    ts = similarity(strip_suffix(cand.get("name") or ""), title)
    if ts < 0.5:
        return -9.0
    score = ts * 2
    if artist:
        asim = similarity(cand.get("artist") or "", artist)
        if asim >= 0.8:
            score += 1.5
        elif asim >= 0.5:
            score += 0.5
        else:
            score -= 1.0
    if NOISE_RE.search(cand.get("name") or ""):
        score -= 1.2
    return score


# ------------------------------------------------------------------ 读文件
# 网易云的「无封面占位图」只有 ~6 KB（灰底 + 红色音符，接口在没有封面时给的就是它）。
# 小于这个尺寸的内嵌封面一律按「没有封面」处理，免得占位图被当成真封面（整理页也会漏报）。
NO_COVER_MAX_BYTES = 8192


def _embedded_cover_len(path: Path) -> int:
    """内嵌封面的字节数（没有则 0）"""
    ext = path.suffix.lower()
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC
            return max((len(p.data) for p in FLAC(str(path)).pictures), default=0)
        if ext == ".mp3":
            from mutagen.id3 import ID3, ID3NoHeaderError
            try:
                return max((len(a.data) for a in ID3(str(path)).getall("APIC")), default=0)
            except ID3NoHeaderError:
                return 0
        if ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            return max((len(bytes(c)) for c in (MP4(str(path)).get("covr") or [])), default=0)
    except Exception:  # noqa: BLE001
        return 0
    return 0


def inspect(path: Path) -> Dict[str, Any]:
    """读一个音频文件的标签情况"""
    info: Dict[str, Any] = {
        "path": str(path), "name": path.name, "ext": path.suffix.lower().lstrip("."),
        "size": 0, "mtime": 0, "title": "", "artist": "", "album": "",
        "album_artist": "", "track": 0, "disc": 0, "date": "", "duration": 0,
        "has_cover": False, "has_lyrics": False, "has_lrc": False,
    }
    try:
        st = path.stat()
        info["size"] = st.st_size
        info["mtime"] = int(st.st_mtime)
    except OSError:
        return info

    info["has_lrc"] = path.with_suffix(".lrc").exists()

    try:
        f = MutagenFile(str(path), easy=True)
        if f is not None:
            def g(key: str) -> str:
                v = f.get(key) or [""]
                return str(v[0]) if v else ""

            info["title"] = g("title")
            info["artist"] = g("artist")
            info["album"] = g("album")
            info["album_artist"] = g("albumartist")
            info["date"] = g("date")[:10] if g("date") else ""
            tnum = str(g("tracknumber")).split("/")[0]
            dnum = str(g("discnumber")).split("/")[0]
            info["track"] = int(tnum) if tnum.isdigit() else 0
            info["disc"] = int(dnum) if dnum.isdigit() else 0
            length = getattr(getattr(f, "info", None), "length", None)
            if length:
                info["duration"] = int(round(length))
            if f.get("lyrics") or f.get("unsyncedlyrics"):
                info["has_lyrics"] = True

        # 内嵌封面（各格式读法不同；占位图不算）
        info["has_cover"] = _embedded_cover_len(path) >= NO_COVER_MAX_BYTES
    except Exception:  # noqa: BLE001  读不动就当没标签
        pass

    if not info["has_cover"]:
        info["has_cover"] = (path.parent / "cover.jpg").exists()
    if info["has_lrc"]:
        info["has_lyrics"] = True
    return info


def walk_audio(root: str, limit: int = 0) -> List[Path]:
    """递归收集音频文件（跳过隐藏目录与回收站）"""
    out: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".") and d not in ("_trash", "#recycle", "@eaDir"))
        for fn in sorted(filenames):
            if Path(fn).suffix.lower() in AUDIO_EXT:
                out.append(Path(dirpath) / fn)
                if limit and len(out) >= limit:
                    return out
    return out


def load_cache() -> Dict[str, Any]:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8")).get("files") or {}
    except Exception:  # noqa: BLE001
        return {}


def save_cache(files: Dict[str, Any]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "files": files},
                       ensure_ascii=False),
            encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 文件 md5（云盘一致性校准）
HASH_PATH = Path("/data/file_hash.json")
_hash_lock = threading.Lock()


def load_hash_cache() -> Dict[str, Any]:
    try:
        return json.loads(HASH_PATH.read_text(encoding="utf-8")).get("files") or {}
    except Exception:  # noqa: BLE001
        return {}


def _save_hash_cache(files: Dict[str, Any]) -> None:
    try:
        HASH_PATH.parent.mkdir(parents=True, exist_ok=True)
        HASH_PATH.write_text(
            json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "files": files},
                       ensure_ascii=False),
            encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def file_md5(path: Any, refresh: bool = False) -> str:
    """本地文件 md5（带缓存：大小 + 修改时间没变就直接用缓存结果）

    一首 30~80MB 的 flac 算一次约 0.1~0.3s，所以**不能**每次列列表都重算；
    缓存以 (size, mtime) 为键，文件被重新整理/替换后会自动重算。
    与云盘条目的 privateCloud.md5 是同一口径（都是原文件字节的 md5，实测一致）。
    """
    p = Path(str(path))
    try:
        st = p.stat()
    except OSError:
        return ""
    size, mtime = int(st.st_size), int(st.st_mtime)
    with _hash_lock:
        cache = load_hash_cache()
    ent = cache.get(str(p))
    if (not refresh and isinstance(ent, dict)
            and int(ent.get("size") or -1) == size and int(ent.get("mtime") or -1) == mtime
            and ent.get("md5")):
        return str(ent["md5"])
    h = hashlib.md5()
    try:
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return ""
    md5 = h.hexdigest()
    with _hash_lock:
        cache = load_hash_cache()          # 重新读一次，避免覆盖别人刚写的结果
        cache[str(p)] = {"size": size, "mtime": mtime, "md5": md5}
        _save_hash_cache(cache)
    return md5


def md5_state(path: Any) -> Optional[str]:
    """缓存里已有的 md5（不触发计算）—— 列表接口用，避免请求变慢"""
    try:
        st = Path(str(path)).stat()
    except OSError:
        return None
    ent = load_hash_cache().get(str(path))
    if (isinstance(ent, dict) and int(ent.get("size") or -1) == int(st.st_size)
            and int(ent.get("mtime") or -1) == int(st.st_mtime) and ent.get("md5")):
        return str(ent["md5"])
    return None


def forget_md5(path: Any) -> None:
    """让某个文件的 md5 缓存失效（文件被改动后调用）"""
    with _hash_lock:
        cache = load_hash_cache()
        if cache.pop(str(path), None) is not None:
            _save_hash_cache(cache)


def scan_music(refresh: bool = False, limit: int = 0) -> Dict[str, Any]:
    """扫描音乐库（download 待整理 + musics 整理后两个目录）；用「大小 + 修改时间」做增量"""
    cfg = config.load()
    dl_root = str(cfg.get("download_dir") or "/music/download")
    lib_root = str(cfg.get("library_dir") or "/music/musics")
    cache = load_cache()
    out: Dict[str, Any] = {}
    changed = 0
    for section, root in (("download", dl_root), ("musics", lib_root)):
        for p in walk_audio(root, limit):
            key = str(p)
            try:
                st = p.stat()
            except OSError:
                continue
            sig = [st.st_size, int(st.st_mtime)]
            old = cache.get(key)
            if old and not refresh and old.get("_sig") == sig:
                old["section"] = section
                out[key] = old
                continue
            info = inspect(p)
            info["_sig"] = sig
            info["section"] = section
            out[key] = info
            changed += 1
    save_cache(out)
    return {"root": lib_root, "total": len(out), "changed": changed, "files": out}


def drop_cache_entry(path: str) -> None:
    """让扫描缓存里的这条失效，下次读取会重新读标签（改完标签后调用）"""
    cache = load_cache()
    entry = cache.get(path)
    if entry is not None:
        entry.pop("_sig", None)
        save_cache(cache)


def read_netease_id(path: Path) -> str:
    """读标签里记着的网易云歌曲 id（本工具刮削时写进去的 sid）

    「外来的」本地歌没有曲目记录，但如果是本工具刮削过的，标签里有这个 id，
    上传到云盘后就能直接匹配到正式曲目、进而加入歌单。
    """
    ext = path.suffix.lower()
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC
            v = FLAC(str(path)).get("netease_id") or []
            return str(v[0]).strip() if v else ""
        if ext == ".mp3":
            from mutagen.id3 import ID3, ID3NoHeaderError
            try:
                frames = ID3(str(path)).getall("TXXX")
            except ID3NoHeaderError:
                return ""
            for f in frames:
                if str(getattr(f, "desc", "")).upper() == "NETEASE_ID" and f.text:
                    return str(f.text[0]).strip()
            return ""
        if ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            v = MP4(str(path)).get("----:com.apple.iTunes:NETEASE_ID") or []
            return bytes(v[0]).decode("utf-8", "ignore").strip() if v else ""
    except Exception:  # noqa: BLE001  读不动就当没有
        return ""
    return ""


def audit(files: Dict[str, Any]) -> Dict[str, int]:
    def miss(key: str) -> int:
        return sum(1 for f in files.values() if not f.get(key))
    return {
        "total": len(files),
        "no_title": sum(1 for f in files.values() if not f.get("title") or not f.get("artist")),
        "no_cover": miss("has_cover"),
        "no_lyrics": miss("has_lyrics"),
    }


def has_tag(f: Dict[str, Any]) -> bool:
    """有没有标签（歌名 + 歌手都算齐了才叫有）"""
    return bool(f.get("title") and f.get("artist"))


def _facet_ok(f: Dict[str, Any], filters: Dict[str, str], key: str, has: bool) -> bool:
    """筛选片的一档：yes=只看有，no=只看缺，其它（all/空）不筛

    云盘这一档沿用了站内其它页面的取值：in=在云盘 / out=不在云盘。
    """
    v = str((filters or {}).get(key) or "all")
    if v in ("yes", "in"):
        return has
    if v in ("no", "out"):
        return not has
    return True


def list_items(files: Dict[str, Any], keyword: str = "", page: int = 1,
               size: int = 20, filters: Optional[Dict[str, str]] = None,
               cloud_paths: Optional[Any] = None,
               delisted_paths: Optional[Any] = None) -> Dict[str, Any]:
    """列表化 + 搜索 + 筛选 + 分页（按 歌手/专辑/文件名 排序）

    筛选（filters；缺省或 "all" = 不筛）：tag / cover / lyric 各取 yes|no；
    cloud 取 all|in|out（在不在云盘，需同时传 cloud_paths，否则不作筛选）；
    delisted 取 all|yes|no（已下架，需同时传 delisted_paths）。
    返回里的 counts 是先按关键词过滤、再统计的各档数量，供筛选片显示数字。
    """
    rows = list(files.values())
    kw = (keyword or "").strip().lower()
    if kw:
        rows = [r for r in rows
                if kw in (r.get("title") or "").lower()
                or kw in (r.get("artist") or "").lower()
                or kw in (r.get("album") or "").lower()
                or kw in (r.get("name") or "").lower()]
    has_cloud = cloud_paths is not None
    in_cloud = lambda r: bool(has_cloud and str(r.get("path")) in cloud_paths)   # noqa: E731
    has_del = delisted_paths is not None
    is_delisted = lambda r: bool(has_del and str(r.get("path")) in delisted_paths)  # noqa: E731
    n_tag = sum(1 for r in rows if has_tag(r))
    n_cov = sum(1 for r in rows if r.get("has_cover"))
    n_lyr = sum(1 for r in rows if r.get("has_lyrics"))
    n_cld = sum(1 for r in rows if in_cloud(r)) if has_cloud else 0
    n_del = sum(1 for r in rows if is_delisted(r)) if has_del else 0
    counts = {"total": len(rows),
              "tag_yes": n_tag, "tag_no": len(rows) - n_tag,
              "cover_yes": n_cov, "cover_no": len(rows) - n_cov,
              "lyric_yes": n_lyr, "lyric_no": len(rows) - n_lyr,
              "cloud_in": n_cld, "cloud_out": len(rows) - n_cld,
              "delisted_yes": n_del, "delisted_no": len(rows) - n_del}
    rows = [r for r in rows
            if _facet_ok(r, filters, "tag", has_tag(r))
            and _facet_ok(r, filters, "cover", bool(r.get("has_cover")))
            and _facet_ok(r, filters, "lyric", bool(r.get("has_lyrics")))
            and (not has_cloud or _facet_ok(r, filters, "cloud", in_cloud(r)))
            and (not has_del or _facet_ok(r, filters, "delisted", is_delisted(r)))]
    rows.sort(key=lambda r: ((r.get("artist") or "zzz"), (r.get("album") or ""),
                             (r.get("track") or 0), (r.get("name") or "")))
    total = len(rows)
    size = max(1, min(200, int(size)))
    pages = max(1, (total + size - 1) // size)
    page = max(1, min(pages, int(page)))
    start = (page - 1) * size
    return {"total": total, "page": page, "pages": pages, "size": size,
            "items": rows[start:start + size], "counts": counts}


# ------------------------------------------------------------------ 封面 / 歌词
def is_image(data: bytes) -> bool:
    """魔数校验：只认真正的图片，防止把 HTML 错误页写进标签"""
    if len(data) > 3 and data[0] == 0xFF and data[1] == 0xD8:
        return True
    if len(data) > 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


async def download_image(url: str) -> Optional[bytes]:
    import aiohttp
    if not re.match(r"^https?://", url or ""):
        return None
    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    return None
                data = await r.read()
    except Exception:  # noqa: BLE001
        return None
    if not data or not is_image(data):
        return None
    if len(data) > 5 * 1024 * 1024:      # 过大的图不嵌入，避免标签膨胀
        return None
    return data


def current_cover(path: Path) -> Optional[bytes]:
    """取文件当前封面（内嵌优先，其次同目录 cover.jpg）"""
    ext = path.suffix.lower()
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC
            pics = FLAC(str(path)).pictures
            if pics:
                return pics[0].data
        elif ext == ".mp3":
            from mutagen.id3 import ID3, ID3NoHeaderError
            try:
                apic = ID3(str(path)).getall("APIC")
                if apic:
                    return apic[0].data
            except ID3NoHeaderError:
                pass
        elif ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            covr = MP4(str(path)).get("covr")
            if covr:
                return bytes(covr[0])
    except Exception:  # noqa: BLE001
        pass
    cj = path.parent / "cover.jpg"
    if cj.exists():
        try:
            return cj.read_bytes()
        except OSError:
            pass
    return None


def current_lyrics(path: Path) -> str:
    side = path.with_suffix(".lrc")
    if side.exists():
        try:
            return side.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            pass
    try:
        f = MutagenFile(str(path), easy=True)
        if f:
            v = f.get("lyrics") or f.get("unsyncedlyrics") or []
            if v:
                return str(v[0])
    except Exception:  # noqa: BLE001
        pass
    return ""


def detail(path: Path) -> Dict[str, Any]:
    """编辑界面需要的全部信息"""
    info = inspect(path)
    cover = current_cover(path)
    info["lyrics"] = current_lyrics(path)
    info["cover_data"] = (
        "data:image/jpeg;base64," + base64.b64encode(cover).decode() if cover else "")
    return info


# ------------------------------------------------------------------ 写标签 / 歌词 / 封面
def _clear_cover(path: Path) -> None:
    ext = path.suffix.lower()
    if ext == ".flac":
        from mutagen.flac import FLAC
        f = FLAC(str(path))
        f.clear_pictures()
        f.save()
    elif ext == ".mp3":
        from mutagen.id3 import ID3, ID3NoHeaderError
        try:
            t = ID3(str(path))
            t.delall("APIC")
            t.save()
        except ID3NoHeaderError:
            pass
    elif ext in (".m4a", ".mp4"):
        from mutagen.mp4 import MP4
        m = MP4(str(path))
        m.pop("covr", None)
        m.save()


def _clear_lyrics(path: Path) -> None:
    ext = path.suffix.lower()
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC
            f = FLAC(str(path))
            for k in ("lyrics", "unsyncedlyrics"):
                if k in f:
                    del f[k]
            f.save()
        elif ext == ".mp3":
            from mutagen.id3 import ID3, ID3NoHeaderError
            try:
                t = ID3(str(path))
                t.delall("USLT")
                t.save()
            except ID3NoHeaderError:
                pass
    except Exception:  # noqa: BLE001
        pass


def save_meta(path: Path, meta: Dict[str, Any], cover: Optional[bytes] = None,
              clear_cover: bool = False, lrc: Optional[str] = None) -> Dict[str, Any]:
    """写标签 / 封面 / 歌词。lrc=None 表示不动歌词；空串表示删掉歌词"""
    try:
        if clear_cover:
            _clear_cover(path)
        if lrc is not None and not lrc.strip():
            _clear_lyrics(path)
        warn = tag_file(path, meta, cover, (lrc or "").strip() or None)
        if lrc is not None:
            side = path.with_suffix(".lrc")
            if lrc.strip():
                side.write_text(lrc, encoding="utf-8")
            elif side.exists():
                side.unlink()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": warn is None, "warning": warn or ""}


# ------------------------------------------------------------------ 网易云取数
def _ncm(cfg: Dict[str, Any]) -> Ncm:
    ck = ((cfg.get("platforms") or {}).get("netease") or {}).get("cookie") or ""
    delay = float((cfg.get("limits") or {}).get("api_delay") or 0.35)
    return Ncm(cookie=ck or "os=pc", delay=delay)


def _song_row(raw: Dict[str, Any]) -> Dict[str, Any]:
    """兼容 /cloudsearch（ar/al/dt）与 /search（artists/album/duration）两种返回"""
    if raw.get("ar") is not None or raw.get("al") is not None:
        artists = "/".join(a.get("name", "") for a in (raw.get("ar") or []))
        album = (raw.get("al") or {}).get("name") or ""
        duration = int((raw.get("dt") or 0) / 1000)
        pic = (raw.get("al") or {}).get("picUrl") or ""
        track = (raw.get("no") or 0)
    else:
        artists = "/".join(a.get("name", "") for a in (raw.get("artists") or []))
        album = (raw.get("album") or {}).get("name") or ""
        duration = int((raw.get("duration") or 0) / 1000)
        pic = (raw.get("album") or {}).get("picUrl") or ""
        track = (raw.get("no") or 0)
    publish = raw.get("publishTime") or 0
    return {
        "id": str(raw.get("id") or ""),
        "name": str(raw.get("name") or ""),
        "artist": artists,
        "album": album,
        "duration": duration,
        "track": int(track or 0),
        "pic_url": pic,
        "date": (time.strftime("%Y-%m-%d", time.localtime(publish / 1000))
                 if publish else ""),
    }


async def search_candidates(cfg: Dict[str, Any], keyword: str, limit: int = 10,
                            platform: str = "wy") -> List[Dict[str, Any]]:
    """搜某个平台的候选。

    platform: wy（网易云，走自建 ncm-api，信息最全）/ tx / kw / kg / mg（各平台公开接口）
    返回统一结构，带 cover 字段供界面显示封面，便于人工确认。
    """
    kw = (keyword or "").strip()
    if not kw:
        return []
    # 非网易云：走公开接口
    if platform and platform != "wy":
        items = await platforms.search_platform(platform, kw, limit)
        for it in items:
            it["source_name"] = platforms.platform_name(platform)
        return items

    ncm = _ncm(cfg)
    try:
        data = await ncm.get("/cloudsearch", keywords=kw, type=1,
                             limit=max(1, min(30, limit)), offset=0)
        songs = ((data or {}).get("result") or {}).get("songs") or []
        if not songs:
            data = await ncm.get("/search", s=kw, type=1,
                                 limit=max(1, min(30, limit)), offset=0)
            songs = ((data or {}).get("result") or {}).get("songs") or []
    except NcmError:
        return []
    finally:
        await ncm.close()

    out = [_song_row(s) for s in songs]
    for x in out:
        x["platform"] = "wy"
        x["source_name"] = "网易云"
    # 网易云搜索结果的封面字段经常是空的 —— 再用 song/detail 批量补上，
    # 否则手动匹配时看不到封面，没法确认是不是想要的那张专辑
    if any(not x.get("cover") for x in out):
        ids = [int(x["id"]) for x in out if str(x.get("id") or "").isdigit()]
        if ids:
            try:
                ncm2 = _ncm(cfg)
                det = await ncm2.song_detail(ids)
                await ncm2.close()
                cmap: Dict[str, str] = {}
                for d in det or []:
                    alb = d.get("al") or d.get("album") or {}
                    pic = str(alb.get("picUrl") or "")
                    if pic:
                        cmap[str(d.get("id"))] = pic
                for x in out:
                    if not x.get("cover") and x["id"] in cmap:
                        x["cover"] = cmap[x["id"]]
            except Exception:  # noqa: BLE001
                pass
    # 统一封面字段（网易云详情里的 picUrl 也拿来用）
    for x in out:
        x["cover"] = x.get("cover") or x.get("pic_url") or ""
    return out


async def apply_candidate(cfg: Dict[str, Any], path: Path, cand: Dict[str, Any],
                          lyrics: Optional[str] = None) -> Dict[str, Any]:
    """把选中的候选写进文件（标签 + 封面 + 歌词）。

    cand 可来自任意平台。非网易云的候选没有歌词接口，会去网易云按
    「歌名 歌手」找一首真有歌词的补上（网易云下架的歌，歌词往往还在）。
    """
    plat = str(cand.get("platform") or "wy")
    cover = await download_image(str(cand.get("cover") or ""))
    lrc = lyrics

    if lrc is None and plat == "wy" and str(cand.get("id") or "").isdigit():
        ncm = _ncm(cfg)
        try:
            lrc = await ncm.lyric(int(cand["id"]))
        except NcmError:
            lrc = None
        finally:
            await ncm.close()

    if lrc is None and plat != "wy":
        try:
            subs = await search_candidates(cfg, f"{cand.get('name')} {cand.get('artist')}", 6, "wy")
            ncm = _ncm(cfg)
            try:
                for c in subs:
                    if str(c.get("id") or "").isdigit():
                        t = await ncm.lyric(int(c["id"]))
                        if t:
                            lrc = t
                            break
            finally:
                await ncm.close()
        except Exception:  # noqa: BLE001
            lrc = None

    artist = str(cand.get("artist") or "")
    meta = {
        "title": str(cand.get("name") or path.stem),
        "artist": artist,
        "album": str(cand.get("album") or ""),
        "album_artist": artist.split("/")[0] if artist else "",
        "track": int(cand.get("track") or 0),
        "disc": 0,
        "date": str(cand.get("date") or ""),
        "sid": str(cand.get("id") or "") if plat == "wy" else "",
    }
    res = save_meta(path, meta, cover=cover or None, lrc=lrc)
    drop_cache_entry(str(path))
    return {**res, "platform": plat, "source_name": platforms.platform_name(plat),
            "got_cover": bool(cover), "got_lyrics": bool(lrc)}


async def fetch_song(cfg: Dict[str, Any], sid: str, with_lyric: bool = True) -> Optional[Dict[str, Any]]:
    """按网易云歌曲 id 取详情（+歌词）；这就是「按 ID 手动匹配」的取数入口"""
    if not str(sid).strip().isdigit():
        return None
    ncm = _ncm(cfg)
    try:
        songs = await ncm.song_detail([int(sid)])
        if not songs:
            return None
        row = _song_row(songs[0])
        if with_lyric:
            row["lyric"] = await ncm.lyric(int(sid)) or ""
        return row
    except NcmError:
        return None
    finally:
        await ncm.close()


# ------------------------------------------------------------------ 刮削
def is_meta_complete(path: Path) -> bool:
    """这首歌的元数据是否已经齐全，不用再刮削就能直接进整理后曲库：

    有 歌名 + 歌手 + 专辑 + 封面 + 歌词，且标签里带着网易云歌曲 id（= 跟歌单/官方对得上）。
    下载器下载时本来就会写入官方 meta + 封面 + 歌词 + netease_id，所以「下载自带的
    就是齐全的官方元数据」时，这里返回 True，可以直接归档，省掉一次手动刮削。
    """
    try:
        if not path.exists():
            return False
        info = inspect(path)
        sid = read_netease_id(path)
        return bool(info["title"] and info["artist"] and info["album"]
                    and info["has_cover"] and info["has_lyrics"]
                    and sid.isdigit())
    except Exception:  # noqa: BLE001  读不动就当不齐全
        return False


def move_into_library(path: Path, cfg: Dict[str, Any]) -> Optional[str]:
    """把待整理目录（download）里的歌移到整理后曲库（musics），保持相对路径不变。

    返回移动后的目标路径；不在 download 目录里 / 移动失败返回 None。
    """
    lib_root = Path(str(cfg.get("library_dir") or "/music/musics")).resolve()
    dl_root = Path(str(cfg.get("download_dir") or "/music/download")).resolve()
    try:
        p_res = path.resolve()
    except OSError:
        p_res = path
    try:
        in_download = p_res.is_relative_to(dl_root)
    except (ValueError, OSError):
        in_download = False
    if not in_download:
        return None
    try:
        rel = p_res.relative_to(dl_root)
    except ValueError:
        rel = Path(path.name)
    dest = lib_root / rel
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.move(str(path), str(dest))
            for side in (".lrc", ".nfo"):
                s = path.with_suffix(side)
                if s.exists():
                    shutil.move(str(s), str(dest.with_suffix(side)))
            # 归档后待整理侧残留的空目录（专辑 / 歌手目录）一并清掉，下次下载会自动重建
            _remove_empty_dirs_upward(path.parent, dl_root)
            return str(dest)
    except OSError:
        pass
    return None


async def scrape_file(cfg: Dict[str, Any], path: Path, song_id: str = "",
                      keyword: str = "", dry_run: bool = False) -> Dict[str, Any]:
    """刮削一个文件：按 ID 或按关键词匹配 → 写标签 + 封面 + 歌词。

    dry_run=True 时只返回匹配结果（供编辑界面「先看后存」），不写文件。
    """
    if not path.exists():
        return {"ok": False, "reason": "文件不存在"}
    info = inspect(path)
    title = info["title"] or path.stem
    artist = info["artist"]

    lrc: Optional[str] = None
    matched: Dict[str, Any] = {}

    if song_id:
        song = await fetch_song(cfg, song_id)
        if not song:
            return {"ok": False, "reason": f"网易云上没有 id={song_id} 这首歌"}
        matched = song
        lrc = song.get("lyric") or None
    else:
        kw = (keyword or f"{title} {artist}").strip()
        cands = await search_candidates(cfg, kw, 10)
        scored = sorted(((score_candidate(c, title, artist), c) for c in cands),
                        key=lambda x: -x[0])
        scored = [(s, c) for s, c in scored if s >= 1.0]     # 分数下限：宁可放弃也不配翻唱
        if not scored:
            return {"ok": False,
                    "reason": "没找到足够匹配的歌曲（为避免配到翻唱，已放弃；可用「按 ID 匹配」手动指定）"}
        matched = dict(scored[0][1])
        # 歌词：按分数逐个候选试，取第一个真有歌词的
        ncm = _ncm(cfg)
        try:
            for _score, cand in scored[:5]:
                try:
                    text = await ncm.lyric(int(cand["id"]))
                except (NcmError, ValueError):
                    continue
                if text:
                    lrc = text
                    break
        finally:
            await ncm.close()
        if not lrc:
            song = await fetch_song(cfg, matched["id"])
            if song:
                lrc = song.get("lyric") or None
                for k, v in song.items():
                    if k != "lyric" and not matched.get(k):
                        matched[k] = v

    cover: Optional[bytes] = None
    if matched.get("pic_url"):
        cover = await download_image(matched["pic_url"])

    meta = {
        "title": matched.get("name") or title,
        "artist": matched.get("artist") or artist,
        "album": matched.get("album") or info["album"],
        "album_artist": matched.get("artist") or info["album_artist"] or artist,
        "track": matched.get("track") or info["track"],
        "disc": info["disc"],
        "date": matched.get("date") or info["date"],
        "sid": str(matched.get("id") or song_id or ""),
    }
    res = {"ok": True,
           "matched": {"id": matched.get("id"), "name": matched.get("name"),
                       "artist": matched.get("artist"), "album": matched.get("album")},
           "cover": bool(cover), "lyrics": bool(lrc)}
    if dry_run:
        res["preview"] = {
            **meta, "lyrics": lrc or "",
            "cover_data": (("data:image/jpeg;base64," + base64.b64encode(cover).decode())
                           if cover else ""),
        }
        return res
    written = save_meta(path, meta, cover=cover, lrc=lrc)
    if not written.get("ok"):
        return {"ok": False,
                "reason": written.get("error") or written.get("warning") or "写入失败"}
    # 刮削完成 → 把 download 目录的文件移进 musics（整理后曲库）
    dest = move_into_library(path, cfg)
    if dest:
        res["moved_to"] = dest
    return res


# ------------------------------------------------------------------ 回收站
def _trash_batch(path: Path) -> Path:
    """一条回收站文件所属的批次目录（/data/_trash/<时间戳>）"""
    try:
        return _trash_dir() / path.relative_to(_trash_dir()).parts[0]
    except (ValueError, IndexError):
        return path.parent


def _trash_tidy() -> None:
    """清掉空的批次目录"""
    if not _trash_dir().exists():
        return
    for batch in list(_trash_dir().iterdir()):
        if batch.is_dir() and not any(batch.rglob("*")):
            try:
                batch.rmdir()
            except OSError:
                pass


def _remove_empty_dirs_upward(start: Path, stop: Path) -> None:
    """从 start 向上逐层删掉空目录，直到 stop（stop 本身不删）。

    删除 / 移走歌曲后，残留的空「专辑目录 / 歌手目录」由这里连带清掉；
    这些目录在下次下载或整理时会被 mkdir(parents=True) 自动重建，
    不会出现找不到目录的情况。
    """
    try:
        stop_res = stop.resolve()
    except OSError:
        return
    cur = Path(start)
    for _ in range(32):                        # 最多向上 32 层，防死循环
        try:
            cur_res = cur.resolve()
        except OSError:
            return
        if cur_res == stop_res:
            return
        try:
            if not cur_res.is_relative_to(stop_res):
                return
        except ValueError:
            return
        try:
            if not cur_res.is_dir() or any(cur_res.iterdir()):
                return
        except OSError:
            return
        try:
            cur_res.rmdir()
        except OSError:
            return
        cur = cur_res.parent


def trash_items() -> List[Dict[str, Any]]:
    """回收站里的歌（歌词 / NFO 跟着主文件，不单列），新的批次排前面"""
    out: List[Dict[str, Any]] = []
    if not _trash_dir().exists():
        return out
    for batch in sorted((d for d in _trash_dir().iterdir() if d.is_dir()),
                        key=lambda d: d.name, reverse=True):
        for f in sorted(batch.rglob("*")):
            if not f.is_file() or f.suffix.lower() in (".lrc", ".nfo"):
                continue
            rel = f.relative_to(batch)
            try:
                size = f.stat().st_size
            except OSError:
                continue
            # 批次目录名是 20260922-232251 → 显示成 2026-09-22 23:22
            stamp = batch.name
            if len(stamp) >= 13 and stamp[8] == "-":
                stamp = f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}"
            item: Dict[str, Any] = {
                "path": str(f), "rel": str(rel), "batch": batch.name,
                "deleted_at": stamp, "size": size, "ext": f.suffix.lower(),
                "title": f.stem, "artist": "", "album": "",
            }
            try:                                    # 读得出标签就用标签里的名字
                info = inspect(f)
                item["title"] = info.get("title") or f.stem
                item["artist"] = info.get("artist") or ""
                item["album"] = info.get("album") or ""
                item["has_cover"] = bool(info.get("has_cover"))
            except Exception:  # noqa: BLE001  读不出来就用文件名，不算错
                pass
            out.append(item)
    return out


def trash_summary(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"count": len(items), "size": sum(int(x.get("size") or 0) for x in items),
            "batches": len({x["batch"] for x in items})}


def from_trash(paths: List[str], root: str) -> Dict[str, Any]:
    """把回收站里的歌恢复到音乐库（放回原来的相对位置），歌词 / NFO 一起回去"""
    restored: List[str] = []
    failed: List[Dict[str, str]] = []
    for raw in paths:
        src = Path(raw)
        if not src.exists() or not src.is_file():
            failed.append({"path": raw, "error": "文件不存在"})
            continue
        batch = _trash_batch(src)
        try:
            rel = src.relative_to(batch)
        except ValueError:
            rel = Path(src.name)
        dest = Path(root) / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():       # 库里已有同名文件 → 加个后缀，别覆盖
                dest = dest.with_name(f"{dest.stem} (回收站恢复){dest.suffix}")
            shutil.move(str(src), str(dest))
            for side in (".lrc", ".nfo"):
                s = src.with_suffix(side)
                if s.exists():
                    shutil.move(str(s), str(dest.with_suffix(side)))
            # 专辑级附属（封面 / 专辑 NFO）一起恢复
            for side_name in ("cover.jpg", "cover.png", "album.nfo"):
                s = src.parent / side_name
                if s.exists():
                    shutil.move(str(s), str(dest.parent / side_name))
            restored.append(str(dest))
        except Exception as e:  # noqa: BLE001
            failed.append({"path": raw, "error": f"{type(e).__name__}: {e}"})
    _trash_tidy()
    return {"restored": restored, "failed": failed, "count": len(restored)}


def purge_trash(paths: List[str]) -> Dict[str, Any]:
    """彻底删除回收站里的文件（**真实删除本地文件**，删了就找不回来了）"""
    killed: List[str] = []
    failed: List[Dict[str, str]] = []
    for raw in paths:
        src = Path(raw)
        if not src.exists():
            failed.append({"path": raw, "error": "文件不存在"})
            continue
        try:
            if src.is_file():
                src.unlink()
            else:
                shutil.rmtree(src)
            for side in (".lrc", ".nfo"):
                s = src.with_suffix(side)
                if s.exists():
                    s.unlink()
            # 专辑级附属（封面 / 专辑 NFO）：回收站里这个目录没别的音频了才一并删
            parent = src.parent
            if not any(f.is_file() and f.suffix.lower() in AUDIO_EXT
                       for f in parent.iterdir()):
                for side_name in ("cover.jpg", "cover.png", "album.nfo"):
                    s = parent / side_name
                    if s.exists():
                        s.unlink()
            # 清掉回收站里残留的空目录，只保留到本批次目录这一层（批次由 _trash_tidy 收尾）
            _remove_empty_dirs_upward(parent, _trash_batch(src))
            killed.append(raw)
        except Exception as e:  # noqa: BLE001
            failed.append({"path": raw, "error": f"{type(e).__name__}: {e}"})
    _trash_tidy()
    return {"purged": killed, "failed": failed, "count": len(killed)}


def to_trash(paths: List[str], root: str) -> Dict[str, Any]:
    """移入回收站（/data/_trash/时间戳/原相对路径），可找回，不是彻底删除"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest_root = _trash_dir() / stamp
    moved: List[str] = []
    failed: List[Dict[str, str]] = []
    for raw in paths:
        src = Path(raw)
        if not src.exists():
            failed.append({"path": raw, "error": "文件不存在"})
            continue
        try:
            rel = src.relative_to(root)
        except ValueError:
            rel = Path(src.name)
        dest = dest_root / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            moved.append(str(rel))
            # 同名歌词 / NFO 一起带走，避免留下孤儿文件
            for side_suffix in (".lrc", ".nfo"):
                side = src.with_suffix(side_suffix)
                if side.exists():
                    shutil.move(str(side), str(dest.with_suffix(side_suffix)))
            # 专辑级附属（封面 / 专辑 NFO）：只有这张专辑目录里没别的音频文件了才带走
            # （删的是这张专辑的最后一首歌，封面/专辑 NFO 就是它的；还有别的歌就别动）
            parent = src.parent
            if not any(f.is_file() and f.suffix.lower() in AUDIO_EXT
                       for f in parent.iterdir()):
                for side_name in ("cover.jpg", "cover.png", "album.nfo"):
                    s = parent / side_name
                    if s.exists():
                        shutil.move(str(s), str(dest.parent / side_name))
            # 清掉被移走后残留的空目录：专辑目录空了就连它一起删，歌手目录也空了
            # 就继续往上删，只保留到它所在的 download/musics 这一层根目录。
            # 这些目录下次下载/整理时会 mkdir(parents=True) 自动重建。
            rel_parts = rel.parts
            stop = (Path(root) / rel_parts[0]) if len(rel_parts) >= 2 else parent
            _remove_empty_dirs_upward(parent, stop)
        except Exception as e:  # noqa: BLE001
            failed.append({"path": raw, "error": f"{type(e).__name__}: {e}"})
    return {"moved": moved, "failed": failed, "trash": str(dest_root),
            "count": len(moved)}
