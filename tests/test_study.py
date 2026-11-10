"""练琴计划（app/study.py）：FSRS 重放、今天的队列、凌晨 4 点换日、练琴时长去重、秒表、手动补记、设置、
study.json 损坏、曲子包带上自评记录、接口形状。时间都用固定的 now。"""

import datetime as dt
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import bundles, main, model, recognize, songs, store, study
from app.errors import Conflict, Invalid, NotFound
from tests.fakes import derived_bars

GP5 = b"\x18FICHIER GUITAR PRO v5.10" + b"\x00" * 64
T0 = dt.datetime(2026, 11, 9, 20, 0, 0)  # 练琴日 2026-11-09


def at(day: str, hm: str = "20:00") -> dt.datetime:
    return dt.datetime.fromisoformat(f"{day} {hm}:00" if len(hm) == 5 else f"{day} {hm}")


def _song(title: str = "曲子", bars: int = 2) -> str:
    sid = songs.create_song([("x.gp5", GP5)], title)
    songs.set_derived(sid, {"title": title}, derived_bars(bars), 0, None)
    return sid


def _planned(*titles: str, now=T0) -> list[str]:
    ids = []
    for t in titles:
        sid = _song(t)
        study.set_plan(sid, in_plan=True, now=now)
        ids.append(sid)
    return ids


def _log(*pairs) -> list[dict]:
    """[(day, rating), ...] → 自评记录。"""
    return [
        {"at": f"{d} 20:00:00", "day": d, "rating": g, "issues": [], "note": "", "bpm": 0} for d, g in pairs
    ]


# ---------------------------------------------------------------------------
# 练琴日
# ---------------------------------------------------------------------------
def test_practice_day_rolls_over_at_four():
    assert study.practice_day(dt.datetime(2026, 11, 9, 3, 59, 59)) == "2026-11-08"
    assert study.practice_day(dt.datetime(2026, 11, 9, 4, 0, 0)) == "2026-11-09"
    assert study.practice_day(dt.datetime(2026, 11, 9, 23, 59)) == "2026-11-09"
    assert study.day_bounds("2026-11-09") == (dt.datetime(2026, 11, 9, 4), dt.datetime(2026, 11, 10, 4))


def test_rating_after_midnight_counts_for_the_previous_day(data_dir):
    (sid,) = _planned("夜里")
    study.rate(sid, 3, [], now=dt.datetime(2026, 11, 10, 2, 30))
    v = study.song_study(store.load(sid), now=dt.datetime(2026, 11, 10, 3, 0))
    assert v["last"]["day"] == "2026-11-09" and v["rated_today"] == 3
    v = study.song_study(store.load(sid), now=dt.datetime(2026, 11, 10, 4, 0))
    assert v["rated_today"] is None and v["due"] == "2026-11-12" and v["due_in"] == 2


# ---------------------------------------------------------------------------
# 重放
# ---------------------------------------------------------------------------
def test_replay_first_rating():
    assert study.replay([]) == {"state": "new", "due": None, "reps": 0, "lapses": 0, "s": None, "d": None}
    good = study.replay([("2026-11-01", 3)])
    assert good["state"] == "review" and good["due"] == "2026-11-04" and good["s"] == 3.173
    easy = study.replay([("2026-11-01", 4)])
    assert easy["state"] == "review" and easy["due"] == "2026-11-17"
    for g in (1, 2):
        r = study.replay([("2026-11-01", g)])
        assert r["state"] == "learning" and r["due"] == "2026-11-02"


def test_replay_lapse_and_graduation():
    lapse = study.replay([("2026-11-01", 3), ("2026-11-04", 1)])
    assert lapse["state"] == "learning" and lapse["lapses"] == 1 and lapse["due"] == "2026-11-05"
    assert lapse["s"] == pytest.approx(1.055561, abs=1e-5)
    # 学习中磕绊：还在学习，明天再练
    hard = study.replay([("2026-11-01", 1), ("2026-11-02", 2)])
    assert hard["state"] == "learning" and hard["due"] == "2026-11-03" and hard["reps"] == 2
    # 学习中顺：毕业进复习（卡住后第二天顺，S ≈ 2.265 → 2 天）
    grad = study.replay([("2026-11-01", 1), ("2026-11-02", 3)])
    assert grad["state"] == "review" and grad["due"] == "2026-11-04" and grad["lapses"] == 0
    # 复习中磕绊：留在复习
    rev = study.replay([("2026-11-01", 3), ("2026-11-04", 2)])
    assert rev["state"] == "review" and rev["lapses"] == 0
    # 复习中顺：3 天后再顺 → S ≈ 10.74 → 11 天
    again = study.replay([("2026-11-01", 3), ("2026-11-04", 3)])
    assert again["due"] == "2026-11-15"


