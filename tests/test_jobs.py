"""app/jobs.py：全局名额（先来先得、上限跟着设置走）、任务出了没处理的错时把曲子标成结束。"""

import threading
import time

from app import config, jobs, recognize, songs, store
from tests.fakes import score_png


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


def test_a_job_that_crashes_does_not_leave_the_song_busy(data_dir, sync_jobs):
    """任务收尾时出了没处理的错（读写曲子失败之类）：曲子不能一直显示“识别中”，识别中的行标成失败。"""
    sid = songs.create_song([("p1.png", score_png())])

    def begin(s):
        s["systems"] = [{"page": 0, "crop": [0, 10, 0, 10], "spans": None, "status": "parsing"}]
        s["activity"] = {"kind": "reparse_row", "restaging": False, "systems": [0]}

    def run(cancel):
        raise OSError("disk full")

    jobs.start(sid, "busy", begin, run)
    s = store.load(sid)
    assert s["activity"] is None and not jobs.jobs.running(sid)
    assert s["systems"][0]["status"] == "error"
    assert s["notice"]["kind"] == "error" and "disk full" in s["notice"]["msg"]


def test_abandon_leaves_finished_songs_alone(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    before = store.load(sid)
    jobs.abandon(sid, "x")
    after = store.load(sid)
    assert (after["notice"], after["status"], after["systems"]) == (
        before["notice"],
        before["status"],
        before["systems"],
    )
    jobs.abandon("no-such-song", "x")  # 曲子不在了：只记一笔


def test_a_crashed_first_parse_is_marked_failed_in_a_real_thread(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    jobs.start(sid, "busy", lambda s: s.update(status="parsing", activity={"kind": "parse"}), lambda c: 1 / 0)
    _wait_for(lambda: not jobs.jobs.running(sid))
    s = store.load(sid)
    assert s["status"] == "error" and s["activity"] is None and "ZeroDivisionError" in s["notice"]["msg"]
