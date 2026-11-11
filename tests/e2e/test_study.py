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


def test_stuck_bars_and_total_time(server, browser):
    # 打分时卡住的小节：今天练过的、计划里的曲名后面写“卡在 … 小节”（提示里列全）；今日里每首的累计时长和曲库同一个数
    img = server["image"]
    b = browser
    try:
        _api(server, f"/api/songs/{img}/study", "PUT", {"in_plan": True})
        _api(
            server,
            f"/api/songs/{img}/practice",
            "POST",
            {"bar_ids": ["bar1"], "loops": 1, "bpm": 60, "seconds": 600},
        )
        _api(
            server,
            f"/api/songs/{img}/study/rate",
            "POST",
            {"rating": 1, "issues": [], "note": "", "stuck_bars": ["bar2", "bar3", "bar5"]},
        )
        b.open(server["base"] + "/#study")
        done = f"document.querySelector('#queue .study-row.done[data-id=\"{img}\"] .stuck')"
        b.wait_for(f"!!{done}")
        assert b.js(f"{done}.textContent") == "卡在 2–3、5 小节"
        assert b.js(f"{done}.title") == "上次打分时卡住的小节：第 2–3、5 小节"
        plan = f"document.querySelector('#plan .plan-row[data-id=\"{img}\"] .stuck')"
        b.wait_for(f"!!{plan}")
        assert b.js(f"{plan}.textContent") == "卡在 2–3、5 小节"
        # 用词：今日 / 日历 / 计划
        titles = b.js(
            "[...document.querySelectorAll('#view-study .section-title > span:first-child')].map(x => x.textContent)"
        )
        assert titles == ["今日", "日历", "计划"]
        # 还没打分的一天（把今天的返回改成“在队列里”）：卡住的小节写在今日的这一行，点开去练习页（from=today）；累计和曲库一样
        lib_total = next(s for s in _api(server, "/api/songs") if s["id"] == img)["practice"]["total_seconds"]
        b.js("""(() => { const of = window.fetch; window.fetch = async (u, init) => {
          const r = await of(u, init);
          if (!String(u).endsWith('/api/study/today')) return r;
          const t = await r.json(); t.queue = t.done.map(x => ({ ...x, kind: 'learning' })); t.done = [];
          t.queue.forEach(x => { x.undated_seconds = 120; });
          return new Response(JSON.stringify(t), { status: 200, headers: { 'Content-Type': 'application/json' } });
        }; return true; })()""")
        b.js("tt.study().then(() => true)")
        row = f"document.querySelector('#queue .study-row:not(.done)[data-id=\"{img}\"]')"
        b.wait_for(f"!!{row}")
        assert b.js(f"{row}.querySelector('.stuck').textContent") == "卡在 2–3、5 小节"
        assert "from=today" in b.js(f"{row}.dataset.href")
        tot = b.js(f"{row}.querySelector('.tot').textContent")
        assert tot == f"累计 {round(lib_total / 60)} 分钟"
        assert "含早期未记日期的 2 分钟" in b.js(f"{row}.querySelector('.tot').title")
        b.js("location.hash = 'library'; true")
        lib_text = b.js(f"document.querySelector('.song-row[data-id=\"{img}\"] .practice').textContent")
        assert tot in lib_text  # 同一首曲子，曲库和今日显示同一个数
        assert not b.errors, b.errors
    finally:
        _leave_plan(server, img)


def test_calendar_total_includes_undated(server, browser):
    # 日历右上角：近一年几天 · 累计（后端的 total_minutes，含早期没日期的），没日期的写在悬停提示里
    b = browser
    b.open(server["base"] + "/#study")
    b.wait_for("!!document.querySelector('#hero .ring')")
    b.js("""(() => { const of = window.fetch; window.fetch = async (u, init) => {
      const r = await of(u, init);
      if (!String(u).includes('/api/study/calendar')) return r;
      const c = await r.json(); c.total_minutes = 300; c.undated_minutes = 120;
      return new Response(JSON.stringify(c), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }; return true; })()""")
    b.js("tt.study().then(() => true)")
    b.wait_for("document.getElementById('calSum').textContent.includes('累计 5 小时')")
    assert "含早期未记日期的 2 小时" in b.js("document.getElementById('calSum').title")
    assert not b.errors, b.errors


