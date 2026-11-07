"""练习记录：data/songs/<id>/practice.jsonl，一段练习追加一行，不再每次重写 song.json。

一行的格式：
    {"at": "2026-11-07 20:15", "ids": [<小节 id>...], "loops": 4, "bpm": 60, "seconds": 120}
旧版本记在 song.json 里的聚合结果，升级时变成第一行“种子”：
    {"seed": true, "bars": {<id>: {loops, seconds, best_bpm, last}}, "last": {a, b, bpm, at}, "total_seconds": ...}

读取时按小节 id 聚合成接口里 practice 的形状（见 aggregate）。小节被重新识别 / 换音轨后对不上的 id 直接忽略，
记录本身不删（以后 id 对得上时也不会丢）。解析结果按文件的修改时间和大小缓存。
"""

from __future__ import annotations

import threading
from pathlib import Path

from app import store
from app.model import now

_cache: dict[Path, tuple[int, int, list[dict]]] = {}
_cache_lock = threading.Lock()


def records(song_id: str, song: dict | None = None) -> list[dict]:
    """这首曲子的全部练习记录（按时间先后）。song 是刚升级、还没存过的旧曲子时，带上内存里的种子。"""
    path = store.practice_path(song_id)
    try:
        st = path.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        sig = None
    out: list[dict] = []
    if sig is not None:
        with _cache_lock:
            hit = _cache.get(path)
        if hit and hit[:2] == sig:
            out = hit[2]
        else:
            out = store.read_jsonl(path)
            with _cache_lock:
                _cache[path] = (*sig, out)
    seed = (song or {}).get("_practice_seed")
    if seed and not any(r.get("seed") for r in out):
        out = [seed, *out]
    return out


def record(song_id: str, bar_ids: list[str], loops: int, bpm: float, seconds: float) -> dict:
    """记一段练习：循环了哪几个小节（按顺序）、几遍、多少 BPM、多少秒（范围在 schemas.PracticeIn 里检查过）。
    已经不存在的小节 id 不记；一个都不存在就什么都不记。返回聚合后的 practice。"""
    song = store.load(song_id)
    known = {b.get("id") for b in song["bars"]}
    valid = list(dict.fromkeys(str(i) for i in bar_ids if str(i) in known))  # 保持顺序去重
    if valid:
        rec = {"at": now(), "ids": valid, "loops": int(loops), "bpm": int(bpm), "seconds": round(float(seconds), 1)}
        store.append_practice(song_id, rec)
    return aggregate(song_id, song)


def aggregate(song_id: str, song: dict) -> dict:
    """接口里的 practice：{bars: {id: {loops, seconds, best_bpm, last}}, last: {a, b, bpm, at, a_n, b_n} | None, total_seconds}。
    时间平摊到这段练习的每个小节；best_bpm 只算循环完整过的；只统计现在还存在的小节。"""
    pos = {b.get("id"): i + 1 for i, b in enumerate(song.get("bars") or [])}
    bars: dict[str, dict] = {}
    total = 0.0
    last = None
    for r in records(song_id, song):
        if r.get("seed"):
            for bid, v in (r.get("bars") or {}).items():
                if isinstance(v, dict):
                    _add(bars, bid, int(v.get("loops") or 0), float(v.get("seconds") or 0), v)
            total += float(r.get("total_seconds") or 0)
            if isinstance(r.get("last"), dict) and r["last"].get("a"):
                last = r["last"]
            continue
        ids = [i for i in r.get("ids") or [] if isinstance(i, str)]
        if not ids:
            continue
        loops, secs = int(r.get("loops") or 0), float(r.get("seconds") or 0)
        bpm = int(r.get("bpm") or 0)
        for bid in ids:
            _add(bars, bid, loops, secs / len(ids), {"best_bpm": bpm if loops else 0, "last": r.get("at", "")})
        total += secs
        last = {"a": ids[0], "b": ids[-1], "bpm": bpm, "at": r.get("at", "")}
    return {
        "bars": {k: v for k, v in bars.items() if k in pos},
        "last": _with_positions(last, pos),
        "total_seconds": round(total, 1),
    }


def _add(bars: dict, bid: str, loops: int, seconds: float, extra: dict) -> None:
    r = bars.setdefault(bid, {"loops": 0, "seconds": 0.0, "best_bpm": 0, "last": ""})
    r["loops"] += loops
    r["seconds"] = round(r["seconds"] + seconds, 1)
    r["best_bpm"] = max(r["best_bpm"], int(extra.get("best_bpm") or 0))
    r["last"] = max(r["last"], str(extra.get("last") or ""))


def _with_positions(last: dict | None, pos: dict) -> dict | None:
    """最近一次练习的范围，加上两头现在的小节位置（1 起；小节不在了是 None），保证 a_n <= b_n。"""
    if not last:
        return None
    out = {
        "a": last.get("a"),
        "b": last.get("b") or last.get("a"),
        "bpm": int(last.get("bpm") or 0),
        "at": last.get("at", ""),
    }
    out["a_n"], out["b_n"] = pos.get(out["a"]), pos.get(out["b"])
    if out["a_n"] is not None and out["b_n"] is not None and out["a_n"] > out["b_n"]:
        out["a"], out["b"], out["a_n"], out["b_n"] = out["b"], out["a"], out["b_n"], out["a_n"]
    return out


def history(song_id: str, song: dict) -> list[dict]:
    """每段练习（按时间先后），给以后的练习统计用。升级前的旧记录只有聚合结果，不在这里。"""
    out = []
    for r in records(song_id, song):
        ids = r.get("ids") or []
        if r.get("seed") or not ids:
            continue
        out.append(
            {
                "at": r.get("at", ""),
                "a": ids[0],
                "b": ids[-1],
                "bpm": int(r.get("bpm") or 0),
                "seconds": float(r.get("seconds") or 0),
                "loops": int(r.get("loops") or 0),
            }
        )
    return out


def summary(song_id: str, song: dict) -> dict | None:
    """曲库列表里的练习信息；没练过是 None。"""
    p = aggregate(song_id, song)
    if not p["last"] and not p["total_seconds"]:
        return None
    last = p["last"]
    return {
        "last_at": (last or {}).get("at", ""),
        "total_seconds": p["total_seconds"],
        "last": {k: last[k] for k in ("a", "b", "bpm", "a_n", "b_n")} if last else None,
    }


def coach_text(song_id: str, song: dict, bar_id: str) -> str:
    """给 AI 看的这个小节的练习记录（中文一句话）；没练过返回空串。"""
    p = aggregate(song_id, song)["bars"].get(bar_id)
    if not p:
        return ""
    mins = round(p.get("seconds", 0) / 60)
    return f"已循环 {p.get('loops', 0)} 遍，累计约 {mins} 分钟，最快练到 {p.get('best_bpm', 0)} BPM，上次练习 {p.get('last', '')}"
