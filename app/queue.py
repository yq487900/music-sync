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

    def add(self, task: Task) -> Task:
        tid = task.track_id
        old = self._tasks.get(tid)
        if old is not None and not old.finished:
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
        for tid in self._order:
            task = self._tasks.get(tid)
            if task is None:
                continue
            if task.state == "queued" and not task.paused:
                return task
        return None

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
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
                continue

            self._active.add(task.track_id)
            try:
                task.state = "downloading"
                await task.gate()
                await self._runner(task, self._cfg_getter())
                if task.state not in TERMINAL:
                    task.state = "done"
            except Canceled:
                task.state = "canceled"
                task.error = "已取消"
            except Exception as e:  # noqa: BLE001
                task.fail(f"{type(e).__name__}: {e}")
            finally:
                task.finished_at = time.strftime("%H:%M:%S")
                self._active.discard(task.track_id)
                self._wake.set()
