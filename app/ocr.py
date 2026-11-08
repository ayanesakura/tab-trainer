"""在本机读六线谱上的品位数字，用来校对 AI 的识别结果。

不调用大模型、不花钱：版面检测已经知道每根弦线的位置和每个小节的横向范围，这里只做两件事——
1. 去掉谱线后，找出中心压在某根弦线上的数字块，挨着的拼成一个数（"12"）；
2. 用 PaddleOCR 的识别模型（ONNX）读这个数，解码时只允许 0–9 和括号、x（括号、x 最后去掉，不让模型把它们硬读成数字）。
数字落在哪根线上就是哪根弦，这是几何判断，不会像大模型那样把数字往下错一根弦。
准确率用 scripts/ocr_bench.py 测（按评测曲存好的版面逐小节比（弦, 品），精确率 / 召回率）：
11 首干净的评测曲合计 98.0% / 98.9%（勿听、Romance 都是 100% / 100%；只有六线谱、符干画在谱线中间的
Mother-tab 最差，91.6% / 95.4%），8 首降质版本合计 99.1% / 98.4%。模型用 PP-OCRv5 mobile：数字和 v4 差不多
（降质版本召回率高 0.3 个百分点），括号读得准得多（见 Digit）。
read_system(detail=True) 还报每个数字两边有没有圆括号 / 尖括号（鬼音、延续音、泛音），见 Digit。

思路参考 PDF-to-GP5（MIT）的图片识别；它自己的谱线检测在我们的谱子上很不稳定，所以版面用我们自己的。
读不准的情况（数字很淡、和别的符号粘在一起）宁可不报：调用方只拿它来提醒和返修，不直接改结果。
"""

from __future__ import annotations

import logging
import threading
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

import numpy as np

log = logging.getLogger("tab_trainer.ocr")

MODEL = Path(__file__).parent / "models" / "ppocr_v5_mobile_rec.onnx"
DARK = 160  # 比这个暗的像素算笔画
# 浅灰色的笔画（有的软件把第二声部画成灰色，截图压缩、模糊后更浅）：比这个暗、整块都没有比 DARK 暗的像素，也算笔画。
# 只收整块都浅的，黑色数字模糊出来的灰边不会让数字变粗、粘连；0 = 不收
LIGHT = 215
GLYPH_HEIGHT = 48  # 送进识别模型的高度（模型按 48 训练；太小的 0 会被读成别的）
MAX_FRET = 24
BRACKETS = {
    "(": "(",
    "（": "(",
    ")": ")",
    "）": ")",
    "<": "<",
    "〈": "<",
    "《": "<",
    ">": ">",
    "〉": ">",
    "》": ">",
    # 死音的 x 也让它读成 x（不然有的模型会把它读成 8），和括号一样最后去掉
    "x": "x",
    "X": "x",
    "×": "x",
}
# 数字块连同括号一起读：块里常混着括号（"(5)"、"<7>" 的括号和数字挨得近，会拼进同一块；单独的括号也像 1），
# 只许读数字时模型只好把括号也读成数字（"<7>" → 77、")" → 1）。让它可以读成括号，再把括号去掉
MARKS_IN_BLOCK = True
_lock = threading.Lock()


class Digit(NamedTuple):
    """read_system(detail=True) 读到的一个音。"""

    string: int
    fret: int
    x: float  # 数字块中心的横坐标（裁剪图坐标）
    paren: bool = False  # 数字两边有圆括号 (n)：鬼音、小节开头的延续音
    angle: bool = False  # 数字两边有尖括号 <n>：泛音（自然泛音的品、人工 / 点弦泛音的标注）
    # 评测（scripts/ocr_bench.py --gp-truth，只算数字读到了的）：干净的谱子 paren 精确率 99.7% / 召回率 98.2%、
    # angle 96.3% / 100%；降质的 paren 99.0% / 73.7%、angle 94.4% / 96.0%。报了基本可信，没报不代表没有


