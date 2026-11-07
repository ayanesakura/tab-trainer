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
    h, w = gray.shape
    staves = _group_staves(_staff_lines(gray), h, w)
    if not staves:
        # 整页都是没占满页宽的短行（比如切页后最后一页只剩一个小节）：放低门槛再找，但要真找到六线谱才算
        for thr in (0.1, 0.05):
            staves = _group_staves(_staff_lines(gray, thr), h, w)
            if any(len(t) == 6 for t in staves):
                break
        else:
            return []
    # 第二遍：低门槛补找没占满页宽的短行（如一页的最后一行、双栏排版；只有一个全音符小节的末行不到页宽的十分之一）。
    # 只接受线距和已找到的谱表一致、且不与它们重叠的，避免把文字行当成谱线。
    sps = [_spacing(t) for t in staves]
    # 门槛太低时普通的短行反而会和音符连成一片，所以先 0.1 再 0.05，后一遍只补前面没找到的
    for cand in [c for thr in (0.1, 0.05) for c in _group_staves(_staff_lines(gray, thr), h, w)]:
        sp = _spacing(cand)
        if not any(abs(sp - x) <= x * 0.15 for x in sps):
            continue
        if any(cand[0] <= t[-1] + sp and t[0] <= cand[-1] + sp for t in staves):
            continue
        staves.append(cand)
    return _drop_ledger_groups(sorted(staves, key=lambda t: t[0]))


def _drop_ledger_groups(staves: list[list[float]]) -> list[list[float]]:
    """五线谱加上一排加线也是 6 根等距的线：线距和五线谱一样、和这页其他六线谱差得远的“六线谱”丢掉。
    （GP、alphaTab、MuseScore 导出的六线谱线距都比五线谱大）"""
    tabs = [_spacing(t) for t in staves if len(t) == 6]
    fives = [_spacing(t) for t in staves if len(t) == 5]
    if len(tabs) < 2 or not fives:
        return staves
    med = float(np.median(tabs))

    def ledger(t: list[float]) -> bool:
        sp = _spacing(t)
        return abs(sp - med) > med * 0.15 and any(abs(sp - f) <= f * 0.08 for f in fives)

    return [t for t in staves if len(t) != 6 or not ledger(t)]


def _segments(on: np.ndarray, sp: float, min_len: float) -> list[tuple[int, int]]:
    """六线谱横向覆盖的连续段：被品格数字打断的小缺口合并，大的空白（双栏之间）分开。
    合并后谱线覆盖不到一半的段丢掉（五线谱加一根加线凑成的“6 根线”，只在有加线的地方连着）。"""
    segs: list[list[int]] = []
    for a, b in _runs(on):
        if segs and a - segs[-1][1] < sp * 4:
            segs[-1][1] = b
        else:
            segs.append([a, b])
    return [(a, b - 1) for a, b in segs if b - a >= min_len and on[a:b].mean() >= 0.5]


def _group_staves(lines: list[float], h: int, w: int) -> list[list[float]]:
    """按等间距把谱线分组。第一个线距要足够小：取页高的 3% 和页宽的 3% 中较大的
    （只截了一两行的短图页高很小，只按页高算的话谱线永远组不起来）。

    从某根线开始贪心地往下接；接出来的不够 5 根（成不了谱表）时，只丢掉第一根、从下一根重新开始，
    而不是整组丢掉：谱表上方的虚线（let ring）、上一行的符杠横梁常和后面的线凑成一组，
    整组丢掉会把真正的 1 弦一起丢了。"""
    first_gap = max(h, w) * 0.03
    staves: list[list[float]] = []
    n, start = len(lines), 0
    while start < n:
        cur, j = [lines[start]], start + 1
        while j < n:
            y = lines[j]
            gap = y - cur[-1]
            sp = cur[1] - cur[0] if len(cur) > 1 else None
            tol = max(2.0, sp * 0.25) if sp else 0
            if (sp is None and gap < first_gap) or (sp is not None and abs(gap - sp) <= tol):
                cur.append(y)
            elif sp is not None and len(cur) <= 4 and abs(gap - 2 * sp) <= tol:
                # 中间一根线被一排数字挡得太多没检出来：补上
                cur += [cur[-1] + gap / 2, y]
            else:
                break
            j += 1
        if len(cur) >= 5:
            staves.append(cur)
            start = j
        else:
            start += 1
    return [t for t in (_trim(s) for s in staves) if len(t) in (5, 6)]


