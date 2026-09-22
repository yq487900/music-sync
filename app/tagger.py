"""文件命名与音频标签写入（mutagen）：flac / mp3 / m4a。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from mutagen.flac import FLAC, Picture
from mutagen.id3 import (APIC, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TPUB,
                         TRCK, TXXX, USLT, ID3, ID3NoHeaderError)
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_SPACES = re.compile(r"\s+")

LAYOUT_LABELS = {
    "album": "按专辑分类（歌手/专辑/序号. 歌名）",
    "artist": "按歌手分类（歌手/序号. 歌名）",
    "playlist": "按歌单分类（歌单名/序号. 歌手 - 歌名）",
    "flat": "全部平铺（序号. 歌手 - 歌名）",
}


def sanitize(name: str, maxlen: int = 120) -> str:
    s = _ILLEGAL.sub(" ", str(name or ""))
    s = _SPACES.sub(" ", s).strip().strip(".")
    return (s[:maxlen].strip()) or "未知"


def _format(template: str, **vars: object) -> str:
    class _D(dict):
        def __missing__(self, key: str) -> str:
            return ""
    try:
        return sanitize(template.format_map(_D(vars)))
    except (ValueError, KeyError, IndexError):
        return sanitize(str(vars.get("title") or "未知"))


def default_naming(layout: str) -> str:
    return "{pos:02d}. {artist} - {title}" if layout == "playlist" else "{track:02d}. {title}"


def song_relative_path(meta: dict, layout: str, naming: str = "") -> Path:
    """返回相对音乐库根目录的路径（含文件名，不含扩展名）"""
    artist = sanitize(meta.get("artist") or "未知歌手")
    album = sanitize(meta.get("album") or "未知专辑")
    track = int(meta.get("track") or 0)
    pos = int(meta.get("pos") or 0)
    template = naming or default_naming(layout)
    # 没有音轨号/序号时，去掉形如 "{track:02d}. " 的前缀，避免出现「00. 歌名」
    if not track and "{track" in template:
        template = re.sub(r"\{track[^}]*\}\s*[.\-]?\s*", "", template, count=1) or "{title}"
    if not pos and "{pos" in template:
        template = re.sub(r"\{pos[^}]*\}\s*[.\-]?\s*", "", template, count=1) or "{title}"
    fname = _format(template, track=track, pos=pos,
                    title=meta.get("title") or "未知标题", artist=artist, album=album)
    if layout == "artist":
        return Path(artist) / fname
    if layout == "flat":
        return Path(fname)
    if layout == "playlist":
        return Path(sanitize(meta.get("playlist") or "未命名歌单")) / fname
    return Path(artist) / album / fname


def _cover_mime(data: bytes) -> str:
    return "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"


def read_track_number(path: Path) -> tuple:
    """读取文件里自带的音轨号 / 碟号（网易云接口没给编号时用来兜底）

    返回 (track, disc)，读不到返回 (0, 0)。
    """
    try:
        from mutagen import File as MutagenFile

        f = MutagenFile(str(path), easy=True)
        if f is None:
            return 0, 0

        def _num(key: str) -> int:
            raw = (f.get(key) or [""])[0] if f.get(key) else ""
            txt = str(raw).split("/")[0].strip()
            return int(txt) if txt.isdigit() else 0

        return _num("tracknumber"), _num("discnumber")
    except Exception:  # noqa: BLE001  读不到就算了，不影响下载
        return 0, 0


def tag_file(path: Path, meta: dict, cover: Optional[bytes] = None,
             lrc: Optional[str] = None) -> Optional[str]:
    """写标签；成功返回 None，失败返回原因（不影响下载成功的判定）"""
    ext = path.suffix.lower()
    try:
        if ext == ".flac":
            _tag_flac(path, meta, cover, lrc)
        elif ext == ".mp3":
            _tag_mp3(path, meta, cover, lrc)
        elif ext in (".m4a", ".mp4"):
            _tag_mp4(path, meta, cover, lrc)
        return None
    except Exception as e:  # noqa: BLE001
        return f"标签写入失败：{type(e).__name__}"


def _tag_flac(path: Path, meta: dict, cover: Optional[bytes], lrc: Optional[str]) -> None:
    f = FLAC(str(path))
    f["title"] = str(meta.get("title") or "")
    f["artist"] = str(meta.get("artist") or "")
    f["album"] = str(meta.get("album") or "")
    if meta.get("album_artist"):
        f["albumartist"] = str(meta["album_artist"])
    if meta.get("genre"):
        f["genre"] = str(meta["genre"])
    if meta.get("label"):
        f["publisher"] = str(meta["label"])
    if meta.get("sid"):
        f["netease_id"] = str(meta["sid"])
    if int(meta.get("track") or 0):
        f["tracknumber"] = str(int(meta["track"]))
    if int(meta.get("disc") or 0):
        f["discnumber"] = str(int(meta["disc"]))
    if meta.get("date"):
        f["date"] = str(meta["date"])
    if lrc:
        f["lyrics"] = lrc
    if cover:
        pic = Picture()
        pic.type = 3
        pic.mime = _cover_mime(cover)
        pic.desc = "Cover"
        pic.data = cover
        f.clear_pictures()
        f.add_picture(pic)
    f.save()


def _tag_mp3(path: Path, meta: dict, cover: Optional[bytes], lrc: Optional[str]) -> None:
    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        tags = ID3()
    tags.add(TIT2(encoding=3, text=str(meta.get("title") or "")))
    tags.add(TPE1(encoding=3, text=str(meta.get("artist") or "")))
    tags.add(TALB(encoding=3, text=str(meta.get("album") or "")))
    if meta.get("album_artist"):
        tags.add(TPE2(encoding=3, text=str(meta["album_artist"])))
    if meta.get("genre"):
        tags.add(TCON(encoding=3, text=str(meta["genre"])))
    if meta.get("label"):
        tags.add(TPUB(encoding=3, text=str(meta["label"])))
    if meta.get("sid"):
        tags.add(TXXX(encoding=3, desc="NETEASE_ID", text=str(meta["sid"])))
    if int(meta.get("track") or 0):
        tags.add(TRCK(encoding=3, text=str(int(meta["track"]))))
    if int(meta.get("disc") or 0):
        tags.add(TPOS(encoding=3, text=str(int(meta["disc"]))))
    if meta.get("date"):
        tags.add(TDRC(encoding=3, text=str(meta["date"])))
    if lrc:
        tags.add(USLT(encoding=3, lang="chi", desc="", text=lrc))
    if cover:
        tags.add(APIC(encoding=3, mime=_cover_mime(cover), type=3, desc="Cover", data=cover))
    tags.save(str(path), v2_version=3)


def _tag_mp4(path: Path, meta: dict, cover: Optional[bytes], lrc: Optional[str]) -> None:
    m = MP4(str(path))
    m["\xa9nam"] = [str(meta.get("title") or "")]
    m["\xa9ART"] = [str(meta.get("artist") or "")]
    m["aART"] = [str(meta.get("album_artist") or meta.get("artist") or "")]
    m["\xa9alb"] = [str(meta.get("album") or "")]
    if meta.get("genre"):
        m["\xa9gen"] = [str(meta["genre"])]
    if meta.get("sid"):
        m["----:com.apple.iTunes:NETEASE_ID"] = [MP4FreeForm(str(meta["sid"]).encode())]
    if int(meta.get("track") or 0):
        m["trkn"] = [(int(meta["track"]), 0)]
    if meta.get("date"):
        m["\xa9day"] = [str(meta["date"])]
    if lrc:
        m["\xa9lyr"] = [lrc]
    if cover:
        kind = MP4Cover.FORMAT_PNG if _cover_mime(cover) == "image/png" else MP4Cover.FORMAT_JPEG
        m["covr"] = [MP4Cover(cover, imageformat=kind)]
    m.save()
