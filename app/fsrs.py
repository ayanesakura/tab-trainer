"""FSRS-5 间隔重复的算法（纯函数，不读写文件）：练琴计划（app/study.py）按它排下次该练哪天。

一首曲子就是一张“卡片”：记忆稳定性 S（天，按目标记住率 90% 时正好是下次的间隔）、难度 D（1–10）。
自评 G：1 卡住 / 2 磕绊 / 3 顺 / 4 轻松（= FSRS 的 Again / Hard / Good / Easy）。
参数用 FSRS-5 的默认 19 个权重；同一天只算最后一次自评，所以用不到同日短期复习的 w17 / w18。
"""

from __future__ import annotations

import math

W = (
    0.40255,
    1.18385,
    3.173,
    15.69105,
    7.1949,
    0.5345,
    1.4604,
    0.0046,
    1.54575,
    0.1192,
    1.01925,
    1.9395,
    0.11,
    0.29605,
    2.2698,
    0.2315,
    2.9898,
    0.51655,
    0.6621,
)
DECAY = -0.5
FACTOR = 19 / 81  # 让 R(S, S) = 0.9
RETENTION = 0.9  # 目标记住率
MAX_INTERVAL = 90  # 天：曲子隔太久不练会生疏，最长三个月
MIN_STABILITY = 0.01


def retrievability(t: float, s: float) -> float:
    """过了 t 天、稳定性 S 时还记得的概率 R(t, S) = (1 + FACTOR·t/S)^DECAY。"""
    return (1 + FACTOR * t / s) ** DECAY


def interval(s: float) -> int:
    """稳定性 S 对应的下次间隔（天）：R 降到目标记住率的那天，取整，限制在 1–MAX_INTERVAL。"""
    days = s / FACTOR * (RETENTION ** (1 / DECAY) - 1)
    return max(1, min(MAX_INTERVAL, round(days)))


def _clamp_d(d: float) -> float:
    return min(10.0, max(1.0, d))


def init_stability(g: int) -> float:
    """第一次评 G 后的稳定性 S0(G) = w[G−1]。"""
    return W[g - 1]


def init_difficulty(g: int) -> float:
    """第一次评 G 后的难度 D0(G) = w4 − e^{w5·(G−1)} + 1，限制在 1–10。"""
    return _clamp_d(W[4] - math.exp(W[5] * (g - 1)) + 1)


def next_difficulty(d: float, g: int) -> float:
    """再评一次 G 后的难度：ΔD = −w6·(G−3)，按离 10 的距离缩放，再向 D0(4) 均值回归，限制在 1–10。"""
    d1 = d + (-W[6] * (g - 3)) * (10 - d) / 9
    return _clamp_d(W[7] * init_difficulty(4) + (1 - W[7]) * d1)


def recall_stability(d: float, s: float, r: float, g: int) -> float:
    """记住了（G ≥ 2）之后的稳定性。磕绊打折（w15）、轻松加成（w16）。"""
    hard = W[15] if g == 2 else 1.0
    easy = W[16] if g == 4 else 1.0
    grow = math.exp(W[8]) * (11 - d) * s ** (-W[9]) * (math.exp(W[10] * (1 - r)) - 1) * hard * easy
    return max(MIN_STABILITY, s * (grow + 1))


def forget_stability(d: float, s: float, r: float) -> float:
    """卡住了（G = 1）之后的稳定性：不会比原来大。"""
    s2 = W[11] * d ** (-W[12]) * ((s + 1) ** W[13] - 1) * math.exp(W[14] * (1 - r))
    return max(MIN_STABILITY, min(s, s2))


def review(d: float, s: float, t: int, g: int) -> tuple[float, float]:
    """隔了 t 天（至少 1）再评 G：返回新的 (D, S)。稳定性按评之前的 D 算（和 FSRS 的参考实现一致）。"""
    r = retrievability(max(1, t), s)
    s2 = forget_stability(d, s, r) if g == 1 else recall_stability(d, s, r, g)
    return next_difficulty(d, g), s2
