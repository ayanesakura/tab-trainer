"""排序打分去掉某个信号以后，前 10% / 20% / 30% 的小节里能抓到多少错。python -m exp.ablate [pool|all] [full|tab]"""

import random
import sys

from exp import eval_all as ea
from exp.dataset import samples
from exp.eval_cols import run as run_cols

SETS = {
    "full": ("U uncertain", "W any warn", "C any", "C strings>=2", "B sf>=1", "B sf>=3", "A staff>=1"),
    "-A": ("U uncertain", "W any warn", "C any", "C strings>=2", "B sf>=1", "B sf>=3"),
    "-B": ("U uncertain", "W any warn", "C any", "C strings>=2", "A staff>=1"),
    "-C": ("U uncertain", "W any warn", "B sf>=1", "B sf>=3", "A staff>=1"),
    "-U": ("W any warn", "C any", "C strings>=2", "B sf>=1", "B sf>=3", "A staff>=1"),
    "-W": ("U uncertain", "C any", "C strings>=2", "B sf>=1", "B sf>=3", "A staff>=1"),
    "U+W only (today)": ("U uncertain", "W any warn"),
}


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    kind = sys.argv[2] if len(sys.argv) > 2 else "full"
    ss = run_cols(samples("pool", "20261107" if mode == "all" else "20261108-11"))
    ea.add_omr(ss, kind)
    for x in ss:
        x["cw"] = ea.content_wrong(x)
    n = len(ss)
    w = sum(not x["exact"] for x in ss)
    cw = sum(x["cw"] for x in ss)
    print(f"{mode}: bars {n} wrong {w} content-wrong {cw}  (同分随机打散，固定种子)")
    print("| score | top 10% P / R / R(content) | top 20% | top 30% |\n|---|---|---|---|")
    for name, keys in SETS.items():
        rnd = random.Random(0)
        order = sorted(ss, key=lambda x: (-sum(ea.SIGNALS[k](x) for k in keys), rnd.random()))
        cells = []
        for frac in (0.1, 0.2, 0.3):
            top = order[: int(n * frac)]
            tp = sum(not x["exact"] for x in top)
            cells.append(f"{tp / len(top):.0%} / {tp / w:.0%} / {sum(x['cw'] for x in top) / cw:.0%}")
        print(f"| {name} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main()
