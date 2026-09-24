"""云盘索引：一次拉全量云盘，供歌单页快速判断「这首歌在不在云盘」。

为什么不能现拉：网易云的云盘列表接口只能一页页翻（3900 首 ≈ 40 次请求 / 约 30 秒），
歌单页每翻一页都去拉一次是没法用的。所以：

  * 全量拉一次 → 存成索引（内存 + /data/cloud_index.json，容器重建也不丢）
  * 后台每 10 分钟刷新一次（scheduler.py 的 cloud_index 任务）
  * 页面查询只读内存，秒回；索引还没建好时返回 None（未知），并顺手在后台补一次

「在云盘」的判定：
  ① 匹配到正式曲目的云盘条目 → sid 就是网易云歌曲 id，和歌单里的 id 直接比对（精确）
  ② 没匹配上的条目（网易云只存了文件里的标签）→ 用「标题 + 歌手」比对（上传的自己的歌走这条）
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

INDEX_PATH = Path("/data/cloud_index.json")
TTL = 600          # 超过 10 分钟算过期：页面照常用旧索引，后台去刷新（不阻塞）

# 归一化用：空格、标点、括号一律去掉，只留字母数字和汉字
_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]+")
# 歌名里的附加信息：括号（含中英文）和 feat./ft. 后面的部分
_BRACKET = re.compile(r"[（(\[【][^）)\]】]*[）)\]】]")
_FEAT = re.compile(r"(feat|ft|featuring)[\s.·_-]*.*$", re.I)


def _norm(s: Any) -> str:
    """歌名/歌手归一化：「圣诞星 (feat. 杨瑞代)」→「圣诞星feat杨瑞代」"""
    return _PUNCT.sub("", str(s or "").lower())


def _base_title(s: Any) -> str:
    """去掉括号和 feat. 之后的歌名：「圣诞星 (feat. 杨瑞代)」→「圣诞星」

    云盘条目的歌名对「自己上传的歌」来说就是文件里的标签，常带 feat./括号，
    而歌单里的歌名往往是干净的 —— 两边都用这个形式比才比得上。
    """
    t = _BRACKET.sub("", str(s or ""))
    t = _FEAT.sub("", t)
    return _norm(t) or _norm(s)


def _first_artist(s: Any) -> str:
    return _norm(str(s or "").split("/")[0].split("&")[0])


class CloudIndex:
    def __init__(self) -> None:
        self.at: float = 0.0                  # 上次成功拉取的时间
        self.count: int = 0                   # 云盘条目数
        self.sids: Set[str] = set()           # 已匹配条目：网易云歌曲 id
        self.entry_ids: Set[str] = set()      # 所有条目自带的 id（用来核对本地记的云盘 id 还在不在）
        self.keys: Set[str] = set()           # 未匹配条目：标题|首个歌手
        self.titles: Set[str] = set()         # 未匹配条目：标题（歌手写法对不上时兜底）
        # 文件 md5 → 云盘条目（按内容认「这份文件在不在云盘」，最硬的证据；
        # 从 items 重建，不额外落盘）
        self.md5_item: Dict[str, Dict[str, Any]] = {}
        self.items: List[Dict[str, Any]] = []  # 全量条目（云盘页列表/搜索/筛选用，省得每次翻接口）
        # 全量条目的「标题|歌手」索引（懒构建，见 has_song）；只给整理页判断「在不在云盘」用
        self._all_keys: Set[str] = set()
        self._all_titles: Set[str] = set()
        self._all_at: float = -1.0
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._failed: float = 0.0             # 上次拉取失败的时间（失败后 1 分钟内不再重试）
        self._load_disk()

    # ---------------- 磁盘 ----------------
    def _load_disk(self) -> None:
        try:
            data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        self.at = float(data.get("at") or 0)
        self.count = int(data.get("count") or 0)
        self.sids = {str(x) for x in (data.get("sids") or [])}
        self.entry_ids = {str(x) for x in (data.get("entry_ids") or [])}
        self.keys = {str(x) for x in (data.get("keys") or [])}
        self.titles = {str(x) for x in (data.get("titles") or [])}
        items = data.get("items")
        self.items = items if isinstance(items, list) else []
        self._rebuild_md5()


    def _save_disk(self) -> None:
        payload = {"at": self.at, "count": self.count,
                   "sids": sorted(self.sids), "entry_ids": sorted(self.entry_ids),
                   "keys": sorted(self.keys), "titles": sorted(self.titles),
                   "items": self.items}
        try:
            INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = INDEX_PATH.with_name(INDEX_PATH.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(INDEX_PATH)          # 原子替换：不会读到写了一半的文件
        except OSError:
            pass

    # ---------------- 状态 ----------------
    @property
    def ready(self) -> bool:
        return self.count > 0

    def stale(self) -> bool:
        return (time.time() - self.at) > TTL

    def status(self) -> Dict[str, Any]:
        return {"ready": self.ready, "count": self.count, "at": self.at,
                "age": int(time.time() - self.at) if self.at else -1,
                "stale": True if not self.at else self.stale(),
                "refreshing": bool(self._task and not self._task.done())}

    # ---------------- 查询 ----------------
    def lookup(self, sid: Any, title: str = "", artist: str = "",
               uploaded_sid: Any = "") -> Optional[bool]:
        """在不在云盘：True / False；索引还没建立时返回 None（未知，别谎报「不在」）

        uploaded_sid：本地库里记着的「本工具上传时拿到的云盘 id」——
        上传过的歌有这条铁证，不靠歌名猜。
        """
        if str(sid) in self.sids:
            return True                       # ① 精确：云盘条目已匹配到这首正式曲目
        if uploaded_sid and str(uploaded_sid) in self.entry_ids:
            return True                       # ② 我们传的那条云盘条目还在（本地库记着它的 id）
        if not self.ready:
            # ③ 索引还没建好：有本地上传记录就先算在（没法核对），否则「不知道」
            return True if uploaded_sid else None
        name = _base_title(title)
        if not name:
            return False
        full = _norm(title)
        ar = _first_artist(artist)
        if ar and (f"{name}|{ar}" in self.keys or f"{full}|{ar}" in self.keys):
            return True                       # ④ 自己传的歌：云盘条目用的是文件标签
        # 歌手写法常有出入（feat. / 多歌手 / 别名），歌名够长时只比歌名
        # （太短的歌名（如「精卫」）不比，避免把同名的别的版本算成同一首）
        if len(name) >= 5 and name in self.titles:
            return True
        return False

    # ---------------- 拉取 ----------------
    def _all_index(self):
        """全量条目（含已匹配到正式曲目的）的「标题|歌手」索引，按需构建一次

        _rebuild / note_upload 会把 self.at 变掉，这里靠它判断要不要重建。
        """
        if self._all_at == self.at and (self._all_keys or not self.items):
            return self._all_keys, self._all_titles
        keys: Set[str] = set()
        titles: Set[str] = set()
        for x in self.items:
            raw = x.get("title")
            base = _base_title(raw)
            if not base:
                continue
            ar = _first_artist(x.get("artist"))
            keys.add(f"{base}|{ar}")
            keys.add(f"{_norm(raw)}|{ar}")
            titles.add(base)
        self._all_keys, self._all_titles, self._all_at = keys, titles, self.at
        return keys, titles

    def has_song(self, title: Any, artist: Any = "") -> bool:
        """按「歌名 / 歌手」判断这首歌在不在云盘（含已匹配到正式曲目的条目）

        与 lookup() 的分工：lookup 对「已匹配」条目只认 sid（精确，歌单页 / 云盘页用）；
        本地文件（尤其是从别处搜集来的）只有标签、没有 sid，整理页要用这个方法，
        否则明明云盘里有（别的工具传上去的），也会显示成「不在云盘」、点一下又传一份。
        """
        if not self.ready:
            return False
        name = _base_title(title)
        if not name:
            return False
        keys, titles = self._all_index()
        ar = _first_artist(artist)
        if ar and (f"{name}|{ar}" in keys or f"{_norm(title)}|{ar}" in keys):
            return True
        # 歌手写法常有出入（feat. / 多歌手 / 别名），歌名够长时只比歌名
        return len(name) >= 5 and name in titles

    def _rebuild_md5(self) -> None:
        """从 items 重建「md5 → 云盘条目」（按文件内容认在不在云盘，最硬的证据）"""
        m: Dict[str, Dict[str, Any]] = {}
        for x in self.items:
            h = str(x.get("md5") or "").lower()
            if h and h not in m:
                m[h] = x
        self.md5_item = m

    def find_by_md5(self, md5: Any) -> Optional[Dict[str, Any]]:
        """按本地文件 md5 找云盘上的同一个文件（找不到 / 索引没建好 → None）"""
        h = str(md5 or "").lower()
        if len(h) != 32:
            return None
        return self.md5_item.get(h)

    # ---------------- 拉取 ----------------
    async def _fetch(self, cookie: str) -> List[Dict[str, Any]]:
        from app import cloud as cloudmod          # 延迟导入：便于脱离 aiohttp 单测本模块

        page = 1000            # 实测 limit=1000 一次性返回没问题：3939 首只要 4 个请求

        async def one(offset: int) -> Dict[str, Any]:
            """拉一页；空了重试一次（多半是被限流）"""
            try:
                d = await cloudmod.list_songs(cookie, limit=page, offset=offset)
                if not (d.get("data") or []) and offset:
                    await asyncio.sleep(1.2)
                    d = await cloudmod.list_songs(cookie, limit=page, offset=offset)
                return d
            except Exception:  # noqa: BLE001
                return {}

        # 单页约 2.8s（几千条数据），原先逐页串行 4 页 ≈ 12s；
        # 改为「先拉第一页拿总数，其余页并发」→ 常见情况约 3s
        first = await one(0)
        items = [cloudmod.simplify(it) for it in (first.get("data") or [])]
        if not items:
            return items
        total = int(first.get("count") or 0)
        want = min(max(total, len(items)), 20000)
        offsets = list(range(len(items), want, page))
        for i in range(0, len(offsets), 8):
            wave = offsets[i:i + 8]
            for d in await asyncio.gather(*[one(o) for o in wave]):
                items.extend(cloudmod.simplify(it) for it in (d.get("data") or []))
        return items

    def _rebuild(self, items: List[Dict[str, Any]]) -> None:
        sids: Set[str] = set()
        entry_ids: Set[str] = set()
        keys: Set[str] = set()
        titles: Set[str] = set()
        for x in items:
            sid = str(x.get("sid") or "")
            if sid:
                entry_ids.add(sid)             # 条目自带的 id：本地记的云盘 id 靠它核对
            raw_title = x.get("title")
            base = _base_title(raw_title)
            if not base:
                continue
            if x.get("matched") or int(x.get("al_id") or 0):
                if sid:
                    sids.add(sid)              # 精确：sid 就是网易云歌曲 id
            else:
                ar = _first_artist(x.get("artist"))
                keys.add(f"{base}|{ar}")
                keys.add(f"{_norm(raw_title)}|{ar}")
                titles.add(base)
        self.sids, self.entry_ids = sids, entry_ids
        self.keys, self.titles = keys, titles
        self.items = items                    # 云盘页直接用这份，不再逐页拉接口
        self.count = len(items)
        self.at = time.time()
        self._rebuild_md5()

    async def refresh(self, cookie: str) -> Dict[str, Any]:
        """拉全量云盘并重建索引（同一时间只跑一次）"""
        if not cookie:
            return {"ok": False, "error": "还没登录网易云"}
        async with self._lock:
            try:
                items = await self._fetch(cookie)
            except Exception as e:                 # noqa: BLE001
                self._failed = time.time()
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
            if not items:
                return {"ok": False, "error": "云盘返回空列表"}
            # 防御：网易云限流时会返回被截断的列表（实测 3931 条只回了 2600 条）。
            # 用它覆盖好索引的话，明明在云盘的歌就会被误报成「不在云盘」——
            # 所以明显变少时保留原索引，只报错。
            if self.count >= 100 and len(items) < self.count * 0.9:
                print(f"[cloud_index] 这次只拿到 {len(items)} 条（上次 {self.count} 条），"
                      f"疑似被限流，保留原索引", flush=True)
                return {"ok": False,
                        "error": f"只拿到 {len(items)} 条（上次 {self.count} 条），疑似限流"}
            self._rebuild(items)
            self._save_disk()
            self._failed = 0.0
            return {"ok": True, "count": self.count, "sid": len(self.sids),
                    "unmatched": len(self.titles)}

    def note_upload(self, cloud_sid: Any, title: str = "", artist: str = "") -> None:
        """本工具刚上传成功：立刻把这条登记进索引

        不登记的话，要等下一次整表刷新（最多 10 分钟）才认得它 —— 这段时间页面上会
        误报「不在云盘」，看起来像上传失败。这里只做加法（登记条目 id + 歌名|歌手），
        权威数据仍由整表刷新覆盖。
        """
        sid = str(cloud_sid or "")
        if sid:
            self.entry_ids.add(sid)
        base = _base_title(title)
        if base:
            ar = _first_artist(artist)
            self.keys.add(f"{base}|{ar}")
            self.keys.add(f"{_norm(title)}|{ar}")
            self.titles.add(base)
            # 让 has_song 的全量索引下次重建（新传上去的歌马上就能被整理页认出来）
            self._all_at = -1.0
        self._save_disk()

    def ensure_async(self, cookie: str, force: bool = False) -> None:
        """索引缺失或过期 → 起个后台任务补一次（页面不等待）

        force=True：每次打开歌单都重新索引一遍（他要求「打开歌单时尽量是最新的」）。
        带 15 秒防抖：连着点开几个歌单只会刷一次；刷新中也不会重复起任务。
        """
        if not cookie:
            return
        if not force and self.ready and not self.stale():
            return
        if self._task and not self._task.done():
            return
        # 刚失败过就先别急着重试（否则每次翻页都白跑一趟）
        if self._failed and time.time() - self._failed < 60:
            return
        if force and self.at and (time.time() - self.at) < 15:
            return
        self._task = asyncio.create_task(self.refresh(cookie))


index = CloudIndex()