def _overhang(dark: np.ndarray, a: int, b: int, tab: list[float], sp: float) -> float:
    """竖线在六线谱上下各伸出去多远（线距为单位，取较长的一端）。"""
    cols = dark[:, max(0, a - 1) : b + 1].any(1)
    h, gap = len(cols), max(2, round(sp * 0.15))  # 从谱线外侧一点开始量，避开谱线本身的粗细
    out = 0
    for y, step in ((round(tab[0]) - gap, -1), (round(tab[-1]) + gap, 1)):
        n = 0
        while 0 <= y + step * n < h and cols[y + step * n]:
            n += 1
        out = max(out, n)
    return out / sp


def _is_barline(dark: np.ndarray, a: int, b: int, tab: list[float], sp: float, ends: np.ndarray) -> bool:
    """贯穿六线谱的竖线里分出小节线：
    - 两端要连到 1 弦和 6 弦（扫弦箭头的尾巴常常到不了最下面那根线）；
    - 不能只在上下伸出去一点：扫弦箭头的箭头、尾巴会超出谱线 0.4–0.5 个线距（小节线停在谱线上，被连线、弧线碰到也只有 0.1–0.2），
      小节线正好停在谱线上（和五线谱连通的小节线、左边的谱表括号伸得远，不受影响）。"""
    if ends[max(0, a - 1) : b + 1].max() < 0.8:
        return False
    if _arrowhead(dark, a, b, tab, sp):
        return False
    return not 0.3 <= _overhang(dark, a, b, tab, sp) <= 2.5


def _arrowhead(dark: np.ndarray, a: int, b: int, tab: list[float], sp: float) -> bool:
    """扫弦箭头：竖线一端有个三角形的箭头（停在谱线以内、几乎不伸出去，靠伸出长度分不出来）。
    沿着竖线逐行量它的宽度（避开谱线本身那几行）：连续 3 行以上比竖线本身宽出 2 像素以上就是箭头。
    小节线（包括加粗的终止线）从头到尾一样宽；连线、弧线横穿过去只占一两行。"""
    c = (a + b) // 2
    w = dark.shape[1]
    lines = [round(y) for y in tab]
    col = dark[:, max(0, c - 1) : c + 2].any(1)
    # 只看和竖线连着的那一段：谱线外面不相连的记号（小节号、文字）不算
    y0, y1 = int(tab[0]), int(tab[-1])
    while y0 > max(0, tab[0] - sp * 0.8) and col[y0 - 1]:
        y0 -= 1
    while y1 < min(dark.shape[0] - 1, tab[-1] + sp * 0.8) and col[y1 + 1]:
        y1 += 1
    widths = []
    for y in range(y0, y1 + 1):
        if any(abs(y - ly) <= 1 for ly in lines):
            continue
        row = dark[y]
        if not row[c]:
            widths.append(0)
            continue
        lo = hi = c
        while lo > 0 and row[lo - 1] and c - lo < sp:
            lo -= 1
        while hi < w - 1 and row[hi + 1] and hi - c < sp:
            hi += 1
        widths.append(hi - lo + 1)
    stem = float(np.median([x for x in widths if x])) if any(widths) else 1.0
    run = 0
    for x in widths:
        run = run + 1 if stem + 2 <= x < sp else 0
        if run >= 3:
            return True
    return False


def _chord_diagrams(dark: np.ndarray, tab: list[float], sp: float, x0: int, x1: int) -> bool:
    """这一段“六线谱”其实是一两个、两三个挨着的和弦图：段很短（不到 16 个线距），里面贯穿上下的竖线（弦）
    又多又密（一个线距左右一根）。真正的短行（只有一个小节的末行）里只有两三条小节线。
    （和弦图上按弦的圆点会让竖线看着像扫弦箭头，所以这里数的是还没按箭头筛过的竖线）"""
    if x1 - x0 >= sp * 16:
        return False
    y_a, y_b = int(tab[0] + sp * 0.3), int(tab[-1] - sp * 0.3)
    cols = [(a + b) / 2 for a, b in _runs(dark[y_a:y_b, x0 : x1 + 1].mean(0) >= 0.93)]
    return len(cols) >= 5 and float(np.median(np.diff(cols))) < sp * 1.2


def _row_split(upper: dict, lower: dict) -> float:
    """相邻两行的分界：一般取中点。两行隔得很远时（弹唱谱：上一行下面有简谱和歌词，下一行上面有和弦图），
    下一行只要它上方 9 个线距（和弦名、和弦图、速度记号都在这个范围里），剩下的都归上一行——
    否则上一行的歌词会被拦腰切掉。"""
    mid = (upper["tab_bottom"] + lower["first_top"]) / 2
    return max(mid, lower["first_top"] - lower["sp"] * 9)