def test_same_day_rating_replaces_the_earlier_one():
    log = _log(("2026-11-09", 1), ("2026-11-09", 3))
    v = study.view({"order": 0}, log, "2026-11-09")
    assert v["state"] == "review" and v["reps"] == 1 and v["rated_today"] == 3 and v["due_in"] == 3
    assert v["last"] == {"day": "2026-11-09", "rating": 3, "issues": [], "note": "", "bpm": 0}


def test_view_preview_replaces_todays_rating():
    v = study.view({"order": 2, "added": "2026-11-01 10:00:00", "paused": True}, [], "2026-11-09")
    assert v["state"] == "new" and v["due"] is None and v["due_in"] is None and v["last"] is None
    assert v["paused"] is True and v["order"] == 2 and v["stability"] is None
    assert v["preview"] == {
        "1": {"state": "learning", "due_in": 1},
        "2": {"state": "learning", "due_in": 1},
        "3": {"state": "review", "due_in": 3},
        "4": {"state": "review", "due_in": 16},
    }
    # 今天已经评过：预览是“换成另一个评分”的结果，和今天已有的那次无关
    rated = study.view({}, _log(("2026-11-09", 1)), "2026-11-09")
    assert rated["preview"]["3"] == {"state": "review", "due_in": 3}
    # 复习中过期了：今天卡住会退回学习
    overdue = study.view({}, _log(("2026-11-01", 3)), "2026-11-09")
    assert overdue["due_in"] == -5 and overdue["preview"]["1"] == {"state": "learning", "due_in": 1}
    assert overdue["preview"]["3"]["state"] == "review" and overdue["preview"]["3"]["due_in"] > 3


# ---------------------------------------------------------------------------
# 队列（纯函数）
# ---------------------------------------------------------------------------
SETTINGS = {"goal_minutes": 45, "max_learning": 2, "max_reviews": 5}


def _item(sid: str, order: int, log=(), paused=False, today="2026-11-09") -> dict:
    v = study.view({"order": order, "paused": paused}, list(log), today)
    return {"id": sid, "title": sid, "artist": "", "study": v, "today_seconds": 0, "playable": True}


def test_queue_order_and_limits():
    items = [
        _item("new-a", 5),
        _item("new-b", 6),
        _item("learn", 9, _log(("2026-11-08", 1))),
        _item("rev-late", 3, _log(("2026-11-01", 3))),  # 11-04 到期
        _item("rev-early", 1, _log(("2026-11-03", 3))),  # 11-06 到期
        _item("rev-same", 0, _log(("2026-11-03", 3))),  # 也是 11-06，计划顺序在前
        _item("rev-future", 2, _log(("2026-11-08", 3))),  # 11-11 才到期
        _item("paused-learn", 4, _log(("2026-11-08", 1)), paused=True),
    ]
    q = study.build_queue(items, SETTINGS, "2026-11-09", {})
    assert [(x["id"], x["kind"]) for x in q["queue"]] == [
        ("learn", "learning"),
        ("rev-late", "review"),
        ("rev-same", "review"),
        ("rev-early", "review"),
        ("new-a", "new"),  # 学习中 1 首 + 新的 1 首 = max_learning 2
    ]
    assert q["counts"] == {"learning": 1, "review_due": 3, "new": 2, "in_plan": 8, "paused": 1}
    assert q["done"] == []

    limited = study.build_queue(items, {**SETTINGS, "max_reviews": 1, "max_learning": 4}, "2026-11-09", {})
    assert [x["id"] for x in limited["queue"]] == ["learn", "rev-late", "new-a", "new-b"]
    assert limited["counts"]["review_due"] == 3  # 超出上限的也算在内
    unlimited = study.build_queue(items, {**SETTINGS, "max_reviews": 0}, "2026-11-09", {})
    assert sum(x["kind"] == "review" for x in unlimited["queue"]) == 3


