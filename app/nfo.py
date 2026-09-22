"""NFO 生成（Emby / Jellyfin / Kodi 识别用）"""
from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape


def _song_xml(meta: dict, duration: int = 0) -> str:
    artist = escape(str(meta.get("artist") or ""))
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        "<song>",
        f"  <title>{escape(str(meta.get('title') or ''))}</title>",
        f"  <artist>{artist}</artist>",
        f"  <album>{escape(str(meta.get('album') or ''))}</album>",
        f"  <albumartist>{escape(str(meta.get('album_artist') or meta.get('artist') or ''))}</albumartist>",
    ]
    if int(meta.get("track") or 0):
        parts.append(f"  <track>{int(meta['track'])}</track>")
    if int(meta.get("disc") or 0):
        parts.append(f"  <disc>{int(meta['disc'])}</disc>")
    if duration:
        parts.append(f"  <duration>{int(duration)}</duration>")
    if meta.get("date"):
        parts.append(f"  <year>{escape(str(meta['date']))}</year>")
    if meta.get("genre"):
        parts.append(f"  <genre>{escape(str(meta['genre']))}</genre>")
    if meta.get("sid"):
        parts.append(f"  <neteaseid>{escape(str(meta['sid']))}</neteaseid>")
    parts.append("</song>")
    return "\n".join(parts) + "\n"


def write_song_nfo(path: Path, meta: dict, duration: int = 0) -> None:
    """单曲同名 .nfo：<歌名>.nfo"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_song_xml(meta, duration), encoding="utf-8")


def write_album_nfo(path: Path, meta: dict) -> None:
    """专辑目录下的 album.nfo"""
    artists = [a for a in str(meta.get("artist") or "").split(" / ") if a]
    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        "<album>",
        f"  <title>{escape(str(meta.get('album') or '未知专辑'))}</title>",
        f"  <albumartist>{escape(str(meta.get('album_artist') or meta.get('artist') or ''))}</albumartist>",
        f"  <artist>{escape(str(meta.get('artist') or ''))}</artist>",
    ]
    for a in artists:
        lines.append(f"  <artist>{escape(a)}</artist>")
    if meta.get("date"):
        lines.append(f"  <year>{escape(str(meta['date']))}</year>")
    if meta.get("genre"):
        lines.append(f"  <genre>{escape(str(meta['genre']))}</genre>")
    if meta.get("label"):
        lines.append(f"  <label>{escape(str(meta['label']))}</label>")
    lines.append("</album>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
