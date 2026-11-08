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
    # 谱线常是 1 像素的浅灰细线，且可能跨在两行像素之间：放宽暗度阈值，并把相邻两行合并后再算覆盖率。
    # 缩小、模糊、JPEG 压缩过的截图里，浅灰的谱线会摊到两行像素上、每行都只有 205 左右：门槛放到 215
    dark = gray < 215
    dark = dark[:-1] | dark[1:]
    h, w = dark.shape
    frac = dark.sum(1) / w
    if thr is None:
        thr = max(0.35, 0.6 * float(frac.max()))
    runs = _runs(frac >= thr)
    # 谱线不能太粗（太粗的是文字、色块）。上限按线距算：和最近的相邻候选线隔开的距离的 0.35 倍；
    # 放大过的截图（Retina 2 倍）谱线有三四个像素粗，按页高算的固定上限会把它们全丢掉。原来的上限照旧保留
    mids = [(a + b) / 2 for a, b in runs]
    gaps = np.diff(mids) if len(mids) > 1 else np.zeros(0)
    near = np.minimum(np.r_[np.inf, gaps], np.r_[gaps, np.inf]) if len(mids) > 1 else np.zeros(len(mids))
    cap = max(4, h // 250)
    return [m for (a, b), m, gap in zip(runs, mids, near) if b - a <= max(cap, 0.35 * gap)]


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


def _overhang(dark: np.ndarray, a: int, b: int, tab: list[float], sp: float) -> tuple[float, float]:
    """竖线在六线谱上方、下方各伸出去多远（线距为单位）。"""
    cols = dark[:, max(0, a - 1) : b + 1].any(1)
    h, gap = len(cols), max(2, round(sp * 0.15))  # 从谱线外侧一点开始量，避开谱线本身的粗细
    out = []
    for y, step in ((round(tab[0]) - gap, -1), (round(tab[-1]) + gap, 1)):
        n = 0
        while 0 <= y + step * n < h and cols[y + step * n]:
            n += 1
        out.append(n / sp)
    return out[0], out[1]


def _is_barline(
    dark: np.ndarray, a: int, b: int, tab: list[float], sp: float, ends: np.ndarray, colfrac: np.ndarray
) -> bool:
    """贯穿六线谱的竖线里分出小节线：
    - 两端要连到 1 弦和 6 弦（扫弦箭头的尾巴常常到不了最下面那根线）；
    - 不能只在上下伸出去一点：扫弦箭头的箭头、尾巴会超出谱线 0.4–0.5 个线距（小节线停在谱线上，被连线、弧线碰到也只有 0.1–0.2），
      小节线正好停在谱线上（和五线谱连通的小节线、左边的谱表括号伸得远，不受影响）；
    - 不能是从品格数字出发、贯穿整个六线谱的符干（见 _notes_at_ends）：两端都扎在数字里（和弦的最高、最低音），
      或者一端扎在数字里、另一端伸出谱线去接横梁（伸得远时上一条管不到）。"""
    if ends[max(0, a - 1) : b + 1].max() < 0.8:
        return False
    if _arrowhead(dark, a, b, tab, sp):
        return False
    up, down = _overhang(dark, a, b, tab, sp)
    top, bottom = _notes_at_ends(dark, a, b, tab, sp, colfrac)
    # 符干伸出谱线去接横梁最多三四个线距；和上方五线谱连通的小节线、谱表括号伸得远得多，不受影响
    if (top and (bottom or 0.3 <= down <= 4)) or (bottom and 0.3 <= up <= 4):
        return False
    return not 0.3 <= max(up, down) <= 2.5


def _notes_at_ends(
    dark: np.ndarray, a: int, b: int, tab: list[float], sp: float, colfrac: np.ndarray
) -> tuple[bool, bool]:
    """竖线的上端（1 弦）、下端（6 弦）是不是扎在一个品格数字里。符干从数字出发：数字骑在弦线上、符干从它中间穿出来，
    伸出谱线的部分被数字挡住（“7” 的横只伸出去 0.26 个线距，“0” 中间是空的），靠伸出长度分不出来。
    小节线两端停在谱线上，旁边没有东西（连线、弧线碰到也只在一侧、一两行）。
    在弦线上下半个线距、竖线左右 0.45 个线距的小框里找竖线以外的墨迹（谱线本身那几行不算；
    另一条贯穿的竖线和它旁边两个像素也不算：双小节线、反复记号的粗细两条线挨得很近，谱表括号末端有个钩）：
    左右两侧都有、加起来占了 0.3 个线距以上的行，就是数字。"""
    h, w = dark.shape
    r = max(3, round(sp * 0.45))
    near = range(max(0, a - r), min(w, b + r + 1))
    through = np.flatnonzero(colfrac >= 0.5)
    through = through[(through < a - 1) | (through > b)]

    def free(x: int) -> bool:
        return not len(through) or np.abs(through - x).min() > 2

    left = [x for x in near if x < a - 1 and free(x)]
    right = [x for x in near if x > b and free(x)]
    if not left or not right:
        return False, False
    found = []
    for y_line in (tab[0], tab[-1]):
        y0, y1 = max(0, int(y_line - sp * 0.5)), min(h, int(y_line + sp * 0.5) + 1)
        lines = dark[y0:y1, max(0, int(a - sp * 2)) : int(b + sp * 2)].mean(1) >= 0.5  # 谱线本身的行
        lo, hi = dark[y0:y1][:, left].any(1) & ~lines, dark[y0:y1][:, right].any(1) & ~lines
        found.append(bool(lo.sum() >= sp * 0.15 and hi.sum() >= sp * 0.15 and (lo | hi).sum() >= sp * 0.3))
    return found[0], found[1]


def _arrowhead(dark: np.ndarray, a: int, b: int, tab: list[float], sp: float) -> bool:
    """扫弦箭头：竖线一端有个三角形的箭头（停在谱线以内、几乎不伸出去，靠伸出长度分不出来）。
    沿着竖线逐行量它向左、向右各伸出多远（避开谱线本身那几行）：连续 3 行以上两边都比竖线本身宽出 m 像素就是箭头。
    小节线（包括加粗的终止线）从头到尾一样宽；连线、弧线横穿过去只占一两行。
    - 两边都要宽出去：箭头是对称的三角形；反复记号的两个点贴在细线一侧，不是箭头；
    - m 按线距算（至少 1 像素）：放大 2 倍（最近邻）的截图里，竖线一侧的灰边会变成 2 像素宽，按固定像素算会被当成箭头。"""
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
    edges = []  # 每行（竖线所在的那段墨迹）向左、向右伸到哪；这一行竖线断开时为 None
    near_line = max(1, round(sp * 0.12))  # 谱线本身（放大过的截图里谱线有三四行粗，和竖线交叉处的灰边也更宽）
    for y in range(y0, y1 + 1):
        if any(abs(y - ly) <= near_line for ly in lines):
            continue
        row = dark[y]
        if not row[c]:
            edges.append(None)
            continue
        lo = hi = c
        while lo > 0 and row[lo - 1] and c - lo < sp:
            lo -= 1
        while hi < w - 1 and row[hi + 1] and hi - c < sp:
            hi += 1
        edges.append((lo, hi))
    found = [e for e in edges if e]
    if not found:
        return False
    stem_lo, stem_hi = np.median([e[0] for e in found]), np.median([e[1] for e in found])
    m = max(1, round(sp * 0.1))
    run = 0
    for e in edges:
        wide = e is not None and e[0] <= stem_lo - m and e[1] >= stem_hi + m and e[1] - e[0] + 1 < sp
        run = run + 1 if wide else 0
        if run >= 3:
            return True
    return False


def _line_cover(g: np.ndarray, lines: list[float], need: int) -> float:
    """这组线横向有多大比例的地方至少 need 根是深色的（浅灰的谱线按 215 算）。"""
    ys = [round(y) for y in lines]
    # 每根线先在自己上下各一行里合并成一行再叠起来：线贴着图片上下边时切出来只有两行，直接叠形状对不上
    return float((np.stack([(g[max(0, y - 1) : y + 2] < 215).any(0) for y in ys]).sum(0) >= need).mean())


def _chord_diagrams(dark: np.ndarray, tab: list[float], sp: float, x0: int, x1: int) -> bool:
    """这一段“六线谱”其实是一两个、两三个挨着的和弦图：段很短（不到 16 个线距），里面贯穿上下的竖线（弦）
    又多又密（一个线距左右一根）。真正的短行（只有一个小节的末行）里只有两三条小节线。
    （和弦图上按弦的圆点会让竖线看着像扫弦箭头，所以这里数的是还没按箭头筛过的竖线）"""
    if x1 - x0 >= sp * 16:
        return False
    y_a, y_b = int(tab[0] + sp * 0.3), int(tab[-1] - sp * 0.3)
    cols = [(a + b) / 2 for a, b in _runs(dark[y_a:y_b, x0 : x1 + 1].mean(0) >= 0.93)]
    return len(cols) >= 5 and float(np.median(np.diff(cols))) < sp * 1.2


def _row_split(upper: dict, lower: dict, dark: np.ndarray | None = None) -> float:
    """相邻两行的分界：一般取中点。两行隔得很远时（弹唱谱：上一行下面有简谱和歌词，下一行上面有和弦图），
    下一行只要它上方的和弦图那一块（找得到的话），否则只要它上方 9 个线距（和弦名、和弦图、速度记号一般都在这个
    范围里），剩下的都归上一行——否则上一行的歌词会被拦腰切掉。"""
    mid = (upper["tab_bottom"] + lower["first_top"]) / 2
    if dark is not None:
        top = _diagram_block_above(dark, lower, upper["tab_bottom"])
        if top is not None:
            return max(top, upper["tab_bottom"] + upper["sp"])
    return max(mid, lower["first_top"] - lower["sp"] * 9)


def _diagram_block_above(dark: np.ndarray, row: dict, limit: float) -> float | None:
    """紧挨在这一行上方的一块和弦图（含上面的和弦名）的顶端；上方紧挨着的不是和弦图（歌词、简谱、文字）返回 None。
    从谱线往上：先跳过一段空白（不超过 3 个线距），再把行间空白不到 1 个线距的墨迹连成一块。
    和弦图里有贯穿好几个线距的竖线（弦），歌词、简谱、文字都没有。
    找到的那一块一直连到上一行谱线（limit）下面时，它是上一行挂下来的符干、横梁：返回两块之间那段空白的正中。"""
    sp, x0, x1 = row["sp"], max(0, row["x0"]), row["x1"]
    band = dark[:, x0 : x1 + 1]
    rows = band.sum(1) >= 3
    lo = max(0, int(limit))
    y = int(row["first_top"]) - 1
    # 连着谱线往上的墨迹（1 弦上伸出来的 ×、数字、连线；五线谱上方的符头、符干；画在谱表上方的节奏）先跳过，
    # 最多 5 个线距；再往上隔着一段空白的才可能是和弦图
    while y > lo and rows[y] and row["first_top"] - y <= sp * 5:
        y -= 1
    if rows[y]:
        return None
    gap, blank_bottom = 0, y
    while y > lo and not rows[y]:
        y -= 1
        gap += 1
        if gap > sp * 3:
            return None
    bottom, gap = y, 0
    top = y
    while y > lo:
        y -= 1
        if rows[y]:
            top, gap = y, 0
        else:
            gap += 1
            if gap > sp * 1.0:  # 和弦名（有的是手写体、浅灰色）和下面的格子之间隔得稍远
                break
    h = bottom - top
    if not sp * 2.5 <= h <= sp * 10:
        return None
    block = band[top : bottom + 1]
    # 每一列最长的连续墨迹（竖线）
    longest = 0
    run = np.zeros(block.shape[1], dtype=int)
    for r in block:
        run = (run + 1) * r
        longest = max(longest, int(run.max()))
    if longest < sp * 2.5:
        return None
    if top <= limit + sp:
        # 这一块一直连到上一行谱线下面：是上一行自己挂下来的长符干、横梁，不是和弦图。分界取在中间那段空白的正中
        return (bottom + blank_bottom) / 2
    return top - sp * 0.5


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


def _systems(g: np.ndarray, x_off: int = 0) -> list[dict]:
    """在一块（整页或双栏里的一栏）里找六线谱行；横坐标加上 x_off 换回整页坐标。"""
    dark = g < 200
    staves = _find_staves(g)
    systems = []
    for ti, tab in enumerate(staves):
        if len(tab) != 6:
            continue
        sp = _spacing(tab)
        # 同一行的五线谱：紧挨在六线谱上方的 5 线谱
        first = tab
        # 同一行的五线谱：六线谱上方 12 个线距以内、横跨大半行的 5 线谱。弹唱谱上方一排和弦图的格子、
        # 一组加线也会被当成 5 线谱，但它们只占一小部分宽度：跳过它们再往上找
        tab_cover = _line_cover(g, tab, 4)
        for j in range(ti - 1, max(-1, ti - 3), -1):
            cand = staves[j]
            if len(cand) != 5 or first[0] - cand[-1] >= sp * 12:
                break
            if _line_cover(g, cand, 3) >= 0.5 * tab_cover:
                first = cand
                break
        ys = [round(y) for y in tab]
        lines_dark = g < 215  # 谱线可能很浅（见 _staff_lines）
        on = np.stack([lines_dark[max(0, y - 1) : y + 2].any(0) for y in ys]).sum(0) >= 4
        for x0, x1 in _segments(on, sp, sp * 6):
            if _chord_diagrams(dark, tab, sp, x0, x1):
                continue
            # 小节线：六线谱上下线之间（掐头去尾）几乎全暗的竖列
            y_a, y_b = int(tab[0] + sp * 0.3), int(tab[-1] - sp * 0.3)
            colfrac = dark[y_a:y_b].mean(0)
            # 两端只量到谱线以内：线心在半像素上时（谱线外挨着一行浅灰），向内取整，不把谱线外面那行算进去
            ends = np.minimum(
                dark[int(np.ceil(tab[0])) : y_a + 1].mean(0), dark[y_b : int(tab[-1]) + 1].mean(0)
            )
            runs_bl = [
                (a, b)
                for a, b in _runs(colfrac >= 0.93)
                if x0 - 6 <= a <= x1 + 6 and _is_barline(dark, a, b, tab, sp, ends, colfrac)
            ]
            # 同一行里比别的小节线浅得多的竖线不是小节线：播放器截图里浅蓝色的播放光标（灰度 190 左右，
            # 刚好算进 dark）贯穿整个六线谱、上下一样宽，和小节线别的都一样。和这一行小节线灰度的中位数比
            # （缩小过的截图里细的小节线都只有 150–190，只有左边的谱表线是黑的：和最黑的那条比会把它们全丢掉）
            ink_of = {r: float(np.median(g[y_a:y_b, max(0, r[0] - 1) : r[1] + 1].min(1))) for r in runs_bl}
            if len(ink_of) >= 3:
                typical = float(np.median(list(ink_of.values())))
                runs_bl = [r for r in runs_bl if ink_of[r] < (255 + typical) * 0.6]
            bl = [(a + b - 1) / 2 for a, b in runs_bl]
            heavy = {round((a + b - 1) / 2) for a, b in runs_bl if b - a >= max(3, sp * 0.25)}  # 加粗的线
            if len(bl) >= 8 and float(np.median(np.diff(bl))) < sp * 2:
                continue  # 一排和弦图：格子的竖线一个线距左右一根，也是 6 根等距横线，不是六线谱
            merged: list[float] = []
            double: set[int] = set()  # 带粗线的双线（‖:、:‖、终止线）、单独一条粗线合并后的位置
            pair: set[int] = set()  # 两条线挨着合并成的双线（不管粗细，‖ 也算）
            for x in bl:  # 双小节线、终止线合并成一条
                if merged and x - merged[-1] < sp * 1.2:
                    pair.add(round(x))
                    if round(x) in heavy or round(merged[-1]) in heavy:
                        double.add(round(x))
                    merged[-1] = x
                else:
                    merged.append(x)
                    if round(x) in heavy:
                        double.add(round(x))
            if not merged or merged[0] - x0 > sp * 1.5:
                merged.insert(0, float(x0))
            if x1 - merged[-1] > sp * 1.5:
                merged.append(float(x1))
            # 中间断开的谱线（D.S. 之后另起一段 Coda、双栏之间）两头的线会框出一个“小节”：里面大半没有谱线的不算
            bars = [
                [int(a), int(b)]
                for a, b in itertools.pairwise(merged)
                if b - a > sp * 3 and on[int(a) : int(b) + 1].mean() >= 0.5
            ]
            # 行首是反复开始记号（‖:）时，谱号到它之间会多出一段（只有 TAB 谱号、拍号），不是小节：
            # 右边是双线、这一段又窄（不到 6 个线距），或者不到 10 个线距、右边是加粗的双线（行首的弱起小节后面
            # 不会是反复记号），就去掉。右边是普通单线的窄段是真小节（一个全音符和弦只有 5 个线距宽）
            if len(bars) > 1:
                w0 = bars[0][1] - bars[0][0]
                if (w0 < sp * 6 and any(abs(bars[0][1] - x) <= 2 for x in pair | double)) or (
                    w0 < sp * 10 and any(abs(bars[0][1] - x) <= 2 for x in double)
                ):
                    bars = bars[1:]
            if bars:
                systems.append(
                    {
                        "first_top": first[0],
                        "tab_bottom": tab[-1],
                        "sp": sp,
                        "x0": int(x0) + x_off,
                        "x1": int(x1) + x_off,
                        "bars": [[a + x_off, b + x_off] for a, b in bars],
                        "lines": [round(float(y), 1) for y in tab],
                    }
                )
    return systems


def _gutter(g: np.ndarray) -> tuple[int, int] | None:
    """双栏排版的栏间空白：在谱线所在的行里（左半页或右半页一大半是线），页面中间 25%–75% 一段几乎没有墨迹的竖条，
    返回最宽那段的（中线, 宽度）。单栏谱的谱线横穿页面中间，找不到（偶尔一根横贯全页的装饰线只占很少几行，不碍事）。"""
    dark = g < 215
    dark = dark[:-1] | dark[1:]
    w = dark.shape[1]
    half = w // 2
    rows = (dark[:, :half].mean(1) >= 0.5) | (dark[:, half:].mean(1) >= 0.5)
    if not rows.any():
        return None
    lo, hi = w // 4, w * 3 // 4
    runs = _runs(dark[rows][:, lo:hi].mean(0) <= 0.05)
    if not runs:
        return None
    a, b = max(runs, key=lambda r: r[1] - r[0])
    if b - a < max(8, w * 0.02):
        return None
    return lo + int(a + b) // 2, int(b - a)


def _columns(g: np.ndarray) -> list[dict] | None:
    """双栏排版：中间有一条贯穿所有谱线的空白时，左右两栏分开找行（两栏上下错开几个像素时，整页一起找
    谱线会交错在一起、一根也组不成谱表）。至少有两行左右两栏在同一高度（允许上下错开，只要纵向有重叠）
    才算双栏：只是一行谱中间隔开一段（Coda 前的空白）不算；栏间空白也要有 4 个线距宽（和 _segments 分段的标准一样）。
    不是双栏返回 None，按单栏处理。"""
    found = _gutter(g)
    if found is None:
        return None
    gut, width = found
    left, right = _systems(g[:, :gut]), _systems(g[:, gut:], gut)
    if not left or not right or width < 4 * max(s_["sp"] for s_ in left + right):
        return None
    pairs = sum(
        any(a["lines"][0] <= b["lines"][-1] and b["lines"][0] <= a["lines"][-1] for b in right) for a in left
    )
    if pairs < 2:
        return None
    return [dict(s_, col=0) for s_ in left] + [dict(s_, col=1) for s_ in right]


def detect_page(img: Image.Image) -> dict:
    """返回 {"width", "height", "systems": [...]}，每个 system 是一行六线谱：
    top/bottom：这一行（含上方五线谱和标记）的纵向范围；x0/x1：谱线横向范围；
    bars：小节横向范围列表 [[bx0, bx1], ...]；lines：六根弦线的纵坐标（1 弦在最上面）。"""
    g = np.asarray(img.convert("L"))
    h, w = g.shape
    if min(h, w) < 16:  # 太小，不可能有谱线（下面按行 / 列统计会遇到空数组）
        return {"width": w, "height": h, "systems": []}
    dark = g < 200
    systems = _columns(g) or [dict(s_, col=0) for s_ in _systems(g)]
    # 阅读顺序：双栏时先左栏从上到下，再右栏；同一行被空白（如 Coda 前）分成几段时从左到右
    systems.sort(key=lambda s_: (s_["col"], s_["tab_bottom"], s_["x0"]))
    # 找行与行的分界时只看真正的黑色印刷（谱子照片上斜着的灰色水印、灰底不算）
    ink = g < 140
    # 纵向范围：同一栏里相邻两行之间取中点；首行向上、末行向下各留一行谱高度左右的余量。
    # 同一行被空白分成的几段（Coda 前）互相不算上下相邻：各自找正上方、正下方的行
    for s_ in systems:
        same = [t for t in systems if t["col"] == s_["col"]]
        above = [t for t in same if t["tab_bottom"] < s_["first_top"]]
        below = [t for t in same if t["first_top"] > s_["tab_bottom"]]
        prev = max(above, key=lambda t: (t["tab_bottom"], t["x0"])) if above else None
        nxt = min(below, key=lambda t: (t["first_top"], t["x0"])) if below else None
        height = s_["tab_bottom"] - s_["first_top"]
        top = (
            _row_split(prev, s_, ink)
            if prev
            else min(
                s_["first_top"] - min(height * 0.6, s_["sp"] * 9),
                _content_above(dark, s_["first_top"], s_["x0"], s_["x1"], s_["sp"]),
            )
        )
        bottom = (
            _row_split(s_, nxt, ink)
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