def test_queue_done_and_new_filling():
    today = "2026-11-09"
    items = [
        _item("a", 0, _log((today, 1))),  # 今天新学的，卡住：学习中，已做完
        _item("b", 1, _log((today, 3))),  # 今天新学的，顺：进复习，已做完
        _item("c", 2),
        _item("d", 3),
    ]
    rated_at = {"b": f"{today} 19:00:00", "a": f"{today} 20:00:00"}
    q = study.build_queue(items, SETTINGS, today, rated_at)
    assert [x["id"] for x in q["done"]] == ["b", "a"]  # 按评的时间
    assert [x["kind"] for x in q["done"]] == ["review", "learning"]
    assert [x["id"] for x in q["queue"]] == ["c"]  # a 还在学（今天练过也算），只能再加 1 首新的
    full = study.build_queue(items, {**SETTINGS, "max_learning": 1}, today, rated_at)
    assert full["queue"] == []


def test_streak():
    active = {"2026-11-09", "2026-11-08", "2026-11-07", "2026-11-05"}
    assert study.streak(active, "2026-11-09") == 3
    assert study.streak(active - {"2026-11-09"}, "2026-11-09") == 2  # 今天还没练：到昨天为止
    assert study.streak(active, "2026-11-11") == 0


# ---------------------------------------------------------------------------
# 时间段
# ---------------------------------------------------------------------------
def test_merge_clip_and_split_by_day():
    t = dt.datetime(2026, 11, 9, 20)
    m = study.merge(
        [
            (t, t + dt.timedelta(minutes=10)),
            (t + dt.timedelta(minutes=5), t + dt.timedelta(minutes=20)),
            (t + dt.timedelta(minutes=20), t + dt.timedelta(minutes=25)),  # 首尾相接
            (t + dt.timedelta(hours=1), t + dt.timedelta(hours=1)),  # 长度 0
        ]
    )
    assert m == [(t, t + dt.timedelta(minutes=25))]
    night = [(dt.datetime(2026, 11, 10, 3, 30), dt.datetime(2026, 11, 10, 4, 15))]
    assert study.seconds_by_day(night) == {"2026-11-09": 1800, "2026-11-10": 900}
    lo, hi = study.day_bounds("2026-11-10")
    assert study.clip(night, lo, hi) == [(dt.datetime(2026, 11, 10, 4), dt.datetime(2026, 11, 10, 4, 15))]


def test_session_intervals_follow_the_recorded_minutes():
    seg = [["2026-11-09 20:00:00", "2026-11-09 20:10:00"], ["2026-11-09 21:00:00", "2026-11-09 22:00:00"]]
    full = study.session_intervals({"segments": seg, "minutes": 70})
    assert study.seconds_of(full) == 70 * 60
    cut = study.session_intervals({"segments": seg, "minutes": 15})  # 忘了停：从头截 15 分钟
    assert cut == [
        (dt.datetime(2026, 11, 9, 20), dt.datetime(2026, 11, 9, 20, 10)),
        (dt.datetime(2026, 11, 9, 21), dt.datetime(2026, 11, 9, 21, 5)),
    ]
    longer = study.session_intervals({"segments": seg, "minutes": 80})
    assert study.seconds_of(longer) == 80 * 60 and longer[0] == (
        dt.datetime(2026, 11, 9, 19, 50),
        dt.datetime(2026, 11, 9, 20),
    )


def _app_practice(sid: str, at_: str, seconds: float) -> None:
    bar = store.load(sid)["bars"][0]["id"]
    store.append_practice(sid, {"at": at_, "ids": [bar], "loops": 1, "bpm": 60, "seconds": seconds})


