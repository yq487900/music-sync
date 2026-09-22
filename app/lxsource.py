"""洛雪(LX)兼容音源脚本管理。

脚本本身由容器内的 Node 沙箱执行（sources/runner.js），这里只负责：
  * 保存/启用/停用音源，从 URL 拉取脚本内容
  * 调用沙箱做「检测」
  * 取链时按启用顺序依次尝试，拿到可用直链即返回
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Any, Dict, List, Optional

import aiohttp

LX_URL = os.environ.get("LX_URL", "http://127.0.0.1:3100").rstrip("/")

# 洛雪音质从高到低
LX_QUALITY_ORDER = ["flac24bit", "flac", "320k", "192k", "128k"]
# 网易云歌曲在洛雪里的平台代号
LX_PLATFORM = "wy"


class LxError(Exception):
    pass


def _kb(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


class LxRunner:
    """与 Node 沙箱通信的薄客户端"""

    @staticmethod
    async def _post(path: str, payload: Dict[str, Any], timeout: int = 60) -> Dict[str, Any]:
        url = f"{LX_URL}{path}"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
                async with s.post(url, json=payload) as r:
                    data = await r.json(content_type=None)
        except Exception as e:  # noqa: BLE001
            raise LxError(f"音源沙箱不可用：{type(e).__name__}") from e
        return data if isinstance(data, dict) else {}

    @staticmethod
    async def ready() -> bool:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
                async with s.get(f"{LX_URL}/health") as r:
                    return r.status == 200
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    async def check(script: str) -> Dict[str, Any]:
        """加载脚本，返回平台/音质等信息"""
        return await LxRunner._post("/check", {"script": script})

    @staticmethod
    async def get_url(script: str, platform: str, quality: str,
                      music_info: Dict[str, Any]) -> Optional[str]:
        data = await LxRunner._post("/url", {
            "script": script, "platform": platform,
            "quality": quality, "musicInfo": music_info,
        }, timeout=45)
        if data.get("ok") and data.get("url"):
            return str(data["url"])
        return None


# ---------------------------------------------------------------- 音源仓库
def list_sources(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(cfg.get("music_sources") or [])


def enabled_sources(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for s in list_sources(cfg):
        if s.get("enabled") and (s.get("script") or "").strip():
            out.append(s)
    return out


def find_source(cfg: Dict[str, Any], sid: str) -> Optional[Dict[str, Any]]:
    for s in list_sources(cfg):
        if str(s.get("id")) == str(sid):
            return s
    return None


def add_source(cfg: Dict[str, Any], name: str, script: str,
               url: str = "") -> Dict[str, Any]:
    entry = {
        "id": uuid.uuid4().hex[:12],
        "name": name or "未命名音源",
        "url": url,
        "script": script,
        "enabled": True,
        "ok": False,
        "note": "尚未检测",
        "platforms": [],
        "qualities": [],
        "checked_at": "",
    }
    cfg.setdefault("music_sources", []).append(entry)
    return entry


def remove_source(cfg: Dict[str, Any], sid: str) -> bool:
    src = cfg.get("music_sources") or []
    left = [s for s in src if str(s.get("id")) != str(sid)]
    if len(left) == len(src):
        return False
    cfg["music_sources"] = left
    return True


async def fetch_script(url: str) -> str:
    """从 URL 拉取音源脚本内容"""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.get(url, allow_redirects=True) as r:
                if r.status != 200:
                    raise LxError(f"下载脚本失败：HTTP {r.status}")
                text = await r.text()
    except LxError:
        raise
    except Exception as e:  # noqa: BLE001
        raise LxError(f"下载脚本失败：{type(e).__name__}") from e
    if not text.strip():
        raise LxError("脚本内容为空")
    return text


def pick_quality(available: List[str], want: str = "highest") -> str:
    """按洛雪音质顺序挑一个可用档位"""
    if not available:
        return "320k" if want == "highest" else want
    if want == "highest":
        for q in LX_QUALITY_ORDER:
            if q in available:
                return q
        return available[-1]
    if want in available:
        return want
    for q in LX_QUALITY_ORDER:
        if q in available:
            return q
    return available[-1]


def build_music_info(track: Any) -> Dict[str, Any]:
    """把库里的曲目转成洛雪脚本习惯的 musicInfo 结构"""
    return {
        "id": int(track.platform_track_id or 0),
        "songmid": str(track.platform_track_id or ""),
        "name": track.title or "",
        "singer": track.artist or "",
        "albumName": track.album or "",
        "albumId": track.album_id or 0,
        "interval": int(track.duration or 0),
        "source": LX_PLATFORM,
    }


async def resolve_url(cfg: Dict[str, Any], track: Any,
                      only_id: Optional[str] = None,
                      exclude: Optional[set] = None) -> Optional[Dict[str, Any]]:
    """按启用顺序尝试第三方音源取链，返回 {url, source, quality, source_id}

    only_id 指定时只尝试该音源（用户在歌曲右侧手动指定音源的情况）。
    exclude 里的音源 id 会被跳过（上一个音源给的直链失效时换下一个）。
    """
    sources = enabled_sources(cfg)
    if only_id:
        sources = [s for s in sources if str(s.get("id")) == str(only_id)]
    skip = {str(x) for x in (exclude or set()) if x}
    if skip:
        sources = [s for s in sources if str(s.get("id")) not in skip]
    if not sources:
        return None
    info = build_music_info(track)
    for src in sources:
        script = str(src.get("script") or "")
        platforms = src.get("platforms") or []
        qualities = src.get("qualities") or []
        if platforms and LX_PLATFORM not in platforms:
            continue
        quality = pick_quality(qualities)
        try:
            url = await LxRunner.get_url(script, LX_PLATFORM, quality, info)
        except LxError:
            continue
        if url:
            return {"url": url, "source": src.get("name") or src.get("id"),
                    "source_id": str(src.get("id") or ""), "quality": quality}
    return None


def mark_checked(entry: Dict[str, Any], data: Dict[str, Any]) -> None:
    entry["ok"] = bool(data.get("ok"))
    entry["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if data.get("ok"):
        entry["platforms"] = data.get("platforms") or []
        qm = data.get("qualityMap") or {}
        entry["qualities"] = qm.get(LX_PLATFORM) or (next(iter(qm.values())) if qm else [])
        if data.get("version"):
            entry["version"] = str(data["version"])
        entry["note"] = "可用"
    else:
        entry["platforms"] = []
        entry["qualities"] = []
        entry["note"] = str(data.get("error") or "检测失败")[:200]
