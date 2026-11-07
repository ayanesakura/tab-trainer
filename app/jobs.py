"""后台任务：登记（每首曲子同一时间只有一个任务：整首识别、单行重新识别 / 补页、生成讲解；登记、取消、查询都在一把锁里）、
启动（start：登记 → 在曲子上标开始 → 后台线程，标开始失败时释放登记），以及把模型用量记到曲子上（UsageMeter）。

任务的状态（status / activity / notice）写在曲子上，见 app/state.py；这里只管进程内“有没有在跑”。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from app import store
from app.errors import Busy, NotFound
from app.model import add_usage

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


def start(
    sid: str,
    busy: str,
    begin: Callable[[dict], object],
    run: Callable[[threading.Event], None],
    cancel: threading.Event | None = None,
) -> threading.Event:
    """登记任务（已有任务在跑：Busy(busy)），在 store.update 里执行 begin(song) 标上开始，再在后台跑 run(cancel)。
    begin 失败时释放登记（不释放的话这首曲子会一直显示“忙”）。cancel：调用方已经登记好的任务，不再登记。"""
    cancel = cancel or claim_or_busy(sid, busy)
    try:
        store.update(sid, begin)
    except Exception:
        jobs.release(sid)
        raise
    spawn(sid, lambda: run(cancel))
    return cancel


class UsageMeter:
    """把一个模型客户端用掉的 token / 费用记到曲子上，每一笔只记一次。
    charge(s)：在 store.update 里调用，记上从上次以来新增的部分（随结果一起写回）；
    flush(sid)：放在 finally 里，失败 / 取消时已经发出去的请求也花了钱，补记上还没记的部分。"""

    def __init__(self, claude):
        self.claude = claude
        self.charged: dict = {}  # 每个任务用自己新建的客户端：它用掉的全算这首曲子的

    def charge(self, s: dict) -> None:
        if self.claude is None:
            return
        now = dict(self.claude.usage)
        add_usage(s, now, self.charged)
        self.charged = now

    def flush(self, sid: str) -> None:
        if self.claude is None or dict(self.claude.usage) == self.charged:
            return
        try:
            store.update(sid, self.charge)
        except Exception as e:  # noqa: BLE001 - 曲子已经删掉之类：不能盖住原来的错误
            log.warning("song=%s usage not recorded: %s", sid, e)
