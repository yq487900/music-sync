"""ncm-api 客户端。

网易云的网页接口需要一套签名/加密，直连经常出现「扫了码但永远停在等待扫码」。
这里统一走自建 ncm-api 容器（NeteaseCloudMusicApi），登录/歌单/取流都由它处理。

约定（与 ncm-api 官方文档一致）：
  * 每个请求都带 timestamp，绕过服务端 2 分钟缓存；
  * 登录凭证以 cookie 查询参数传入，未登录时也带 os=pc。
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any, Dict, List, Optional

import aiohttp

NCM_BASE = os.environ.get("NCM_API", "http://127.0.0.1:3000").rstrip("/")

# 扫码状态码
QR_EXPIRED = 800
QR_WAITING = 801
QR_SCANNED = 802
QR_OK = 803

# 音质优先级（高 → 低）
QUALITY_CHAIN = ["jymaster", "hires", "lossless", "exhigh", "standard"]

_MUSIC_U_RE = re.compile(r"MUSIC_U=([^;,\s]+)")


def extract_music_u(cookie: str) -> str:
    """从 ncm-api 返回的 Set-Cookie 串里取出 MUSIC_U 值"""
    m = _MUSIC_U_RE.search(cookie or "")
    return m.group(1) if m else ""


class NcmError(Exception):
    pass


class Ncm:
    """轻量异步客户端：串行限速，避免触发风控"""

    def __init__(self, cookie: str = "", base: Optional[str] = None, delay: float = 0.35,
                 concurrency: int = 1):
        self.base = (base or NCM_BASE).rstrip("/")
        self.cookie = cookie or "os=pc"
        self._delay = max(0.0, delay)
        self._last = 0.0
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        # 并发许可：>1 时允许多个请求同时在飞（批量查询用）；默认 1 = 完全串行，行为不变
        self._sem = asyncio.Semaphore(max(1, int(concurrency)))

    # ---------- 底层 ----------
    async def _sess(self) -> aiohttp.ClientSession:
        s = self._session
        if s is None or s.closed:
            s = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
            self._session = s
        return s

    async def _pace(self) -> None:
        await self._sem.acquire()      # 先拿并发许可（超出并发的请求在此排队）
        async with self._lock:         # 再保证两次「发起」之间至少间隔 _delay
            wait = self._delay - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    async def get(self, path: str, **params) -> Dict[str, Any]:
        await self._pace()
        try:
            params.setdefault("timestamp", int(time.time() * 1000))
            params["cookie"] = self.cookie
            url = f"{self.base}{path}"
            last_err: Optional[Exception] = None
            for attempt in range(3):
                try:
                    s = await self._sess()
                    async with s.get(url, params=params) as r:
                        if r.status >= 500:
                            raise NcmError(f"{path}: HTTP {r.status}")
                        data = await r.json(content_type=None)
                    if not isinstance(data, dict):
                        raise NcmError(f"{path}: 返回非 JSON")
                    return data
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    await asyncio.sleep(1.0 * (attempt + 1))
            raise NcmError(f"{path}: {last_err}")
        finally:
            self._sem.release()

    async def ready(self) -> bool:
        """ncm-api 是否可用"""
        try:
            s = await self._sess()
            async with s.get(f"{self.base}/login/status", params={"timestamp": int(time.time() * 1000)}) as r:
                return r.status < 500
        except Exception:  # noqa: BLE001
            return False

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    # ---------- 扫码登录 ----------
    async def qr_key(self) -> str:
        d = await self.get("/login/qr/key")
        return str(((d.get("data") or {}).get("unikey")) or "")

    async def qr_create(self, key: str) -> Dict[str, str]:
        d = await self.get("/login/qr/create", key=key, qrimg="true")
        data = d.get("data") or {}
        return {"qrurl": str(data.get("qrurl") or ""), "qrimg": str(data.get("qrimg") or "")}

    async def qr_check(self, key: str) -> Dict[str, Any]:
        """扫码状态：800 失效 / 801 待扫码 / 802 已扫码待确认 / 803 成功（含 cookie）"""
        d = await self.get("/login/qr/check", key=key)
        code = 0
        for v in (d.get("code"), (d.get("data") or {}).get("code") if isinstance(d.get("data"), dict) else None):
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if iv in (QR_EXPIRED, QR_WAITING, QR_SCANNED, QR_OK):
                code = iv
                break
        return {
            "code": code,
            "message": str(d.get("message") or ""),
            "cookie": str(d.get("cookie") or ""),
        }

    # ---------- 账号 ----------
    async def logged_in(self) -> bool:
        try:
            d = await self.get("/login/status")
        except NcmError:
            return False
        return int(((d.get("data") or {}).get("code")) or 0) == 200

    async def user_id(self) -> str:
        try:
            d = await self.get("/user/account")
        except NcmError:
            return ""
        profile = d.get("profile") or {}
        return str(profile.get("userId") or "")

    async def user_playlists(self, uid: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        offset = 0
        while True:
            d = await self.get("/user/playlist", uid=uid, limit=50, offset=offset)
            batch = d.get("playlist") or []
            out += batch
            offset += len(batch)
            if not batch or (len(batch) < 50 and not d.get("more")):
                break
        return out

    # ---------- 歌单 / 曲目 ----------
    async def playlist_detail(self, pid: Any) -> Dict[str, Any]:
        d = await self.get("/playlist/detail", id=pid)
        return d.get("playlist") or {}

    async def playlist_track_ids(self, pid: Any, pl: Optional[Dict[str, Any]] = None) -> List[int]:
        """trackIds 是完整列表；/playlist/track/all 会被截断（上千首只回 499）"""
        if pl is None:
            pl = await self.playlist_detail(pid)
        ids: List[int] = []
        for t in pl.get("trackIds") or []:
            try:
                ids.append(int(t["id"]))
            except (KeyError, TypeError, ValueError):
                continue
        return ids

    async def song_detail(self, ids: List[int]) -> List[Dict[str, Any]]:
        """批量取歌曲详情：分块并发（并发度由 self._sem 控制），返回顺序与入参一致"""
        # 分块加大到 300：2095 首由 21 批降到 7 批，配合并发 8 只跑 1 轮。
        # 300 个 id 的 URL 约 3.3KB，离 8KB 上限还很远。
        chunks = [ids[i:i + 300] for i in range(0, len(ids), 300)]
        if not chunks:
            return []

        async def one(chunk: List[int]) -> List[Dict[str, Any]]:
            batch = ",".join(str(x) for x in chunk)
            d = await self.get("/song/detail", ids=batch)
            privs = {int(p["id"]): p for p in (d.get("privileges") or []) if p.get("id") is not None}
            out: List[Dict[str, Any]] = []
            for s in d.get("songs") or []:
                sid = s.get("id")
                if sid is not None and int(sid) in privs and not s.get("privilege"):
                    s["privilege"] = privs[int(sid)]
                out.append(s)
            return out

        results = await asyncio.gather(*[one(c) for c in chunks])
        return [s for batch in results for s in batch]

    # ---------- 取流 / 歌词 ----------
    async def song_url(self, sid: int, level: str) -> Dict[str, Any]:
        d = await self.get("/song/url/v1", id=sid, level=level)
        data = d.get("data") or []
        return (data[0] if data else {}) or {}

    async def lyric(self, sid: int) -> Optional[str]:
        """取歌词；网易云没有歌词的歌曲会返回占位符，此时返回 None"""
        try:
            d = await self.get("/lyric", id=sid)
        except NcmError:
            return None
        text = str((d.get("lrc") or {}).get("lyric") or "")
        if not text.strip():
            return None
        # 未收录歌词的歌会返回「[00:00.00]暂无歌词」这类占位符，别当成歌词写进文件
        # （「纯音乐，请欣赏」这种有实际含义的标记保留）
        if len(text) < 40 and "暂无歌词" in text:
            return None
        return text
