"""唯一的时钟：新建曲子、练习记录、练琴计划（自评、秒表、今天是哪个练琴日）的时间都从这里取。
测试用 set_now 换成固定时间（或一个函数），不用到处传 now。

时间都是本地时间：存盘的格式见 stamp（"YYYY-MM-DD HH:MM:SS"）；只给人看、不参与计算的（新建 / 导入时间、问答时间、
上次精修）用 minute_stamp（"YYYY-MM-DD HH:MM"），和以前存的一样。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

STAMP_FMT = "%Y-%m-%d %H:%M:%S"
MINUTE_FMT = "%Y-%m-%d %H:%M"

_source: Callable[[], dt.datetime] | None = None


def now() -> dt.datetime:
    """现在（本地时间，到秒）。"""
    t = _source() if _source is not None else dt.datetime.now()
    return t.replace(microsecond=0)


def stamp(t: dt.datetime | None = None) -> str:
    """存盘用的时间 "YYYY-MM-DD HH:MM:SS"（练习记录、练琴计划）：同一分钟里的几段练习也分得开。"""
    return (t or now()).strftime(STAMP_FMT)


def minute_stamp(t: dt.datetime | None = None) -> str:
    """只给人看的时间 "YYYY-MM-DD HH:MM"。"""
    return (t or now()).strftime(MINUTE_FMT)


def set_now(source: dt.datetime | Callable[[], dt.datetime] | None) -> None:
    """测试用：固定时间（datetime）或按函数取（每次调用），None 恢复成系统时间。"""
    global _source
    if source is None or callable(source):
        _source = source
    else:
        fixed = source
        _source = lambda: fixed
