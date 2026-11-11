"""给 guitar-tab-omr 准备每一行的裁剪图和清单。python -m exp.tabomr_crops OUT_DIR
full：整行（含五线谱）；tab：只要六线谱（上 2.5 个线距、下 4 个线距，留着六线谱下面的节奏符干）。"""

import json
import os
import sys
from pathlib import Path

from app import store
from exp.dataset import SONGS, song
from exp.rows import row_geometry


def main():
    out = sys.argv[1]
    for kind in ("full", "tab"):
        os.makedirs(f"{out}/{kind}", exist_ok=True)
    clips = {"full": [], "tab": []}
    for sid in SONGS:
        s = song(sid)
        for si, sy in enumerate(s["systems"]):
            img = store.page_image(sid, s["pages"][sy["page"]])
            x0, x1, y0, y1 = sy["crop"]
            geo = row_geometry(sid, si)
            tab = geo["tab"]
            sp = (tab[-1] - tab[0]) / 5
            boxes = {
                "full": (x0, y0, x1, y1),
                "tab": (x0, max(y0, int(tab[0] - 2.5 * sp)), x1, min(y1, int(tab[-1] + 4 * sp))),
            }
            for kind, box in boxes.items():
                cid = f"{sid}__{si:03d}"
                p = f"{out}/{kind}/{cid}.png"
                img.crop(box).save(p)
                clips[kind].append({"id": cid, "imagePath": p, "systemIndex": si})
    for kind, c in clips.items():
        Path(f"{out}/{kind}/manifest.json").write_text(json.dumps({"clips": c}))
        print(kind, len(c))


if __name__ == "__main__":
    main()
