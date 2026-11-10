"""后台任务：登记（每首曲子同一时间只有一个任务：整首识别、单行重新识别 / 补页、生成讲解；登记、取消、查询都在一把锁里）、
启动（start：登记 → 在曲子上标开始 → 后台线程，标开始失败时释放登记）、
所有曲子共用的模型调用名额（slots / run_queued），以及把模型用量记到曲子上（UsageMeter）。

任务的状态（status / activity / notice）写在曲子上，见 app/state.py；这里只管进程内“有没有在跑”。
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable

from app import store
from app.config import settings
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


# ---------------------------------------------------------------------------
# 全局名额：所有曲子加起来同时进行几个模型调用（识别一行、精修一个小节的一遍核对）
# ---------------------------------------------------------------------------
class Slots:
    """先来先得的计数名额，上限每次取名额时按 limit() 读（改了 settings.max_rows_global 马上生效，
    调大时排着队的也会在 0.2 秒内拿到）。先来先得：放下名额又马上再要的（精修一个小节的几遍核对之间）
    排在已经在等的后面，不会一直占着。"""

    def __init__(self, limit: Callable[[], int]):
        self._limit = limit
        self._lock = threading.Lock()
        self._used = 0
        self._waiting: deque[threading.Event] = deque()

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    def _grant(self) -> None:  # 在 _lock 里
        while self._waiting and self._used < max(1, self._limit()):
            self._used += 1
            self._waiting.popleft().set()

    def acquire(self, cancel: threading.Event | None = None) -> None:
        """拿一个名额；排队期间点了取消就不再等（Cancelled）。"""
        with self._lock:
            if not self._waiting and self._used < max(1, self._limit()):
                self._used += 1
                return
            me = threading.Event()
            self._waiting.append(me)
        while not me.wait(0.2):
            with self._lock:
                self._grant()  # 上限可能调大了
                if me.is_set():
                    break
                if cancel is not None and cancel.is_set():
                    self._waiting.remove(me)
                    raise Cancelled()

    def release(self) -> None:
        with self._lock:
            self._used -= 1
            self._grant()


slots = Slots(lambda: settings.max_rows_global)


def run_queued(fn: Callable[[], object], cancel: threading.Event | None = None):
    """排队等全局名额再执行 fn()（识别一行、精修的一遍核对）；排队期间点了取消就不再执行。"""
    slots.acquire(cancel)
    try:
        return fn()
    finally:
        slots.release()


class UsageMeter:
    """把一个模型客户端用掉的 token / 费用记到曲子上，每一笔只记一次。
    charge(s)：在写回的 fn 里调用，把从上次记上以来新增的部分加到曲子上；
    update(sid, fn)：代替 store.update，写盘成功后才算记上——写盘失败时这部分下一次 charge / flush 会再记
    （直接用 store.update 的调用方：到下一次 charge / flush 时按已经写上处理）；
    flush(sid)：放在 finally 里，失败 / 取消时已经发出去的请求也花了钱，补记上还没记的部分。"""

    def __init__(self, claude):
        self.claude = claude
        self.charged: dict = {}  # 已经写进曲子的（每个任务用自己新建的客户端：它用掉的全算这首曲子的）
        self._staged: tuple[dict, dict] | None = None  # (曲子, 已加到它上面的累计用量)：还没确认写盘

    def charge(self, s: dict) -> None:
        if self.claude is None:
            return
        now = dict(self.claude.usage)
        if self._staged is not None and self._staged[0] is s:  # 同一次写回里记第二次：只加新增的
            add_usage(s, now, self._staged[1])
        else:
            self._commit()
            add_usage(s, now, self.charged)
        self._staged = (s, now)

    def _commit(self) -> None:
        if self._staged is not None:
            self.charged, self._staged = self._staged[1], None

    def update(self, sid: str, fn: Callable[[dict], object]) -> dict:
        self._commit()
        try:
            out = store.update(sid, fn)
        except BaseException:
            self._staged = None  # 没写上：下次重新记
            raise
        self._commit()
        return out

    def flush(self, sid: str) -> None:
        self._commit()
        if self.claude is None or dict(self.claude.usage) == self.charged:
            return
        try:
            self.update(sid, self.charge)
        except Exception as e:  # noqa: BLE001 - 曲子已经删掉之类：不能盖住原来的错误
            log.warning("song=%s usage not recorded: %s", sid, e)