def test_minutes_merge_timer_app_and_manual(data_dir):
    a, b = _song("甲"), _song("乙")
    _app_practice(a, "2026-11-09 20:30", 1800)  # 20:00–20:30
    _app_practice(b, "2026-11-09 20:20", 600)  # 20:10–20:20，和上面重叠
    _app_practice(b, "2026-11-10 03:50", 1200)  # 03:30–03:50：还算 11-09
    _app_practice(b, "2026-11-08 21:00", 600)  # 前一天
    store.append_practice(b, {"seed": True, "total_seconds": 9999})  # 旧版本的聚合：没有时间，不算
    # 秒表：20:20–20:50（和练习页重叠 10 分钟）
    study.timer_action("start", now=at("2026-11-09", "20:20"))
    study.timer_action("stop", now=at("2026-11-09", "20:50"))
    study.add_session("2026-11-09", 15, "公园里练的", now=T0)
    now = at("2026-11-10", "03:55")
    d = study.day_detail("2026-11-09", now=now)
    # 20:00–20:50 = 50 分钟，03:30–03:50 = 20 分钟，加手动 15
    assert d["minutes"] == {"total": 85.0, "manual": 15.0, "goal": 45}
    assert d["intervals"] == [
        ["2026-11-09 20:00:00", "2026-11-09 20:50:00"],
        ["2026-11-10 03:30:00", "2026-11-10 03:50:00"],
    ]
    assert {s["kind"] for s in d["sessions"]} == {"timer", "manual"}
    by_id = {s["id"]: s for s in d["songs"]}
    assert by_id[a]["seconds"] == 1800 and by_id[b]["seconds"] == 1800 and by_id[a]["rating"] is None
    t = study.today(now=now)
    assert t["day"] == "2026-11-09" and t["minutes"]["total"] == 85.0
    assert t["week_minutes"] == 95.0 and t["streak"] == 2
    cal = study.calendar(now=now)
    assert cal["days"] == [
        {"day": "2026-11-08", "minutes": 10.0, "ratings": 0},
        {"day": "2026-11-09", "minutes": 85.0, "ratings": 0},
    ]
    assert cal["today"] == "2026-11-09" and cal["goal"] == 45
    assert study.calendar(days=1, now=now)["days"] == [{"day": "2026-11-09", "minutes": 85.0, "ratings": 0}]


def test_running_timer_counts_until_now(data_dir):
    study.timer_action("start", now=at("2026-11-09", "20:00"))
    t = study.today(now=at("2026-11-09", "20:12"))
    assert t["minutes"]["total"] == 12.0
    assert t["timer"] == {
        "running": True,
        "seconds": 720.0,
        "since": "2026-11-09 20:00:00",
        "started": "2026-11-09 20:00:00",
    }


# ---------------------------------------------------------------------------
# 秒表
# ---------------------------------------------------------------------------
def test_timer_state_machine(data_dir):
    with pytest.raises(Conflict, match="没有在计时"):
        study.timer_action("pause", now=T0)
    r = study.timer_action("start", now=at("2026-11-09", "20:00"))
    assert r["session"] is None and r["timer"]["running"] and r["timer"]["seconds"] == 0
    with pytest.raises(Conflict, match="已经在计时"):
        study.timer_action("start", now=T0)
    r = study.timer_action("pause", now=at("2026-11-09", "20:10"))
    assert r["timer"] == {
        "running": False,
        "seconds": 600.0,
        "since": None,
        "started": "2026-11-09 20:00:00",
    }
    assert (
        study.timer_action("pause", now=at("2026-11-09", "20:30"))["timer"]["seconds"] == 600
    )  # 暂停中再暂停
    assert study.timer(now=at("2026-11-09", "21:00"))["timer"]["seconds"] == 600  # 暂停中不走
    r = study.timer_action("resume", now=at("2026-11-09", "21:00"))
    assert r["timer"]["since"] == "2026-11-09 21:00:00"
    assert study.timer(now=at("2026-11-09", "21:05"))["timer"]["seconds"] == 900
    r = study.timer_action("stop", now=at("2026-11-09", "21:20"))
    assert r["timer"] is None
    s = r["session"]
    assert s["kind"] == "timer" and s["day"] == "2026-11-09" and s["minutes"] == 30
    assert s["segments"] == [
        ["2026-11-09 20:00:00", "2026-11-09 20:10:00"],
        ["2026-11-09 21:00:00", "2026-11-09 21:20:00"],
    ]
    for action in ("pause", "resume", "stop", "discard"):
        with pytest.raises(Conflict):
            study.timer_action(action, now=T0)
    assert study.timer(now=T0) == {"timer": None}
    assert study.load()["sessions"] == [s]


