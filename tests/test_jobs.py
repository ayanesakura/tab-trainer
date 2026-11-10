"""app/jobs.py：全局名额（先来先得、上限跟着设置走）。"""

import threading
import time

from app import config, jobs


def _wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while not cond():
        assert time.time() < end, "timed out"
        time.sleep(0.01)


def test_slots_follow_the_current_setting(monkeypatch):
    """上限每次取名额时按 settings.max_rows_global 读：调大以后排着队的不用等别人放下名额。"""
    monkeypatch.setattr(config.settings, "max_rows_global", 1)
    slots = jobs.Slots(lambda: config.settings.max_rows_global)
    slots.acquire()
    got = threading.Event()
    t = threading.Thread(target=lambda: (slots.acquire(), got.set()))
    t.start()
    assert not got.wait(0.3)
    monkeypatch.setattr(config.settings, "max_rows_global", 2)
    assert got.wait(2)
    assert slots.used == 2
    t.join()


def test_slots_are_first_come_first_served():
    """放下名额又马上再要的，排在已经在等的后面（精修一个小节的几遍核对之间让别的曲子先用）。"""
    slots = jobs.Slots(lambda: 1)
    slots.acquire()
    order = []
    t = threading.Thread(target=lambda: (slots.acquire(), order.append("other"), slots.release()))
    t.start()
    _wait_for(lambda: len(slots._waiting) == 1)
    slots.release()
    slots.acquire()  # 马上再要：等别人用完
    order.append("again")
    slots.release()
    t.join()
    assert order == ["other", "again"] and slots.used == 0


def test_cancel_while_queued_gives_up_the_place():
    slots = jobs.Slots(lambda: 1)
    slots.acquire()
    cancel = threading.Event()
    err = []

    def wait():
        try:
            slots.acquire(cancel)
        except jobs.Cancelled as e:
            err.append(e)

    t = threading.Thread(target=wait)
    t.start()
    _wait_for(lambda: len(slots._waiting) == 1)
    cancel.set()
    t.join(2)
    assert err and not slots._waiting and slots.used == 1
    slots.release()
    assert slots.used == 0