def _content_below(dark: np.ndarray, tab_bottom: float, x0: int, x1: int, sp: float) -> float:
    """一栏的最后一行：下方紧挨着的内容（符干、简谱、歌词）也要裁进来，和 _content_above 一样一块一块往下找，
    块之间的空白不到 3 个线距就算连着，最多往下 14 个线距（页码、版权说明隔着一大段空白，不要）。"""
    rows = dark[:, max(0, x0) : x1 + 1].any(1)
    limit = min(len(rows) - 1, int(tab_bottom + sp * 14))
    bottom, gap = tab_bottom, 0
    for y in range(int(tab_bottom) + 1, limit + 1):
        if rows[y]:
            bottom, gap = y, 0
        else:
            gap += 1
            if gap > sp * 3:
                break
    return bottom + sp * 0.5


def _content_above(dark: np.ndarray, first_top: float, x0: int, x1: int, sp: float) -> float:
    """一栏的第一行：上方紧挨着的标记（弹唱谱的和弦名和和弦图、速度、反复记号）也要裁进来。
    从谱线往上一块一块地找有墨迹的行，块之间的空白不到 3 个线距就算连着，最多往上 12 个线距；
    再往上隔着一大段空白的（曲名、作词作曲）不要。"""
    rows = dark[:, max(0, x0) : x1 + 1].any(1)
    limit = max(0, int(first_top - sp * 12))
    top, gap = first_top, 0
    for y in range(int(first_top) - 1, limit - 1, -1):
        if rows[y]:
            top, gap = y, 0
        else:
            gap += 1
            if gap > sp * 3:
                break
    return top - sp * 0.5


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
    bars：小节横向范围列表 [[bx0, bx1], ...]；lines：六根弦线的纵坐标（1 弦在最上面）。"""
    g = np.asarray(img.convert("L"))
    h, w = g.shape
    if min(h, w) < 16:  # 太小，不可能有谱线（下面按行 / 列统计会遇到空数组）
        return {"width": w, "height": h, "systems": []}
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
        for x0, x1 in _segments(on, sp, sp * 6):
            if _chord_diagrams(dark, tab, sp, x0, x1):
                continue
            # 小节线：六线谱上下线之间（掐头去尾）几乎全暗的竖列
            y_a, y_b = int(tab[0] + sp * 0.3), int(tab[-1] - sp * 0.3)
            colfrac = dark[y_a:y_b].mean(0)
            ends = np.minimum(
                dark[int(tab[0]) : y_a + 1].mean(0), dark[y_b : int(np.ceil(tab[-1])) + 1].mean(0)
            )
            bl = [
                (a + b - 1) / 2
                for a, b in _runs(colfrac >= 0.93)
                if x0 - 6 <= a <= x1 + 6 and _is_barline(dark, a, b, tab, sp, ends)
            ]
            if len(bl) >= 8 and float(np.median(np.diff(bl))) < sp * 2:
                continue  # 一排和弦图：格子的竖线一个线距左右一根，也是 6 根等距横线，不是六线谱
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
            # 行首是反复开始记号（‖:）时，谱号到它之间会多出一段（只有 TAB 谱号、拍号），不是小节：
            # 这一段窄（不到 6 个线距；真正的小节、包括弱起小节都宽得多）就去掉
            if len(bars) > 1 and bars[0][1] - bars[0][0] < sp * 6:
                bars = bars[1:]
            if bars:
                systems.append(
                    {
                        "first_top": first[0],
                        "tab_bottom": tab[-1],
                        "sp": sp,
                        "x0": int(x0),
                        "x1": int(x1),
                        "bars": bars,
                        "lines": [round(float(y), 1) for y in tab],
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
            _row_split(prev, s_)
            if prev
            else min(
                s_["first_top"] - min(height * 0.6, s_["sp"] * 9),
                _content_above(dark, s_["first_top"], s_["x0"], s_["x1"], s_["sp"]),
            )
        )
        bottom = (
            _row_split(s_, nxt)
            if nxt
            else max(
                s_["tab_bottom"] + min(height * 0.6, s_["sp"] * 6),
                _content_below(dark, s_["tab_bottom"], s_["x0"], s_["x1"], s_["sp"]),
            )
        )
        s_["top"], s_["bottom"] = max(0, int(top)), min(h, int(bottom))

    return {
        "width": w,
        "height": h,
        "systems": [{k: s_[k] for k in ("top", "bottom", "x0", "x1", "bars", "lines")} for s_ in systems],
    }
