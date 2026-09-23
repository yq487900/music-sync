"""洛雪(LX)兼容音源脚本管理。

脚本本身由容器内的 Node 沙箱执行（sources/runner.js），这里只负责：
  * 保存/启用/停用音源，从 URL 拉取脚本内容
  * 调用沙箱做「检测」
  * 取链时按启用顺序依次尝试，拿到可用直链即返回
"""
from __future__ import annotations

import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional

import aiohttp

from app import platform_search

LX_URL = os.environ.get("LX_URL", "http://127.0.0.1:3100").rstrip("/")

# 洛雪音质从高到低
LX_QUALITY_ORDER = ["flac24bit", "flac", "320k", "192k", "128k"]
# 网易云歌曲在洛雪里的平台代号（与 SPlayer-Next 的 PLATFORM_TO_PLUGIN_SOURCE 一致：
#   netease->wy  qqmusic->tx  kugou->kg）
LX_PLATFORM = "wy"
# 跨平台回退顺序：网易云有原生 id 优先；拿不到时按此顺序搜索其它平台
#   tx=QQ音乐  kg=酷狗  kw=酷我  mg=咪咕
# 回退顺序按实测排：kg 匹配准且快 > kw 准但慢 > mg 快但易匹到翻唱 > tx（搜索接口
# 时好时坏，实测多次返回 0 条，放最后当兄。反正搜不到只花 0.2s）
LX_FALLBACK_PLATFORMS = ["kg", "kw", "mg", "tx"]
# 每个平台取前 N 条搜索结果做匹配（咪咕翻唱很多，给多一点候选才容易挑到原唱）
_SEARCH_LIMIT = 20

# ---------------------------- 音源熔断
# 实测 8 个音源里有 4 个已经取不到链（服务器不可达/连不上），而每次取链都要
# 按顺序把它们试一遍 —— 这是「同一首歌有时 3 秒、有时 25 秒」的根因。
# 连续多次失败就短期跳过，避免坏音源每次拖时间；只要成功一次就清零。
_FAIL_UNTIL: Dict[str, float] = {}
_FAIL_COUNT: Dict[str, int] = {}
_FAIL_THRESHOLD = 5          # 连续失败多少首后熔断
_COOLDOWN = 300              # 熔断冷却时长（秒）


def _suspended(sid: str) -> bool:
    return _FAIL_UNTIL.get(sid, 0.0) > time.time()


def _note_fail(sid: str) -> None:
    if not sid:
        return
    n = _FAIL_COUNT.get(sid, 0) + 1
    if n >= _FAIL_THRESHOLD:
        _FAIL_UNTIL[sid] = time.time() + _COOLDOWN
        _FAIL_COUNT[sid] = 0
    else:
        _FAIL_COUNT[sid] = n


def _note_ok(sid: str) -> None:
    _FAIL_COUNT.pop(sid, None)
    _FAIL_UNTIL.pop(sid, None)


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
    async def search(script: str, platform: str, keyword: str,
                     page: int = 1, limit: int = 20) -> List[Dict[str, Any]]:
        """在指定平台搜索歌曲（音源未实现 musicSearch 时返回空列表）"""
        try:
            data = await LxRunner._post("/search", {
                "script": script, "platform": platform,
                "keyword": keyword, "page": page, "limit": limit,
            }, timeout=45)
        except LxError:
            return []
        items = data.get("list") if data.get("ok") else None
        return [x for x in (items or []) if isinstance(x, dict)]

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
        "quality_map": {},          # {平台: [音质]}，跨平台取链时按平台挑档位
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


def _keyword_of(track: Any) -> str:
    """搜索关键词：歌名 + 首位歌手（带全部歌手反而会降命中）"""
    title = str(getattr(track, "title", "") or "").strip()
    artist = str(getattr(track, "artist", "") or "").split("/")[0].strip()
    return (title + " " + artist).strip() or title


def _norm(v: Any) -> str:
    """归一化文本：小写、去掉括号里的补充说明，只保留字母/数字/汉字。
    两边（本地曲目与搜索结果）走同一个函数，所以去掉哪些符号不影响比对结果。"""
    t = str(v or "").lower()
    t = re.sub(r"[（(【\[].*?[)）】\]]", "", t)
    return re.sub(r"[\W_]+", "", t)


