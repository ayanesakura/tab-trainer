"""在本机读六线谱上的品位数字，用来校对 AI 的识别结果。

不调用大模型、不花钱：版面检测已经知道每根弦线的位置和每个小节的横向范围，这里只做两件事——
1. 去掉谱线后，找出中心压在某根弦线上的数字块，挨着的拼成一个数（"12"）；
2. 用 PaddleOCR 的识别模型（ONNX）读这个数，解码时只允许 0–9。
数字落在哪根线上就是哪根弦，这是几何判断，不会像大模型那样把数字往下错一根弦。
在评测曲上，读出的（弦, 品）和标准答案对比：勿听 99.6% / 100%、Romance 99.4% / 99.7%（精确率 / 召回率）。

思路参考 PDF-to-GP5（MIT）的图片识别；它自己的谱线检测在我们的谱子上很不稳定，所以版面用我们自己的。
读不准的情况（数字很淡、和别的符号粘在一起）宁可不报：调用方只拿它来提醒和返修，不直接改结果。
"""

from __future__ import annotations

import logging
import threading
from functools import lru_cache
from pathlib import Path

import numpy as np

log = logging.getLogger("tab_trainer.ocr")

MODEL = Path(__file__).parent / "models" / "ppocr_v4_rec.onnx"
DARK = 160  # 比这个暗的像素算笔画
GLYPH_HEIGHT = 48  # 送进识别模型的高度（模型按 48 训练；太小的 0 会被读成别的）
MAX_FRET = 24
_lock = threading.Lock()


class _Recognizer:
    def __init__(self):
        import onnxruntime as ort

        self.session = ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])
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
        self.input = self.session.get_inputs()[0].name

    def digits(self, glyph: np.ndarray) -> str:
        """glyph：白底黑字的灰度图。返回读到的数字串（可能为空）。"""
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
        best = self.keep[probs[:, self.keep].argmax(axis=1)]  # 每一步只在“空白 + 数字”里选
        out, prev = [], 0
        for k in best:
            if k != 0 and (k != prev):
                d = self.digit_class[int(k)]
                if not (out and prev != 0 and out[-1] == d):  # O 和 0 两类连着出现也只算一个字
                    out.append(d)
            prev = k
        return "".join(out)


@lru_cache(maxsize=1)
def _recognizer() -> _Recognizer | None:
    try:
        return _Recognizer()
    except Exception as e:  # noqa: BLE001 - 缺依赖 / 模型文件时整个校对功能关闭，不影响识别
        log.warning("request_id=none 本地数字识别不可用：%s", e)
        return None


def available() -> bool:
    return MODEL.exists() and _recognizer() is not None


def read_system(
    gray: np.ndarray, lines: list[float], spans: list[tuple[float, float]]
) -> list[list[tuple[int, int]]] | None:
    """读一行六线谱。gray：这一行的灰度裁剪图；lines：6 根弦线的 y；spans：每个小节的 [x0, x1)。
    返回每个小节读到的 [(弦, 品), ...]（按横坐标排序）；不可用时返回 None。"""
    rec = _recognizer()
    if rec is None or len(lines) != 6 or not spans:
        return None
    import cv2

    sp = (lines[-1] - lines[0]) / 5
    if sp < 4:
        return None
    dark = gray < DARK
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

    out: list[list[tuple[int, int]]] = [[] for _ in spans]
    pad = max(4, int(sp * 0.4))
    for c in numbers:
        sub = lab[c["y"] : c["y"] + c["h"], c["x"] : c["x"] + c["w"]]
        mask = np.isin(sub, c["ids"])
        glyph = np.pad(np.where(mask, 0, 255).astype(np.uint8), pad, constant_values=255)
        with _lock:
            t = rec.digits(glyph)
        if not t:
            continue
        frets = [int(t)] if int(t) <= MAX_FRET else ([int(d) for d in t] if len(t) <= 3 else [])
        if t == "1" and not _upright(mask, c, lines, sp):
            continue
        xc = c["x"] + c["w"] / 2
        for i, (a, b) in enumerate(spans):
            if a <= xc < b:
                out[i] += [(c["s"], f) for f in frets]
    return out


def _upright(mask: np.ndarray, c: dict, lines: list[float], sp: float) -> bool:
    """读成 1 的块要真是竖着的一笔：滑音斜线、弧线的末端也常被读成 1。"""
    ys, xs = np.nonzero(mask)
    half = c["h"] / 2
    if not (ys < half).any() or not (ys >= half).any():
        return False
    tilt = abs(xs[ys < half].mean() - xs[ys >= half].mean())
    off_line = abs(c["y"] + half - lines[c["s"] - 1])
    return tilt <= 0.2 * c["h"] and c["h"] >= 0.6 * sp and off_line <= 0.2 * sp
