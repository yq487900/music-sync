"""网易云云盘：列表 / 配额 / 上传 / 匹配元数据 / 删除（全部经 ncm-api）。"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict

import aiohttp

from app.ncm import NCM_BASE, NcmError


async def _get(path: str, cookie: str, timeout: int = 60, **params: Any) -> Dict[str, Any]:
    params.setdefault("timestamp", int(time.time() * 1000))
    params["cookie"] = cookie
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.get(f"{NCM_BASE}{path}", params=params) as r:
                data = await r.json(content_type=None)
    except Exception as e:  # noqa: BLE001
        raise NcmError(f"云盘接口不可用：{type(e).__name__}") from e
    return data if isinstance(data, dict) else {}


async def list_songs(cookie: str, limit: int = 20, offset: int = 0) -> Dict[str, Any]:
    """云盘歌曲列表；返回 {data:[{simpleSong, fileSize, bitrate, ...}], count, size, maxSize}"""
    return await _get("/user/cloud", cookie, limit=max(1, int(limit)), offset=max(0, int(offset)))


async def quota(cookie: str) -> Dict[str, Any]:
    """云盘容量：已用 size / 上限 maxSize，歌曲数 count"""
    data = await _get("/user/cloud", cookie, limit=1, offset=0)
    return {
        "count": int(data.get("count") or 0),
        "size": int(data.get("size") or 0),
        "max_size": int(data.get("maxSize") or 0),
    }


async def upload(cookie: str, path: Path) -> Dict[str, Any]:
    """上传本地音频文件到云盘（大文件，超时放宽）"""
    path = Path(path)
    if not path.exists():
        raise NcmError("本地文件不存在")
    params = {"cookie": cookie, "timestamp": int(time.time() * 1000)}
    timeout = aiohttp.ClientTimeout(total=3600, sock_connect=30, sock_read=600)
    with open(path, "rb") as fh:
        form = aiohttp.FormData()
        form.add_field("songFile", fh, filename=path.name,
                       content_type="application/octet-stream")
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(f"{NCM_BASE}/cloud", params=params, data=form) as r:
                    data = await r.json(content_type=None)
        except Exception as e:  # noqa: BLE001
            raise NcmError(f"上传请求失败：{type(e).__name__}") from e
    if not isinstance(data, dict):
        raise NcmError("上传返回异常")
    return data


# 上传成功可能返回 200 或 201
UPLOAD_OK_CODES = (200, 201)


def uploaded_song_id(data: Dict[str, Any]) -> str:
    """取云盘歌曲 id（数字 id，用于 song/detail、cloud/match、cloud/del）

    注意：顶层 songId 是内容哈希，真正的云盘歌曲 id 在 privateCloud.simpleSong.id。
    """
    pc = data.get("privateCloud") or {}
    if isinstance(pc, dict):
        ss = pc.get("simpleSong") or pc.get("song") or {}
        if isinstance(ss, dict) and ss.get("id"):
            return str(ss["id"])
        for key in ("songId", "songid", "id"):
            v = pc.get(key)
            if v and str(v).isdigit():
                return str(v)
    for key in ("songId", "songid"):
        v = data.get(key)
        if v and str(v).isdigit():          # 只有纯数字才算歌曲 id
            return str(v)
    return ""


def upload_ok(data: Dict[str, Any]) -> bool:
    code = data.get("code")
    if code in UPLOAD_OK_CODES:
        return True
    return False


async def match(cookie: str, uid: str, sid: str, asid: str) -> Dict[str, Any]:
    """云盘歌曲匹配：把云盘条目(sid) 绑定到网易云正式曲目(asid)。

    绑定后这首歌就会使用正式曲目的元数据 / 封面 / 歌词 —— 也是「纠错」的手段：
    网易云自动匹配错了，就手动指定正确的歌曲 id 重新匹配。
    """
    return await _get("/cloud/match", cookie, uid=uid, sid=sid, asid=asid)


async def delete(cookie: str, sid: str) -> Dict[str, Any]:
    return await _get("/user/cloud/del", cookie, id=sid)


def simplify(item: Dict[str, Any]) -> Dict[str, Any]:
    """把云盘列表项整理成界面需要的字段"""
    simple = item.get("simpleSong") or item.get("song") or {}
    artists = [a.get("name") for a in (simple.get("ar") or simple.get("artists") or []) if a.get("name")]
    al = simple.get("al") or simple.get("album") or {}
    pc = item.get("privateCloud") or {}
    # 云盘条目的封面只有两个来源：① 匹配到正式曲目（al.id 非 0）→ 官方封面；
    # ② 网易云自己存过封面（coverId 非 0）。都没有时它给的就是那张占位图。
    cover_id = str(pc.get("coverId") or pc.get("cover") or "0")
    al_id = int(al.get("id") or 0)
    has_cover = bool(al_id) or cover_id not in ("0", "", "None")
    return {
        "sid": str(simple.get("id") or item.get("songId") or ""),
        "title": str(simple.get("name") or item.get("songName") or "未知曲目"),
        "artist": " / ".join(artists) or str(item.get("artist") or "未知歌手"),
        "album": str(al.get("name") or item.get("album") or ""),
        "cover": str(al.get("picUrl") or ""),
        "has_cover": has_cover,
        # 匹配到正式曲目时 sid 就是网易云歌曲 id（能直接和歌单里的 id 对上）；
        # 没匹配上时 sid 只是云盘条目自己的 id，只能靠标题/歌手认。
        "matched": bool(al_id),
        "al_id": al_id,
        "match_type": str(pc.get("matchType") or ""),
        "duration": int((simple.get("dt") or 0) // 1000),
        "file_size": int(item.get("fileSize") or 0),
        "bitrate": int(item.get("bitrate") or 0),
    }
