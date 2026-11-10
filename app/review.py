"""小节“是否需要核对 / 已经核对过”的判断（领域规则，不是接口形状）：曲库的待核对数、练习页的标记（app/views.py）、
精修整首跳过确认过的小节（app/refine.py）、评测曲只用核对过的小节（scripts/make_real_eval.py）都按这里算。

用户点“确认没问题”时记下当时音符的指纹和警告（songs.review_bar：reviewed_key / reviewed_warn）；之后音变了、
或者冒出确认时没有的警告就要重新核对，提示变了不算。
"""

from __future__ import annotations

from app.model import notes_key
from app.music import warn_texts


def has_doubts(bar: dict) -> bool:
    """有警告或者模型标了不确定。"""
    return bool(bar.get("warn")) or bool(str(bar.get("uncertain") or "").strip())


def reviewed(bar: dict) -> bool:
    """用户看过、点了“确认没问题”，而且之后音没再变过（提示变了不算）、也没冒出确认时没有的警告
    （比如改了拍号后时值对不上）。早先的确认只记了 rev（reviewed_rev）：rev 没变就有效；
    reviewed_warn 为空（None）的只看音 / rev。"""
    if bar.get("reviewed_key") is not None:
        if bar["reviewed_key"] != notes_key(bar):
            return False
    elif bar.get("reviewed_rev") is None or bar.get("reviewed_rev") != int(bar.get("rev") or 0):
        return False
    seen = bar.get("reviewed_warn")
    return seen is None or set(warn_texts(bar)) <= set(seen)


def needs_review(bar: dict) -> bool:
    """有警告或者模型标了不确定、用户还没确认过：练习页标出来、曲库里算作“待核对”。"""
    return has_doubts(bar) and not reviewed(bar)
