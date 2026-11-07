"""版面检测：在谱子图片里找出六线谱（TAB）所在的行和小节线。

只依赖像素：横向贯穿大半页宽的暗行是谱线，6 根一组是六线谱、5 根一组是五线谱；
在六线谱上下两条线之间几乎全暗的竖列是小节线。适用于软件导出的清晰谱子；
手机拍照（倾斜、透视）时通常检测不到，调用方会退回整页交给模型识别。
"""

from __future__ import annotations

import itertools

import numpy as np
from PIL import Image


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """连续 True 区间 [start, end)。"""
    if not mask.any():
        return []
    d = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)))


def _staff_lines(gray: np.ndarray, thr: float | None = None) -> list[float]:
    # 谱线常是 1 像素的浅灰细线，且可能跨在两行像素之间：放宽暗度阈值，并把相邻两行合并后再算覆盖率
    dark = gray < 200
    dark = dark[:-1] | dark[1:]
    h, w = dark.shape
    frac = dark.sum(1) / w
    if thr is None:
        thr = max(0.35, 0.6 * float(frac.max()))
    return [(a + b) / 2 for a, b in _runs(frac >= thr) if b - a <= max(4, h // 250)]


def _spacing(staff: list[float]) -> float:
    return (staff[-1] - staff[0]) / (len(staff) - 1)


def _find_staves(gray: np.ndarray) -> list[list[float]]:
    h = gray.shape[0]
    staves = _group_staves(_staff_lines(gray), h)
    if not staves:
        return []
    # 第二遍：低门槛补找没占满页宽的短行（如一页的最后一行、双栏排版）。
    # 只接受线距和已找到的谱表一致、且不与它们重叠的，避免把文字行当成谱线。
    sps = [_spacing(t) for t in staves]
    for cand in _group_staves(_staff_lines(gray, 0.1), h):
        sp = _spacing(cand)
        if not any(abs(sp - x) <= x * 0.15 for x in sps):
            continue
        if any(cand[0] <= t[-1] + sp and t[0] <= cand[-1] + sp for t in staves):
            continue
        staves.append(cand)
    return sorted(staves, key=lambda t: t[0])


def _segments(on: np.ndarray, sp: float, min_len: float) -> list[tuple[int, int]]:
    """六线谱横向覆盖的连续段：被品格数字打断的小缺口合并，大的空白（双栏之间）分开。"""
    segs: list[list[int]] = []
    for a, b in _runs(on):
        if segs and a - segs[-1][1] < sp * 4:
            segs[-1][1] = b
        else:
            segs.append([a, b])
    return [(a, b - 1) for a, b in segs if b - a >= min_len]


def _group_staves(lines: list[float], h: int) -> list[list[float]]:
    staves: list[list[float]] = []
    cur: list[float] = []
    for y in lines:
        if not cur:
            cur = [y]
            continue
        gap = y - cur[-1]
        sp = cur[1] - cur[0] if len(cur) > 1 else None
        tol = max(2.0, sp * 0.25) if sp else 0
        if (sp is None and gap < h * 0.03) or (sp is not None and abs(gap - sp) <= tol):
            cur.append(y)
        elif sp is not None and len(cur) <= 4 and abs(gap - 2 * sp) <= tol:
            # 中间一根线被一排数字挡得太多没检出来：补上
            cur += [cur[-1] + gap / 2, y]
        else:
            staves.append(cur)
            cur = [y]
    if cur:
        staves.append(cur)
    return [t for t in (_trim(s) for s in staves) if len(t) in (5, 6)]


def _trim(group: list[float]) -> list[float]:
    """一组超过 6 根“线”：多出来的通常是贴着谱表的虚线标记（let ring、P.M. 的延续线）。
    从两端去掉和中位线距差得最多的那根，直到剩 6 根。"""
    g = list(group)
    while len(g) > 6:
        gaps = np.diff(g)
        med = float(np.median(gaps))
        g = g[1:] if abs(gaps[0] - med) >= abs(gaps[-1] - med) else g[:-1]
    return g


def detect_page(img: Image.Image) -> dict:
    """返回 {"width", "height", "systems": [...]}，每个 system 是一行六线谱：
    top/bottom：这一行（含上方五线谱和标记）的纵向范围；x0/x1：谱线横向范围；
    bars：小节横向范围列表 [[bx0, bx1], ...]。"""
    g = np.asarray(img.convert("L"))
    h, w = g.shape
    dark = g < 200
    staves = _find_staves(g)

    systems = []
    for ti, tab in enumerate(staves):
        if len(tab) != 6:
            continue
        sp = _spacing(tab)
        # 同一行的五线谱：紧挨在六线谱上方的 5 线谱
        first = tab
        if ti > 0 and len(staves[ti - 1]) == 5 and tab[0] - staves[ti - 1][-1] < sp * 12:
            first = staves[ti - 1]

        ys = [round(y) for y in tab]
        on = np.stack([dark[max(0, y - 1) : y + 2].any(0) for y in ys]).sum(0) >= 4
        for x0, x1 in _segments(on, sp, max(sp * 12, w * 0.12)):
            # 小节线：六线谱上下线之间（掐头去尾）几乎全暗的竖列
            y_a, y_b = int(tab[0] + sp * 0.3), int(tab[-1] - sp * 0.3)
            colfrac = dark[y_a:y_b].mean(0)
            bl = [(a + b - 1) / 2 for a, b in _runs(colfrac >= 0.93) if x0 - 6 <= a <= x1 + 6]
            merged: list[float] = []
            for x in bl:  # 双小节线、终止线合并成一条
                if merged and x - merged[-1] < sp * 1.2:
                    merged[-1] = x
                else:
                    merged.append(x)
            if not merged or merged[0] - x0 > sp * 1.5:
                merged.insert(0, float(x0))
            if x1 - merged[-1] > sp * 1.5:
                merged.append(float(x1))
            bars = [[int(a), int(b)] for a, b in itertools.pairwise(merged) if b - a > sp * 3]
            if bars:
                systems.append(
                    {
                        "first_top": first[0],
                        "tab_bottom": tab[-1],
                        "sp": sp,
                        "x0": int(x0),
                        "x1": int(x1),
                        "bars": bars,
                    }
                )

    # 阅读顺序：有一行被分成左右两段时按双栏处理（先左栏从上到下，再右栏）
    rows: dict[float, int] = {}
    for s_ in systems:
        rows[s_["tab_bottom"]] = rows.get(s_["tab_bottom"], 0) + 1
    two_col = any(n > 1 for n in rows.values())
    for s_ in systems:
        s_["col"] = int((s_["x0"] + s_["x1"]) / 2 > w / 2) if two_col else 0
    systems.sort(key=lambda s_: (s_["col"], s_["tab_bottom"]))

    # 纵向范围：同一栏里相邻两行之间取中点；首行向上、末行向下各留一行谱高度左右的余量
    for i, s_ in enumerate(systems):
        prev = systems[i - 1] if i > 0 and systems[i - 1]["col"] == s_["col"] else None
        nxt = systems[i + 1] if i + 1 < len(systems) and systems[i + 1]["col"] == s_["col"] else None
        height = s_["tab_bottom"] - s_["first_top"]
        top = (
            (prev["tab_bottom"] + s_["first_top"]) / 2
            if prev
            else s_["first_top"] - min(height * 0.6, s_["sp"] * 9)
        )
        bottom = (
            (s_["tab_bottom"] + nxt["first_top"]) / 2
            if nxt
            else s_["tab_bottom"] + min(height * 0.6, s_["sp"] * 6)
        )
        s_["top"], s_["bottom"] = max(0, int(top)), min(h, int(bottom))

    return {
        "width": w,
        "height": h,
        "systems": [{k: s_[k] for k in ("top", "bottom", "x0", "x1", "bars")} for s_ in systems],
    }
