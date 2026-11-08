"""现有信号（AI 自己写的 uncertain、warn 代码）的精确率 / 召回率。python -m exp.eval_existing [newest|pool|all]"""

import collections
import sys

from exp.dataset import samples
from exp.eval_cols import prf


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "newest"
    since = "20261107" if mode == "all" else "20261108-11"
    ss = samples("newest" if mode == "newest" else "pool", since)
    codes = collections.Counter(c for x in ss for c in x["warn"])
    print(mode, len(ss), "wrong", sum(not x["exact"] for x in ss), "warn codes", dict(codes))
    for part, sub in (
        ("all", ss),
        ("score+tab", [x for x in ss if x["score_tab"]]),
        ("tab-only", [x for x in ss if not x["score_tab"]]),
    ):
        print(f" [{part}]")
        print(f"  {'uncertain':24s} {prf(sub, lambda x: bool(x['uncertain'].strip()))}")
        print(f"  {'any warn':24s} {prf(sub, lambda x: bool(x['warn']))}")
        for c in codes:
            print(f"  {'warn ' + c:24s} {prf(sub, lambda x, c=c: c in x['warn'])}")
        print(
            f"  {'uncertain or warn':24s} {prf(sub, lambda x: bool(x['uncertain'].strip()) or bool(x['warn']))}"
        )


if __name__ == "__main__":
    main()
