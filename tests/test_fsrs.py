"""FSRS-5：默认权重下的数值和手算的一致；间隔取整、限制在 1–90 天。"""

import pytest

from app import fsrs


def test_initial_stability_and_interval():
    assert [fsrs.init_stability(g) for g in (1, 2, 3, 4)] == [0.40255, 1.18385, 3.173, 15.69105]
    assert fsrs.interval(3.173) == 3  # 第一次“顺”：3 天后
    assert fsrs.interval(15.69105) == 16  # 第一次“轻松”：16 天后
    assert fsrs.interval(0.40255) == 1  # 不到 1 天的按 1 天
    assert fsrs.interval(500) == fsrs.MAX_INTERVAL == 90


def test_interval_equals_stability_at_ninety_percent():
    for s in (2.0, 7.0, 33.0):
        assert fsrs.interval(s) == round(s)
        assert fsrs.retrievability(s, s) == pytest.approx(0.9)


def test_initial_difficulty():
    # D0(G) = w4 − e^{w5(G−1)} + 1
    assert fsrs.init_difficulty(1) == pytest.approx(7.1949)
    assert fsrs.init_difficulty(3) == pytest.approx(5.282434, abs=1e-5)
    assert fsrs.init_difficulty(4) == pytest.approx(3.224502, abs=1e-5)


def test_good_then_good_after_three_days():
    d0 = fsrs.init_difficulty(3)
    d, s = fsrs.review(d0, 3.173, 3, 3)
    assert fsrs.retrievability(3, 3.173) == pytest.approx(0.904698, abs=1e-5)
    assert s == pytest.approx(10.738926, abs=1e-4)
    assert d == pytest.approx(5.272968, abs=1e-5)  # Good 不改难度，只向 D0(4) 回归一点
    assert fsrs.interval(s) == 11


def test_again_on_review_forgets():
    d0 = fsrs.init_difficulty(3)
    d, s = fsrs.review(d0, 3.173, 3, 1)
    assert s == pytest.approx(1.055561, abs=1e-5)
    assert d == pytest.approx(6.796933, abs=1e-5)
    assert s < 3.173


def test_hard_and_easy_modifiers():
    d0 = fsrs.init_difficulty(3)
    _, hard = fsrs.review(d0, 10, 10, 2)
    _, good = fsrs.review(d0, 10, 10, 3)
    _, easy = fsrs.review(d0, 10, 10, 4)
    assert 10 < hard < good < easy


def test_difficulty_is_clamped():
    d = 9.9
    for _ in range(20):
        d = fsrs.next_difficulty(d, 1)
    assert 1 <= d <= 10
    d = 1.1
    for _ in range(20):
        d = fsrs.next_difficulty(d, 4)
    assert 1 <= d <= 10