def test_timer_stop_zero_discard_and_override(data_dir):
    study.timer_action("start", now=T0)
    assert study.timer_action("stop", now=T0) == {"timer": None, "session": None}  # 一秒都没走：不记
    study.timer_action("start", now=T0)
    short = study.timer_action("stop", now=T0 + dt.timedelta(seconds=59))  # 不到 1 分钟：不记
    assert short == {"timer": None, "session": None}
    study.timer_action("start", now=T0)
    assert (
        study.timer_action("stop", 3, now=T0 + dt.timedelta(seconds=5))["session"]["minutes"] == 3
    )  # 手填了照记
    study.timer_action("start", now=T0)
    assert study.timer_action("stop", now=T0 + dt.timedelta(seconds=60))["session"]["minutes"] == 1
    store.update_study(lambda d: d.update(sessions=[]))
    study.timer_action("start", now=T0)
    assert study.timer_action("discard", now=T0 + dt.timedelta(hours=1))["session"] is None
    assert study.load()["sessions"] == []
    # 忘了停：开了 5 小时，实际练了 40 分钟
    study.timer_action("start", now=at("2026-11-09", "19:00"))
    s = study.timer_action("stop", 40, now=at("2026-11-10", "00:00"))["session"]
    assert s["minutes"] == 40 and s["segments"] == [["2026-11-09 19:00:00", "2026-11-10 00:00:00"]]
    assert study.today(now=at("2026-11-10", "00:01"))["minutes"]["total"] == 40.0
    with pytest.raises(Invalid):
        study.timer_action("start", 10, now=T0)


# ---------------------------------------------------------------------------
# 手动补记、设置、study.json 损坏
# ---------------------------------------------------------------------------
def test_manual_sessions(data_dir):
    s = study.add_session("2026-11-01", 30, "  琴行  ", now=T0)
    assert s["kind"] == "manual" and s["note"] == "琴行" and s["segments"] == [] and len(s["id"]) == 8
    with pytest.raises(Invalid, match="今天以后"):
        study.add_session("2026-11-10", 30, now=T0)
    assert study.add_session("2026-11-09", 1, now=T0)["day"] == "2026-11-09"  # 今天可以
    study.delete_session(s["id"])
    assert [x["day"] for x in study.load()["sessions"]] == ["2026-11-09"]
    with pytest.raises(NotFound):
        study.delete_session(s["id"])


def test_corrupt_study_file_is_moved_aside(data_dir):
    path = store.study_file()
    assert path.parent == data_dir
    path.write_text("{oops", encoding="utf-8")
    assert study.load() == {
        "settings": {"goal_minutes": 45, "max_learning": 2, "max_reviews": 5},
        "timer": None,
        "sessions": [],
        "next_order": 0,
    }
    assert not path.exists() and list(data_dir.glob("study.corrupt-*.json"))
    # 能读的文件里个别坏值：用默认值 / 跳过那一条
    path.write_text(
        json.dumps(
            {
                "settings": {"goal_minutes": 9999, "max_learning": 3, "max_reviews": True},
                "timer": {"segments": [["nope", None]]},
                "sessions": [
                    {"id": "zz", "kind": "manual", "day": "2026-11-01", "minutes": 5},
                    {"id": "0123abcd", "kind": "manual", "day": "2026-11-01", "minutes": 5},
                ],
            }
        ),
        encoding="utf-8",
    )
    data = study.load()
    assert data["settings"] == {"goal_minutes": 45, "max_learning": 3, "max_reviews": 5}
    assert data["timer"] is None and [s["id"] for s in data["sessions"]] == ["0123abcd"]


# ---------------------------------------------------------------------------
# 计划：加入 / 移出 / 暂停 / 排序 / 自评 / 今天
# ---------------------------------------------------------------------------
def test_plan_add_pause_remove_keeps_ratings(data_dir):
    a, b = _planned("甲", "乙")
    assert store.load(a)["study"] == {"order": 0, "added": "2026-11-09 20:00:00", "paused": False}
    assert store.load(b)["study"]["order"] == 1
    assert study.set_plan(b, paused=True, now=T0)["study"]["paused"] is True
    assert study.set_plan(b, in_plan=True, now=T0)["study"]["order"] == 1  # 已经在计划里：不变
    study.rate(a, 3, ["rhythm"], "第二段赶拍", 72, now=T0)
    assert study.set_plan(a, in_plan=False, now=T0) == {"study": None}
    assert "study" not in json.loads((store.song_dir(a) / "song.json").read_text())  # 不写空字段
    assert len(study.ratings(a)) == 1  # 自评记录留着
    with pytest.raises(Invalid, match="不在练琴计划里"):
        study.rate(a, 3, [], now=T0)
    with pytest.raises(Invalid, match="不在练琴计划里"):
        study.set_plan(a, paused=True, now=T0)
    # 再加回来：排在最后，以前的自评接着算
    back = study.set_plan(a, in_plan=True, now=T0)["study"]
    assert back["order"] == 2 and back["state"] == "review" and back["last"]["issues"] == ["rhythm"]


