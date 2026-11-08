"""标准答案当 AI 结果时，逐列对不上的符头。python -m exp.debug_heads song [max_bars]"""

import collections
import sys

from app import music
from app import staffcheck as sc
from exp.dataset import song
from exp.rows import row_geometry


def main():
    sid = sys.argv[1]
    lim = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    s = song(sid)
    om = music.tuning_midi(s["meta"]["tuning"])
    capo = s["meta"]["capo"]
    rows = collections.Counter()
    prev = None
    for b in s["bars"][:lim]:
        k = rows[b["system"]]
        rows[b["system"]] += 1
        geo = row_geometry(sid, b["system"])
        if not geo["staff"] or k >= len(geo["spans"]):
            continue
        span = geo["spans"][k]
        cols = [c for c in geo["cols"] if span[0] <= c.x < span[1]]
        live = [c for c in cols if not c.paren]
        d = (geo["staff"][-1] - geo["staff"][0]) / 4
        r = sc.check_bar(cols, sc.heads_in(geo["heads"], span), b["events"], om, capo, 1.3 * d, prev)
        ons = [e for e in b["events"] if e["notes"]]
        for i, j, sm, hm in r["pairs"]:
            if hm:
                hs = [
                    (sc.LETTERS[h.letter] + str(h.natural_midi() // 12 - 1), round(h.y))
                    for h in r["col_heads"][j]
                ]
                ai = [
                    music.midi_to_name(min(sc.note_pitches(n, om, capo)) + 12)
                    + ("/" + ",".join(n["tech"]) if n["tech"] else "")
                    for n in ons[i]["notes"]
                ]
                print(f"bar {b['n']} sys {b['system']} x {round(live[j].x)} heads {hs} ai(written) {ai}")
        prev = b["events"]


if __name__ == "__main__":
    main()
