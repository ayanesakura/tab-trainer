"""把一首评测曲做成“像微信 / 截图转发过”的版本：页面缩小到 1100–1300 像素宽、轻微模糊、JPEG 压缩、盖水印
（和 make_chord_eval.py --degrade 同一套处理），标准答案、版面（行、小节线位置按比例缩放）原样保留。
用来在更接近真实图片的条件下评测识别（干净的渲染图上已经很准，剩下的错太少，看不出改进有没有用）。

    uv run python scripts/make_degraded_eval.py eval-amy eval-d-amy [--seed 1]
"""

from __future__ import annotations

import argparse
import copy
import io
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from make_chord_eval import degrade
from PIL import Image

from app import store


def _scale(v, k):
    if isinstance(v, list):
        return [_scale(x, k) for x in v]
    return round(v * k, 1) if isinstance(v, float) else round(v * k)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("src", help="原评测曲 id")
    ap.add_argument("dst", help="新评测曲 id（已存在会覆盖）")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()

    # 只覆盖这个脚本自己生成的评测曲：目标写错（比如和原曲同名）时不能把别的曲子删掉
    if a.dst == a.src or not a.dst.startswith("eval-d-"):
        raise SystemExit(f"目标 id 要以 eval-d- 开头、且和原曲不同：{a.dst}")
    song = store.load(a.src)
    out = copy.deepcopy(song)
    dst = store.SONGS / a.dst
    if dst.exists():
        shutil.rmtree(dst)
    (dst / "pages").mkdir(parents=True)
    factors = []
    for i, page in enumerate(song["pages"]):
        img = store.page_image(a.src, page)
        small = degrade(img, a.seed * 100 + i)
        k = small.width / img.width
        factors.append(k)
        buf = io.BytesIO()
        small.save(buf, "PNG")
        (dst / "pages" / page["file"]).write_bytes(buf.getvalue())
        out["pages"][i] = {**page, "width": small.width, "height": small.height}
    for s in out["systems"]:
        k = factors[s["page"]]
        for key in ("crop", "spans", "lines"):
            if s.get(key) is not None:
                s[key] = _scale(s[key], k)
    for b in out["bars"]:
        if b.get("region") is not None and b.get("page") is not None:
            b["region"] = _scale(b["region"], factors[b["page"]])
    out["id"] = a.dst
    out["title"] = f"{song.get('title') or a.src}（降质）"
    store.write_song(dst, out)
    print(f"已生成 {a.dst}：{len(out['pages'])} 页，缩放 {', '.join(f'{k:.2f}' for k in factors)}")


if __name__ == "__main__":
    Image.MAX_IMAGE_PIXELS = None
    main()