def test_calendar_keyboard(server, browser):
    # 日历只有一格能 Tab 到（默认今天），Tab 一下就跳过整个日历；格子之间用方向键走，焦点框看得见，回车打开那一天
    import datetime as dt
    import time

    b = browser
    b.open(server["base"] + "/#study")
    today = _api(server, "/api/study/today")["day"]
    b.wait_for(f"!!document.querySelector('#cal .cell[data-day=\"{today}\"]')")
    tabbable = "[...document.querySelectorAll('#cal button.cell')].filter(c => c.tabIndex === 0).map(c => c.dataset.day)"
    assert b.js(tabbable) == [today]
    # 从日历前面 Tab 进来：落在今天；再 Tab 就到了日历后面
    b.js("""{ const mk = id => { const t = document.createElement('button'); t.id = id; t.textContent = id; return t; };
      const cal = document.getElementById('cal'); cal.before(mk('__before')); cal.after(mk('__after'));
      document.getElementById('__before').focus(); } true""")
    b.key("Tab", vk=9)
    assert b.js("document.activeElement.dataset.day") == today
    b.key("Tab", vk=9)
    assert b.js("document.activeElement.id") == "__after"
    b.key("Tab", vk=9, modifiers=8)  # Shift+Tab 回来
    assert b.js("document.activeElement.dataset.day") == today
    # ←：上一周同一天；↑：前一天
    d = dt.date.fromisoformat(today)
    b.key("ArrowLeft", vk=37)
    assert b.js("document.activeElement.dataset.day") == (d - dt.timedelta(days=7)).isoformat()
    # 焦点框看得见（不是今天、没选中的格子）：和全局焦点框一样是强调色
    time.sleep(0.3)  # 格子的 outline-color 有过渡
    style = b.js("""(s => [s.outlineStyle, s.outlineColor, s.outlineWidth])(getComputedStyle(document.activeElement))""")
    accent = b.js("""(() => { const x = document.createElement('i'); x.style.color = 'var(--accent)'; document.body.append(x);
      const c = getComputedStyle(x).color; x.remove(); return c; })()""")
    assert style == ["solid", accent, "2px"], (style, accent)
    b.key("ArrowUp", vk=38)
    target = (d - dt.timedelta(days=8)).isoformat()
    assert b.js("document.activeElement.dataset.day") == target
    assert b.js(tabbable) == [target]
    # 回车：打开那一天，焦点还在那一格
    b.key("Enter", vk=13, text="\r")
    b.wait_for("!document.getElementById('dayCard').hidden && !document.querySelector('#dayCard .spinner')")
    assert b.js("document.querySelector('#cal .cell.sel').dataset.day") == target
    assert b.js("document.activeElement.dataset.day") == target
    b.js("document.getElementById('__before').remove(); document.getElementById('__after').remove(); true")
    assert not b.errors, b.errors


def test_calendar_scrolled_to_today_when_opened_from_library(server):
    # 窄于日历的窗口：先开曲库（练琴页隐藏、宽度为 0）再切到练琴，日历也要停在最近的几周，不是一年前
    from tests.e2e.cdp import Browser

    with Browser(width=800, height=900) as b:
        b.open(server["base"] + "/#library")
        b.wait_for("!!document.querySelector('.song-row') && !!document.querySelector('#cal .cell')")
        assert b.js("innerWidth") == 800
        b.js("location.hash = 'study'; true")
        b.wait_for("!document.getElementById('view-study').hidden")
        cal = "document.getElementById('cal')"
        assert b.js(f"{cal}.scrollWidth > {cal}.clientWidth + 50")  # 日历确实比窗口宽
        b.wait_for(f"{cal}.scrollLeft + {cal}.clientWidth >= {cal}.scrollWidth - 1", timeout=3)
        assert b.js("(r => r.left >= 0 && r.right <= innerWidth)(document.querySelector('#cal .cell.today').getBoundingClientRect())")
        assert not b.errors, b.errors


def test_other_tab_changes_refresh_plan_and_library(server, browser):
    # 别的标签页改了计划（BroadcastChannel 'tt-study' 发 'changed'）：曲库的徽章、练琴页的计划列表都跟着变
    gp = server["gp"]
    b = browser
    badge = f"document.querySelector('.song-row[data-id=\"{gp}\"] .plan-badge')"
    plan_row = f"document.querySelector('#plan .plan-row[data-id=\"{gp}\"]')"
    announce = "new BroadcastChannel('tt-study').postMessage('changed'); true"  # 同一页面里另一个频道对象收得到
    try:
        b.open(server["base"] + "/#library")
        b.wait_for(f"!!document.querySelector('.song-row[data-id=\"{gp}\"]')")
        assert b.js(f"!{badge}")
        _api(server, f"/api/songs/{gp}/study", "PUT", {"in_plan": True})
        b.js(announce)
        b.wait_for(f"({badge} || {{}}).textContent === '待学'", timeout=5)
        b.js("location.hash = 'study'; true")
        b.wait_for(f"!!{plan_row}")
        _leave_plan(server, gp)
        b.js(announce)
        b.wait_for(f"!{plan_row}", timeout=5)
        assert not b.errors, b.errors
    finally:
        _leave_plan(server, gp)
