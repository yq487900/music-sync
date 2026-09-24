"""歌单索引：他所有网易云歌单里的歌曲 id（本工具自己的歌单视角）

用途有两个：

1. 云盘页判断「这首歌在不在歌单里」（不在任何歌单的云盘歌 = 可能要清理的）
2. 歌单监控（见 runner.monitor_playlists）：发现歌单里新加的、或云盘里缺的歌

省流量的关键：`/user/playlist` 回来的每个歌单都带 `trackCount` 和 `updateTime`，
**没变过的歌单不重新拉曲目**（直接用上次缓存的 id 列表）。所以稳态下一轮刷新
基本只有 1 次请求，只有你改过某个歌单时才会为它多拉一次。

结果落盘 `/data/playlist_index.json`，容器重建也不丢。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

INDEX_PATH = Path("/data/playlist_index.json")
TTL = 1800          # 30 分钟算过期：页面照常用旧的，后台去刷新
VERSION = 2         # 磁盘格式版本：不匹配就丢掉缓存重新拉（at 毫秒→秒那次修过一版）


class PlaylistIndex:
    def __init__(self) -> None:
        self.at: float = 0.0                      # 上次成功刷新时间
        self.playlists: Dict[str, Dict[str, Any]] = {}   # pid -> {name, track_count, update_time, ids}
        self.sids: Set[str] = set()               # 所有歌单里的歌曲 id 合集
        self.added: Dict[str, float] = {}         # sid -> 最早加入时间（秒）
        self.latest: Dict[str, float] = {}        # sid -> 最近一次被加进歌单的时间（秒）
        self.pl_name: Dict[str, str] = {}         # sid -> 所在歌单名（第一个）
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._failed: float = 0.0
        self._load_disk()

    # ---------------- 磁盘 ----------------
    def _load_disk(self) -> None:
        try:
            data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        if int(data.get("version") or 0) != VERSION:
            return                     # 老格式/老口径：整份丢掉，下次刷新重新拉
        self.at = float(data.get("at") or 0)
        pls = data.get("playlists")
        self.playlists = pls if isinstance(pls, dict) else {}
        self._rebuild_sets()

    def _save_disk(self) -> None:
        try:
            INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = INDEX_PATH.with_name(INDEX_PATH.name + ".tmp")
            tmp.write_text(json.dumps({"version": VERSION, "at": self.at,
                                       "playlists": self.playlists},
                                      ensure_ascii=False), encoding="utf-8")
            tmp.replace(INDEX_PATH)
        except OSError:
            pass

    def _rebuild_sets(self) -> None:
        sids: Set[str] = set()
        added: Dict[str, float] = {}
        latest: Dict[str, float] = {}
        names: Dict[str, str] = {}
        for pid, pl in self.playlists.items():
            name = str((pl or {}).get("name") or pid)
            for pair in (pl or {}).get("ids") or []:
                try:
                    sid = str(pair[0])
                    ts = float(pair[1] or 0)
                except (TypeError, ValueError, IndexError):
                    continue
                sids.add(sid)
                if sid not in added or (ts and ts < added[sid]):
                    added[sid] = ts
                if ts and ts > latest.get(sid, 0):
                    latest[sid] = ts
                names.setdefault(sid, name)
        self.sids, self.added, self.pl_name = sids, added, names
        self.latest = latest

    # ---------------- 状态 ----------------
    @property
    def ready(self) -> bool:
        return bool(self.playlists)

    def stale(self) -> bool:
        return (time.time() - self.at) > TTL

    def status(self) -> Dict[str, Any]:
        return {"ready": self.ready, "playlists": len(self.playlists),
                "songs": len(self.sids), "at": self.at,
                "age": int(time.time() - self.at) if self.at else -1,
                "stale": True if not self.at else self.stale(),
                "refreshing": bool(self._task and not self._task.done())}

    # ---------------- 查询 ----------------
    def in_playlist(self, sid: Any) -> Optional[bool]:
        """这首歌在不在他的歌单里：True/False；索引没好或认不出（不是正式歌曲 id）返回 None"""
        s = str(sid or "")
        if not s.isdigit():
            return None                 # 云盘条目自己的 id：认不出是哪首歌
        if not self.ready:
            return None
        return s in self.sids

    def candidates(self, mode: str = "new", since: float = 0.0) -> List[str]:
        """监控要处理的歌曲 id：new=开启监控之后新加进歌单的；full=歌单里全部

        判定用**「最近一次被加进歌单的时间」**而不是「最早」：
        一首歌如果以前就在别的歌单里，后来又被重新收藏 / 加进另一个歌单，
        「最早加入时间」是很久以前的旧值 —— 拿它筛会把这种新加的歌永久漏掉。
        （实测：手机上新收藏的歌监控毫无反应，就是因为被旧时间盖住了。）
        只取「最近时间」不会踢掉任何原本的候选，只会多认出真正新加的那些。

        since 是秒级时间戳（跟 at 同一口径）。没有加入时间的（at=0）也算老歌，
        不会被 new 模式选中 —— 否则会把整个歌单当新歌下一遍。
        """
        def when(s: str) -> float:
            return float(self.latest.get(s) or self.added.get(s) or 0)

        if mode == "full":
            out = list(self.sids)
        else:
            cut = float(since or 0)
            out = [s for s in self.sids if when(s) >= cut and when(s) > 0]
        out.sort(key=lambda s: (when(s), s))
        return out

    # ---------------- 刷新 ----------------
    async def refresh(self, cookie: str, uid: str) -> Dict[str, Any]:
        """刷新歌单索引；只重拉「trackCount/updateTime 变过」的歌单"""
        if not cookie or not uid:
            return {"ok": False, "error": "还没登录网易云"}
        from app.ncm import Ncm
        async with self._lock:
            ncm = Ncm(cookie=cookie)
            changed = 0
            try:
                pls = await ncm.user_playlists(uid)
                if not pls:
                    return {"ok": False, "error": "没取到歌单列表"}
                keep: Dict[str, Dict[str, Any]] = {}
                for p in pls:
                    pid = str(p.get("id") or "")
                    if not pid:
                        continue
                    name = str(p.get("name") or pid)
                    cnt = int(p.get("trackCount") or 0)
                    upd = int(p.get("updateTime") or 0)
                    old = self.playlists.get(pid) or {}
                    if (old.get("track_count") == cnt and old.get("update_time") == upd
                            and old.get("ids") is not None and old.get("name") == name):
                        keep[pid] = old                     # 没变过，直接用缓存
                        continue
                    detail = await ncm.playlist_detail(pid)
                    ids = []
                    for t in detail.get("trackIds") or []:
                        try:
                            # 注意：网易云的 at 是**毫秒**时间戳，统一换成秒再存
                            # （不换的话「从现在开始监控」会把所有老歌当成新歌）
                            at = float(t.get("at") or 0)
                            ids.append([str(t["id"]), at / 1000 if at > 1e11 else at])
                        except (KeyError, TypeError, ValueError):
                            continue
                    keep[pid] = {"name": name, "track_count": cnt, "update_time": upd,
                                 "ids": ids}
                    changed += 1
            except Exception as e:                          # noqa: BLE001
                self._failed = time.time()
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
            finally:
                await ncm.close()
            self.playlists = keep
            self.at = time.time()
            self._rebuild_sets()
            self._save_disk()
            self._failed = 0.0
            return {"ok": True, "playlists": len(self.playlists), "songs": len(self.sids),
                    "refreshed": changed}

    def ensure_async(self, cookie: str, uid: str, force: bool = False) -> None:
        """索引缺失/过期（或 force）→ 后台补一次，页面不等"""
        if not cookie or not uid:
            return
        if not force and self.ready and not self.stale():
            return
        if self._task and not self._task.done():
            return
        if self._failed and time.time() - self._failed < 60:
            return
        self._task = asyncio.create_task(self.refresh(cookie, uid))


index = PlaylistIndex()
