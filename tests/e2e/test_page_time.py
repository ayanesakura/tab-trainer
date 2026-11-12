"""练习页开着的时间算进练琴时间（不开播放也算）：自动记下，点“返回”时确认记多少分钟。"""

import datetime as dt
import json
import time
import urllib.request

import pytest

pytestmark = pytest.mark.e2e


def _api(server, path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        server["base"] + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    raw = urllib.request.urlopen(req).read()
    return json.loads(raw) if raw else None


def _page_sessions(song: str) -> list[dict]:
    from app import study

    return [s for s in study.load()["sessions"] if s["kind"] == "page" and s.get("song") == song]


def _open_and_backdate(b, server, sid, query="", minutes=25):
    """打开练习页，等第一次心跳记下，再把这条记录改成已经开了 minutes 分钟（不用真等）。→ 记录 id"""
    from app import store, study

    known = {s["id"] for s in _page_sessions(sid)}
    b.open(f"{server['base']}/trainer?id={sid}{query}")
    b.wait_for("window.tt && tt.state.ready", timeout=40)
    for _ in range(100):
        new = [s for s in _page_sessions(sid) if s["id"] not in known]
        if new:
            break
        time.sleep(0.1)
    pid = new[-1]["id"]

    def fn(data):
        for s in data.get("sessions") or []:
            if s.get("id") == pid:
                end = dt.datetime.fromisoformat(s["segments"][-1][1])
                s["segments"] = [
                    [(end - dt.timedelta(minutes=minutes)).strftime(study.TIME_FMT), s["segments"][-1][1]]
                ]

    store.update_study(fn)
    return pid


def _session(pid):
    from app import study

    return next((s for s in study.load()["sessions"] if s["id"] == pid), None)


def _cleanup(*pids):
    import contextlib

    from app import study
    from app.errors import NotFound

    for pid in pids:
        with contextlib.suppress(NotFound):  # 已经删了
            study.delete_session(pid)


def test_page_time_is_recorded_and_confirmed_on_the_way_back(server, browser):
    """开着练习页就记；点“返回”问一句记多少分钟（取消留在这页，改成 10 分钟记 10 分钟）。"""
    b, sid = browser, server["image"]
    pid = _open_and_backdate(b, server, sid)
    try:
        b.js("document.getElementById('backBtn').click(); true")
        dlg = "document.querySelector('dialog[open]')"
        b.wait_for(f"!!{dlg} && {dlg}.textContent.includes('这次练了 25 分钟')")
        assert b.js(f"{dlg}.querySelector('input').value") == "25"
        b.js(f"{dlg}.querySelector('[data-cancel]').click(); true")  # 取消：留在这页
        b.wait_for(f"!{dlg}")
        assert "/trainer" in b.js("location.pathname")
        b.js("document.getElementById('backBtn').click(); true")
        b.wait_for(f"!!{dlg}")
        b.js(f"{dlg}.querySelector('input').value = '10'; {dlg}.querySelector('[data-ok]').click(); true")
        b.wait_for("location.pathname === '/'")
        s = _session(pid)
        assert s["minutes"] == 10 and s["edited"] is True and s["song"] == sid
        day = _api(server, f"/api/study/day/{s['day']}")
        assert any(x["id"] == pid and x["title"] == "E2E 图片曲" for x in day["sessions"])
        assert not b.errors, b.errors
    finally:
        _cleanup(pid)


def test_page_time_rides_along_with_the_rating_prompt(server, browser):
    """在计划里、从今日进来：练琴时间放在走之前的打分弹层里，填 0 跳过 = 这次不算（记录删掉）。"""
    b, sid = browser, server["gp"]
    _api(server, f"/api/songs/{sid}/study", "PUT", {"in_plan": True})
    pid = None
    try:
        pid = _open_and_backdate(b, server, sid, "&from=today", minutes=7)
        b.js("document.getElementById('backBtn').click(); true")
        b.wait_for("!document.getElementById('ratePop').hidden")
        assert b.js("document.getElementById('rateTimeRow').hidden") is False
        assert b.js("document.getElementById('rateMinutes').value") == "7"
        assert b.js("!document.querySelector('dialog[open]')")  # 不再单独问
        b.js(
            "document.getElementById('rateMinutes').value = '0'; document.getElementById('rateSkip').click(); true"
        )
        b.wait_for("location.hash === '#study'")
        assert _session(pid) is None
        # 自己点“打分”打开的弹层不问练琴时间
        b.open(f"{server['base']}/trainer?id={sid}")
        b.wait_for("window.tt && tt.state.ready && !document.getElementById('rateBtn').hidden", timeout=40)
        b.js("document.getElementById('rateBtn').click(); true")
        b.wait_for("!document.getElementById('ratePop').hidden")
        assert b.js("document.getElementById('rateTimeRow').hidden") is True
        assert not b.errors, b.errors
    finally:
        _api(server, f"/api/songs/{sid}/study", "PUT", {"in_plan": False})
        _cleanup(*[s["id"] for s in _page_sessions(sid)])


def test_page_time_alone_is_enough_to_prompt_for_a_rating(server, browser):
    """在计划里、不是从今日进来、一直没播放：这页开了几分钟，点“返回”也提醒打分（练琴时间在弹层里）。"""
    b, sid = browser, server["gp"]
    _api(server, f"/api/songs/{sid}/study", "PUT", {"in_plan": True})
    try:
        pid = _open_and_backdate(b, server, sid, minutes=5)
        b.js("document.getElementById('backBtn').click(); true")
        b.wait_for("!document.getElementById('ratePop').hidden")
        assert b.js("document.getElementById('rateMinutes').value") == "5"
        b.js("document.getElementById('rateSkip').click(); true")
        b.wait_for("location.pathname === '/'")
        s = _session(pid)
        assert s is not None and round(s["minutes"]) == 5 and not s.get("edited")  # 没改：按自动记下的算
        assert not b.errors, b.errors
    finally:
        _api(server, f"/api/songs/{sid}/study", "PUT", {"in_plan": False})
        _cleanup(*[s["id"] for s in _page_sessions(sid)])