class _Recognizer:
    def __init__(self, model: Path):
        import onnxruntime as ort

        self.session = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
        chars = self.session.get_modelmeta().custom_metadata_map["character"].splitlines()
        # 输出第 0 类是 CTC 的空白，第 i+1 类是字表里第 i 个字符
        # 小号的 0 常被模型当成字母 O 或句号：这几个字形也算 0，否则只在数字里选时 0 会输给“空白”
        as_zero = {"O", "o", "〇", "。", "D", "Q", "○"}
        self.digit_class = {
            i + 1: ("0" if c in as_zero else c)
            for i, c in enumerate(chars)
            if c in "0123456789" or c in as_zero
        }
        self.keep = np.array([0, *self.digit_class])
        # 读括号时再加上圆括号、尖括号（全角的也算）和死音的 x
        self.mark_class = {
            **self.digit_class,
            **{i + 1: BRACKETS[c] for i, c in enumerate(chars) if c in BRACKETS},
        }
        self.keep_marks = np.array([0, *self.mark_class])
        self.input = self.session.get_inputs()[0].name

    def digits(self, glyph: np.ndarray) -> str:
        """glyph：白底黑字的灰度图。返回读到的数字串（可能为空）。"""
        return self._decode(glyph, self.keep, self.digit_class)

    def marks(self, glyph: np.ndarray) -> str:
        """和 digits 一样，但也读括号和 x：返回如 "(5)"、"<12>"、"x"。"""
        return self._decode(glyph, self.keep_marks, self.mark_class)

    def _decode(self, glyph: np.ndarray, keep: np.ndarray, cls: dict[int, str]) -> str:
        import cv2

        h, w = glyph.shape
        width = max(1, round(w * GLYPH_HEIGHT / h))
        img = cv2.resize(glyph, (width, GLYPH_HEIGHT), interpolation=cv2.INTER_CUBIC).astype(np.float32)
        img = (img / 255 - 0.5) / 0.5
        # 和 PaddleOCR 的预处理一致：右边补到至少 320 宽（补的值是归一化后的 0），模型是这样训练的
        canvas = np.zeros((GLYPH_HEIGHT, max(320, width)), dtype=np.float32)
        canvas[:, :width] = img
        x = np.repeat(canvas[None, None], 3, axis=1)  # 1×3×H×W
        probs = self.session.run(None, {self.input: x})[0][0]  # T×classes
        best = keep[probs[:, keep].argmax(axis=1)]  # 每一步只在“空白 + 数字”里选
        out, prev = [], 0
        for k in best:
            if k != 0 and (k != prev):
                d = cls[int(k)]
                if not (out and prev != 0 and out[-1] == d):  # O 和 0 两类连着出现也只算一个字
                    out.append(d)
            prev = k
        return "".join(out)


@lru_cache(maxsize=1)
def _recognizer() -> _Recognizer | None:
    try:
        return _Recognizer(MODEL)
    except Exception as e:  # noqa: BLE001 - 缺依赖 / 模型文件时整个校对功能关闭，不影响识别
        log.warning("本地数字识别不可用：%s", e)
        return None


def available() -> bool:
    return MODEL.exists() and _recognizer() is not None


def read_system(
    gray: np.ndarray, lines: list[float], spans: list[tuple[float, float]], detail: bool = False
) -> list[list[tuple[int, int]]] | list[list[Digit]] | None:
    """读一行六线谱。gray：这一行的灰度裁剪图；lines：6 根弦线的 y；spans：每个小节的 [x0, x1)。
    返回每个小节读到的 [(弦, 品), ...]（按弦、再按横坐标排序）；不可用时返回 None。
    detail=True 时每个音换成 Digit（多了横坐标和括号 / 尖括号标记），读到的音和顺序完全一样；
    数字块本身没带上括号的，要连同左右再读一遍，大约多花一倍时间。
    括号标记的准确率见 scripts/ocr_bench.py --gp-truth（(n)：鬼音、小节开头的延续音；<n>：泛音）。"""
    rec = _recognizer()
    if rec is None or len(lines) != 6 or not spans:
        return None
    import cv2

    sp = (lines[-1] - lines[0]) / 5
    if sp < 4:
        return None
    dark = gray < DARK
    if LIGHT > DARK:
        weak = gray < LIGHT
        nw, wl = cv2.connectedComponents(weak.astype(np.uint8), connectivity=8)
        has_dark = np.zeros(nw, dtype=bool)
        has_dark[np.unique(wl[dark])] = True
        dark |= weak & ~has_dark[wl]
    # 去掉谱线：横向长度超过 1.2 个线距的连续暗像素
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(8, int(sp * 1.2)), 1))
    horiz = cv2.morphologyEx(dark.astype(np.uint8), cv2.MORPH_OPEN, kernel).astype(bool)
    clean = dark & ~horiz
    top, bot = max(0, int(lines[0] - sp * 0.7)), int(lines[-1] + sp * 0.7)
    region = np.zeros_like(clean)
    region[top:bot] = clean[top:bot]
    n, lab, stats, _ = cv2.connectedComponentsWithStats(region.astype(np.uint8), connectivity=8)

    found = []
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        # 数字大约一个线距高；排除谱号 TAB（在第一个小节左边）、滑音斜线（又短又宽）、小节线和弧线
        if not (
            0.5 * sp <= h <= 1.15 * sp and w <= 1.2 * sp and w <= 1.1 * h and area >= 4 and x >= spans[0][0]
        ):
            continue
        k = int(np.argmin([abs(y + h / 2 - ly) for ly in lines]))
        if abs(y + h / 2 - lines[k]) <= 0.38 * sp:
            found.append({"x": x, "y": y, "w": w, "h": h, "s": k + 1, "ids": [i]})
    found.sort(key=lambda c: (c["s"], c["x"]))

    numbers: list[dict] = []
    for c in found:  # 同一根弦上紧挨着的块是一个多位数
        p = numbers[-1] if numbers else None
        if p and p["s"] == c["s"] and c["x"] - (p["x"] + p["w"]) <= max(2, 0.25 * sp):
            x0, y0 = min(p["x"], c["x"]), min(p["y"], c["y"])
            x1, y1 = max(p["x"] + p["w"], c["x"] + c["w"]), max(p["y"] + p["h"], c["y"] + c["h"])
            p.update(x=x0, y=y0, w=x1 - x0, h=y1 - y0, ids=p["ids"] + c["ids"])
        else:
            numbers.append(dict(c))

    out: list[list] = [[] for _ in spans]
    pad = max(4, int(sp * 0.4))
    owner = {i: k for k, c in enumerate(numbers) for i in c["ids"]}
    for k, c in enumerate(numbers):
        sub = lab[c["y"] : c["y"] + c["h"], c["x"] : c["x"] + c["w"]]
        mask = np.isin(sub, c["ids"])
        glyph = np.pad(np.where(mask, 0, 255).astype(np.uint8), pad, constant_values=255)
        with _lock:
            raw = rec.marks(glyph) if MARKS_IN_BLOCK else rec.digits(glyph)
        t = "".join(ch for ch in raw if ch.isdigit())
        if not t:
            continue
        frets = [int(t)] if int(t) <= MAX_FRET else ([int(d) for d in t] if len(t) <= 3 else [])
        if t == "1" and not _upright(mask, c, lines, sp, clean, (top, bot)):
            continue
        xc = c["x"] + c["w"] / 2
        for i, (a, b) in enumerate(spans):
            if a <= xc < b:
                if detail:
                    paren, angle = _pair(raw, "(", ")"), _pair(raw, "<", ">")
                    if not (paren or angle):
                        paren, angle = _brackets(rec, region, lab, owner, k, c, pad)
                    out[i] += [Digit(c["s"], f, xc, paren, angle) for f in frets]
                else:
                    out[i] += [(c["s"], f) for f in frets]
    return out


