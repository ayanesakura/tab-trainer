"""练琴：今日清单、计划（加入 / 排序 / 移出）、练习页打分、计时器、补记和练琴日历。"""

import json
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


def _leave_plan(server, *sids):
    for sid in sids:
        _api(server, f"/api/songs/{sid}/study", "PUT", {"in_plan": False})


def test_plan_queue_and_rate(server, browser):
    gp, img = server["gp"], server["image"]
    b = browser
    try:
        # 曲库：选择模式里两首一起加入计划（按列表顺序）
        b.open(server["base"] + "/#library")
        b.wait_for(f"!!document.querySelector('.song-row[data-id=\"{gp}\"]')")
        b.js("document.getElementById('selectBtn').click(); true")
        assert b.js("document.getElementById('planAddBtn').disabled") is True
        for sid in (gp, img):
            b.js(f"document.querySelector('.song-row[data-id=\"{sid}\"] .who').click(); true")
        assert b.js("document.getElementById('planAddBtn').disabled") is False
        b.js("document.getElementById('planAddBtn').click(); true")
        b.wait_for("document.getElementById('selectBar').hidden")
        b.wait_for(
            f"(document.querySelector('.song-row[data-id=\"{gp}\"] .plan-badge') || {{}}).textContent === '待学'"
        )

        # 练琴页：同时在学默认 2 首 → 两首新曲都在今日清单里，第一首是主色按钮
        b.js("location.hash = 'study'; true")
        b.wait_for("document.querySelectorAll('#queue .study-row').length === 2")
        assert b.js("document.getElementById('view-library').hidden") is True
        assert b.js("[...document.querySelectorAll('#queue .kind')].map(x => x.textContent)") == [
            "新曲",
            "新曲",
        ]
        assert b.js("document.querySelectorAll('#queue .btn.primary').length") == 1
        assert b.js("document.getElementById('studyDate').textContent") != ""
        assert b.js("!!document.querySelector('#hero .ring')")
        # 计划：两首，第一首往后移
        b.wait_for("document.querySelectorAll('#plan .plan-row').length === 2")
        first = b.js("document.querySelector('#plan .plan-row').dataset.id")
        b.js("document.querySelector('#plan .plan-row [data-mv=\"1\"]').click(); true")
        b.wait_for(f"document.querySelector('#plan .plan-row').dataset.id !== '{first}'")
        b.wait_for(
            f"tt.songs().find(s => s.id === '{first}').study.order === 1", timeout=5
        )  # 后端也改了（曲库列表重新取过）

        # 练习页：从今日清单进来，“返回”回到练琴；打分：勾“杂音”，点“顺”
        b.open(
            server["base"] + f"/trainer?id={gp}&from=today"
        )  # 不带 resume：有练习记录时页面会把它从地址里去掉
        b.wait_for("!document.getElementById('rateBtn').hidden")
        assert b.js("document.getElementById('backBtn').getAttribute('href')") == "/#study"
        assert "打分" in b.js("document.getElementById('rateBtn').textContent")
        b.js("document.getElementById('rateBtn').click(); true")
        b.wait_for("!document.getElementById('ratePop').hidden")
        assert b.js("[...document.querySelectorAll('#rateBtns .rate b')].map(x => x.textContent)") == [
            "卡住",
            "磕绊",
            "顺",
            "轻松",
        ]
        assert b.js("document.querySelector('#rateBtns .r3 span').textContent").endswith("复习")
        b.js("document.querySelector('#rateIssues [data-issue=\"noise\"]').click(); true")
        b.js("document.querySelector('#rateBtns .r3').click(); true")
        b.wait_for("document.getElementById('rateBtn').classList.contains('rated')")
        assert b.js("document.getElementById('ratePop').hidden") is True
        assert "顺" in b.js("document.getElementById('rateBtn').textContent")
        today = _api(server, "/api/study/today")
        done = [x for x in today["done"] if x["id"] == gp]
        assert done and done[0]["study"]["last"]["rating"] == 3
        assert done[0]["study"]["last"]["issues"] == ["noise"]
        # 再打开：带出今天勾的问题，“顺”是按下的
        b.js("document.getElementById('rateBtn').click(); true")
        b.wait_for("!document.getElementById('ratePop').hidden")
        assert (
            b.js("document.querySelector('#rateIssues [data-issue=\"noise\"]').getAttribute('aria-pressed')")
            == "true"
        )
        assert b.js("document.querySelector('#rateBtns .r3').getAttribute('aria-pressed')") == "true"
        b.key("Escape")
        b.wait_for("document.getElementById('ratePop').hidden")

        # 回到练琴页：这首在“今天练过”下面
        b.open(server["base"] + "/#study")
        b.wait_for(f"!!document.querySelector('#queue .study-row.done[data-id=\"{gp}\"]')")
        assert b.js("document.querySelector('#queue .list-sep').textContent").strip().startswith("今天练过")
        assert (
            b.js(f"document.querySelector('#queue .study-row.done[data-id=\"{gp}\"] .rating').textContent")
            == "顺"
        )
    finally:
        _leave_plan(server, gp, img)


