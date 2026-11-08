"""五线谱对六线谱：在本机比对五线谱上的音高和 AI 读出的（弦, 品），挑出可能读错的小节。

不调用大模型、不花钱。五线谱 + 六线谱的谱子里，五线谱上的每个符头和下面六线谱的数字是同一个音：
1. 找出六线谱正上方的五线谱（版面检测只给了六线谱的位置）；
2. 用形状找符头：去掉谱线和加线，留下够粗的块，再用一个符头大小的椭圆做开运算去掉符干、符杠；
   空心符头先把小的洞填上。符头中心离五线谱最下面一线的距离（半个线距一级）就是音级；
3. 吉他谱用高八度记谱的高音谱号（treble 8vb）：记谱音高减 12 是实际音高；
4. 调号和临时记号不在图上认：只比字母（允许 ±1 个半音）；
5. 六线谱上的数字只按几何找竖列（哪几根弦有数字、是不是带括号），不读数字（tab_columns）；
6. check_bar：AI 的起拍按先后和数字列做序列对齐，逐列比弦和五线谱音高，
   图上多出来的列（AI 漏了的起拍）和 AI 多出来的起拍也报出来。

只用来挑出值得再核对的小节、给核对的模型提供线索，不直接改结果：符头检测会漏（挤在一起的和弦、
倚音、被符杠压住的），也会把力度记号、休止符当成符头。思路参考 scan-to-gp（MIT）的 inspect_crop.py。
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from app import layout

DARK = 170  # 比这个暗的像素算墨迹
LETTERS = "CDEFGAB"
_NAT = [0, 2, 4, 5, 7, 9, 11]  # C D E F G A B 的半音数
E4_STEP = 4 * 7 + 2  # 高音谱号最下面一线是 E4（音级按 C0 = 0 往上数白键）


@dataclass
class Head:
    x: float  # 页面坐标
    y: float
    step: int  # 0 = 五线谱最下面一线，偶数在线上、奇数在间里；负数在下加线
    w: float
    h: float

    @property
    def diatonic(self) -> int:
        return E4_STEP + self.step

    def natural_midi(self) -> int:
        """记谱的本位音高（不算升降号）。"""
        d = self.diatonic
        return 12 * (d // 7 + 1) + _NAT[d % 7]

    @property
    def letter(self) -> int:
        return self.diatonic % 7


# ---------------------------------------------------------------------------
# 五线谱和符头
# ---------------------------------------------------------------------------
def find_staff(gray: np.ndarray, tab: list[float], x0: int, x1: int) -> list[float] | None:
    """六线谱正上方的五线谱（5 根线的 y，页面坐标）；没有（纯六线谱）返回 None。
    只在这一行的横向范围、六线谱上方 18 个线距以内找；线距要在六线谱线距的 0.5–0.9 倍之间
    （软件导出的谱子五线谱线距大约是六线谱的 0.7 倍；纯六线谱时找到的“5 根线”是上一行六线谱的下面 5 根）。"""
    sp_tab = (tab[-1] - tab[0]) / 5
    top = max(0, int(tab[0] - sp_tab * 18))
    bot = int(tab[0] - sp_tab * 0.5)
    if bot - top < sp_tab * 4:
        return None
    sub = gray[top:bot, max(0, x0) : x1]
    best = None
    for thr in (None, 0.35, 0.25):
        lines = layout._staff_lines(sub, thr)
        groups = layout._group_staves(lines, sub.shape[0], max(sub.shape[1], 1))
        for g in groups:
            if len(g) != 5:
                continue
            sp = (g[-1] - g[0]) / 4
            if not 0.5 * sp_tab <= sp <= 0.9 * sp_tab:
                continue
            if best is None or g[-1] > best[-1]:
                best = g
        if best:
            break
    return [top + y for y in best] if best else None


def _ellipse(h: int, w: int) -> np.ndarray:
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (max(3, w), max(3, h)))


def find_heads(gray: np.ndarray, staff: list[float], x0: int, x1: int, y_hi: float) -> list[Head]:
    """五线谱上的符头（页面坐标）。y_hi：往下最多找到哪里（六线谱上方，别把品位数字当符头）。"""
    d = (staff[-1] - staff[0]) / 4
    y0 = max(0, int(staff[0] - d * 4.5))
    y1 = min(gray.shape[0], int(min(staff[-1] + d * 7, y_hi)))
    x0 = max(0, int(x0))
    g = gray[y0:y1, x0:x1]
    ink = (g < DARK).astype(np.uint8)
    # 只留纵向够厚的部分（去掉谱线、加线、细的连线）
    t = max(3, round(0.4 * d))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, t))
    body = cv2.morphologyEx(ink, cv2.MORPH_OPEN, vk)
    # 空心符头（二分、全音符）：符头大小的洞填上，再补上紧挨着洞的一圈墨迹（洞上下的细边被上一步去掉了）。
    # 只补紧挨着的一圈：补多了升号（中间也有个小洞）会变成实心方块，和旁边的符头粘在一起
    n, lab, stats, _ = cv2.connectedComponentsWithStats(1 - ink, connectivity=4)
    ok = np.zeros(n, bool)
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        # 空心符头的洞是扁的（宽比高大）；升号、还原号、降号中间的洞是方的或竖的
        ok[i] = 0.08 * d * d <= area <= 0.8 * d * d and h * 1.25 <= w <= 1.2 * d and x > 0 and y > 0
    small = ok[lab].astype(np.uint8)
    if small.any():
        grown = cv2.dilate(small, np.ones((3, 3), np.uint8), iterations=max(1, round(0.15 * d)))
        body = body | small | (ink & grown)
    # 符杠：横向连续 2.8 个线距以上的粗横条（符头最宽也就两个错开的二度、2.4 个线距左右）
    beam = cv2.morphologyEx(
        body, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (int(2.8 * d), 1))
    )
    body = body & (1 - beam)
    # 一个符头大小的椭圆能放进去的地方才算符头：去掉符干和大部分符杠
    eh, ew = max(3, int(0.55 * d)), max(3, int(0.8 * d))
    heads = cv2.morphologyEx(body, cv2.MORPH_OPEN, _ellipse(eh, ew))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(heads, connectivity=8)
    bottom = staff[-1]
    # 谱线所在的行（这一段横向一大半是墨迹的行）：算符头中心时不用（符头一边挨着谱线时，开运算会把一小段谱线
    # 并进来，中心往线那边偏；线有两个像素粗时按理想位置排除又不对称）
    on_line = ink.mean(1) >= 0.5
    out: list[Head] = []
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        if w < 0.7 * d or h < 0.55 * d or w > 2.4 * d or h > 4.5 * d:
            continue
        if area < 0.45 * w * h:  # 斜的符杠、弧线留下的细长块
            continue
        blob = lab[y : y + h, x : x + w] == i
        if w > 1.7 * d:  # 两个音（二度）左右错开挨在一起：拆成左右两半
            half = w // 2
            parts = [(0, half), (w - half, w)]
            ys = [np.nonzero(blob[:, a_:b_].any(1))[0] for a_, b_ in parts]
            if len(ys[0]) and len(ys[1]) and abs(ys[0].mean() - ys[1].mean()) < 0.25 * d:
                continue  # 左右两半一样高：一段符杠，不是错开的二度
        else:
            parts = [(0, w)]
        for a_, b_ in parts:
            sub = blob[:, a_:b_]
            rows = np.nonzero(sub.any(1))[0]
            if not len(rows):
                continue
            r0, r1 = int(rows[0]), int(rows[-1]) + 1
            hh = r1 - r0
            k = max(1, int((hh + 0.25 * d) / (0.95 * d)))  # 叠在一起的和弦音（三度）；一个符头带着升降号不拆
            for j in range(k):
                c0, c1 = r0 + j * hh // k, r0 + (j + 1) * hh // k
                weights = sub[c0:c1].sum(1).astype(float)
                weights[on_line[y + c0 : y + c1]] = 0
                weights[weights < 0.35 * (b_ - a_)] = 0  # 只剩符干那么窄的行
                if weights.sum() > 0:
                    cy = y0 + y + c0 + float((np.arange(c1 - c0) * weights).sum() / weights.sum()) + 0.5
                else:
                    cy = y0 + y + (c0 + c1) / 2
                step = round((bottom - cy) / (d / 2))
                hx = x0 + x + (a_ + b_) / 2
                if (step >= 10 or step <= -2) and not _ledger(
                    ink, staff[0] - d if step >= 10 else bottom + d, hx, d, x0, y0
                ):
                    continue  # 五线谱外面的符头一定有加线；没有的是记号、文字
                out.append(Head(hx, cy, step, b_ - a_, hh / k))
    out.sort(key=lambda hd: (hd.x, -hd.step))
    return out


def _ledger(ink: np.ndarray, ly: float, hx: float, d: float, x0: int, y0: int) -> bool:
    """离五线谱最近的那根加线在不在（符头左右各伸出去一点的一段横线）。"""
    r = round(ly) - y0
    a, b = int(hx - 0.8 * d) - x0, int(hx + 0.8 * d) - x0
    if r < 1 or r + 2 > ink.shape[0] or a < 0 or b > ink.shape[1]:
        return False
    return bool((ink[r - 1 : r + 2, a:b].mean(1) >= 0.8).any())


@dataclass
class Column:
    """六线谱上的一竖列数字（同一时刻弹的音）。"""

    x: float  # 页面坐标
    strings: frozenset[int]  # 有数字的弦（1 = 最上面一线）
    paren: bool  # 这一列的数字全都带括号：连线连过来的音，不是新弹的


def tab_columns(gray: np.ndarray, tab: list[float], x0: int, x1: int) -> list[Column]:
    """六线谱上品位数字所在的竖列。只看几何形状（数字块的筛选和 ocr.read_system 一样），不读数字。
    同一根弦上紧挨着的块是一个多位数；数字左边紧挨着一个细高的块（“(”）算带括号。"""
    sp = (tab[-1] - tab[0]) / 5
    top, bot = max(0, int(tab[0] - sp * 0.9)), int(tab[-1] + sp * 0.9)
    x0 = max(0, int(x0))
    g = gray[top:bot, x0:x1]
    dark = (g < 160).astype(np.uint8)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(8, int(sp * 1.2)), 1))
    horiz = cv2.morphologyEx(dark, cv2.MORPH_OPEN, kernel)
    clean = dark & (1 - horiz)
    n, _, stats, _ = cv2.connectedComponentsWithStats(clean, connectivity=8)
    digits, parens = [], []
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        cy = top + y + h / 2
        k = int(np.argmin([abs(cy - ly) for ly in tab]))
        if abs(cy - tab[k]) > 0.38 * sp:
            continue
        if 0.5 * sp <= h <= 1.15 * sp and 0.22 * sp <= w <= 1.2 * sp and area >= max(4, 0.2 * w * h):
            # 太细的是伸进六线谱的符干、小节线的一段；太空的是连线、延音线的弧
            digits.append([x0 + x, x0 + x + w, k + 1])
        elif 0.95 * sp <= h <= 1.6 * sp and 0.9 * sp <= w <= 3 * sp:
            # 带括号的数字：括号比数字高，和数字之间那一小段谱线去不掉，三个连成一块
            parens.append((x0 + x, x0 + x + w, k + 1))
        elif 0.95 * sp <= h <= 1.6 * sp and w <= 0.45 * sp:
            parens.append((x0 + x, x0 + x + w, k + 1))  # 单独的“(”
    digits.sort(key=lambda d: (d[2], d[0]))
    numbers: list[list] = []
    for d in digits:  # 同一根弦上紧挨着的块拼成一个数
        if numbers and numbers[-1][2] == d[2] and d[0] - numbers[-1][1] <= max(2, 0.25 * sp):
            numbers[-1][1] = max(numbers[-1][1], d[1])
        else:
            numbers.append(list(d))
    marked = []
    for a, b, s_ in numbers:
        par = any(ps == s_ and a - 0.6 * sp <= pb <= a + 1 for pa, pb, ps in parens)
        marked.append(((a + b) / 2, s_, par))
    # 连成一块的“(数字)”自己算一列；行首的 TAB 谱号（三个字母也是比数字高的块）不算
    start = x0 + sp * 2.2
    marked += [((a + b) / 2, s_, True) for a, b, s_ in parens if b - a >= 0.9 * sp and a >= start]
    marked.sort()
    groups: list[list] = []
    for m in marked:
        if groups and m[0] - groups[-1][-1][0] <= sp * 0.9:
            groups[-1].append(m)
        else:
            groups.append([m])
    return [
        Column(float(np.mean([m[0] for m in c])), frozenset(m[1] for m in c), all(m[2] for m in c))
        for c in groups
    ]


def heads_in(heads: list[Head], span: tuple[float, float]) -> list[Head]:
    a, b = span
    return [h for h in heads if a <= h.x < b]


# ---------------------------------------------------------------------------
# 音高比对
# ---------------------------------------------------------------------------
def note_pitches(n: dict, open_midi: list[int], capo: int) -> set[int]:
    """一个音可能的实际音高（自然泛音、人工泛音、点弦泛音时五线谱可能写泛音的音高，也可能写按的品）。"""
    base = open_midi[n["string"] - 1] + capo
    m = base + n["fret"]
    out = {m}
    tech = n.get("tech") or []
    if "nh" in tech:
        out |= {base + _harm(n["fret"])}
    if "ah" in tech:
        out |= {m + 12, m + 19, m + 24}
    if "th" in tech and n.get("harm"):
        out |= {m + _harm(n["harm"] - n["fret"])}
    return out


def _harm(fret: int) -> int:
    """自然泛音比空弦高几个半音。"""
    return {12: 12, 7: 19, 19: 19, 5: 24, 24: 24, 4: 28, 9: 28, 16: 28, 3: 31}.get(fret, fret)


def head_ok(head: Head, pitches: set[int]) -> bool:
    """符头和某个实际音高对得上：记谱音高 = 实际音高 + 12，升降号不在图上认，允许 ±1 个半音（只认字母）。
    同一根弦品位差一两品的错大多会换字母；只差一个半音的错（很少见）看不出来。"""
    nat = head.natural_midi() - 12
    return any(p - nat in (-1, 0, 1) for p in pitches)


GAP = 1.5  # 对齐时跳过一列数字或 AI 的一个起拍的代价（一个音对不上算 1）


def check_bar(
    cols: list[Column],
    heads: list[Head],
    events: list[dict] | None,
    open_midi: list[int],
    capo: int,
    tol: float,
    prev_events: list[dict] | None = None,
    sp: float = 0.0,
    span: tuple[float, float] | None = None,
) -> dict:
    """按起拍逐个比。sp：六线谱线距；span：小节的横向范围（对齐时位置差得远的稍微加一点代价，分不出时按位置挑）。cols：这个小节里六线谱的数字列（从左到右）；heads：这个小节的符头（没有五线谱时为空）。

    AI 的起拍（有音的事件）按先后和图上的数字列做序列对齐（动态规划）：一对的代价是弦对不上的个数
    （图上这一列哪几根弦有数字 vs AI 这个起拍用了哪几根弦），加上这一列的符头里 AI 这个起拍解释不了的个数；
    跳过一列或一个起拍各算 GAP。带括号的列（连线连过来的音）不参加对齐。
    返回 {"pairs": [(起拍序号, 列序号, 弦对不上, 解释不了的符头)], "extra_cols": [...], "extra_onsets": [...],
    "string_miss": 总数, "staff_miss": 总数}。"""
    onsets = [e["notes"] for e in events or [] if e.get("notes")]
    live = [c for c in cols if not c.paren]
    # 每个符头归到最近的一列
    col_heads: list[list[Head]] = [[] for _ in live]
    if live:
        xs = np.asarray([c.x for c in live])
        for h in heads:
            j = int(np.argmin(np.abs(xs - h.x)))
            if abs(xs[j] - h.x) <= tol:
                col_heads[j].append(h)
    pitches = [[note_pitches(n, open_midi, capo) for n in o] for o in onsets]
    # 挨着旁边一列（1.8 个线距以内）、用的弦是旁边那列的一部分：倚音、滑音的起点、点弦泛音的 <12>，
    # AI 常把它们写进同一个起拍（grace、th），对齐时跳过它不算钱
    satellite = [
        any(
            0 <= jj < len(live) and abs(live[jj].x - c.x) <= 1.8 * sp and c.strings <= live[jj].strings
            for jj in (j - 1, j + 1)
        )
        for j, c in enumerate(live)
    ]
    graces = [any(n.get("grace", -1) >= 0 for n in o) for o in onsets]

    prev_all = [note_pitches(nn, open_midi, capo) for e in prev_events or [] for nn in e.get("notes") or []]

    # 起拍在小节里的位置（按时值）和数字列的位置（按横坐标）都折成 0–1
    from fractions import Fraction

    t, starts = Fraction(0), []
    for e in events or []:
        if e.get("notes"):
            starts.append(t)
        t += Fraction(str(e["dur"]))
    t_frac = [float(x / t) if t else 0.0 for x in starts]
    a, b = span if span else ((live[0].x - sp, live[-1].x + sp) if live else (0.0, 1.0))
    x_frac = [(c.x - a) / (b - a) if b > a else 0.0 for c in live]

    def pair_cost(i: int, j: int) -> tuple[int, int]:
        ss = {n["string"] for n in onsets[i]}
        smiss = len(ss ^ set(live[j].strings))
        # 前面起拍的音（含上一小节）也算：另一个声部用连线延续下来的音，五线谱上又画了一个符头，六线谱上不写
        ok = pitches[i] + [p for o in pitches[:i] for p in o] + prev_all
        hmiss = sum(1 for h in col_heads[j] if not any(head_ok(h, p) for p in ok))
        return smiss, hmiss

    n, m = len(onsets), len(live)
    inf = float("inf")
    dp = [[inf] * (m + 1) for _ in range(n + 1)]
    back: list[list] = [[None] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            if dp[i][j] == inf:
                continue
            if i < n and j < m:
                sm, hm = pair_cost(i, j)
                # 一对再差也比“跳过一列 + 跳过一个起拍”便宜一点：个数对得上时按位置配对
                v = dp[i][j] + min(sm + hm, 2 * GAP - 0.5) + 0.5 * abs(t_frac[i] - x_frac[j])
                if v < dp[i + 1][j + 1]:
                    dp[i + 1][j + 1], back[i + 1][j + 1] = v, ("pair", sm, hm)
            if j < m:  # 图上多一列：倚音（下一个起拍有倚音、或者这一列挨着旁边一列时不算钱）
                v = dp[i][j] + (0.2 if (i < n and graces[i]) or satellite[j] else GAP)
                if v < dp[i][j + 1]:
                    dp[i][j + 1], back[i][j + 1] = v, ("col",)
            if i < n:
                v = dp[i][j] + GAP
                if v < dp[i + 1][j]:
                    dp[i + 1][j], back[i + 1][j] = v, ("onset",)
    pairs, extra_cols, extra_onsets = [], [], []
    i, j = n, m
    while i or j:
        b = back[i][j]
        if b[0] == "pair":
            i, j = i - 1, j - 1
            pairs.append((i, j, b[1], b[2]))
        elif b[0] == "col":
            j -= 1
            if not ((i < n and graces[i]) or satellite[j]):
                extra_cols.append(j)
        else:
            i -= 1
            extra_onsets.append(i)
    pairs.reverse()
    # 多出来的列不因为音高和前面一样而原谅：不带括号的数字是新弹的音（连线连过来的音带括号，不参加对齐）
    extra_cols.sort()
    return {
        "pairs": pairs,
        "extra_cols": extra_cols,
        "extra_onsets": sorted(extra_onsets),
        "string_miss": sum(p[2] for p in pairs),
        "staff_miss": sum(p[3] for p in pairs),
        "cols": m,
        "onsets": n,
        "col_heads": col_heads,
        "live": live,
        "onset_notes": onsets,
        "onset_pitches": pitches,
    }


def _strings(ss) -> str:
    return "、".join(str(x) for x in sorted(ss)) + " 弦"


def hints(r: dict, events: list[dict] | None) -> list[str]:
    """check_bar 的结果写成给核对模型看的线索（中文短句）：哪个起拍（按拍数）、图上看到了什么、AI 写的是什么。
    只说对不上的地方，不给答案（数字本身没读，五线谱的升降号也没认）。"""
    from fractions import Fraction

    from app.music import midi_to_name

    beats, t = [], Fraction(0)
    for e in events or []:
        if e.get("notes"):
            beats.append(t)
        t += Fraction(str(e["dur"]))

    def at(i: int) -> str:
        return f"第 {float(beats[i]) + 1:g} 拍" if i < len(beats) else f"第 {i + 1} 个起拍"

    out = []
    for i, j, sm, hm in r["pairs"]:
        mine = {n["string"] for n in r["onset_notes"][i]}
        if sm:
            out.append(
                f"{at(i)}：图上这一列 {_strings(r['live'][j].strings)}有数字，你写的是 {_strings(mine)}"
            )
        if hm:
            ok = [p for k in range(i + 1) for p in r["onset_pitches"][k]]
            miss = [h for h in r["col_heads"][j] if not any(head_ok(h, p) for p in ok)]
            names = "、".join(sorted({midi_to_name(h.natural_midi() - 12) for h in miss}))
            out.append(f"{at(i)}：五线谱这里有 {names} 附近的音（不计升降），你这个起拍里没有")
    for j in r["extra_cols"]:
        c = r["live"][j]
        prev = [i for i, jj, *_ in r["pairs"] if jj < j]
        where = f"{at(prev[-1])}之后" if prev else "小节开头"
        out.append(f"{where}图上还有一列 {_strings(c.strings)}的数字，你的结果里没有对应的起拍")
    for i in r["extra_onsets"]:
        out.append(
            f"{at(i)}（{_strings({n['string'] for n in r['onset_notes'][i]})}）在图上找不到对应的一列数字"
        )
    return out