def test_reorder(data_dir):
    a, b, c, d = _planned("a", "b", "c", "d")
    study.reorder([c, "nope-1234", a, c])
    orders = {sid: store.load(sid)["study"]["order"] for sid in (a, b, c, d)}
    assert orders == {c: 0, a: 1, b: 2, d: 3}


def test_today_queue_with_real_songs(data_dir):
    a, b, c = _planned("甲", "乙", "丙", now=at("2026-11-01"))
    broken = _planned("坏的", now=at("2026-11-01"))[0]
    study.rate(a, 1, [], now=at("2026-11-08"))  # 学习中
    study.rate(b, 3, [], now=at("2026-11-01"))  # 11-04 到期
    store.update(c, lambda s: s["study"].update(paused=True))
    (store.song_dir(broken) / "song.json").write_text("{", encoding="utf-8")
    (store.song_dir(broken) / "song.json.bak").write_text("{", encoding="utf-8")
    t = study.today(now=T0)
    assert [(x["id"], x["kind"]) for x in t["queue"]] == [(a, "learning"), (b, "review")]
    first = t["queue"][0]
    assert set(first) == {"id", "title", "artist", "kind", "study", "today_seconds", "playable"}
    assert first["title"] == "甲" and first["playable"] is True and first["today_seconds"] == 0
    assert t["counts"] == {"learning": 1, "review_due": 1, "new": 0, "in_plan": 3, "paused": 1}
    assert t["streak"] == 1 and t["timer"] is None and t["done"] == []  # 昨天评过、今天还没练
    assert t["settings"] == {"goal_minutes": 45, "max_learning": 2, "max_reviews": 5}

    study.rate(b, 4, ["noise", "phrasing"], "", 80, now=at("2026-11-09", "21:00"))
    study.rate(a, 2, [], now=at("2026-11-09", "21:30"))
    t = study.today(now=at("2026-11-09", "22:00"))
    assert t["queue"] == [] and [x["id"] for x in t["done"]] == [b, a]
    assert t["streak"] == 2
    assert study.calendar(now=T0)["days"][-1] == {"day": "2026-11-09", "minutes": 0.0, "ratings": 2}
    d = study.day_detail("2026-11-09", now=T0)
    rated = {s["id"]: s["rating"] for s in d["songs"]}
    assert rated[b] == {"rating": 4, "issues": ["noise", "phrasing"], "note": "", "bpm": 80}
    with pytest.raises(Invalid):
        study.day_detail("2026-13-01", now=T0)


def test_bad_rating_lines_are_skipped(data_dir):
    (sid,) = _planned("甲")
    study.rate(sid, 3, [], now=T0)
    with open(store.study_log_path(sid), "a", encoding="utf-8") as f:
        f.write('{"at": "x", "day": "2026-11-09", "rating": 1}\n')
        f.write('{"at": "2026-11-09 21:00:00", "day": "2026-11-09", "rating": 9}\n')
        f.write(
            '{"at": "2026-11-09 21:00:00", "day": "2026-11-09", "rating": 2, "issues": ["old", "rhythm"]}\n'
        )
        f.write("not json\n")
        f.write('{"at": "2026-11-09 21:00:00", "day": "2026-11')  # 写到一半断电
    log = study.ratings(sid)
    assert [r["rating"] for r in log] == [3, 2] and log[1]["issues"] == ["rhythm"]
    study.rate(sid, 4, [], now=T0)  # 半行之后另起一行
    assert [r["rating"] for r in study.ratings(sid)] == [3, 2, 4]


