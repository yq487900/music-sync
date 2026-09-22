"""多平台歌曲搜索（刮削来源扩展）。

网易云走自建 ncm-api（信息最全、有歌词）；其余平台用各自的公开搜索接口：
  tx = QQ音乐   kg = 酷狗   kw = 酷我   mg = 咪咕
用途：网易云搜不到的（下架/无版权）歌曲，用别的平台的信息补齐标签 / 封面。

统一返回：[{platform, id, name, artist, album, duration, cover, extra}]
  * cover 是可直接访问的封面地址（可能为空）
  * id 是该平台的歌曲标识（tx=songmid, kg=FileHash, kw=rid, mg=copyrightId）
"""

from __future__ import annotations

import json
import re
import ssl
from typing import Any, Dict, List

import aiohttp

# 平台代号 -> 中文名（顺序即界面下拉框顺序）
PLATFORMS: Dict[str, str] = {
    "wy": "网易云",
    "tx": "QQ音乐",
    "kw": "酷我",
    "kg": "酷狗",
    "mg": "咪咕",
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
_TIMEOUT = aiohttp.ClientTimeout(total=18)


def _session() -> aiohttp.ClientSession:
    """部分平台证书链有问题（酷狗 CDN），统一放宽校验并设超时"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return aiohttp.ClientSession(timeout=_TIMEOUT, connector=aiohttp.TCPConnector(ssl=ctx))


def _clean(s: Any) -> str:
    """去掉 HTML 实体与多余空白"""
    t = str(s or "")
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
          .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'"))
    return re.sub(r"\s+", " ", t).strip()


async def _get_json(s: aiohttp.ClientSession, url: str, headers: Dict[str, str]) -> Any:
    async with s.get(url, headers=headers) as r:
        raw = await r.text()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 酷我返回的是单引号 JSON
        return json.loads(raw.replace("'", '"'))


def _rows(raw_list: Any, build) -> List[Dict[str, Any]]:
    """逐条构造候选：单条解析失败只跳过这一条，不影响整份结果"""
    out: List[Dict[str, Any]] = []
    for it in raw_list or []:
        try:
            row = build(it)
        except Exception:  # noqa: BLE001
            continue
        if row and row.get("name"):
            out.append(row)
    return out


def _dur(v: Any) -> int:
    """时长统一成秒：可能是 319 / 319000 / '319' / '03:19'"""
    s = str(v or "").strip()
    if ":" in s:
        parts = [p for p in re.split(r"[:.]", s) if p.isdigit()]
        if len(parts) >= 2:
            return int(parts[0]) * 60 + int(parts[1])
    n = re.sub(r"\D", "", s)
    if not n:
        return 0
    n = int(n)
    return n // 1000 if n > 10000 else n


# ------------------------------------------------------------------ QQ音乐
async def search_tx(s: aiohttp.ClientSession, kw: str, limit: int) -> List[Dict[str, Any]]:
    url = ("https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
           f"?w={kw}&p=1&n={limit}&format=json&cr=1&aggr=1&lossless=0&new_json=1")
    d = await _get_json(s, url, {"User-Agent": UA, "Referer": "https://y.qq.com/"})
    songs = (((d or {}).get("data") or {}).get("song") or {}).get("list") or []

    def build(it: Dict[str, Any]) -> Dict[str, Any]:
        mid = str(it.get("mid") or it.get("songmid") or "")
        album = it.get("album") or {}
        singers = it.get("singer") or []
        return {
            "platform": "tx", "id": mid,
            "name": _clean(it.get("title") or it.get("songname")),
            "artist": "/".join(_clean(x.get("name")) for x in singers if x.get("name")),
            "album": _clean(album.get("title") or album.get("name")),
            "duration": _dur(it.get("interval")),
            "cover": (f"https://y.qq.com/music/photo_new/T002R300x300M000{album.get('mid')}.jpg"
                      if album.get("mid") else ""),
        }

    return _rows(songs, build)


# ------------------------------------------------------------------ 酷狗
async def search_kg(s: aiohttp.ClientSession, kw: str, limit: int) -> List[Dict[str, Any]]:
    url = ("https://songsearch.kugou.com/song_search_v2"
           f"?keyword={kw}&page=1&pagesize={limit}&platform=WebFilter&filter=2"
           "&iscorrection=1&privilege_filter=0")
    d = await _get_json(s, url, {"User-Agent": UA, "Referer": "https://www.kugou.com/"})
    lists = ((d or {}).get("data") or {}).get("lists") or []

    def build(it: Dict[str, Any]) -> Dict[str, Any]:
        img = str(it.get("Image") or it.get("image") or "")
        return {
            "platform": "kg", "id": str(it.get("FileHash") or it.get("EMixSongID") or ""),
            "name": _clean(it.get("SongName")),
            "artist": _clean(it.get("SingerName")),
            "album": _clean(it.get("AlbumName")),
            "duration": _dur(it.get("Duration")),
            "cover": img.replace("{size}", "240") if img else "",
        }

    return _rows(lists, build)


# ------------------------------------------------------------------ 酷我
async def search_kw(s: aiohttp.ClientSession, kw: str, limit: int) -> List[Dict[str, Any]]:
    url = ("http://search.kuwo.cn/r.s"
           f"?all={kw}&ft=music&itemset=web_2013&client=kt&pn=0&rn={limit}"
           "&rformat=json&encoding=utf8")
    d = await _get_json(s, url, {"User-Agent": UA, "Referer": "http://www.kuwo.cn/"})
    lst = (d or {}).get("abslist") or []

    def build(it: Dict[str, Any]) -> Dict[str, Any]:
        pic = _clean(it.get("web_albumpic_short") or it.get("hts_MVPIC") or "")
        rid = _clean(it.get("MUSICRID")).replace("MUSIC_", "")
        return {
            "platform": "kw", "id": rid,
            "name": _clean(it.get("SONGNAME")),
            "artist": _clean(it.get("ARTIST")).replace("\\u0026", "&").replace("\\\\u0026", "&"),
            "album": _clean(it.get("ALBUM")),
            "duration": _dur(it.get("DURATION")),
            "cover": (f"https://img1.kuwo.cn/star/albumcover/{pic.lstrip('/')}"
                      if pic else ""),
        }

    return _rows(lst, build)


# ------------------------------------------------------------------ 咪咕
async def search_mg(s: aiohttp.ClientSession, kw: str, limit: int) -> List[Dict[str, Any]]:
    url = ("https://app.c.nf.migu.cn/MIGUM2.0/v1.0/content/search_all.do"
           f"?text={kw}&pageNo=1&pageSize={limit}&isCopyright=1&sort=1"
           "&searchSwitch=%7B%22song%22%3A1%7D")
    d = await _get_json(s, url, {"User-Agent": UA, "Referer": "https://m.music.migu.cn/"})
    srd = (d or {}).get("songResultData") or {}
    lst = srd.get("result") or srd.get("resultList") or []

    def build(it: Dict[str, Any]) -> Dict[str, Any]:
        singers = it.get("singerList") or []
        cover = ""
        for im in (it.get("imgItems") or []):
            if im.get("img"):
                cover = str(im["img"]).replace("http://", "https://")
                break
        albums = it.get("albums") or []
        album = ""
        if isinstance(albums, list) and albums:
            album = _clean((albums[0] or {}).get("name"))
        elif isinstance(albums, dict):
            album = _clean(albums.get("name"))
        return {
            "platform": "mg", "id": str(it.get("copyrightId") or it.get("id") or ""),
            "name": _clean(it.get("name") or it.get("songName")),
            "artist": "/".join(_clean(x.get("name")) for x in singers if x.get("name"))
                      or _clean(it.get("singer")),
            "album": album,
            "duration": _dur(it.get("duration")),
            "cover": cover,
        }

    return _rows(lst, build)


_SEARCHERS = {"tx": search_tx, "kg": search_kg, "kw": search_kw, "mg": search_mg}


async def search_platform(platform: str, keyword: str, limit: int = 12) -> List[Dict[str, Any]]:
    """搜单个平台（wy 由调用方走 ncm-api）。失败返回空列表，不抛异常。"""
    fn = _SEARCHERS.get(platform)
    if fn is None or not keyword.strip():
        return []
    try:
        async with _session() as s:
            return await fn(s, keyword.strip(), max(1, min(int(limit), 30)))
    except Exception:  # noqa: BLE001
        return []


def platform_name(code: str) -> str:
    return PLATFORMS.get(code, code or "")
