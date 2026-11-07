"""后台任务登记：每首曲子同一时间只有一个任务（整首识别、单行重新识别 / 补页、生成讲解），登记、取消、查询都在一把锁里。

任务的状态（status / activity / notice）写在曲子上，见 app/state.py；这里只管进程内“有没有在跑”。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from app.errors import Busy, NotFound

log = logging.getLogger("tab_trainer.jobs")


class Cancelled(Exception):
    pass


class Jobs:
    def __init__(self):
        self._lock = threading.Lock()
        self._active: dict[str, threading.Event] = {}

    def claim(self, sid: str) -> threading.Event | None:
        """登记任务，返回它的取消信号；已经有任务在跑就返回 None。"""
        with self._lock:
            if sid in self._active:
                return None
            ev = self._active[sid] = threading.Event()
            return ev

    def release(self, sid: str) -> None:
        with self._lock:
            self._active.pop(sid, None)

    def running(self, sid: str) -> bool:
        with self._lock:
            return sid in self._active

    def cancel(self, sid: str) -> bool:
        with self._lock:
            ev = self._active.get(sid)
        if ev:
            ev.set()
        return ev is not None


jobs = Jobs()


def claim_or_busy(sid: str, message: str) -> threading.Event:
    cancel = jobs.claim(sid)
    if cancel is None:
        raise Busy(message)
    return cancel


def spawn(sid: str, target: Callable[[], None]) -> None:
    """在后台线程里跑已登记（jobs.claim）的任务，结束后释放登记。target 自己处理并记录错误；
    曲子在任务进行中被删掉时，写回会抛 NotFound，这里只记一笔。"""

    def run():
        try:
            target()
        except NotFound:
            log.info("song=%s deleted while its job was running", sid)
        finally:
            jobs.release(sid)

    threading.Thread(target=run, daemon=True).start()
