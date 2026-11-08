"""全部信号放在一起的结果表：每个信号 / 组合挑出几个小节、精确率、召回率；分有五线谱 / 纯六线谱。
python -m exp.eval_all [newest|pool|all] [full|tab]

“错”= 小节和标准答案不完全一样（evaluate.compare_bar 的 exact）。另外单列“音错”（不看起拍位置，(弦, 品) 的
多重集合就不一样）的召回率：只差起拍位置的错（节奏、连音）A/B/C 这类比音高的信号本来就看不出来。
"""

import json
import os
import sys

from app import music
from exp.dataset import samples, song
from exp.eval_cols import run as run_cols
from scripts import tabomr_vote as tv

OMR = "/Users/ayane/Documents/tabomr/crops2"


def content_wrong(x):
    return tv.notes_of(x["truth"]) != tv.notes_of(x["pred"] or [])


def add_omr(ss, kind):
    path = f"{OMR}/{kind}/pred.json"
    if not os.path.exists(path):
        return False
    pred = json.load(open(path))
    cache = {}
    for x in ss:
        sid = x["song"]
        if sid not in cache:
            cache[sid] = tv.song_bars(pred, song(sid), sid)
        s = song(sid)
        om = music.tuning_midi(s["meta"]["tuning"])
        omr = cache[sid].get(x["n"])
        x["B"] = (
            None
            if omr is None
            else {
                "sf": tv.disagree(x["pred"], omr),
                "pitch": tv.disagree(x["pred"], omr, om, s["meta"]["capo"]),
                "omr_exact_notes": tv.notes_of(omr) == tv.notes_of(x["truth"]),
            }
        )
    return True


def bsum(b, k="sf"):
    return b[k]["only_ai"] + b[k]["only_omr"]


SIGNALS = {
    "U uncertain": lambda x: bool(x["uncertain"].strip()),
    "W any warn": lambda x: bool(x["warn"]),
    "W ocr_mismatch": lambda x: "ocr_mismatch" in x["warn"],
    "U|W": lambda x: bool(x["uncertain"].strip()) or bool(x["warn"]),
    "A staff>=1": lambda x: x["C"]["staff"] and x["C"]["hm"] >= 1,
    "A staff>=2": lambda x: x["C"]["staff"] and x["C"]["hm"] >= 2,
    "C strings>=1": lambda x: x["C"]["sm"] >= 1,
    "C strings>=2": lambda x: x["C"]["sm"] >= 2,
    "C count": lambda x: x["C"]["xc"] + x["C"]["xo"] >= 1,
    "C any": lambda x: x["C"]["sm"] + x["C"]["xc"] + x["C"]["xo"] >= 1,
    "B sf>=1": lambda x: x["B"] is not None and bsum(x["B"]) >= 1,
    "B sf>=2": lambda x: x["B"] is not None and bsum(x["B"]) >= 2,
    "B sf>=3": lambda x: x["B"] is not None and bsum(x["B"]) >= 3,
    "B pitch>=1": lambda x: x["B"] is not None and bsum(x["B"], "pitch") >= 1,
    "B pitch>=2": lambda x: x["B"] is not None and bsum(x["B"], "pitch") >= 2,
    "B omr-only>=1": lambda x: x["B"] is not None and x["B"]["sf"]["only_omr"] >= 1,
    "B ai-only>=1": lambda x: x["B"] is not None and x["B"]["sf"]["only_ai"] >= 1,
    "B ai-only>=1&omr-only>=1": lambda x: (
        x["B"] is not None and x["B"]["sf"]["only_ai"] >= 1 and x["B"]["sf"]["only_omr"] >= 1
    ),
    "B abstain (row bar count)": lambda x: x["B"] is None,
}
COMBOS = {
    "A or B(sf>=2)": lambda x: SIGNALS["A staff>=1"](x) or SIGNALS["B sf>=2"](x),
    "A and B(sf>=1)": lambda x: SIGNALS["A staff>=1"](x) and SIGNALS["B sf>=1"](x),
    "C(str>=2) or A": lambda x: SIGNALS["C strings>=2"](x) or SIGNALS["A staff>=1"](x),
    "U and (A|B2|C)": lambda x: (
        SIGNALS["U uncertain"](x)
        and (SIGNALS["A staff>=1"](x) or SIGNALS["B sf>=2"](x) or SIGNALS["C any"](x))
    ),
    "U or A or C(str>=2)": lambda x: (
        SIGNALS["U uncertain"](x) or SIGNALS["A staff>=1"](x) or SIGNALS["C strings>=2"](x)
    ),
    "U or W or A or C(str>=2)": lambda x: (
        SIGNALS["U|W"](x) or SIGNALS["A staff>=1"](x) or SIGNALS["C strings>=2"](x)
    ),
    "U or B(sf>=2)": lambda x: SIGNALS["U uncertain"](x) or SIGNALS["B sf>=2"](x),
    "any (U|W|A|B2|C)": lambda x: (
        SIGNALS["U|W"](x) or SIGNALS["A staff>=1"](x) or SIGNALS["B sf>=2"](x) or SIGNALS["C any"](x)
    ),
    "none of U|W|A|C (clean, no OMR)": lambda x: (
        not (SIGNALS["U|W"](x) or SIGNALS["A staff>=1"](x) or SIGNALS["C any"](x))
    ),
    "none of U|W|A|B1|C (=clean)": lambda x: (
        not (SIGNALS["U|W"](x) or SIGNALS["A staff>=1"](x) or SIGNALS["B sf>=1"](x) or SIGNALS["C any"](x))
    ),
}