def test_timer_manual_log_and_calendar(server, browser):
    b = browser
    created = []
    try:
        b.open(server["base"] + "/#study")
        b.wait_for("!!document.querySelector('#hero .ring')")
        # 计时：开始 → 暂停 → 继续 → 结束（不到 1 分钟不记）
        b.js("document.querySelector('#timer [data-act=\"start\"]').click(); true")
        b.wait_for("!!document.querySelector('#timer .timer-pill.on')")
        assert b.js("/^\\d\\d:\\d\\d · /.test(document.title)")
        b.js("document.querySelector('#timer [data-act=\"pause\"]').click(); true")
        b.wait_for("!!document.querySelector('#timer .timer-pill.paused')")
        b.js("document.querySelector('#timer [data-act=\"resume\"]').click(); true")
        b.wait_for("!!document.querySelector('#timer .timer-pill.on')")
        b.js("document.querySelector('#timer [data-act=\"stop\"]').click(); true")
        b.wait_for("!!document.querySelector('#timer [data-act=\"start\"]')")
        b.wait_for("(document.querySelector('.toast') || {}).textContent === '不到 1 分钟，没有记'")
        assert not b.js("/^\\d\\d:\\d\\d · /.test(document.title)")

        # 补记 25 分钟：进度环、日历今天的格子跟着变
        before = _api(server, "/api/study/today")["minutes"]["total"]
        b.js("document.getElementById('logAdd').click(); true")
        b.wait_for("document.getElementById('logDlg').open")
        b.js(
            "document.getElementById('logMin').value = '25'; document.getElementById('logNote').value = '爬格子'; true"
        )
        b.js("document.getElementById('logSave').click(); true")
        b.wait_for("!document.getElementById('logDlg').open")
        b.wait_for(f"document.querySelector('#hero .ring-c b').textContent === '{round(before + 25)}'")
        today = _api(server, "/api/study/today")["day"]
        b.wait_for(f"!!document.querySelector('#cal .cell.today[data-day=\"{today}\"]:not(.l0)')")
        # 点今天的格子：明细里有这条补记，删掉
        b.js(f"document.querySelector('#cal .cell[data-day=\"{today}\"]').click(); true")
        b.wait_for("!!document.querySelector('#dayCard [data-del]')")
        assert "爬格子" in b.js("document.getElementById('dayCard').textContent")
        created = [s["id"] for s in _api(server, f"/api/study/day/{today}")["sessions"]]
        b.js("document.querySelector('#dayCard [data-del]').click(); true")
        b.wait_for("!!document.querySelector('.dlg[open] [data-ok]')")
        b.js("document.querySelector('.dlg[open] [data-ok]').click(); true")
        b.wait_for(f"document.querySelector('#hero .ring-c b').textContent === '{round(before)}'")
        created = []
    finally:
        for sid in created:
            _api(server, f"/api/study/sessions/{sid}", "DELETE")


def test_study_settings(server, browser):
    b = browser
    b.open(server["base"] + "/#settings")
    b.wait_for("document.querySelector('#goalSeg [aria-pressed=\"true\"]')?.dataset.v === '45'")
    try:
        b.js("document.querySelector('#goalSeg [data-v=\"30\"]').click(); true")
        b.wait_for("(document.querySelector('.toast') || {}).textContent === '每日目标改成 30 分钟'")
        assert _api(server, "/api/study/today")["settings"]["goal_minutes"] == 30
    finally:
        _api(server, "/api/study/settings", "PUT", {"goal_minutes": 45})