def _brackets(rec, region, lab, owner: dict, k: int, c: dict, pad: int) -> tuple[bool, bool]:
    """数字两边有没有圆括号 / 尖括号：把数字连同左右各约一个字宽的范围（去掉别的数字块）再读一遍，
    这次允许读出括号。上下也放宽一点：括号比数字高，竖着叠在一起的几个 (n) 的括号常连成一长条。"""
    h, w = region.shape
    ext = round(0.9 * c["h"])
    x0, x1 = max(0, c["x"] - ext), min(w, c["x"] + c["w"] + ext)
    y0, y1 = max(0, c["y"] - int(0.3 * c["h"])), min(h, c["y"] + c["h"] + int(0.3 * c["h"]) + 1)
    sub = lab[y0:y1, x0:x1]
    ink = region[y0:y1, x0:x1] & ~np.isin(sub, [i for i, o in owner.items() if o != k])
    glyph = np.pad(np.where(ink, 0, 255).astype(np.uint8), pad, constant_values=255)
    with _lock:
        t = rec.marks(glyph)
    # 括号里的数字这次可能读错（"(0)" 读成 "()"）：只看括号，数字以第一遍读的为准
    return _pair(t, "(", ")"), _pair(t, "<", ">")


def _pair(t: str, a: str, b: str) -> bool:
    return a in t and t.rfind(b) > t.find(a)


def _upright(mask: np.ndarray, c: dict, lines: list[float], sp: float, clean: np.ndarray, band) -> bool:
    """读成 1 的块要真是竖着的一笔：滑音斜线、弧线的末端也常被读成 1。
    也不能是一段符干：只有六线谱的谱子（符干画在谱线中间）里，符干被弦线、数字截成一段一段，夹在两根弦线
    之间的一段也像 1。符干的特点是上下还接着别的竖段（或被裁到弦线范围边上），而且粗细上下一样；
    印刷体的 1 顶上有向左的小钩（或底下有横），总有一头比中间宽。两条都占才当符干扔掉（模糊的小 1 没有钩）。"""
    ys, xs = np.nonzero(mask)
    half = c["h"] / 2
    if not (ys < half).any() or not (ys >= half).any():
        return False
    tilt = abs(xs[ys < half].mean() - xs[ys >= half].mean())
    off_line = abs(c["y"] + half - lines[c["s"] - 1])
    if not (tilt <= 0.2 * c["h"] and c["h"] >= 0.6 * sp and off_line <= 0.2 * sp):
        return False
    gap = max(2, int(0.5 * sp))
    cols = slice(c["x"], c["x"] + c["w"])
    above = clean[max(0, c["y"] - gap) : c["y"], cols].sum(axis=0)
    below = clean[c["y"] + c["h"] : c["y"] + c["h"] + gap, cols].sum(axis=0)
    continues = (
        c["y"] <= band[0]
        or c["y"] + c["h"] >= band[1]
        or max(above.max(initial=0), below.max(initial=0)) >= 0.3 * sp
    )
    if not continues:
        return True
    widths = np.array([np.ptp(xs[ys == y]) + 1 if (ys == y).any() else 0 for y in range(c["h"])])
    k = max(1, c["h"] // 4)
    stroke = np.median(widths[k:-k]) if c["h"] > 2 * k else widths.max()
    return max(widths[:k].max(), widths[-k:].max()) >= stroke + max(1, 0.12 * c["h"])
