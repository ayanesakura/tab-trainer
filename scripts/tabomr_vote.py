"""guitar-tab-omr（HF kk9293/guitar-tab-omr）的输出换成我们的小节 / 事件，和 AI 的结果逐小节比，当作免费的第二意见。

模型不在本仓库里：按 ~/Documents/tabomr 的说明另外装（ConvNeXt + 解码器，1200×224 的行图，MPS 贪心解码每行几秒）。
本脚本只做两件事：
1. to_bars：模型的 token 序列 → 每个小节的事件列表。模型的 S1 是最低音弦（我们的 6 弦）；连线的后半个音
   （NTECH_TIE_DEST）不算新起拍，并进前一个事件的时值；TUPLET_a_b 按 b/a 缩放时值；
2. disagree：一个小节里 AI 和模型的音对不上多少——不看起拍位置，只比（弦, 品）或实际音高的多重集合。

用法（离线评测）：python scripts/tabomr_vote.py PRED_JSON song_id ...  → 每首每个小节的 OMR 事件（JSON，stdout）。
一行里模型读出的小节数和版面检测的对不上时，这一行不投票（返回 None）。
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from fractions import Fraction
from pathlib import Path

DUR = {
    "DUR_1": Fraction(4),
    "DUR_2": Fraction(2),
    "DUR_4": Fraction(1),
    "DUR_8": Fraction(1, 2),
    "DUR_16": Fraction(1, 4),
    "DUR_32": Fraction(1, 8),
    "DUR_64": Fraction(1, 16),
}


def _tuplet(t: str) -> Fraction:
    parts = t[len("TUPLET_") :].split("_")
    if len(parts) == 2:
        return Fraction(int(parts[1]), int(parts[0]))
    n = int(parts[0])
    if n in (2, 4):  # 只写了一个数又是 2 / 4：说不清是几连音，不缩放
        return Fraction(1)
    p = 1
    while p * 2 < n:
        p *= 2
    return Fraction(p, n)


def to_bars(tokens: list[str]) -> list[list[dict]]:
    """模型的 token 序列 → 小节列表，每个小节是事件列表（{"dur": "1/2", "notes": [{"string", "fret", "tech"}]}）。"""
    bars: list[list[dict]] = []
    cur: list[dict] | None = None
    beat: dict | None = None

    def flush() -> None:
        nonlocal beat
        if beat is not None and cur is not None:
            d = beat["d"] * {0: 1, 1: Fraction(3, 2), 2: Fraction(7, 4)}.get(beat["dots"], 1) * beat["tup"]
            notes = [n for n in beat["notes"] if not n.pop("_dest")]
            if not notes and beat["had_dest"] and cur:  # 整个都是连线连过来的音：并进前一个事件
                cur[-1]["dur"] = str(Fraction(cur[-1]["dur"]) + d)
            else:
                cur.append({"dur": str(d), "notes": notes})
        beat = None

    for t in tokens:
        if t == "BAR":
            flush()
            cur = []
        elif t == "END_BAR":
            flush()
            if cur is not None:
                bars.append(cur)
            cur = None
        elif t == "BEAT":
            flush()
            beat = {"d": Fraction(1), "dots": 0, "tup": Fraction(1), "notes": [], "had_dest": False}
        elif beat is None:
            continue
        elif t in DUR:
            beat["d"] = DUR[t]
        elif t.startswith("DOTS_"):
            beat["dots"] = int(t[5:])
        elif t.startswith("TUPLET_"):
            beat["tup"] = _tuplet(t)
        elif t == "NTECH_TIE_DEST" and beat["notes"]:
            beat["notes"][-1]["_dest"] = True
            beat["had_dest"] = True
        elif t.startswith("N_S") and "_F" in t:
            s, f = t[3:].split("_F")
            if not s.isdigit() or not (f.isdigit() or f == "X"):
                continue
            # 模型 S1 = 最低音弦；X（闷音）记成 0 品、加 x 技巧
            beat["notes"].append(
                {
                    "string": 7 - int(s),
                    "fret": 0 if f == "X" else int(f),
                    "tech": ["x"] if f == "X" else [],
                    "_dest": False,
                }
            )
    flush()
    if cur:
        bars.append(cur)
    return bars


def notes_of(events: list[dict] | None, open_midi: list[int] | None = None, capo: int = 0) -> Counter:
    """小节里的音（不看起拍）：给了定弦就是实际音高的多重集合，否则是（弦, 品）的多重集合。闷音（x）不算。"""
    out: Counter = Counter()
    for e in events or []:
        for n in e.get("notes") or []:
            if "x" in (n.get("tech") or []):
                continue
            out[open_midi[n["string"] - 1] + capo + n["fret"] if open_midi else (n["string"], n["fret"])] += 1
    return out


def disagree(
    ai: list[dict] | None, omr: list[dict] | None, open_midi: list[int] | None = None, capo: int = 0
) -> dict:
    """AI 和模型在一个小节里对不上的音：only_ai（AI 有、模型没有）、only_omr（模型有、AI 没有）的个数，
    以及按集合（不数重复）比的版本。模型这一行没有可用结果时返回 None。"""
    if omr is None:
        return None
    a, o = notes_of(ai, open_midi, capo), notes_of(omr, open_midi, capo)
    return {
        "only_ai": sum((a - o).values()),
        "only_omr": sum((o - a).values()),
        "set_diff": len(set(a) ^ set(o)),
        "n_ai": sum(a.values()),
        "n_omr": sum(o.values()),
    }


def song_bars(pred: dict, song: dict, sid: str) -> dict[int, list[dict] | None]:
    """一首曲子每个小节（按 n）模型读出的事件；那一行模型读出的小节数和版面检测的不一样时，这一行的小节都是 None。"""
    by_clip = {p["clipId"]: p for p in pred["predictions"]}
    out: dict[int, list[dict] | None] = {}
    for si in range(len(song["systems"])):
        tb = [b for b in song["bars"] if b["system"] == si]
        p = by_clip.get(f"{sid}__{si:03d}")
        pb = to_bars(p.get("tokens") or []) if p else []
        ok = len(pb) == len(tb)
        for i, b in enumerate(tb):
            out[b["n"]] = pb[i] if ok else None
    return out


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app import store

    pred = json.loads(Path(sys.argv[1]).read_text())
    res = {sid: song_bars(pred, store.load(sid), sid) for sid in sys.argv[2:]}
    json.dump(res, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
