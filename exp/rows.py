"""每一行的几何信息（六线谱线、小节横向范围、五线谱、符头、数字列），按 (曲, 行) 缓存。"""

from functools import lru_cache

import numpy as np

from app import layout, store
from app import staffcheck as sc
from exp.dataset import song


@lru_cache(None)
def _page(sid: str, pi: int):
    s = song(sid)
    img = store.page_image(sid, s["pages"][pi])
    return np.asarray(img.convert("L")), img


@lru_cache(None)
def _detect(sid: str, pi: int):
    return layout.detect_page(_page(sid, pi)[1])["systems"]


@lru_cache(None)
def row_geometry(sid: str, si: int) -> dict:
    s = song(sid)
    sy = s["systems"][si]
    g, _ = _page(sid, sy["page"])
    x0, x1, y0, y1 = sy["crop"]
    tab = sy.get("lines")
    spans = sy.get("spans")
    if not tab:
        # 老的评测曲没存弦线：用现在的版面检测找纵向范围重叠最多的一行
        best = max(
            _detect(sid, sy["page"]),
            key=lambda d: min(d["bottom"], y1) - max(d["top"], y0) - abs(d["x0"] - x0) * 0.01,
        )
        tab = best["lines"]
        spans = spans or best["bars"]
    staff = sc.find_staff(g, tab, x0, x1)
    sp = (tab[-1] - tab[0]) / 5
    heads = sc.find_heads(g, staff, x0, x1, tab[0] - sp * 1.5) if staff else []
    cols = sc.tab_columns(g, tab, int(spans[0][0]) - 2, x1)
    return {"tab": tab, "spans": spans, "staff": staff, "heads": heads, "cols": cols}
