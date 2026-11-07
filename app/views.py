"""接口返回的形状：曲子详情（SongView）和曲库列表项（LibraryItem），字段说明见 app/model.py 和 README 的接口约定。

这里是“读”的一侧：从存储的曲子（已补上派生字段）加上进程内的任务登记、练习记录算出前端要的一切，
前端只显示，不再自己推断“忙不忙 / 能不能改 / 哪个小节锁住”。以 _ 开头的内部字段一律不给。
"""

from __future__ import annotations

from typing import Any

from app import practice, state, store
from app.errors import CorruptSong
from app.jobs import jobs
from app.llm import DEFAULT_MODEL, price_of
from app.model import is_score

# 存储里有、但不给前端的字段
_HIDDEN = ("meta_user", "schema_version")


def _public(x: Any) -> Any:
    """去掉所有以 _ 开头的键（小节上的 _orig、识别中途的 _ocr 等）。"""
    if isinstance(x, dict):
        return {k: _public(v) for k, v in x.items() if not (isinstance(k, str) and k.startswith("_"))}
    if isinstance(x, list):
        return [_public(v) for v in x]
    return x


def _legacy(s: dict) -> dict:
    """旧前端还在读的字段（kind / job），从 source_type / activity 算出来，保留一版。"""
    a = s.get("activity") or {}
    job = None
    if a.get("kind") == "explain":
        job = "explain"
    elif a.get("kind") == "reparse_row" and s.get("progress"):  # 补页（单行重新识别以前也没有 job）
        job = "pages"
    return {"kind": "score" if is_score(s) else "image", "job": job}


def needs_review(bar: dict) -> bool:
    """有警告或者模型标了不确定：练习页标出来、曲库里算作“待核对”。"""
    return bool(bar.get("warn")) or bool(str(bar.get("uncertain") or "").strip())


def bar_view(s: dict, bar: dict) -> dict:
    out = _public(bar)
    out["warn"] = list(out.get("warn") or [])
    out["edited"] = bool(bar.get("edited"))
    out["needs_review"] = needs_review(bar)
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
        activity=s.get("activity"),
        notice=notice,
        **flags,
        bars=[bar_view(s, b) for b in s.get("bars") or []],
        practice=practice.aggregate(s["id"], s),
        # 兼容旧前端，保留一版
        error=notice["msg"] if notice else None,
        running=flags["busy"],
        **_legacy(s),
    )
    return out


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
        "tags": [t for t in s.get("tags") or [] if isinstance(t, str)],
        "practice": p,
        **cost_summary(s.get("usage")),
        # 兼容旧前端，保留一版
        "error": notice["msg"] if notice else None,
        "warn_bars": review,
        "last_practiced": (p or {}).get("last_at", ""),
        **_legacy(s),
    }


def corrupt_item(sid: str, e: CorruptSong) -> dict:
    """数据坏了的曲子：一首坏了不能让整个曲库打不开，列出来，可以删除。"""
    msg = str(e)
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
        "error": msg,
        "warn_bars": 0,
    }


def list_library() -> list[dict]:
    out = []
    for sid in store.song_ids():
        try:
            out.append(library_item(store.load(sid)))
        except CorruptSong as e:
            out.append(corrupt_item(sid, e))
        except LookupError:  # 刚被删掉
            continue
    return out


def cost_summary(usage) -> dict:
    """曲库里显示的花费（美元）。早期的曲子只记了 token：按默认模型（Opus）的价格估算，标成估算。
    用了价格表里没有的模型（自定义）的那部分只有 token，没法算钱，也标出来。"""
    u = usage if isinstance(usage, dict) else {}
    tin, tout = u.get("input_tokens") or 0, u.get("output_tokens") or 0
    if "cost_usd" in u:
        cost, estimated = float(u.get("cost_usd") or 0), False
    else:
        price = price_of(DEFAULT_MODEL) or (0, 0)
        cost, estimated = (tin * price[0] + tout * price[1]) / 1e6, bool(tin or tout)
    return {
        "cost_usd": round(cost, 4),
        "cost_estimated": estimated,
        "unpriced_tokens": int(u.get("unpriced_tokens") or 0),
        "tokens": tin + tout,
    }