def _artist_set(v: Any) -> set:
    return {_norm(x) for x in re.split(r"[/&,、]", str(v or "")) if _norm(x)}


def _to_seconds(v: Any) -> int:
    """时长可能是 "04:23" 或秒数，统一成秒"""
    t = str(v or "").strip()
    if ":" in t:
        try:
            m, sec = t.split(":")[:2]
            return int(m) * 60 + int(float(sec))
        except (TypeError, ValueError):
            return 0
    try:
        return int(float(t))
    except (TypeError, ValueError):
        return 0


def pick_best_match(items: List[Dict[str, Any]], track: Any) -> Optional[Dict[str, Any]]:
    """从搜索结果里挑最像目标曲目的一条：歌名必须对得上，再看歌手与时长"""
    want_t = _norm(getattr(track, "title", ""))
    want_a = _artist_set(getattr(track, "artist", ""))
    want_d = int(getattr(track, "duration", 0) or 0)
    best, best_score = None, 0
    for it in items:
        item_name = _norm(it.get("name"))
        if not item_name or not want_t:
            continue
        if item_name == want_t:
            score = 60
        elif want_t in item_name or item_name in want_t:
            score = 40
        else:
            continue
        if want_a and (_artist_set(it.get("singer")) & want_a):
            score += 25
        elif want_a:
            # 歌手完全对不上 → 很可能是翻唱/伴奏（实测咪咕搜索首条就常是翻唱），
            # 扣到阈值（40）以下直接淘汰，宁可不下也不要下错版本
            score -= 25
        d = _to_seconds(it.get("interval"))
        if want_d and d:
            gap = abs(d - want_d)
            score += 15 if gap <= 5 else (7 if gap <= 15 else 0)
        if score > best_score:
            best, best_score = it, score
    return best if best_score >= 40 else None


def music_info_from_hit(hit: Dict[str, Any], platform: str) -> Dict[str, Any]:
    """搜索结果一条 → 洛雪脚本期望的 musicInfo（结构对齐 SPlayer-Next 的插件入参）"""
    sid = str(hit.get("songmid") or hit.get("id") or hit.get("songId") or "")
    is_hash = len(sid) == 32 and all(c in "0123456789abcdefABCDEF" for c in sid)
    info: Dict[str, Any] = {
        "id": hit.get("id") or sid,
        "songmid": sid,
        "songId": str(hit.get("songId") or sid),
        "name": hit.get("name") or "",
        "singer": hit.get("singer") or "",
        "albumName": hit.get("albumName") or "",
        "albumId": str(hit.get("albumId") or ""),
        "interval": hit.get("interval") or "",
        "img": hit.get("img") or "",
        "source": platform,
    }
    if is_hash or platform == "kg":
        info["hash"] = sid
    info["meta"] = {
        "songId": info["songId"], "albumName": info["albumName"],
        "albumId": info["albumId"], "picUrl": info["img"],
    }
    if info.get("hash"):
        info["meta"]["hash"] = info["hash"]
    return info


