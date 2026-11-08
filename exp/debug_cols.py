"""六线谱数字列和标准答案的起拍对不上的小节。python -m exp.debug_cols song [max]"""

import collections
import sys

from exp.dataset import song
from exp.rows import row_geometry


def main():
    sid = sys.argv[1]
    lim = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    s = song(sid)
    rows = collections.Counter()
    shown = 0
    for b in s["bars"]:
        k = rows[b["system"]]
        rows[b["system"]] += 1
        geo = row_geometry(sid, b["system"])
        if k >= len(geo["spans"]):
            continue
        a, bb = geo["spans"][k]
        cols = [c for c in geo["cols"] if a <= c.x < bb]
        on = [sorted(n["string"] for n in e["notes"]) for e in b["events"] if e["notes"]]
        if len([c for c in cols if not c.paren]) != len(on):
            print(f"bar {b['n']} sys {b['system']} k {k} span {a}-{bb}")
            print("   img:", [(round(c.x), sorted(c.strings), "P" if c.paren else "") for c in cols])
            print("   truth:", on)
            shown += 1
            if shown >= lim:
                break


if __name__ == "__main__":
    main()
