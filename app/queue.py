"""任务队列：支持并发、暂停、继续、取消、进度上报。

下载与上传共用同一套队列；执行逻辑由外部注入（见 app/runner.py），
避免本模块反向依赖业务代码。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional

TERMINAL = ("done", "failed", "canceled")
RUNNING = ("queued", "downloading", "paused")


class Canceled(Exception):
    """任务被用户取消"""


class Task:
    """一个可暂停 / 取消的任务单元"""

    def __init__(self, kind: str, track_id: int, title: str = "", artist: str = "",
                 source_pref: str = "", album: str = "", to_cloud: bool = False) -> None:
        self.kind = kind                  # download / upload
        self.track_id = int(track_id)
        self.title = title
        self.artist = artist
        self.path = ""                    # organize 任务：要处理的文件
        self.album = album
        self.source_pref = source_pref    # "" = 自动（网易云官方优先）
        self.state = "queued"
        self.source = ""                  # 实际使用的音源
        self.level = ""                   # 实际音质
        self.total = 0
        self.done = 0
        self.error = ""
        self.queued_at = time.strftime("%H:%M:%S")
        self.finished_at = ""
        self._resume = asyncio.Event()
        self._resume.set()
        self.canceled = False
        # 单次请求的「下载完成后转存云盘」标记（不影响全局 auto_upload 配置）
        self.to_cloud = bool(to_cloud)
        # 上传成功后要顺手加进去的歌单 id 列表（整理页「上传并加入歌单」用）
        self.to_playlists: List[str] = []
        # 「重新上传」的模式（整理页里点已在云盘那首歌的云朵时用）：
        #   ""        = 默认：已经在云盘就跳过（只补歌单）
        #   "again"   = 不管云盘有没有，再传一份新的
        #   "replace" = 先删掉云盘上旧的那一条，再传新的
        self.cloud_mode = ""
        # ---- 失败重试（退到队尾、隔 N 秒再来，**不占住队列**）----
        self.retries: Dict[str, int] = {}   # 各类失败已重试次数，如 {"server": 1}
        self.not_before = 0.0               # monotonic 时刻：这之前不要取我
        self.retry_note = ""                # 页面显示的一句话（第几次、为什么）
        self.requeue_pending = False        # 本次执行是「等会儿再来」，不是结束

    # ---------------- 控制 ----------------
    @property
    def paused(self) -> bool:
        return not self._resume.is_set()

    def pause(self) -> None:
        if self.state in ("queued", "downloading"):
            self._resume.clear()
            if self.state == "downloading":
                self.state = "paused"

    def resume(self) -> None:
        self._resume.set()
        if self.state == "paused":
            self.state = "downloading"

    def cancel(self) -> None:
        """取消任务。

        进行中的：标记取消并中断；
        **已失败的也算取消** —— 页面只渲染未结束的任务，所以对失败项点「取消」
        就等于把它从列表里移除（比另开一个 remove 接口简单，语义也说得通）。
        """
        self.canceled = True
        self._resume.set()
        if self.state in ("queued", "downloading", "paused", "failed"):
            self.state = "canceled"

    async def gate(self) -> None:
        """在读写每个数据块前调用：暂停则挂起，取消则抛出"""
        if self.canceled:
            raise Canceled()
        if not self._resume.is_set():
            await self._resume.wait()
        if self.canceled:
            raise Canceled()

    # ---------------- 状态 ----------------
    def progress(self, done: int, total: int = 0) -> None:
        self.done = int(done)
        if total:
            self.total = int(total)

    def done_with(self, source: str = "", level: str = "") -> None:
        if source:
            self.source = source
        if level:
            self.level = level

    def fail(self, message: str) -> None:
        self.error = str(message)[:300]
        if self.state not in TERMINAL:
            self.state = "failed"
        self.retry_note = ""

    # ---------------- 失败重试 ----------------
    def retry_later(self, delay: float, note: str = "") -> None:
        """标记「稍后重试」：回到排队中，delay 秒内不会被取出。

        只改任务自己；「挪到队尾」由 Queue.requeue() 做（任务不认识队列）。
        上传失败重试走这条路：任务退回排队，让后面的歌先跑，到点再自己回来。
        """
        self.state = "queued"
        self.not_before = time.monotonic() + max(0.0, float(delay))
        self.retry_note = str(note or "")
        self.requeue_pending = True

    @property
    def retry_in(self) -> int:
        """还有几秒才轮到它重试（不在等待中则 0）"""
        if self.state != "queued":
            return 0
        return max(0, int(round(self.not_before - time.monotonic())))

    @property
    def retrying(self) -> bool:
        return self.state == "queued" and self.not_before > time.monotonic()

    def clear_retry_wait(self) -> None:
        """取消退避等待、重置重试预算（用户手动点「上传/重试」时用，免得点了没反应）"""
        self.not_before = 0.0
        self.retry_note = ""
        self.requeue_pending = False
        self.retries.clear()

    @property
    def percent(self) -> int:
        if not self.total:
            return 0
        return min(100, int(self.done * 100 / self.total))

    @property
    def finished(self) -> bool:
        return self.state in TERMINAL

    def snapshot(self) -> Dict[str, Any]:
        return {
            "kind": self.kind, "track_id": self.track_id, "title": self.title,
            "artist": self.artist, "album": self.album, "state": self.state,
            "source": self.source, "level": self.level, "error": self.error,
            "done": self.done, "total": self.total, "percent": self.percent,
            "queued_at": self.queued_at, "finished_at": self.finished_at,
            "path": self.path,
            "retry_in": self.retry_in, "retry_note": self.retry_note,
            "retries": sum(self.retries.values()),
        }


class Queue:
    """固定并发的工作队列，任务按加入顺序执行"""

    def __init__(self, name: str,
                 runner: Callable[[Task, Dict[str, Any]], Coroutine],
                 cfg_getter: Callable[[], Dict[str, Any]],
                 concurrency: int = 1) -> None:
        self.name = name
        self._runner = runner
        self._cfg_getter = cfg_getter
        self._concurrency = max(1, int(concurrency))
        self._tasks: Dict[int, Task] = {}
        self._order: List[int] = []
        self._active: set = set()
        self._wake = asyncio.Event()
        self._workers: List[asyncio.Task] = []

    # ---------------- 外部接口 ----------------
    def set_concurrency(self, n: int) -> None:
        self._concurrency = max(1, min(8, int(n)))
        self._ensure_workers()
        self._wake.set()

    @property
    def concurrency(self) -> int:
        return self._concurrency

    def get(self, track_id: int) -> Optional[Task]:
        return self._tasks.get(int(track_id))

    def add(self, task: Task, force_ready: bool = False) -> Task:
        """加入队列。同一首已有未完成任务时不重复入队。

        force_ready=True（用户在页面上主动点上传/重试）：如果那首正在退避等待，
        就顺手取消等待、挪到队尾立刻可跑，免得「点了没反应、要等好几秒」。
        """
        tid = task.track_id
        old = self._tasks.get(tid)
        if old is not None and not old.finished:
            if force_ready and old.retrying:
                old.clear_retry_wait()
                if tid in self._order:
                    self._order.remove(tid)
                self._order.append(tid)
                self._wake.set()
            return old                      # 已有未完成任务，不重复入队
        if tid in self._order:
            self._order.remove(tid)
        self._tasks[tid] = task
        self._order.append(tid)
        self._trim()
        self._ensure_workers()
        self._wake.set()
        return task

    def clear_finished(self) -> int:
        gone = [tid for tid, t in self._tasks.items() if t.finished]
        for tid in gone:
            self._tasks.pop(tid, None)
            if tid in self._order:
                self._order.remove(tid)
        return len(gone)

    def requeue(self, task: Task, delay: float, note: str = "") -> Task:
        """失败重试：把任务挪到**队尾**，并让它 delay 秒后才可被取出。

        与「原地 sleep 重试」的区别：退避期间 queue 是空的，后面的歌照常上传，
        不会因为某一首失败就把整条队列堵住（上传队列是串行的，这点很关键）。
        """
        task.retry_later(delay, note)
        tid = task.track_id
        if tid in self._order:
            self._order.remove(tid)
        self._order.append(tid)
        self._wake.set()
        return task

    def pause_all(self) -> int:
        n = 0
        for t in self._tasks.values():
            if not t.finished and not t.paused:
                t.pause()
                n += 1
        return n

    def resume_all(self) -> int:
        n = 0
        for t in self._tasks.values():
            if not t.finished and t.paused:
                t.resume()
                n += 1
        self._wake.set()
        return n

    def cancel_all(self) -> int:
        n = 0
        for t in self._tasks.values():
            if not t.finished:
                t.cancel()
                n += 1
        self._wake.set()
        return n

    def pending(self, include_paused: bool = True) -> List[Task]:
        states = RUNNING if include_paused else ("queued", "downloading")
        return [t for t in self._tasks.values() if t.state in states]

    def snapshot(self) -> Dict[str, Any]:
        tasks = [self.get(tid) for tid in self._order]
        tasks = [t.snapshot() for t in tasks if t is not None]
        running = [t for t in tasks if t["state"] in RUNNING]
        return {
            "name": self.name,
            "concurrency": self._concurrency,
            "active": len(self._active),
            "queued": sum(1 for t in tasks if t["state"] == "queued"),
            "downloading": sum(1 for t in tasks if t["state"] == "downloading"),
            "paused": sum(1 for t in tasks if t["state"] == "paused"),
            "done": sum(1 for t in tasks if t["state"] == "done"),
            "failed": sum(1 for t in tasks if t["state"] == "failed"),
            "canceled": sum(1 for t in tasks if t["state"] == "canceled"),
            "busy": bool(running),
            "tasks": tasks,
        }

    async def drain(self, timeout: Optional[float] = None) -> None:
        """等待队列跑完（暂停中的任务不计入等待）"""
        start = time.monotonic()
        while True:
            if not self.pending(include_paused=False):
                return
            if timeout is not None and time.monotonic() - start > timeout:
                return
            await asyncio.sleep(0.4)

    # ---------------- 内部 ----------------
    def _trim(self, keep: int = 300) -> None:
        """只保留最近的若干条已完成记录，避免内存无限增长"""
        if len(self._tasks) <= keep:
            return
        finished = [tid for tid in self._order if self._tasks.get(tid) and self._tasks[tid].finished]
        for tid in finished[:max(0, len(self._tasks) - keep)]:
            self._tasks.pop(tid, None)
            if tid in self._order:
                self._order.remove(tid)

    def _next(self) -> Optional[Task]:
        """取下一个可执行任务：排队中、没暂停、且已过重试等待时间"""
        now = time.monotonic()
        for tid in self._order:
            task = self._tasks.get(tid)
            if task is None:
                continue
            if task.state == "queued" and not task.paused and task.not_before <= now:
                return task
        return None

    def _ready_in(self) -> Optional[float]:
        """最近的「等会儿重试」还有几秒到期（None = 当前没有等待中的任务）"""
        now = time.monotonic()
        waits = [t.not_before - now for t in self._tasks.values()
                 if t.state == "queued" and not t.paused and t.not_before > now]
        return min(waits) if waits else None

    def _ensure_workers(self) -> None:
        self._workers = [w for w in self._workers if not w.done()]
        for _ in range(max(0, self._concurrency - len(self._workers))):
            self._workers.append(asyncio.create_task(self._worker()))

    async def _worker(self) -> None:
        """常驻 worker：没有可执行任务时等待信号，超时后重新检查"""
        while True:
            task = self._next()
            if task is None:
                self._wake.clear()
                # 有任务在重试退避中 → 精确等到它到期（上限 5s，防漏掉别的信号）
                wait = self._ready_in()
                timeout = 5.0 if wait is None else min(5.0, max(0.05, wait))
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass
                continue

            self._active.add(task.track_id)
            try:
                task.state = "downloading"
                await task.gate()
                await self._runner(task, self._cfg_getter())
                if task.requeue_pending:
                    task.requeue_pending = False    # 等会儿重试：保持 queued，不写完成
                elif task.state not in TERMINAL:
                    task.state = "done"
                    task.retry_note = ""            # 之前的重试说明已无意义
            except Canceled:
                task.state = "canceled"
                task.error = "已取消"
            except Exception as e:  # noqa: BLE001
                task.fail(f"{type(e).__name__}: {e}")
            finally:
                if task.state in TERMINAL:          # 还在等重试的不算结束，不写完成时间
                    task.finished_at = time.strftime("%H:%M:%S")
                self._active.discard(task.track_id)
                self._wake.set()
