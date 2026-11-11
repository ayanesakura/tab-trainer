"""画出检测到的五线谱、符头（标音名）和六线谱数字列，看检测得准不准。用法：python -m exp.viz_staff song si [si...]"""

import sys

from PIL import ImageDraw

from app import store
from exp import staffcheck as sc
from exp.dataset import song
from exp.rows import row_geometry


def main():
    sid = sys.argv[1]
    s = song(sid)
    for si in map(int, sys.argv[2:]):
        geo = row_geometry(sid, si)
        sy = s["systems"][si]
        img = store.page_image(sid, s["pages"][sy["page"]])
        x0, x1, y0, _ = sy["crop"]
        dr = ImageDraw.Draw(img)
        if geo["staff"]:
            for y in geo["staff"]:
                dr.line([(x0, y), (x0 + 30, y)], fill=(0, 200, 0), width=1)
            for h in geo["heads"]:
                d = h.natural_midi()
                dr.ellipse(
                    [h.x - h.w / 2, h.y - h.h / 2, h.x + h.w / 2, h.y + h.h / 2], outline=(220, 0, 0), width=2
                )
                dr.text(
                    (h.x - 6, h.y + h.h / 2 + 1), sc.LETTERS[h.letter] + str(d // 12 - 1), fill=(0, 0, 220)
                )
        for col in geo["cols"]:
            x = col.x
            dr.line(
                [(x, geo["tab"][0]), (x, geo["tab"][-1])],
                fill=(0, 160, 255) if col.paren else (255, 0, 255),
                width=1,
            )
        c = img.crop((x0, int(geo["staff"][0] - 80) if geo["staff"] else y0, x1, int(geo["tab"][-1] + 20)))
        c.resize((c.width * 3 // 2, c.height * 3 // 2)).save(f"exp/out/viz_{sid}_{si}.png")
        print(si, "staff", geo["staff"], "heads", len(geo["heads"]), "cols", len(geo["cols"]))


if __name__ == "__main__":
    main()
