"""逐个起拍对齐的版本（staffcheck.check_bar）：A = 五线谱符头对不上，C = 六线谱数字列（弦 / 列数）对不上。
python -m exp.eval_cols [truth|newest|pool|all]"""

import collections
import json
import sys

from app import music
from exp import staffcheck as sc
from exp.dataset import SONGS, samples, song
from exp.rows import row_geometry


def prf(ss, flag):
    tp = sum(1 for x in ss if flag(x) and not x["exact"])
    fp = sum(1 for x in ss if flag(x) and x["exact"])
    wrong = sum(1 for x in ss if not x["exact"])
    n = tp + fp
    return f"flag {n:4d}/{len(ss):4d}  P {tp / n if n else 0:5.1%}  R {tp}/{wrong} {tp / wrong if wrong else 0:5.1%}"


def measure(x, prev_ev):
    geo = row_geometry(x["song"], x["system"])
    if x["k"] >= len(geo["spans"]):
        return None
    span = geo["spans"][x["k"]]
    cols = [c for c in geo["cols"] if span[0] <= c.x < span[1]]
    heads = sc.heads_in(geo["heads"], span)
    d = (geo["staff"][-1] - geo["staff"][0]) / 4 if geo["staff"] else 0
    s = song(x["song"])
    tab = geo["tab"]
    r = sc.check_bar(
        cols,
        heads,
        x["pred"],
        music.tuning_midi(s["meta"]["tuning"]),
        s["meta"]["capo"],
        1.3 * d,
        prev_ev,
        sp=(tab[-1] - tab[0]) / 5,
    )
    return {
        "staff": bool(geo["staff"]),
        "sm": r["string_miss"],
        "hm": r["staff_miss"],
        "xc": len(r["extra_cols"]),
        "xo": len(r["extra_onsets"]),
        "pairs_bad": sum(1 for p in r["pairs"] if p[2] or p[3]),
    }


def run(ss):
    by_rep = collections.defaultdict(dict)
    for x in ss:
        by_rep[x["report"]][x["n"]] = x
    for x in ss:
        prev = by_rep[x["report"]].get(x["n"] - 1)
        x["C"] = measure(x, prev["pred"] if prev else None)
    return [x for x in ss if x["C"]]


VARIANTS = {
    "A staff_miss>=1": lambda c: c["staff"] and c["hm"] >= 1,
    "A staff_miss>=2": lambda c: c["staff"] and c["hm"] >= 2,
    "C string_miss>=1": lambda c: c["sm"] >= 1,
    "C string_miss>=2": lambda c: c["sm"] >= 2,
    "C extra_col>=1": lambda c: c["xc"] >= 1,
    "C extra_onset>=1": lambda c: c["xo"] >= 1,
    "C count (col|onset)": lambda c: c["xc"] + c["xo"] >= 1,
    "C any": lambda c: c["sm"] + c["xc"] + c["xo"] >= 1,
    "A|C any": lambda c: c["sm"] + c["xc"] + c["xo"] + (c["hm"] if c["staff"] else 0) >= 1,
}


def truth_samples():
    ss = []
    for sid in SONGS:
        s = song(sid)
        rows = collections.Counter()
        for b in s["bars"]:
            k = rows[b["system"]]
            rows[b["system"]] += 1
            ss.append(
                dict(
                    song=sid,
                    report="truth-" + sid,
                    n=b["n"],
                    system=b["system"],
                    k=k,
                    truth=b["events"],
                    pred=b["events"],
                    exact=True,
                    notes_ok=True,
                    uncertain="",
                    warn=[],
                    score_tab=None,
                )
            )
    return ss


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "newest"
    if mode == "truth":
        ss = run(truth_samples())
        for part in ("staff", "nostaff"):
            sub = [x for x in ss if x["C"]["staff"] == (part == "staff")]
            per = collections.defaultdict(list)
            for x in sub:
                per[x["song"]].append(x)
            print(f"[{part}] bars {len(sub)}")
            for name, f in VARIANTS.items():
                print(
                    f"  {name:22s} FA {sum(f(x['C']) for x in sub) / len(sub):5.1%}  "
                    + " ".join(
                        f"{sid[5:11]}:{sum(f(x['C']) for x in v) / len(v):.0%}" for sid, v in per.items()
                    )
                )
        return
    since = "20261107" if mode == "all" else "20261108-11"
    ss = run(samples("newest" if mode == "newest" else "pool", since))
    for part in ("staff", "nostaff"):
        sub = [x for x in ss if x["C"]["staff"] == (part == "staff")]
        print(f"[{mode} {part}] bars {len(sub)} wrong {sum(not x['exact'] for x in sub)}")
        for name, f in VARIANTS.items():
            print(f"  {name:22s} {prf(sub, lambda x: f(x['C']))}")
    json.dump(
        [{k: x[k] for k in ("song", "report", "n", "exact", "C")} for x in ss],
        open(f"exp/out/cols_{mode}.json", "w"),
    )


if __name__ == "__main__":
    main()