def qualities_of(src: Dict[str, Any], platform: str) -> List[str]:
    """某平台的可用音质。优先完整 quality_map；老数据只存了网易云的 qualities，做兼容"""
    qm = src.get("quality_map") or {}
    q = qm.get(platform)
    if isinstance(q, list) and q:
        return list(q)
    if platform == LX_PLATFORM:
        return list(src.get("qualities") or [])
    return []


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
                      exclude: Optional[set] = None,
                      allow_fallback: Optional[bool] = None) -> Optional[Dict[str, Any]]:
    """按启用顺序尝试第三方音源取链，返回 {url, source, quality, source_id, platform}

    分两阶段（思路参照 SPlayer-Next 的音源回退链）：

      阶段 1（快）—— 各音源用**网易云原生 id** 直连取链。无歧义、命中率最高，
        所以先把所有支持 wy 的音源跑完。绝大多数歌曲在这里就返回了，
        坏音源不会拖慢正常下载。

      阶段 2（慢）—— 阶段 1 全失败时才启用：去其它平台（QQ音乐/酷狗/酷我/咪咕）
        搜同名歌，挑最匹配的一条，再用它的 id 让音源取链。
        搜索走内置接口（app/platform_search.py）—— 实测手头 8 个音源
        没有一个实现 musicSearch，所以这层自己做；音源只负责取链。
        搜索结果按平台缓存，多个音源复用同一次搜索。

    only_id 指定时只尝试该音源（用户在歌曲右侧手动指定音源的情况）。
    exclude 里的音源 id 会被跳过（上一个音源给的直链失效时换下一个）。
    allow_fallback=None 时读配置 limits.source_fallback（默认开）。
    """
    sources = enabled_sources(cfg)
    if only_id:
        sources = [s for s in sources if str(s.get("id")) == str(only_id)]
    skip = {str(x) for x in (exclude or set()) if x}
    if skip:
        sources = [s for s in sources if str(s.get("id")) not in skip]
    if not sources:
        return None
    if allow_fallback is None:
        allow_fallback = bool((cfg.get("limits") or {}).get("source_fallback", True))
    info = build_music_info(track)

    # ---------- 阶段 1：网易云原生 id（先把所有音源跑完，快） ----------
    for src in sources:
        platforms = src.get("platforms") or []
        if platforms and LX_PLATFORM not in platforms:
            continue
        sid = str(src.get("id") or "")
        if _suspended(sid):
            continue
        script = str(src.get("script") or "")
        quality = pick_quality(qualities_of(src, LX_PLATFORM))
        try:
            url = await LxRunner.get_url(script, LX_PLATFORM, quality, info)
        except LxError:
            url = None
        if url:
            _note_ok(sid)
            return {"url": url, "source": src.get("name") or src.get("id"),
                    "source_id": sid,
                    "quality": quality, "platform": LX_PLATFORM}
        _note_fail(sid)

    # ---------- 阶段 2：跨平台回退（仅在阶段 1 全失败时） ----------
    keyword = _keyword_of(track)
    if not allow_fallback or not keyword:
        return None
    search_cache: Dict[str, List[Dict[str, Any]]] = {}
    for plat in LX_FALLBACK_PLATFORMS:
        cands: Optional[List[Dict[str, Any]]] = None
        hit: Optional[Dict[str, Any]] = None
        for src in sources:
            platforms = src.get("platforms") or []
            if platforms and plat not in platforms:
                continue
            sid = str(src.get("id") or "")
            if _suspended(sid):
                continue
            if cands is None:
                if plat not in search_cache:
                    search_cache[plat] = await platform_search.search(plat, keyword, _SEARCH_LIMIT)
                cands = search_cache[plat]
                hit = pick_best_match(cands, track) if cands else None
                if not hit:
                    break          # 这个平台没搜到/没匹配上，换下一个平台
            script = str(src.get("script") or "")
            quality = pick_quality(qualities_of(src, plat))
            try:
                url = await LxRunner.get_url(script, plat, quality,
                                             music_info_from_hit(hit, plat))
            except LxError:
                continue
            if url:
                _note_ok(sid)
                return {"url": url, "source": src.get("name") or src.get("id"),
                        "source_id": sid, "quality": quality, "platform": plat,
                        "matched": "%s - %s" % (hit.get("name"), hit.get("singer"))}
            _note_fail(sid)
    return None


def mark_checked(entry: Dict[str, Any], data: Dict[str, Any]) -> None:
    entry["ok"] = bool(data.get("ok"))
    entry["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if data.get("ok"):
        entry["platforms"] = data.get("platforms") or []
        qm = data.get("qualityMap") or {}
        entry["qualities"] = qm.get(LX_PLATFORM) or (next(iter(qm.values())) if qm else [])
        # 完整音质表：{平台: [音质]}。跨平台取链要按平台挑档位，只存 wy 的不够用
        entry["quality_map"] = {k: v for k, v in (qm or {}).items() if isinstance(v, list)}
        if data.get("version"):
            entry["version"] = str(data["version"])
        entry["note"] = "可用"
    else:
        entry["platforms"] = []
        entry["qualities"] = []
        entry["note"] = str(data.get("error") or "检测失败")[:200]
