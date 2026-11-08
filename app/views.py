"""接口返回的形状：曲子详情（SongView）和曲库列表项（LibraryItem），字段说明见 app/model.py 和 README 的接口约定。

这里是“读”的一侧：从存储的曲子（已补上派生字段）加上进程内的任务登记、练习记录算出前端要的一切，
前端只显示，不再自己推断“忙不忙 / 能不能改 / 哪个小节锁住”。以 _ 开头的内部字段一律不给。
"""

from __future__ import annotations

import logging
from typing import Any

from app import practice, state, store
from app.errors import CorruptSong, NotFound
from app.jobs import jobs
from app.llm import DEFAULT_MODEL, price_of
from app.model import is_score, notes_key
from app.music import warn_texts

log = logging.getLogger("tab_trainer.views")

# 存储里有、但不给前端的字段
_HIDDEN = ("meta_user", "schema_version")


def _public(x: Any) -> Any:
    """去掉所有以 _ 开头的键（小节上的 _orig、识别中途的 _ocr 等）。"""
    if isinstance(x, dict):
        return {k: _public(v) for k, v in x.items() if not (isinstance(k, str) and k.startswith("_"))}
    if isinstance(x, list):
        return [_public(v) for v in x]
    return x


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


def bar_view(s: dict, bar: dict) -> dict:
    out = _public(bar)
    out["warn"] = list(out.get("warn") or [])
    out["rev"] = int(bar.get("rev") or 0)
    out["edited"] = bool(bar.get("edited"))
    out["needs_review"] = needs_review(bar)
    out["reviewed"] = has_doubts(bar) and reviewed(bar)
    for k in ("reviewed_key", "reviewed_rev", "reviewed_warn"):
        out.pop(k, None)
    out["locked"] = state.bar_locked(s, bar)
    return out


def song_view(s: dict) -> dict:
    """GET /api/songs/{id}。s 是 store.load 的结果。"""
    running = jobs.running(s["id"])
    flags = state.flags(s, running)
    notice = state.public_notice(s)
    out = {k: v for k, v in s.items() if k not in _HIDDEN and k not in ("bars", "notice", "practice")}
    out = _public(out)
    out.update(
        title=s.get("title") or "",
        rev=int(s.get("rev") or 0),
        meta_rev=int(s.get("meta_rev") or 0),
        activity=s.get("activity"),
        notice=notice,
        **flags,
        bars=[bar_view(s, b) for b in s.get("bars") or []],
        practice=practice.aggregate(s["id"], s),
        is_score=is_score(s),
        failed_rows=failed_rows(s),
    )
    return out


def failed_rows(s: dict) -> int:
    """识别失败的行数（“重试失败的行”只重新识别这些）。"""
    return sum(1 for x in s.get("systems") or [] if isinstance(x, dict) and x.get("status") == "error")


def library_item(s: dict) -> dict:
    """GET /api/songs 的一项。"""
    running = jobs.running(s["id"])
    meta = s.get("meta") if isinstance(s.get("meta"), dict) else {}
    bars = s.get("bars") or []
    notice = state.public_notice(s)
    p = practice.summary(s["id"], s)
    review = sum(1 for b in bars if needs_review(b))
    return {
        "id": s["id"],
        "title": s.get("title") or "未命名",
        "artist": meta.get("artist", ""),
        "status": s["status"],
        "progress": s.get("progress"),
        "activity": s.get("activity"),
        "busy": state.busy(s, running),
        "recognizing": state.recognizing(s),
        "notice": notice,
        "bars": len(bars),
        "needs_review_bars": review,
        "created": s.get("created"),
        "imported": s.get("imported"),
        "source_type": s.get("source_type"),
        "is_score": is_score(s),
        "failed_rows": failed_rows(s),
        "tags": [t for t in s.get("tags") or [] if isinstance(t, str)],
        "practice": p,
        **cost_summary(s.get("usage")),
    }


def corrupt_item(sid: str, msg: str) -> dict:
    """数据坏了的曲子：一首坏了不能让整个曲库打不开，列出来，可以删除。"""
    return {
        "id": sid,
        "title": sid,
        "status": "corrupt",
        "busy": False,
        "recognizing": False,
        "notice": {"kind": "error", "msg": msg},
        "bars": 0,
        "needs_review_bars": 0,
        "practice": None,
        "tags": [],
    }


def list_library() -> list[dict]:
    out = []
    for sid in store.song_ids():
        try:
            out.append(library_item(store.load(sid)))
        except CorruptSong as e:
            out.append(corrupt_item(sid, str(e)))
        except NotFound:  # 刚被删掉（别的 KeyError 之类是数据不对，按下面的坏曲子列出来，能删）
            continue
        except Exception as e:  # 内容不对（手改坏的文件之类）算到这一首上，曲库照常能打开
            log.exception("song=%s 无法列进曲库", sid)
            out.append(corrupt_item(sid, f"曲子数据格式不对，无法显示（{type(e).__name__}: {e}）"[:300]))
    return out


def cost_summary(usage) -> dict:
    """曲库里显示的花费（美元）。早期的曲子只记了 token：按默认模型（Opus）的价格估算，标成估算。
    用了价格表里没有的模型（自定义）的那部分只有 token，没法算钱，也标出来。"""
    u = usage if isinstance(usage, dict) else {}
    tin, tout = u.get("input_tokens") or 0, u.get("output_tokens") or 0
    price = price_of(DEFAULT_MODEL) or (0, 0)
    if "cost_usd" in u:  # 之后的调用都记了钱；早期只记 token 的那部分（legacy_*）按估算加上
        lin, lout = u.get("legacy_input_tokens") or 0, u.get("legacy_output_tokens") or 0
        cost = float(u.get("cost_usd") or 0) + (lin * price[0] + lout * price[1]) / 1e6
        estimated = bool(lin or lout)
    else:
        cost, estimated = (tin * price[0] + tout * price[1]) / 1e6, bool(tin or tout)
    return {
        "cost_usd": round(cost, 4),
        "cost_estimated": estimated,
        "unpriced_tokens": int(u.get("unpriced_tokens") or 0),
        "tokens": tin + tout,
    }