def score(x):
    """给精修排优先级的加分：每个信号 1 分，强的再加 1 分。"""
    return sum(
        SIGNALS[k](x)
        for k in ("U uncertain", "W any warn", "C any", "C strings>=2", "B sf>=1", "B sf>=3", "A staff>=1")
    )


def line(name, ss, f):
    fl = [x for x in ss if f(x)]
    tp = sum(not x["exact"] for x in fl)
    wrong = sum(not x["exact"] for x in ss)
    cw = [x for x in ss if x["cw"]]
    ctp = sum(1 for x in fl if x["cw"])
    p = tp / len(fl) if fl else 0
    return (
        f"| {name} | {len(fl)}/{len(ss)} | {p:.1%} | {tp}/{wrong} = {tp / wrong if wrong else 0:.0%} | "
        f"{ctp}/{len(cw)} = {ctp / len(cw) if cw else 0:.0%} |"
    )


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "pool"
    kind = sys.argv[2] if len(sys.argv) > 2 else "full"
    since = "20261107" if mode == "all" else "20261108-11"
    ss = run_cols(samples("newest" if mode == "newest" else "pool", since))
    has_b = add_omr(ss, kind)
    for x in ss:
        x["cw"] = content_wrong(x)
    print(
        f"## {mode} (OMR crops: {kind if has_b else 'none'})  bars {len(ss)} wrong {sum(not x['exact'] for x in ss)}"
        f" content-wrong {sum(x['cw'] for x in ss)}"
    )
    for part in ("score+tab", "tab-only", "all"):
        sub = [x for x in ss if part == "all" or x["C"]["staff"] == (part == "score+tab")]
        print(
            f"\n### {part}: bars {len(sub)}, wrong {sum(not x['exact'] for x in sub)}, content-wrong {sum(x['cw'] for x in sub)}"
        )
        print(
            "| signal | flagged | precision | recall (all wrong) | recall (content-wrong) |\n|---|---|---|---|---|"
        )
        for name, f in {**SIGNALS, **COMBOS}.items():
            if not has_b and "B" in name:
                continue
            if part == "tab-only" and name.startswith("A"):
                continue
            print(line(name, sub, f))
        if has_b:
            print("\n score = U + W + C(any) + C(strings>=2) + B(sf>=1) + B(sf>=3) + A：分数 >= t 的小节")
            print(
                "| score >= t | flagged | precision | recall (all wrong) | recall (content-wrong) |\n|---|---|---|---|---|"
            )
            for t in range(1, 6):
                print(line(str(t), sub, lambda x, t=t: score(x) >= t))
            ok = [x for x in sub if x["B"] is not None]
            agree = [x for x in ok if bsum(x["B"]) == 0]
            print(
                f"\nB coverage {len(ok)}/{len(sub)}; agree(sf) {len(agree)} of which AI right {sum(x['exact'] for x in agree)}"
                f" (content-right {sum(not x['cw'] for x in agree)}); OMR notes exact {sum(x['B']['omr_exact_notes'] for x in ok)}/{len(ok)}"
            )


if __name__ == "__main__":
    main()
