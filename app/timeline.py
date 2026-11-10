"""时间段的纯函数：解析存盘的时间、合并重叠、裁剪、求总长。练习时长（app/practice.py 的每首曲子、app/study.py 的每天）
都按这一套算：同一段时间不管被记了几次（两个标签页同时练、秒表和练习页同时在走）只算一次。"""

from __future__ import annotations

import datetime as dt

Interval = tuple[dt.datetime, dt.datetime]


def parse_time(s: object) -> dt.datetime | None:
    """ "YYYY-MM-DD HH:MM[:SS]" → datetime；格式不对是 None。"""
    if not isinstance(s, str) or len(s) < 16:
        return None
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def merge(intervals: list[Interval]) -> list[Interval]:
    """去掉重叠（首尾相接的也连成一段），按开始时间排好。长度为 0 的不要。"""
    out: list[list[dt.datetime]] = []
    for a, b in sorted(x for x in intervals if x[1] > x[0]):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def clip(intervals: list[Interval], lo: dt.datetime, hi: dt.datetime) -> list[Interval]:
    return [(max(a, lo), min(b, hi)) for a, b in intervals if b > lo and a < hi]


def seconds_of(intervals: list[Interval]) -> float:
    return sum((b - a).total_seconds() for a, b in intervals)