# ---------------------------------------------------------------------------
# 曲子包
# ---------------------------------------------------------------------------
def test_bundle_roundtrip_keeps_plan_and_ratings(data_dir):
    a, b = _planned("甲", "乙")
    study.rate(a, 3, ["dynamics"], "强弱", 66, now=T0)
    before = store.load(a)["study"]
    path, _ = bundles.export_bundle([a, b])
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        assert f"songs/{a}/study.jsonl" in names and f"songs/{b}/study.jsonl" not in names
        assert json.loads(zf.read(f"songs/{a}/song.json"))["study"] == before
        text = zf.read(f"songs/{a}/study.jsonl").decode()
    import shutil

    shutil.rmtree(store.SONGS)
    # 包里的自评记录夹带坏行：导入时丢掉
    buf = io.BytesIO()
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(buf, "w") as dst:
        for n in src.namelist():
            data = src.read(n)
            if n.endswith("study.jsonl"):
                data = (text + '{"rating": 7}\n[1]\nnope\n').encode()
            dst.writestr(n, data)
    path.unlink()
    buf.seek(0)
    r = bundles.import_bundles([("x.zip", buf)])
    assert sorted(x["id"] for x in r["imported"]) == sorted([a, b])
    assert store.load(a)["study"] == before and store.load(b)["study"]["order"] == 1
    assert [x["rating"] for x in study.ratings(a)] == [3]
    assert study.ratings(a)[0]["issues"] == ["dynamics"]
    assert study.song_study(store.load(a), now=T0)["state"] == "review"


