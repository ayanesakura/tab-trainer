"""评测样本：每个 (报告, 小节) 是一个样本——标准答案、AI 的结果、AI 自己的 uncertain / warn。只读 data/。"""

from __future__ import annotations

import glob
import json
import os
from functools import lru_cache
from pathlib import Path

from app import store
from app.evaluate import compare_bar

EVALS = str(store.DATA / "evals")
SCORE_TAB = [
    "eval-amy",
    "eval-r-imagine",
    "eval-r-mother",
    "eval-r-felicity",
    "eval-andiloveher",
    "eval-romance",
    "example-wuting",
    "eval-n-foreveryoung",
    "eval-n-alwayswithme",
    "eval-n-chiisana",
    "eval-n-closetoyou",
    "eval-n-airong",
    "eval-n-canon",
    "eval-n-foryou",
    "eval-n-letitbeme",
]
TAB_ONLY = [
    "eval-kisstherain",
    "eval-wonderfulworld",
    "eval-r-imagine-tab",
    "eval-r-mother-tab",
    "eval-n-aquarion",
    "eval-n-merrygoround",
    "eval-n-christmasrose",
    "eval-n-komorebi",
    "eval-n-alwaysbehere",
    "eval-n-laputa",
    "eval-n-endoftheworld",
]
SONGS = SCORE_TAB + TAB_ONLY
# 评测还在主仓库里跑：固定一份报告清单，数字不随新报告变（python -m exp.dataset freeze 生成）
_FROZEN_PATH = os.path.join(os.path.dirname(__file__), "frozen_reports.json")
FROZEN = set(json.loads(Path(_FROZEN_PATH).read_text())) if os.path.exists(_FROZEN_PATH) else None


@lru_cache(None)
def song(sid: str) -> dict:
    return store.load(sid)


def reports(mode: str = "newest", since: str = "20261108-11") -> list[dict]:
    """mode=newest：每首最新的一份；mode=pool：since 以后的全部（同一首多次识别算多个样本）。"""
    out: dict[str, list] = {}
    for d in sorted(glob.glob(EVALS + "/2*")):
        name = os.path.basename(d)
        p = d + "/report.json"
        if not os.path.exists(p) or name < since or (FROZEN is not None and name not in FROZEN):
            continue
        r = json.loads(Path(p).read_text())
        if r.get("song") not in SONGS or r.get("errors"):
            continue
        r["_name"] = name
        out.setdefault(r["song"], []).append(r)
    if mode == "newest":
        return [v[-1] for v in out.values()]
    return [r for v in out.values() for r in v]


def samples(mode: str = "newest", since: str = "20261108-11") -> list[dict]:
    res = []
    for r in reports(mode, since):
        s = song(r["song"])
        by_n = {b["n"]: b for b in s["bars"]}
        for row in r["bars"]:
            tb = by_n[row["bar"]]
            si = tb["system"]
            k = [b["n"] for b in s["bars"] if b["system"] == si].index(tb["n"])
            res.append(
                {
                    "song": r["song"],
                    "report": r["_name"],
                    "n": row["bar"],
                    "system": si,
                    "k": k,  # 这一行里的第几个小节
                    "truth": tb["events"],
                    "pred": row["pred_events"],
                    # 按现在的标准答案重新比（报告生成以后标准答案可能修过）
                    "exact": compare_bar(tb["events"], row["pred_events"])["exact"],
                    "report_exact": row["cmp"]["exact"],
                    "notes_ok": not row["cmp"]["missing"] and not row["cmp"]["extra"],
                    "uncertain": row.get("pred_uncertain") or "",
                    "warn": [
                        w.get("code") if isinstance(w, dict) else str(w) for w in row.get("pred_warn") or []
                    ],
                    "score_tab": r["song"] in SCORE_TAB,
                }
            )
    return res


if __name__ == "__main__":
    import collections
    import sys

    if sys.argv[1:] == ["freeze"]:
        names = sorted({x["report"] for x in samples("pool", "20261107")})
        Path(_FROZEN_PATH).write_text(json.dumps(names, indent=0))
        print("frozen", len(names))

    for mode in ("newest", "pool"):
        ss = samples(mode)
        c = collections.Counter((x["score_tab"], x["exact"], x["notes_ok"]) for x in ss)
        print(mode, len(ss), sorted(c.items()))
        print("  reports:", len({x["report"] for x in ss}))