def test_bundle_with_a_bad_study_field_is_rejected(data_dir):
    song = {
        "schema_version": 2,
        "id": "bad-study",
        "source_type": "image",
        "status": "ready",
        "bars": [],
        "study": {"order": "first"},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(bundles.MANIFEST, json.dumps({"format": bundles.FORMAT, "version": 1, "songs": [song]}))
        zf.writestr("songs/bad-study/song.json", json.dumps(song))
    buf.seek(0)
    r = bundles.import_bundles([("x.zip", buf)])
    assert r["imported"] == [] and "study" in r["skipped"][0]["reason"]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
@pytest.fixture
def client(data_dir, monkeypatch):
    monkeypatch.setattr(recognize, "start_parse", lambda sid: True)
    return TestClient(main.app, base_url="http://127.0.0.1:8765")


def test_http_plan_rate_and_views(client):
    sid = _song("甲")
    assert client.get(f"/api/songs/{sid}").json()["study"] is None
    assert client.get("/api/songs").json()[0]["study"] is None
    r = client.put(f"/api/songs/{sid}/study", json={"in_plan": True})
    assert r.status_code == 200 and r.json()["study"]["state"] == "new"
    assert client.put(f"/api/songs/{sid}/study", json={"plan": True}).status_code == 400  # 写错了键名
    assert client.put("/api/songs/20260101-000000-abcdef/study", json={"in_plan": True}).status_code == 404

    r = client.post(f"/api/songs/{sid}/study/rate", json={"rating": 3, "issues": ["rhythm"], "note": "稳"})
    assert r.status_code == 200
    v = r.json()["study"]
    assert v["state"] == "review" and v["rated_today"] == 3 and v["due_in"] == 3 and v["last"]["bpm"] == 0
    for bad in (
        {"rating": 5},
        {"rating": 0},
        {"rating": 3, "issues": ["rhythm", "rhythm"]},
        {"rating": 3, "issues": ["loud"]},
        {"rating": 3, "note": "x" * 501},
        {"rating": 3, "bpm": -1},
    ):
        assert client.post(f"/api/songs/{sid}/study/rate", json=bad).status_code == 400, bad
    r = client.post(f"/api/songs/{sid}/study/rate", json={"rating": 3, "issues": ["loud"]})
    assert "问题" in r.json()["detail"]

    song = client.get(f"/api/songs/{sid}").json()
    model.SongView.model_validate(song)
    assert song["study"]["state"] == "review" and set(song["study"]["preview"]) == {"1", "2", "3", "4"}
    row = client.get("/api/songs").json()[0]
    model.LibraryItem.model_validate(row)
    assert row["study"]["rated_today"] == 3

    today = client.get("/api/study/today").json()
    assert set(today) == {
        "day",
        "settings",
        "minutes",
        "streak",
        "week_minutes",
        "timer",
        "queue",
        "done",
        "counts",
    }
    assert [x["id"] for x in today["done"]] == [sid] and today["streak"] == 1
    assert set(today["minutes"]) == {"total", "manual", "goal"}

    assert client.put(f"/api/songs/{sid}/study", json={"in_plan": False}).json() == {"study": None}
    r = client.post(f"/api/songs/{sid}/study/rate", json={"rating": 3})
    assert r.status_code == 400 and r.json()["detail"] == "这首曲子不在练琴计划里"


def test_http_corrupt_song_in_library_has_no_study(client):
    _song("好的")
    d = store.SONGS / "20260101-000000-broken"
    d.mkdir(parents=True)
    (d / "song.json").write_text("{", encoding="utf-8")
    rows = client.get("/api/songs").json()
    assert {r["status"]: r["study"] for r in rows}["corrupt"] is None
    assert client.get("/api/study/today").status_code == 200


def test_http_order_timer_sessions_settings(client):
    a, b = _song("甲"), _song("乙")
    for sid in (a, b):
        client.put(f"/api/songs/{sid}/study", json={"in_plan": True})
    assert client.put("/api/study/order", json={"ids": [b, a]}).json() == {"ok": True}
    assert store.load(b)["study"]["order"] == 0

    assert client.get("/api/study/timer").json() == {"timer": None}
    r = client.post("/api/study/timer", json={"action": "start"})
    assert r.status_code == 200 and r.json()["timer"]["running"] and r.json()["session"] is None
    r = client.post("/api/study/timer", json={"action": "start"})
    assert r.status_code == 409 and r.json()["detail"] == "已经在计时"
    assert client.get("/api/study/timer").json()["timer"]["running"] is True
    assert client.post("/api/study/timer", json={"action": "stop", "minutes": 0}).status_code == 400
    assert client.post("/api/study/timer", json={"action": "stop", "minutes": 601}).status_code == 400
    assert client.post("/api/study/timer", json={"action": "pause", "minutes": 5}).status_code == 400
    assert client.post("/api/study/timer", json={"action": "jump"}).status_code == 400
    r = client.post("/api/study/timer", json={"action": "stop", "minutes": 25}).json()
    assert r["timer"] is None and r["session"]["minutes"] == 25
    assert client.post("/api/study/timer", json={"action": "discard"}).status_code == 409

    day = client.get("/api/study/today").json()["day"]
    r = client.post("/api/study/sessions", json={"day": day, "minutes": 20, "note": "外面"})
    assert r.status_code == 200 and r.json()["kind"] == "manual"
    for bad in (
        {"day": "1999-12-31", "minutes": 20},
        {"day": "2026-02-30", "minutes": 20},
        {"day": "2026/11/09", "minutes": 20},
        {"day": day, "minutes": 0.5},
        {"day": day, "minutes": 601},
        {"day": "2999-01-01", "minutes": 20},
    ):
        assert client.post("/api/study/sessions", json=bad).status_code == 400, bad
    detail = client.get(f"/api/study/day/{day}").json()
    assert detail["minutes"]["manual"] == 20 and detail["minutes"]["total"] >= 45
    assert {s["kind"] for s in detail["sessions"]} == {"timer", "manual"}
    assert client.get("/api/study/day/yesterday").status_code == 400
    assert client.delete(f"/api/study/sessions/{r.json()['id']}").json() == {"ok": True}
    assert client.delete(f"/api/study/sessions/{r.json()['id']}").status_code == 404

    r = client.put("/api/study/settings", json={"goal_minutes": 30})
    assert r.json() == {"goal_minutes": 30, "max_learning": 2, "max_reviews": 5}
    assert client.put("/api/study/settings", json={"max_reviews": 0}).json()["max_reviews"] == 0
    for bad in (
        {"goal_minutes": 4},
        {"goal_minutes": 601},
        {"max_learning": 0},
        {"max_learning": 11},
        {"max_reviews": 51},
        {"goal": 30},
    ):
        assert client.put("/api/study/settings", json=bad).status_code == 400, bad
    assert client.get("/api/study/today").json()["settings"]["goal_minutes"] == 30

    cal = client.get("/api/study/calendar").json()
    assert cal["goal"] == 30 and cal["days"][-1]["day"] == day
    r = client.get("/api/study/calendar", params={"days": 0})
    assert r.status_code == 400 and "天数" in r.json()["detail"]
    assert client.get("/api/study/calendar", params={"days": 801}).status_code == 400
