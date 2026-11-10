"""练习页的冒烟测试：加载、播放和练习记录、改音和撤销、方向键换段、调弦和记号示范音、GP 导入（成功 / 失败）、曲库页、敲节奏。"""

import json
import shutil
import sys
import time
import urllib.request

import pytest

from tests.e2e.conftest import _gp_bytes

pytestmark = pytest.mark.e2e
UNDO_MOD = 4 if sys.platform == "darwin" else 2  # ⌘ / Ctrl


def _ai_on(b):
    """e2e 的服务没有 API key，AI 按钮默认灰掉（点了只说明去设置）：当作有 key（模型调用由测试换成假的或拦下）。"""
    b.wait_for("tt.get('ui.hasKey') !== undefined")
    b.js("tt.set('ui.hasKey', true); true")


def _song(server, sid):
    return json.load(urllib.request.urlopen(f"{server['base']}/api/songs/{sid}"))


def test_practice_page_loads_cleanly(trainer):
    b = trainer()
    assert b.js("window.scrollY") == 0  # 打开时不会跳到谱面
    assert b.js("document.querySelectorAll('#secBtns button').length") == 3  # 全曲 + 两行
    assert b.js("document.getElementById('tipText').textContent") == "第 1 小节的提示"
    assert b.js("tt.song.bars.length") == 6
    assert not b.errors, b.errors


def test_playback_records_completed_loops(server, trainer):
    b = trainer()
    b.js("tt.set('play.countIn', false); tt.setLoop(0, 0); tt.setTempo(240); tt.play(); true")
    time.sleep(4)
    b.js("tt.stop(); true")
    b.wait_for("!tt.state.playing")
    time.sleep(0.8)
    p = _song(server, server["image"])["practice"]["bars"]["bar1"]
    assert p["loops"] >= 2 and p["best_bpm"] == 240
    # 播放中换段：不能把跳回起点误记成弹完一遍
    before = p["loops"]
    b.js("tt.setLoop(0, 0); tt.play(); true")
    time.sleep(1.2)
    b.js("tt.setLoop(3, 3); tt.setLoop(4, 4); tt.stop(); true")
    time.sleep(0.8)
    bars = _song(server, server["image"])["practice"]["bars"]
    assert bars.get("bar4", {}).get("loops", 0) == 0 and bars.get("bar5", {}).get("loops", 0) == 0
    assert bars["bar1"]["loops"] >= before
    assert not b.errors, b.errors


def test_pause_during_count_in_then_play_again_plays(trainer):
    """预备拍里暂停再播放：alphaTab 1.9 直接 pause() 会卡在“播放中”不前进（图标变了却没有声音）。"""
    b = trainer()
    b.js("tt.set('play.countIn', true); tt.setLoop(1, 2); tt.setTempo(120); true")
    start = b.js("tt.api.tickPosition")
    b.js("tt.play(); true")
    time.sleep(0.4)  # 120 BPM 一小节预备拍 2 秒：还在预备拍里
    assert abs(b.js("tt.api.tickPosition") - start) <= 2
    b.js("tt.stop(); true")
    b.wait_for("!tt.state.playing")
    b.js("tt.play(); true")  # 马上再播放（连按两下空格）
    # 预备拍之后真的从原来的位置往前走了
    b.wait_for(f"tt.state.playing && tt.api.tickPosition > {start} + 200", timeout=8)
    b.js("tt.stop(); true")
    # 预备拍里停下、等一会再播放：停在原处，之后也能正常开始
    b.wait_for("!tt.state.playing")
    b.js("tt.setLoop(1, 2); true")
    b.wait_for(f"Math.abs(tt.api.tickPosition - {start}) <= 2")  # alphaTab 设位置有 1 tick 的取整
    b.js("tt.play(); true")
    time.sleep(0.4)
    b.js("tt.stop(); true")
    time.sleep(1)
    assert abs(b.js("tt.api.tickPosition") - start) <= 2 and not b.js("tt.state.playing")  # 停在原处
    b.js("tt.play(); true")
    b.wait_for(f"tt.state.playing && tt.api.tickPosition > {start} + 200", timeout=8)
    b.js("tt.stop(); true")


def test_playback_does_not_scroll_when_follow_is_off(trainer):
    b = trainer()
    b.js("tt.set('play.follow', false); true")
    # 开始播放会收起“上次练到…”的提示，页面变矮、滚动位置跟着变：先收起来，下面只比较播放本身
    b.js(
        "import('/static/js/trainer/notices.js').then(({ notice }) => { notice('resume', null); return true; })"
    )
    # 页面加高，滚到最下面：播放光标在视野上方
    b.js("document.body.style.paddingBottom = '4000px'; scrollTo(0, document.body.scrollHeight); true")
    y = b.js("scrollY")
    assert y > 0 and b.js("document.getElementById('followBtn').hidden")
    b.js("tt.set('play.countIn', false); tt.setLoop(3, 3); tt.play(); true")
    b.wait_for("tt.state.playing && !document.getElementById('followBtn').hidden")
    time.sleep(1.5)
    assert b.js("scrollY") == y  # 播放时不会自己滚到光标处
    b.js("document.getElementById('followBtn').click(); true")
    b.wait_for("document.getElementById('followBtn').hidden")  # 滚到光标处以后按钮消失
    assert b.js("scrollY") < y and not b.js("tt.cursorOutOfView()")
    b.js("tt.stop(); true")
    assert not b.errors, b.errors


def test_selected_bar_shows_original_image(trainer):
    b = trainer()
    assert "来源：图片" in b.js("document.getElementById('songTitle').title")
    b.wait_for("!!(tt.api.tracks && tt.api.tracks[0].staves[0].bars.length === 6 && tt.api.boundsLookup)")
    b.js("tt.onBeatClick(tt.api.tracks[0].staves[0].bars[1].voices[0].beats[0]); true")
    b.wait_for("!document.getElementById('cropBox').hidden")  # 点了小节，侧栏就显示这一小节的原谱截图
    b.js("document.getElementById('cropBox').click(); true")
    assert b.js("document.getElementById('origModal').open")
    assert b.js("document.getElementById('origTitle').textContent") == "原谱 · 第 2 小节"
    b.wait_for("document.getElementById('origCanvas').width > 0")
    b.js(
        "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowRight', code: 'ArrowRight'})); true"
    )
    assert b.js("document.getElementById('origTitle').textContent") == "原谱 · 第 3 小节"
    assert b.js("tt.state.a") == 0  # 点小节不改循环范围；弹窗里换小节也不影响选段（←→ 不会被当成换段）
    b.key("Escape", vk=27)
    assert not b.js("document.getElementById('origModal').open")
    assert not b.errors, b.errors


def test_edit_note_then_undo(server, trainer):
    b = trainer()
    orig = _song(server, server["image"])["bars"][1]
    b.js("""(() => { tt.setEditMode(true);
      const beat = tt.api.score.tracks[0].staves[0].bars[1].voices[0].beats.find(x => x.notes.length);
      tt.openNotePop(beat, beat.notes[0]); document.getElementById('npFret').value = '9'; return true; })()""")
    b.key("Enter", vk=13)
    b.wait_for("!tt.editor.saving && tt.editor.undo === 1")
    edited = _song(server, server["image"])["bars"][1]
    assert edited["events"][0]["notes"][0]["fret"] == 9 and edited["tip"] == ""
    b.js("document.activeElement.blur(); true")
    b.key("z", code="KeyZ", vk=90, modifiers=UNDO_MOD)
    b.wait_for("!tt.editor.saving && tt.editor.undo === 0")
    back = _song(server, server["image"])["bars"][1]
    assert back["events"] == orig["events"] and back["tip"] == orig["tip"]  # 撤销后提示也恢复
    assert not b.errors, b.errors


def test_arrow_keys_step_through_sections(trainer):
    b = trainer()
    b.js("document.activeElement.blur(); true")
    label = "['loopA', 'loopB'].map(id => document.getElementById(id).value).join('–')"
    assert b.js(label) == "1–6"
    b.key("ArrowRight", vk=39)
    assert b.js(label) == "1–3"
    b.key("ArrowRight", vk=39)
    assert b.js(label) == "4–6"
    b.key("ArrowLeft", vk=37)
    assert b.js(label) == "1–3"
    bpm = b.js("tt.state.bpm")
    b.key("ArrowUp", vk=38)
    assert b.js("tt.state.bpm") == bpm + 4


def test_tuner_and_glossary_demos_play(trainer):
    """调弦表的“听”和记号说明的“试听”用的是页面里自带的拨弦合成（不是 alphaTab）。"""
    b = trainer()
    b.js("location.hash = '#tune'; true")
    b.wait_for(
        "!document.getElementById('view-tune').hidden && document.getElementById('view-practice').hidden"
    )
    assert b.js("document.querySelectorAll('#tuneRows tr').length") == 6
    b.js("document.querySelector('#tuneRows button').click(); true")
    b.js("location.hash = '#glossary'; true")
    b.wait_for("!document.getElementById('view-glossary').hidden")
    demos = b.js("[...document.querySelectorAll('button.demo')].map(x => x.dataset.demo)")
    assert len(demos) == 11 and "th" in demos
    for d in demos:  # 每个示范都点一遍：闷音、滑音、击勾弦、三种泛音、倚音各条发声路径都会走到
        b.js(f"document.querySelector('button.demo[data-demo={d}]').click(); true")
    time.sleep(0.3)
    assert b.js("tt.ac.state") == "running"
    assert not b.errors, b.errors


def test_guitar_pro_import_becomes_ready(server, trainer):
    b = trainer(server["gp"])
    b.wait_for("tt.song.bars.length === 3 && tt.song.status === 'ready'")
    s = _song(server, server["gp"])
    assert s["status"] == "ready" and s["meta"]["tuning"][5] == "E2" and len(s["bars"]) == 3
    assert s["derived"]["version"] == b.js("tt.DERIVE_VERSION") and s["derived"]["alphatab"]
    assert not b.errors, b.errors


def test_outdated_derived_data_is_rebuilt_once(server, trainer):
    """转换规则升级以后（这里把记下的版本改旧）：打开时重新转换一次，内容没变的小节保留 id 和提示。"""
    store = server["store"]
    trainer(server["gp"]).wait_for("tt.song.bars.length === 3 && tt.song.status === 'ready'")

    def age(s):
        s["derived"]["version"] = 0
        s["bars"][0]["tip"] = "保留的提示"

    store.update(server["gp"], age)
    before = _song(server, server["gp"])
    b = trainer(server["gp"])
    b.wait_for("tt.song.derived && tt.song.derived.version === tt.DERIVE_VERSION && tt.state.ready")
    f = store.song_dir(server["gp"]) / "song.json"
    time.sleep(0.5)
    mtime = f.stat().st_mtime_ns
    time.sleep(1.5)
    assert f.stat().st_mtime_ns == mtime  # 写回之后不会再反复转换
    after = _song(server, server["gp"])
    assert after["derived"]["version"] > 0
    assert [x["id"] for x in after["bars"]] == [x["id"] for x in before["bars"]]
    assert after["bars"][0]["tip"] == "保留的提示"
    assert not b.errors, b.errors


def test_broken_guitar_pro_file_is_reported(server, browser):
    browser.open(f"{server['base']}/trainer?id={server['broken']}")
    browser.wait_for("window.tt && tt.song && tt.song.status === 'error'", timeout=30)
    assert "曲谱读取失败" in browser.js("document.getElementById('notices').innerText")
    assert _song(server, server["broken"])["status"] == "error"


def test_library_lists_songs(server, browser):
    browser.open(server["base"] + "/")
    browser.wait_for("document.getElementById('songs').innerText.includes('E2E 图片曲')")
    text = browser.js("document.getElementById('songs').innerText")
    assert "E2E 图片曲" in text and "失败" in text  # 读不了的 GP 文件在曲库里标成失败
    assert not browser.errors, browser.errors


def test_layout_score_first_and_sidebar(server, browser):
    """1440×1000：谱面在第一屏里，小节卡片在右边；“更多”弹层打开时快捷键不响应，Esc 关掉；指板开关记住。"""
    browser._run(
        browser._send(
            "Emulation.setDeviceMetricsOverride", width=1440, height=1000, deviceScaleFactor=1, mobile=False
        )
    )
    browser.open(f"{server['base']}/trainer?id={server['image']}")
    browser.wait_for(
        "window.tt && tt.state.ready && !!document.querySelector('#score .at-surface')", timeout=40
    )
    b = browser
    assert b.js("document.querySelector('#score .at-surface').getBoundingClientRect().top") < 600
    side = b.js(
        "(() => { const r = document.getElementById('side').getBoundingClientRect(); return [r.left, r.top] })()"
    )
    score = b.js("document.getElementById('scoreCard').getBoundingClientRect().right")
    assert side[0] > score and side[1] < 200
    b.js("document.activeElement.blur(); document.getElementById('moreBtn').click(); true")
    assert not b.js("document.getElementById('morePop').hidden")
    bpm = b.js("tt.state.bpm")
    b.key("ArrowUp", vk=38)
    assert b.js("tt.state.bpm") == bpm  # 有浮层时不调速
    b.key("Escape", vk=27)
    assert b.js("document.getElementById('morePop').hidden")
    was = b.js("tt.get('ui.fb')")
    b.js("document.getElementById('fbToggle').click(); true")
    assert b.js("localStorage.getItem('tt.fb')") == ("0" if was else "1")
    assert b.js("document.getElementById('fbBody').hidden") == was
    assert not b.errors, b.errors


def test_resume_param_continues_last_practice(server, browser):
    body = json.dumps({"bar_ids": ["bar4", "bar5", "bar6"], "loops": 1, "bpm": 66, "seconds": 30}).encode()
    req = urllib.request.Request(
        f"{server['base']}/api/songs/{server['image']}/practice",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req)
    b = browser
    # 页面会把地址里的 resume 去掉，所以不用 open()（它要等地址一致）
    b._run(b._send("Page.navigate", url=f"{server['base']}/trainer?id={server['image']}&resume=1"))
    b.wait_for("window.tt && tt.state.ready && tt.state.a === 3 && tt.state.b === 5", timeout=40)
    assert b.js("tt.state.bpm") == 66
    assert "resume" not in b.js("location.search")  # 刷新页面不会再跳
    assert not b.js("document.querySelector('#notices [data-id=resume]')")


# ---------------------------------------------------------------------------
# 审查修复的回归测试
# ---------------------------------------------------------------------------
def _post_practice(server, sid, bar_ids, bpm):
    body = json.dumps({"bar_ids": bar_ids, "loops": 1, "bpm": bpm, "seconds": 30}).encode()
    req = urllib.request.Request(
        f"{server['base']}/api/songs/{sid}/practice", data=body, headers={"Content-Type": "application/json"}
    )
    urllib.request.urlopen(req)


def _edit_fret(b, bi, fret):
    """在谱面上点第 bi 小节的第一个音，品位改成 fret，回车保存，等保存结束。"""
    b.js(f"""(() => {{ tt.setEditMode(true);
      const beat = tt.api.score.tracks[0].staves[0].bars[{bi}].voices[0].beats.find(x => x.notes.length);
      tt.openNotePop(beat, beat.notes[0]); document.getElementById('npFret').value = '{fret}'; return true; }})()""")
    b.key("Enter", vk=13)
    b.wait_for("!tt.editor.saving && !tt.editor.np")
    time.sleep(0.6)  # 谱面按新数据重画完


def test_background_writes_do_not_cause_false_conflicts(server, trainer):
    """乐观锁按对象：问 AI、别的小节被改都不让这个小节 / 谱头的保存报 409；这个小节真在别处改过才报，
    并且只作废这个小节的撤销记录。"""
    sid, store = server["image"], server["store"]
    raw = store.load(sid)["bars"]
    b = trainer()
    _ai_on(b)
    try:
        # 模拟问 AI：后端存了一次（写 qa，顶层 rev 变了），前端把返回的 qa 并进 store
        store.update(sid, lambda s: s["bars"][0].update(qa=[{"q": "Q", "a": "A", "at": "now"}]))
        b.js("""(() => { const of = window.fetch; window.fetch = (u, init) => String(u).includes('/ask')
          ? Promise.resolve(new Response(JSON.stringify({type: 'done', answer: 'A', qa: [{q: 'Q', a: 'A', at: 'now'}]}) + '\\n', {status: 200, headers: {'Content-Type': 'application/x-ndjson'}}))
          : of(u, init); return true; })()""")
        b.js(
            "tt.set('ui.bar', 0); document.getElementById('askInput').value = 'Q'; document.getElementById('askBtn').click(); true"
        )
        b.wait_for("tt.song.bars[0].qa && tt.song.bars[0].qa.length === 1")
        assert b.js("tt.song.rev") < _song(server, sid)["rev"]  # 前端的顶层 rev 已经旧了
        _edit_fret(b, 2, 7)
        _edit_fret(b, 4, 8)
        server_bars = _song(server, sid)["bars"]
        assert [server_bars[i]["events"][0]["notes"][0]["fret"] for i in (2, 4)] == [7, 8]  # 两次都没报 409
        assert b.js("tt.editor.undoBars") == ["bar3", "bar5"]
        b.js("document.getElementById('metaSave').click(); true")
        b.wait_for("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('已保存'))")
        # 第 5 小节在别处改了：再改它 → 409，刷新，第 5 小节的撤销记录作废，第 3 小节的留着
        store.update(sid, lambda s: s["bars"][4]["events"][0]["notes"][0].update(fret=11))
        _edit_fret(b, 4, 9)
        b.wait_for("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('别处改过'))")
        b.wait_for("tt.song.bars[4].events[0].notes[0].fret === 11")
        assert b.js("tt.song.bars[4].rev") == _song(server, sid)["bars"][4]["rev"]
        assert b.js("tt.editor.undoBars") == ["bar3"]
        assert not b.errors, b.errors
    finally:
        store.update(sid, lambda s: s.update(bars=raw))


def test_resume_applies_tempo_to_guitar_pro_songs(server, browser):
    """GP 曲子的谱面是异步加载的：接着练定下的速度不能被“谱上原速”的默认值盖掉（地址带 resume=1 和点“接着练”都一样）。"""
    gp, b = server["gp"], browser
    ready = (
        "!!(window.tt && tt.state.ready && tt.song && tt.song.bars.length === 3 && tt.api && tt.api.score)"
    )
    b.open(f"{server['base']}/trainer?id={gp}")
    b.wait_for(ready, timeout=40)
    ids = [x["id"] for x in _song(server, gp)["bars"]]
    _post_practice(server, gp, ids[1:3], 200)
    b._run(b._send("Page.navigate", url=f"{server['base']}/trainer?id={gp}&resume=1"))
    b.wait_for(ready + " && tt.state.a === 1 && tt.state.b === 2", timeout=40)
    time.sleep(1.5)
    assert b.js("tt.state.bpm") == 200
    assert abs(b.js("tt.api.playbackSpeed") - 200 / b.js("tt.api.score.tempo")) < 1e-6
    # 不带 resume：通知里的“接着练”一出现就点（这时音色多半还没加载好）
    b.open(f"{server['base']}/trainer?id={gp}")
    b.wait_for("!!document.querySelector('#notices [data-id=resume] .btn.primary')", timeout=40)
    b.js("document.querySelector('#notices [data-id=resume] .btn.primary').click(); true")
    b.wait_for(ready, timeout=40)
    time.sleep(1.5)
    assert b.js("[tt.state.a, tt.state.b, tt.state.bpm]") == [1, 2, 200]
    assert not b.errors, b.errors


def test_song_updates_keep_paused_position(trainer):
    """问 AI 写了问答、轮询拿到新数据：循环范围没变时，暂停的位置不跳回段首。"""
    b = trainer()
    b.js("tt.set('play.countIn', false); tt.setLoop(1, 4); tt.setTempo(240); tt.play(); true")
    b.wait_for("tt.state.playing")
    time.sleep(2)
    b.js("tt.stop(); true")
    b.wait_for("!tt.state.playing")
    time.sleep(0.6)
    t1 = b.js("tt.api.tickPosition")
    start = b.js("tt.api.playbackRange.startTick")
    assert t1 > start
    b.js("tt.set('song.bars.2.qa', [{q: 'q', a: 'a', at: 'x'}]); true")
    b.js("tt.set('song', {...tt.song, rev: tt.song.rev + 1, bars: tt.song.bars.map(x => ({...x}))}); true")
    time.sleep(0.3)
    assert b.js("tt.api.tickPosition") == t1
    assert b.js("[tt.state.a, tt.state.b]") == [1, 4]
    assert not b.errors, b.errors


def test_ask_ai_answer_streams_in(server, trainer, monkeypatch):
    """问 AI：回答一段段出现在问答列表里（真服务 + 替身模型，每段之间停一会儿），按钮显示“回答中…”；
    中途换到别的小节，按钮禁用并说明在等哪一小节；回答完存进问的那个小节，列表里是存好的问答。"""
    from app import config
    from tests.fakes import FakeClaude

    monkeypatch.setattr(config, "llm_factory", FakeClaude)
    monkeypatch.setattr(FakeClaude, "stream_delay", 0.5)
    sid, store = server["image"], server["store"]
    raw = store.load(sid)["bars"]
    answer = "回答：这里左手怎么按呢"  # 替身每 3 个字一段：4 段
    b = trainer()
    _ai_on(b)
    try:
        b.js(
            "tt.set('ui.bar', 2); document.getElementById('askInput').value = '这里左手怎么按呢'; "
            "document.getElementById('askBtn').click(); true"
        )
        pending = "(document.querySelector('#qaList .qa.pending .a') || {}).textContent || ''"
        b.wait_for(f"{pending}.startsWith('回答：')")
        assert b.js(pending) != answer  # 还没收完就已经显示出来了
        assert "回答中" in b.js("document.getElementById('askBtn').textContent")
        assert b.js("document.getElementById('askInput').value") == ""
        b.js("tt.set('ui.bar', 0); true")  # 换到别的小节：不显示别处的回答，按钮等着
        assert b.js("!document.querySelector('#qaList .qa.pending')")
        assert b.js("document.getElementById('askBtn').disabled")
        assert "第 3 小节" in b.js("document.getElementById('askBtn').title")
        b.js("tt.set('ui.bar', 2); true")
        b.wait_for(f"{pending}.length > 3")
        b.wait_for("(tt.song.bars[2].qa || []).length === 1", timeout=20)
        saved = store.load(sid)["bars"][2]["qa"]
        assert saved[0]["a"] == answer and b.js("tt.song.bars[2].qa") == saved
        assert b.js("!document.querySelector('#qaList .qa.pending')")
        assert b.js("[...document.querySelectorAll('#qaList .qa .a')].map(x => x.textContent)") == [answer]
        assert b.js("document.getElementById('askBtn').disabled") is False
        assert not b.errors, b.errors
    finally:
        store.update(sid, lambda s: s.update(bars=raw))


def test_ask_ai_error_mid_answer_restores_the_question(trainer):
    """回答到一半出错：提示错误，半截回答不留，问题放回输入框。"""
    b = trainer()
    _ai_on(b)
    b.js("""(() => { const of = window.fetch; window.fetch = (u, init) => String(u).includes('/ask/stream')
      ? Promise.resolve(new Response('{"type":"delta","text":"先用"}\\n{"type":"error","detail":"和模型服务的连接中断了"}\\n', {status: 200}))
      : of(u, init); return true; })()""")
    b.js(
        "tt.set('ui.bar', 1); document.getElementById('askInput').value = '怎么练'; document.getElementById('askBtn').click(); true"
    )
    b.wait_for("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('连接中断'))")
    assert b.js("document.getElementById('askInput').value") == "怎么练"
    assert b.js("!document.querySelector('#qaList .qa.pending') && !(tt.song.bars[1].qa || []).length")
    assert b.js("document.getElementById('askBtn').disabled") is False
    assert not b.errors, b.errors


def test_ask_ai_answer_survives_a_late_poll(server, trainer):
    """回答存好之前发出的读取（轮询）晚回来：不能用旧数据把刚并进来的问答盖掉。"""
    b = trainer()
    _ai_on(b)
    b.js(f"""(() => {{ const of = window.fetch; window.__held = [];
      window.fetch = (u, i) => {{
        if (String(u).includes('/ask/stream')) return Promise.resolve(new Response(
          '{{"type":"done","qa":[{{"q":"怎么练","a":"慢练","at":"今天"}}]}}\\n', {{status: 200}}));
        if (String(u) === '/api/songs/{server["image"]}' && window.__hold)
          return new Promise(r => __held.push(r)).then(() => of(u, i));
        return of(u, i); }};
      return true; }})()""")
    b.js("window.__hold = true; tt.refresh(); true")  # 读取发出去了，先不让它回来
    b.wait_for("__held.length === 1")
    b.js(
        "window.__hold = false; tt.set('ui.bar', 1); document.getElementById('askInput').value = '怎么练'; "
        "document.getElementById('askBtn').click(); true"
    )
    b.wait_for("(tt.song.bars[1].qa || []).length === 1")
    b.js("__held[0](); true")  # 旧的读取回来了（服务上还没有这条问答）
    time.sleep(1)
    assert b.js("(tt.song.bars[1].qa || []).map(x => x.a)") == ["慢练"]
    assert "慢练" in b.js("document.getElementById('qaList').textContent")
    assert not b.errors, b.errors


def test_refine_hint_is_only_sent_for_its_own_bar(trainer):
    """“哪里不对”的说明只跟着写它的那个小节：精修发出去以后清掉；换到别的小节也清掉，不会带到别的小节上。"""
    b = trainer()
    _ai_on(b)
    b.js("""(() => { const of = window.fetch; window.__refines = [];
      window.fetch = (u, i) => String(u).endsWith('/refine') && i && i.method === 'POST'
        ? (__refines.push(JSON.parse(i.body)), Promise.resolve(new Response('{}', {status: 200}))) : of(u, i);
      return true; })()""")
    typed = "(t => { const el = document.getElementById('hintInput'); el.value = t; el.dispatchEvent(new Event('input')); return true; })"
    b.js(f"tt.set('ui.bar', 1); {typed}('第 3 拍是 2 弦 4 品')")
    b.wait_for("!document.getElementById('refineBarBtn').disabled")
    b.js("document.getElementById('refineBarBtn').click(); true")
    b.wait_for("__refines.length === 1")
    assert b.js("__refines[0]") == {"bars": ["bar2"], "hint": "第 3 拍是 2 弦 4 品"}
    b.wait_for("document.getElementById('hintInput').value === ''")
    b.js(f"{typed}('这里少了一个音')")
    b.js("tt.set('ui.bar', 4); true")  # 换了小节：说明清掉
    assert b.js("document.getElementById('hintInput').value") == ""
    b.wait_for("!document.getElementById('refineBarBtn').disabled")
    b.js("document.getElementById('refineBarBtn').click(); true")
    b.wait_for("__refines.length === 2")
    assert b.js("__refines[1]") == {"bars": ["bar5"]}
    assert not b.errors, b.errors


def test_notice_buttons_survive_song_updates(trainer):
    """识别进度每次轮询都会更新：文字变了只换文字，按钮还是原来那个（按下和松开之间不会被换掉）。"""
    b = trainer()
    b.js("""tt.set('song', {...tt.song, activity: {kind: 'explain', restaging: false, systems: []}, progress: {done: 1, total: 3}});
      window.__btn = document.querySelector('#notices [data-id=progress] button'); true""")
    assert b.js("!!window.__btn")
    b.js(
        "tt.set('song', {...tt.song, progress: {done: 2, total: 3}}); tt.set('song', {...tt.song, rev: tt.song.rev + 1}); true"
    )
    assert b.js("document.querySelector('#notices [data-id=progress] button') === window.__btn")
    assert "2/3" in b.js("document.querySelector('#notices [data-id=progress]').textContent")
    b.js("tt.set('song', {...tt.song, activity: null, progress: null}); true")
    assert not b.js("document.querySelector('#notices [data-id=progress]')")
    assert not b.errors, b.errors


def test_corrupt_song_stops_polling(server, browser):
    """曲子数据损坏（500 带说明）：说清楚原因，不再“稍后自动重试”反复请求。"""
    from app import songs

    store = server["store"]
    sid = songs.create_song([("bad.gp", _gp_bytes())])
    try:
        d = store.song_dir(sid)
        for name in ("song.json", "song.json.bak"):
            (d / name).write_text("{broken")
        b = browser
        b._run(
            b._send(
                "Page.addScriptToEvaluateOnNewDocument",
                source=f"""window.__gets = 0; const of = window.fetch;
                window.fetch = (u, i) => {{ if (String(u) === '/api/songs/{sid}') window.__gets++; return of(u, i); }};""",
            )
        )
        b.open(f"{server['base']}/trainer?id={sid}")
        gone = "(document.getElementById('songGone') || {}).textContent || ''"
        b.wait_for(f"{gone}.includes('数据损坏')", timeout=20)
        time.sleep(6)  # 退避重试的话这段时间里至少还会再请求一次
        assert b.js("window.__gets") == 1
        text = b.js(gone)
        assert "自动重试" not in text and "重试" in text and "返回曲库" in text
        assert b.js("document.getElementById('songTitle').textContent") == "加载失败"
        assert b.js("document.querySelector('.practice-grid').getClientRects().length") == 0
        b.js("document.getElementById('goneRetry').click(); true")  # 手动重试：再请求一次
        b.wait_for("window.__gets === 2")
    finally:
        shutil.rmtree(store.song_dir(sid), ignore_errors=True)


def test_unknown_song_shows_an_empty_state(server, browser):
    """曲子不存在：不显示空谱面、假的小节卡片和问 AI，只有一句说明和“返回曲库”。"""
    b = browser
    b.open(f"{server['base']}/trainer?id=no-such-song#tune")
    b.wait_for("!!document.getElementById('songGone')")
    assert "曲子不存在" in b.js("document.getElementById('songGone').textContent")
    assert b.js("document.querySelector('#songGone a.btn').getAttribute('href')") == "/"
    assert b.js("!document.getElementById('goneRetry')")
    for sel in (".practice-grid", "#fbDock", "#nav", "#view-tune"):
        assert b.js(f"document.querySelector('{sel}').getClientRects().length") == 0, sel
    assert b.js("location.hash") == "#practice"  # 不在调弦页开麦克风
    assert not b.errors, b.errors


def test_load_failure_offers_retry(server, browser):
    """第一次就连不上服务：说明 + “重试”，重试成功后正常显示练习页。"""
    b = browser
    b.open(f"{server['base']}/trainer?id={server['image']}")
    b._run(
        b._send(
            "Page.addScriptToEvaluateOnNewDocument",
            source="""window.__fail = true; const of = window.fetch;
            window.fetch = (u, i) => window.__fail && String(u).startsWith('/api/songs/')
              ? Promise.reject(new TypeError('Failed to fetch')) : of(u, i);""",
        )
    )
    b._run(b._send("Page.navigate", url=f"{server['base']}/trainer?id={server['image']}&offline=1"))
    b.wait_for("location.search.includes('offline') && !!document.getElementById('goneRetry')")
    assert "加载失败" in b.js("document.getElementById('songGone').textContent")
    assert b.js("document.querySelector('.practice-grid').getClientRects().length") == 0
    b.js("window.__fail = false; document.getElementById('goneRetry').click(); true")
    b.wait_for("!document.getElementById('songGone') && !!tt.song")
    assert b.js("document.querySelector('.practice-grid').getClientRects().length") > 0
    assert b.js("document.getElementById('songTitle').textContent") == "E2E 图片曲"


def test_rhythm_drill_scores_taps(trainer):
    """敲节奏：打开后节奏卡片代替侧栏的小节卡片（指板收起）；敲击按音频时钟配对，每遍打分；
    空格是敲击不是停止，Esc 停止；关掉后小节卡片回来，播放键又是 alphaTab。"""
    b = trainer()
    rh = "tt.rhythm.state"
    b.js("tt.set('play.countIn', false); tt.setLoop(0, 0); document.activeElement.blur(); true")
    b.js("document.getElementById('rhythmBtn').click(); true")
    assert b.js("tt.get('rhythm.on')")
    assert b.js("document.getElementById('rhythmBtn').getAttribute('aria-pressed')") == "true"
    assert b.js("getComputedStyle(document.getElementById('rhythmCard')).display") != "none"
    assert b.js("document.getElementById('barCard').hidden")
    assert b.js("document.getElementById('fbDock').hidden")
    assert b.js("document.querySelectorAll('#rhSvg .rh-note').length") == 4  # 第 1 小节四个四分音符
    assert b.js("document.getElementById('rhRange').textContent") == "第 1 小节 · 4 个音"
    assert "空格开始" in b.js("document.getElementById('rhResult').textContent")
    assert "Esc" in b.js("document.getElementById('rhHelp').textContent")  # 按键说明在“?”里

    # 开始（播放键 / 空格开始的是敲节奏，不是 alphaTab）
    b.js("tt.setTempo(120); tt.play(); true")
    b.wait_for(f"{rh}.run && {rh}.run.loops.some(l => l.kind === 'loop')")
    assert not b.js("tt.state.playing") and b.js("tt.get('rhythm.running')")
    assert b.js("document.getElementById('playBtn').classList.contains('on')")  # 播放键显示成停止
    # 第一遍：前三个音敲准（晚 10 ms），第四个不敲，另外在两个音中间多敲一下
    b.js(
        f"const L = {rh}.run.loops.find(l => l.kind === 'loop');"
        "[0, 1, 2].forEach(i => tt.rhythm.tapAt(L.notes[i].T + 0.01)); tt.rhythm.tapAt((L.notes[1].T + L.notes[2].T) / 2); true"
    )
    b.wait_for(f"{rh}.last && {rh}.last.n === 1", timeout=10)
    s = b.js(f"{rh}.last")
    assert (s["good"], s["ok"], s["miss"], s["extra"], s["score"], s["mean"]) == (3, 0, 1, 1, 60, 10)
    text = b.js("document.getElementById('rhResult').textContent")
    assert "60" in text and "漏了" not in text and "多敲了 1 下" in text
    # 空格是敲击，不会停下（也不会开始 alphaTab）；Esc 才停
    b.key(" ", code="Space", vk=32, text=" ")
    assert b.js(f"!!{rh}.run") and not b.js("tt.state.playing")
    b.key("Escape", vk=27)
    b.wait_for(f"!{rh}.run")
    assert not b.js("tt.get('rhythm.running')") and b.js("tt.get('rhythm.on')")
    assert not b.js("document.getElementById('playBtn').classList.contains('on')")
    # 停着时空格开始的也是敲节奏
    b.key(" ", code="Space", vk=32, text=" ")
    b.wait_for(f"!!{rh}.run")
    assert not b.js("tt.state.playing")
    b.key("Escape", vk=27)
    b.wait_for(f"!{rh}.run")
    # 换段：成绩清掉，节奏条换成新的段
    b.js("tt.setLoop(1, 2); true")
    assert b.js(f"{rh}.last") is None and b.js("document.querySelectorAll('#rhSvg .rh-note').length") == 8
    assert b.js("document.getElementById('rhRange').textContent") == "第 2–3 小节 · 8 个音"
    # 关掉：小节卡片和指板回来，播放键又是 alphaTab
    b.js("document.getElementById('rhClose').click(); true")
    assert not b.js("tt.get('rhythm.on')")
    assert b.js("document.getElementById('rhythmCard').hidden")
    assert not b.js("document.getElementById('barCard').hidden") and not b.js(
        "document.getElementById('fbDock').hidden"
    )
    b.js("tt.play(); true")
    b.wait_for("tt.state.playing")
    assert not b.js(f"!!{rh}.run")
    b.js("tt.stop(); true")
    assert not b.errors, b.errors


def test_rhythm_calibration_measures_latency(trainer):
    b = trainer()
    rh = "tt.rhythm.state"
    b.js("document.getElementById('rhythmBtn').click(); document.getElementById('rhCalib').click(); true")
    b.wait_for(f"{rh}.run && {rh}.run.calib && {rh}.run.loops.some(l => l.kind === 'loop')")
    # 每一下都晚 80 ms：补偿值就是 +80 ms
    b.js(
        f"const L = {rh}.run.loops.find(l => l.kind === 'loop'); L.notes.forEach(n => tt.rhythm.tapAt(n.T + 0.08)); true"
    )
    b.wait_for(f"!{rh}.run", timeout=20)
    assert b.js(f"{rh}.latency") == 80 and b.js("localStorage.getItem('tt.tapLatency')") == "80"
    assert "+80 ms" in b.js("document.getElementById('rhCalib').textContent")
    # 校准完回到原来的节奏条，校准那一遍不算成绩
    assert b.js(f"{rh}.last") is None
    assert b.js("document.querySelectorAll('#rhSvg .rh-note').length") == b.js(f"{rh}.prog.onsets.length")
    b.js("localStorage.removeItem('tt.tapLatency'); true")
    assert not b.errors, b.errors


def test_rerender_during_playback_keeps_position(trainer):
    """播放中谱面重画（拖窗口、切主题、通知出现）：循环范围没变，不跳回段首。"""
    b = trainer()
    b.js("tt.set('play.countIn', false); tt.setLoop(1, 4); tt.setTempo(240); tt.play(); true")
    b.wait_for("tt.state.playing")
    time.sleep(1.2)
    start = b.js("tt.api.playbackRange.startTick")
    t1 = b.js("tt.api.tickPosition")
    assert t1 > start + 200
    b.js("tt.api.render(); true")
    time.sleep(0.5)
    assert b.js("tt.api.tickPosition") > t1
    b.js("tt.stop(); true")
    assert not b.errors, b.errors


def _mouse(b, x, y):
    for t in ("mousePressed", "mouseReleased"):
        b._run(b._send("Input.dispatchMouseEvent", type=t, x=x, y=y, button="left", clickCount=1))


def test_clicking_a_note_auditions_without_changing_loop(trainer):
    """点音符只试听，不把循环改成那一小节；点小节里的空白处才循环这一小节。"""
    b = trainer()
    b.wait_for("!!(tt.api.boundsLookup && tt.api.tracks[0].staves[0].bars.length === 6)")
    b.js("tt.setLoop(0, 3); true")
    b.js("document.querySelector('#score .at-surface').scrollIntoView({block: 'center'}); true")
    time.sleep(0.3)
    pos = b.js("""(() => {
      const beat = tt.api.tracks[0].staves[0].bars[1].voices[0].beats.find(x => x.notes.length);
      const nb = tt.api.boundsLookup.findBeat(beat).notes[0].noteHeadBounds;
      const s = document.querySelector('#score .at-surface').getBoundingClientRect();
      return [s.left + nb.x + nb.w / 2, s.top + nb.y + nb.h / 2]; })()""")
    _mouse(b, *pos)
    time.sleep(0.3)
    assert b.js("[tt.state.a, tt.state.b]") == [0, 3]
    assert not b.errors, b.errors


def test_song_info_form_edits_artist(server, trainer):
    """曲名 / 艺人：点顶栏的曲名打开对话框；图片曲子能改艺人；GP 曲子只能改曲名，调弦页提示以原文件为准。"""
    sid, store = server["image"], server["store"]
    old = store.load(sid)["meta"].get("artist", "")
    b = trainer()
    try:
        b.js("document.getElementById('songTitle').click(); true")
        b.wait_for("document.getElementById('metaDlg').open")
        assert b.js("document.activeElement.id") == "mTitle"
        assert not b.js("document.getElementById('mArtistLbl').hidden")
        b.js(
            "document.getElementById('mArtist').value = '测试艺人'; document.getElementById('metaDlgSave').click(); true"
        )
        b.wait_for("tt.song.meta.artist === '测试艺人'")
        b.wait_for("!document.getElementById('metaDlg').open")
        assert b.js("tt.song.title") == "E2E 图片曲"
        assert _song(server, sid)["meta"]["artist"] == "测试艺人"
    finally:
        store.update(
            sid, lambda s: (s["meta"].update(artist=old), s.get("meta_user", {}).pop("artist", None))
        )
    b = trainer(server["gp"])
    b.wait_for("tt.song.status === 'ready'")
    b.js("document.getElementById('titleEditBtn').click(); true")
    b.wait_for("document.getElementById('metaDlg').open")
    assert b.js("document.getElementById('mArtistLbl').hidden")
    b.key("Escape", vk=27)
    b.wait_for("!document.getElementById('metaDlg').open")
    assert "原文件" in b.js("document.getElementById('metaMsg').textContent")
    assert b.js("document.getElementById('metaSave').hidden")
    assert not b.errors, b.errors


def test_tuner_mic_follows_the_tune_page(trainer):
    """进调弦页自动打开麦克风（没有开关按钮），离开就关掉，麦克风不会一直开着；再进来再打开。"""
    b = trainer()
    b.js("""(() => { window.__streams = []; const md = navigator.mediaDevices, of = md.getUserMedia.bind(md);
      md.getUserMedia = async c => { const s = await of(c); __streams.push(s); return s; }; return true; })()""")
    assert b.js("document.getElementById('micBtn') === null")
    assert b.js("__streams.length") == 0  # 练习页不开麦克风
    b.js("location.hash = '#tune'; true")
    b.wait_for("__streams.length === 1 && __streams[0].getTracks().every(t => t.readyState === 'live')")
    b.wait_for("document.getElementById('tRead').textContent.includes('弹一根空弦')")
    b.js("location.hash = '#practice'; true")
    b.wait_for("__streams[0].getTracks().every(t => t.readyState === 'ended')")
    b.js("location.hash = '#tune'; true")
    b.wait_for("__streams.length === 2 && __streams[1].getTracks().every(t => t.readyState === 'live')")
    b.js("location.hash = '#glossary'; true")
    b.wait_for("__streams[1].getTracks().every(t => t.readyState === 'ended')")
    assert not b.errors, b.errors


def test_tuner_explains_a_denied_mic(trainer):
    """不给麦克风权限：调音器那里说清楚去哪里允许，调弦表的“听”照常能用。"""
    b = trainer()
    b.js("""(() => { navigator.mediaDevices.getUserMedia = async () => {
        throw new DOMException('Permission denied', 'NotAllowedError'); }; return true; })()""")
    b.js("location.hash = '#tune'; true")
    b.wait_for("document.getElementById('tRead').textContent.includes('没有麦克风权限')")
    assert "地址栏" in b.js("document.getElementById('tRead').textContent")
    assert not b.errors, b.errors


def test_tuner_reading_settles_on_a_steady_tone(trainer):
    """调音器：麦克风换成合成的 A2 锯齿波（低 10 音分），读数稳在 5 弦 −10 音分、指针不抖；
    调到正好 A2 显示“准了”；没声音了先保留读数，再回到待机。"""
    b = trainer()
    b.js("""(() => { navigator.mediaDevices.getUserMedia = async () => {
        const c = new AudioContext(), o = c.createOscillator(), g = c.createGain(), d = c.createMediaStreamDestination();
        o.type = 'sawtooth'; o.frequency.value = 110 * 2 ** (-10 / 1200); g.gain.value = 0.2;
        o.connect(g); g.connect(d); o.start(); await c.resume();
        window.__osc = { o, g }; return d.stream; };
      return true; })()""")
    b.js("location.hash = '#tune'; true")  # 进调弦页就自动开麦克风
    read = "document.getElementById('tRead').textContent"
    b.wait_for(f"{read}.includes('5 弦') && {read}.includes('-10 音分')", timeout=10)
    seen = set()
    for _ in range(12):  # 一秒多：读数和指针位置一直不变
        seen.add(b.js(f"[{read}, document.getElementById('tNeedle').style.left].join('|')"))
        time.sleep(0.1)
    assert len(seen) == 1, seen
    text, left = seen.pop().split("|")
    assert "目标 A2" in text and "偏低，往上调" in text and left == "40%", text
    assert b.js("document.getElementById('tNote').textContent") == "A2"
    assert b.js("document.getElementById('tr5').classList.contains('hit')")
    assert not b.js("document.getElementById('tNeedle').classList.contains('ok')")
    # 拧准了
    b.js("__osc.o.frequency.value = 110; true")
    b.wait_for(f"{read}.includes('，0 音分') && {read}.includes('准了')", timeout=5)
    assert b.js("document.getElementById('tNeedle').classList.contains('ok')")
    assert b.js("document.getElementById('tNeedle').style.left") == "50%"
    # 没声音了：保留一会儿读数，然后回到待机
    b.js("__osc.g.gain.value = 0; true")
    b.wait_for("document.getElementById('tNeedle').classList.contains('held')", timeout=3)
    b.wait_for("document.getElementById('tNote').textContent === '--'", timeout=5)
    assert "弹一根空弦" in b.js(read)
    b.js("location.hash = '#practice'; true")
    assert not b.errors, b.errors


_FAKE_MIC = """(() => { window.__streams = [];
  navigator.mediaDevices.getUserMedia = async () => {
    const c = new AudioContext(), o = c.createOscillator(), d = c.createMediaStreamDestination();
    o.frequency.value = 110; o.connect(d); o.start(); await c.resume(); __streams.push(d.stream); return d.stream; };
  return true; })()"""
_LIVE = "__streams.at(-1).getTracks().every(t => t.readyState === 'live')"
_ENDED = "__streams.at(-1).getTracks().every(t => t.readyState === 'ended')"


def test_tuner_opened_directly_before_the_song_loads(server, browser):
    """直接打开（刷新）#tune、曲子还没加载完就开了麦克风：先按标准调弦识别，不能出错卡在“正在打开麦克风…”；
    曲子到了换成它的调弦，照样有读数。"""
    b = browser
    b.open(f"{server['base']}/trainer?id={server['image']}")
    b._run(
        b._send(
            "Page.addScriptToEvaluateOnNewDocument",
            source=_FAKE_MIC
            + """; (() => { const of = window.fetch.bind(window);  // 曲子等测试放行（__go）才到
              const gate = new Promise(r => { window.__go = r; });
              window.fetch = (u, i) => String(u).includes('/api/songs/') ? gate.then(() => of(u, i)) : of(u, i); })();""",
        )
    )
    b._run(b._send("Page.navigate", url=f"{server['base']}/trainer?id={server['image']}&direct=1#tune"))
    read = "document.getElementById('tRead').textContent"
    b.wait_for(f"location.search.includes('direct') && window.__streams && __streams.length === 1 && {_LIVE}")
    b.wait_for(f"{read}.includes('5 弦')", timeout=5)  # 曲子还没到：按标准调弦识别出 5 弦 A2
    assert b.js("!tt.view")
    b.js("__go(); true")
    b.wait_for("!!tt.view && !!document.getElementById('tr5')")
    b.wait_for(f"{read}.includes('5 弦 → 目标 A2')", timeout=5)  # 曲子到了：还在读
    assert b.js("document.getElementById('tr5').classList.contains('hit')")
    b.js("location.hash = '#practice'; true")
    b.wait_for(_ENDED)
    assert not b.errors, b.errors


def test_tuner_mic_stops_while_the_page_is_hidden(trainer):
    """切到别的标签页 / 最小化时关掉麦克风，回来还在调弦页就重新打开；关页面（pagehide）也关掉。"""
    b = trainer()
    b.js(_FAKE_MIC)
    hide = """(h => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => h});
      document.dispatchEvent(new Event('visibilitychange')); return true; })"""
    b.js("location.hash = '#tune'; true")
    b.wait_for(f"__streams.length === 1 && {_LIVE}")
    b.js(f"{hide}(true)")
    b.wait_for(_ENDED)
    b.js("location.hash = '#practice'; location.hash = '#tune'; true")  # 在后台切过来也不开
    time.sleep(0.5)
    assert b.js("__streams.length") == 1
    b.js(f"{hide}(false)")
    b.wait_for(f"__streams.length === 2 && {_LIVE}")
    b.wait_for("document.getElementById('tRead').textContent.includes('5 弦')", timeout=5)
    b.js("dispatchEvent(new PageTransitionEvent('pagehide')); true")
    b.wait_for(_ENDED)
    assert not b.errors, b.errors


def test_leaving_the_practice_page_stops_playback(trainer):
    """切到调弦等页面时停下播放（播放键在练习页，切走就停不了；调弦的麦克风也会听到）；预备拍里切走也停，之后不会自己开始。"""
    b = trainer()
    b.js("tt.set('play.countIn', false); tt.setLoop(0, 5); tt.setTempo(120); tt.play(); true")
    b.wait_for("tt.state.playing")
    b.js("location.hash = '#tune'; true")
    b.wait_for("!tt.state.playing", timeout=3)
    b.js("location.hash = '#practice'; true")
    b.js("tt.set('play.countIn', true); tt.play(); true")
    b.wait_for("tt.state.playing")
    time.sleep(0.3)  # 还在预备拍里
    b.js("location.hash = '#plan'; true")
    b.wait_for("!tt.state.playing", timeout=3)
    time.sleep(2.5)  # 预备拍里停下要重新生成 MIDI：生成好了也不能接着放
    assert not b.js("tt.state.playing")
    assert not b.errors, b.errors


def test_song_info_shows_the_lock_message(trainer):
    """精修进行中改曲子信息：服务返回 423 时把它的说明原样告诉用户。"""
    b = trainer()
    b.js("""(() => { const of = window.fetch; window.fetch = (u, i) => String(u).endsWith('/meta') && i && i.method === 'PUT'
      ? Promise.resolve(new Response(JSON.stringify({detail: '正在精修，完成后再改曲子信息'}), {status: 423}))
      : of(u, i); return true; })()""")
    b.js("location.hash = '#tune'; document.getElementById('metaSave').click(); true")
    b.wait_for(
        "[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('正在精修，完成后再改曲子信息'))"
    )
    assert not b.errors, b.errors


def test_song_info_says_why_settings_are_locked(trainer):
    """调弦、变调夹和拍号被锁住时说清楚原因：精修中说“正在精修”，识别中说“正在识别”。"""
    b = trainer()
    b.js("location.hash = '#tune'; true")
    for kind, word in (("refine", "正在精修"), ("parse", "正在识别")):
        b.js(
            f"tt.set('song.activity', {{kind: '{kind}'}}); tt.set('song.editable_meta', ['title', 'artist']); true"
        )
        b.wait_for(f"document.getElementById('metaMsg').textContent.startsWith('{word}')")
        b.js("tt.set('song.editable_meta', ['title', 'artist', 'tuning', 'capo', 'time_signature']); true")
    assert not b.errors, b.errors


def _viewport(b, w, h):
    b._run(
        b._send("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=1, mobile=False)
    )


def test_follow_keeps_the_next_row_in_view(trainer):
    """自动跟随（默认开）：弹着这一行时，下一行（含和弦、歌词）也要完整露出来——还在这一行就提前滚好；
    循环段尾那一行的“下一行”是段首那一行；手动滚动后停几秒不抢。"""
    b = trainer()
    _viewport(b, 1400, 640)
    b.js(
        "document.body.style.paddingBottom = '2000px'; true"
    )  # 只有两行的曲子：页面加高，最后一行也能滚到控制条下面
    time.sleep(1)  # 窗口变了，谱面重画
    b.wait_for("tt.followDelta(0) !== null")
    # 这个宽度下第 1–5 小节一行、第 6 小节在下一行；页面在最上面时第二行露不全
    rows = "[0, 5].map(i => tt.player.rowOfBar(i))"
    assert b.js(rows) == [0, 1]
    b.js("scrollTo(0, 0); true")
    time.sleep(0.2)
    assert b.js("tt.followDelta(0)") > 0
    b.js("tt.set('play.countIn', false); tt.setLoop(0, 5); tt.setTempo(240); true")
    b.js("tt.play(); true")
    b.wait_for("tt.state.playing")
    # 还在第一行（一小节 1 秒，第一行 5 小节）就已经滚好了：两行都看得见
    b.wait_for("scrollY > 0 && tt.followDelta(0) === 0", timeout=3)
    assert b.js("tt.view.all[tt.state.note].bar") <= 3
    bottom = b.js("""(() => { const r = tt.player.scoreRow(1), s = document.getElementById('score').getBoundingClientRect();
      return s.top + r.y + r.h; })()""")
    assert bottom <= b.js("innerHeight")
    b.js("tt.stop(); true")
    # 循环第 5–6 小节：在段尾那一行（第二行）时，下一行是段首那一行（第一行）。窗口高一些（两行放得下），
    # 第二行顶在控制条下面、第一行滚出去了：全曲时第二行是最后一行，不用滚；循环时要往回滚，把第一行也露出来
    _viewport(b, 1400, 1100)
    time.sleep(1)
    b.wait_for("tt.player.rowOfBar(5) === 1")
    b.js("""(() => { const r = tt.player.scoreRow(1), s = document.getElementById('score').getBoundingClientRect();
      const t = document.getElementById('transport'); scrollTo(0, scrollY + s.top + r.y - (parseFloat(getComputedStyle(t).top) + t.offsetHeight + 16)); return true; })()""")
    time.sleep(0.3)
    assert b.js("tt.followDelta(5)") == 0
    assert b.js("tt.followDelta(5, 4, 5)") < 0
    # 手动滚轮往上翻：停几秒，不会马上又被拽回去
    b.js("tt.play(); true")
    b.wait_for("tt.state.playing")
    b._run(b._send("Input.dispatchMouseEvent", type="mouseWheel", x=600, y=400, deltaX=0, deltaY=-2000))
    b.wait_for("tt.follow.pausedUntil > Date.now()")
    time.sleep(0.4)
    y2 = b.js("scrollY")
    time.sleep(1.5)
    assert b.js("scrollY") == y2
    b.js("tt.stop(); true")
    assert not b.errors, b.errors


def test_play_settings_survive_reload(trainer):
    """节拍器、预备拍、音量、自动跟随记在本机，换一首 / 重新打开还在；存坏了的值用默认值。"""
    b = trainer()
    b.js("""(() => { const ch = (id, v) => { const el = document.getElementById(id);
        if (el.type === 'checkbox') { el.checked = v; el.dispatchEvent(new Event('change')); }
        else { el.value = v; el.dispatchEvent(new Event('input')); } };
      ch('metro', false); ch('countIn', false); ch('gtVol', 33); ch('metroVol', 12);
      ch('follow', false); return true; })()""")
    b = trainer()
    got = b.js(
        "(({metro, countIn, gtVol, metroVol, follow}) => ({metro, countIn, gtVol, metroVol, follow}))(tt.state)"
    )
    assert got == {
        "metro": False,
        "countIn": False,
        "gtVol": 33,
        "metroVol": 12,
        "follow": False,
    }
    assert (
        not b.js("document.getElementById('follow').checked")
        and b.js("document.getElementById('gtVol').value") == "33"
    )
    b.js(
        """localStorage.setItem('tt.playPrefs', '{"gtVol": "x", "metro": 1, "metroVol": 999, "follow": false}'); true"""
    )
    b = trainer()
    assert b.js("[tt.state.gtVol, tt.state.metro, tt.state.metroVol, tt.state.follow]") == [
        80,
        True,
        70,
        False,
    ]
    b.js("localStorage.setItem('tt.playPrefs', 'not json'); true")
    b = trainer()
    assert b.js("tt.state.follow") is True and b.js("tt.state.gtVol") == 80
    assert not b.errors, b.errors


def test_metronome_only_without_the_demo_guitar(trainer):
    """关掉示范音只剩节拍器：吉他音轨音量 0，节拍器照常响（以前示范音量用的是总音量，节拍器跟着一起没了）。"""
    b = trainer()
    b.js("""(() => { window.__vols = [];
      const orig = tt.api.changeTrackVolume.bind(tt.api);
      tt.api.changeTrackVolume = (tracks, v) => { window.__vols.push(v); return orig(tracks, v); };
      tt.set('play.metro', true); tt.set('play.demo', false); })()""")
    assert b.js("window.__vols[window.__vols.length - 1]") == 0
    assert b.js("tt.api.masterVolume") == 1 and b.js("tt.api.metronomeVolume") > 0
    b.js("tt.set('play.demo', true)")
    assert b.js("window.__vols[window.__vols.length - 1]") > 0
    assert b.js("JSON.parse(localStorage.getItem('tt.playPrefs')).demo") is True
    assert not b.errors, b.errors


def test_note_audition_is_audible_with_the_demo_guitar_off(trainer):
    """关掉示范音后点音符试听：playNote 走的是同一个音轨通道，试听前要把音轨音量放回来，开始播放时再按开关静音。"""
    b = trainer()
    b.js("""(() => { window.__log = [];
      const vol = tt.api.changeTrackVolume.bind(tt.api), note = tt.api.playNote.bind(tt.api), play = tt.api.play.bind(tt.api);
      tt.api.changeTrackVolume = (tracks, v) => { window.__log.push(['vol', v]); return vol(tracks, v); };
      tt.api.playNote = n => { window.__log.push(['note']); return note(n); };
      tt.api.play = () => { window.__log.push(['play']); return play(); };
      tt.set('play.demo', false); return true; })()""")
    b.js("""(() => { window.__log = [];
      const beat = tt.api.tracks[0].staves[0].bars[0].voices[0].beats.find(x => x.notes.length);
      tt.api.noteMouseDown.trigger(beat.notes[0]); return true; })()""")
    log = b.js("window.__log")
    assert log[-1] == ["note"] and log[-2][0] == "vol"
    assert log[-2][1] == b.js("tt.state.gtVol") / 100 > 0  # 试听时音轨音量是吉他音量，不是 0
    b.js("window.__log = []; tt.play(); true")
    b.wait_for("window.__log.some(x => x[0] === 'play')")
    log = b.js("window.__log")
    k = log.index(["play"])
    assert ["vol", 0] in log[:k]  # 开始播放前又按示范音开关静音了
    b.js("tt.stop(); true")
    assert not b.errors, b.errors


def test_side_panel_ends_above_the_fretboard_dock(trainer):
    """侧栏最下面（“识别有误？”）不能被底部的指板挡住：--dock-h 要是指板的实际高度。"""
    b = trainer()
    b.wait_for("parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--dock-h')) > 0")
    side, dock = b.js(
        "[document.querySelector('.side').getBoundingClientRect().bottom, document.getElementById('fbDock').getBoundingClientRect().top]"
    )
    assert side <= dock + 1


def test_clicking_a_bar_starts_playback_there_without_looping_it(trainer):
    """点谱面上的小节：播放位置跳到这个小节开头，循环范围不变；点到范围外面就改成全曲。Shift + 点才扩大范围。"""
    b = trainer()
    b.wait_for("!!(tt.api.tracks && tt.api.boundsLookup)")
    beat = "tt.api.tracks[0].staves[0].bars[{}].voices[0].beats[0]"
    b.js("tt.setLoop(0, 2); true")
    b.js(f"tt.onBeatClick({beat.format(1)}); true")
    assert [b.js("tt.state.a"), b.js("tt.state.b")] == [0, 2]
    assert b.js("tt.api.tickPosition === tt.api.score.masterBars[1].start") and b.js("tt.get('ui.bar')") == 1
    b.js(f"tt.onBeatClick({beat.format(4)}); true")  # 范围外：改成全曲，从第 5 小节开始
    assert [b.js("tt.state.a"), b.js("tt.state.b")] == [0, 5]
    assert b.js("tt.api.tickPosition === tt.api.score.masterBars[4].start")
    assert not b.errors, b.errors


def test_choosing_a_section_scrolls_it_into_view(trainer):
    """点上面的段落按钮（或 ←→、输入起止小节）选了别的段：谱面自己滚到这一段，不用手动去找。"""
    b = trainer()
    _viewport(b, 1400, 640)
    b.js("document.body.style.paddingBottom = '2000px'; true")
    time.sleep(1)
    b.wait_for("tt.player.rowOfBar(5) === 1")
    b.js("scrollTo(0, 0); true")
    time.sleep(0.2)
    b.js("""document.querySelector('#secBtns button[data-a="3"]').click(); true""")  # 第 4–6 小节：跨两行
    b.wait_for("tt.state.a === 3 && tt.state.b === 5")
    b.wait_for("scrollY > 0 && tt.followDelta(3, 3, 5) === 0", timeout=3)
    bottom = b.js("""(() => { const r = tt.player.scoreRow(1), s = document.getElementById('score').getBoundingClientRect();
      return s.top + r.y + r.h; })()""")
    assert bottom <= b.js("innerHeight")
    assert not b.errors, b.errors


def test_rating_survives_later_song_updates(server, trainer):
    """打了分以后曲子的其他部分变了（练完一遍写练习记录、问答、轮询）：打分按钮不变回“打分”。
    以前 on('song') 每次都把（旧的）song.study 抄回打分状态，下一遍练完按钮就复原了。"""
    sid = server["image"]
    req = urllib.request.Request(
        f"{server['base']}/api/songs/{sid}/study",
        data=json.dumps({"in_plan": True}).encode(),
        method="PUT",
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req)
    try:
        b = trainer()
        b.wait_for("!document.getElementById('rateBtn').hidden")
        b.js("document.getElementById('rateBtn').click(); true")
        b.wait_for("!document.getElementById('ratePop').hidden")
        b.js("document.querySelector('#rateBtns .r3').click(); true")
        b.wait_for("document.getElementById('rateBtn').classList.contains('rated')")
        # 练完一遍：practice-log 把新的练习记录写进 song.practice（别的 song.* 写入也一样）
        b.js("tt.set('song.practice', { ...(tt.song.practice || {}), total_seconds: 12345 }); true")
        b.js("tt.set('song.bars.0.qa', [{ q: '问', a: '答', at: '' }]); true")
        assert b.js("document.getElementById('rateBtn').classList.contains('rated')")
        assert b.js("tt.song.study.rated_today") == 3
        b.js("tt.refresh(); true")  # 轮询到的也是打过分的状态
        time.sleep(0.5)
        assert b.js("document.getElementById('rateBtn').classList.contains('rated')")
        assert not b.errors, b.errors
    finally:
        req = urllib.request.Request(
            f"{server['base']}/api/songs/{sid}/study",
            data=json.dumps({"in_plan": False}).encode(),
            method="PUT",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req)


def test_original_image_popup_survives_the_song_getting_shorter(trainer):
    """原谱弹窗开着时曲子变短（重新识别、删页）：看的小节没了就关掉弹窗，不报错。"""
    b = trainer()
    b.wait_for("!!(tt.api.tracks && tt.api.tracks[0].staves[0].bars.length === 6 && tt.api.boundsLookup)")
    b.js("tt.onBeatClick(tt.api.tracks[0].staves[0].bars[5].voices[0].beats[0]); true")
    b.wait_for("!document.getElementById('cropBox').hidden")
    b.js("document.getElementById('cropBox').click(); true")
    assert b.js("document.getElementById('origTitle').textContent") == "原谱 · 第 6 小节"
    b.js("const s = tt.song; tt.set('song', { ...s, bars: s.bars.slice(0, 2) }); true")
    b.wait_for("!document.getElementById('origModal').open", timeout=3)
    assert b.js("tt.get('ui.orig')") == -1
    b.js("window.dispatchEvent(new Event('resize')); document.dispatchEvent(new Event('tt-images')); true")
    assert not b.errors, b.errors


def test_arrow_keys_on_a_focused_control_belong_to_the_control(trainer):
    """焦点在速度滑块上按 ←→：只改速度（滑块自己的），不换段；在顶栏标签页上按 →：切页面，回到练习页时也不换段。"""
    b = trainer()
    label = "['loopA', 'loopB'].map(id => document.getElementById(id).value).join('–')"
    assert b.js(label) == "1–6"
    bpm = b.js("tt.state.bpm")
    b.js("document.getElementById('bpm').focus(); true")
    b.key("ArrowRight", vk=39)
    assert b.js(label) == "1–6"
    assert b.js("tt.state.bpm") == bpm + 1  # 浏览器自带：滑块加一格
    b.key("ArrowLeft", vk=37)
    assert b.js(label) == "1–6" and b.js("tt.state.bpm") == bpm
    # 顶栏标签页：从最后一页（记号说明）按 → 绕回练习页
    b.js("location.hash = '#glossary'; true")
    b.wait_for("tt.get('ui.view') === 'glossary'")
    b.js("document.querySelector('#nav [data-v=\"glossary\"]').focus(); true")
    b.key("ArrowRight", vk=39)
    b.wait_for("tt.get('ui.view') === 'practice'")
    assert b.js(label) == "1–6"
    # 焦点离开控件以后：←→ 照常换段
    b.js("document.activeElement.blur(); true")
    b.key("ArrowRight", vk=39)
    assert b.js(label) == "1–3"
    assert not b.errors, b.errors


def test_home_returns_to_the_range_start_and_m_toggles_the_metronome(trainer):
    """Home：回到循环范围的开头（播放中就从段首重新开始）；M：节拍器开 / 关。两个都写在按钮的悬停提示里。"""
    b = trainer()
    b.js(
        "document.activeElement.blur(); tt.set('play.countIn', false); tt.setLoop(3, 5); tt.setTempo(200); true"
    )
    start = b.js("tt.api.playbackRange.startTick")
    b.js("tt.play(); true")
    b.wait_for(f"tt.state.playing && tt.api.tickPosition > {start} + 1500", timeout=10)
    b.key("Home", vk=36)
    b.wait_for(f"tt.api.tickPosition < {start} + 1000", timeout=3)
    assert b.js("tt.state.playing") and b.js("tt.state.loops") == 0
    assert b.js("tt.get('ui.bar')") == 3
    b.js("tt.stop(); true")
    b.wait_for("!tt.state.playing")
    # 停着：光标放到段首
    b.js(
        "tt.onBeatClick(tt.api.tracks[0].staves[0].bars[5].voices[0].beats[0]); document.activeElement.blur(); true"
    )
    b.wait_for(f"tt.api.tickPosition > {start} + 1000")
    b.key("Home", vk=36)
    b.wait_for(f"Math.abs(tt.api.tickPosition - {start}) <= 2")
    metro = b.js("tt.state.metro")
    b.key("m", code="KeyM", vk=77, text="m")
    assert b.js("tt.state.metro") is (not metro)
    assert b.js("tt.api.metronomeVolume") == (0 if metro else b.js("tt.state.metroVol") / 100 * 1.2)
    b.key("m", code="KeyM", vk=77, text="m")
    assert b.js("tt.state.metro") is metro
    assert "Home" in b.js("document.getElementById('playBtn').title")
    assert "M" in b.js("document.getElementById('metro').closest('label').title")
    assert not b.errors, b.errors


def test_undo_and_edit_mode_only_on_the_practice_view(server, trainer, image_song_restore):
    """⌘Z 只在练习页、没有浮层时撤销改音；离开练习页就退出改音模式。"""
    b = trainer()
    b.js("""(() => { tt.setEditMode(true);
      const beat = tt.api.score.tracks[0].staves[0].bars[2].voices[0].beats.find(x => x.notes.length);
      tt.openNotePop(beat, beat.notes[0]); document.getElementById('npFret').value = '7'; return true; })()""")
    b.key("Enter", vk=13)
    b.wait_for("!tt.editor.saving && tt.editor.undo === 1")
    b.js("document.activeElement.blur(); true")
    # 有浮层（“更多”弹层）开着：不撤销
    b.js("document.getElementById('moreBtn').click(); true")
    b.wait_for("!document.getElementById('morePop').hidden")
    b.key("z", code="KeyZ", vk=90, modifiers=UNDO_MOD)
    time.sleep(0.3)
    assert b.js("tt.editor.undo") == 1
    b.key("Escape", vk=27)
    # 切到调弦页：退出改音模式，⌘Z 也不撤销
    b.js("location.hash = '#tune'; true")
    b.wait_for("tt.get('ui.view') === 'tune'")
    assert b.js("tt.get('ui.editMode')") is False
    b.key("z", code="KeyZ", vk=90, modifiers=UNDO_MOD)
    time.sleep(0.3)
    assert b.js("tt.editor.undo") == 1
    # 回到练习页、重新打开改音：可以撤销
    b.js("location.hash = '#practice'; true")
    b.wait_for("tt.get('ui.view') === 'practice'")
    b.js("tt.setEditMode(true); document.activeElement.blur(); true")
    b.key("z", code="KeyZ", vk=90, modifiers=UNDO_MOD)
    b.wait_for("!tt.editor.saving && tt.editor.undo === 0")
    b.js("tt.setEditMode(false); true")
    assert not b.errors, b.errors


@pytest.fixture
def image_song_restore(server):
    """改了图片曲子的测试：结束后把小节恢复原样（其他测试也用这首）。"""
    sid, store = server["image"], server["store"]
    raw = store.load(sid)["bars"]
    yield
    store.update(sid, lambda s: s.update(bars=raw))


# ---------------------------------------------------------------------------
# 界面：谱面的地方、控制条、抽屉、小节卡片
# ---------------------------------------------------------------------------
def _open_at(server, b, w, h, sid=None, query=""):
    _viewport(b, w, h)
    b.open(f"{server['base']}/trainer?id={sid or server['image']}{query}")
    b.wait_for("window.tt && tt.state.ready && !!document.querySelector('#score .at-surface')", timeout=40)
    return b


def test_score_gets_most_of_a_1440x900_screen(server, browser):
    """1440×900 第一次打开：通知合成一行（+N 展开）、选段只有一行、指板收成细栏，谱面至少露出 560 px。"""
    b = _open_at(server, browser, 1440, 900)
    b.js("""import('/static/js/trainer/notices.js').then(({ notice }) => {
      notice('resume', { kind: 'info', html: '上次练到', actions: [{ label: '接着练', primary: true, onClick: () => {} }] });
      notice('metaNote', { kind: '', html: '备注' }); return true; })""")
    time.sleep(0.3)
    assert b.js("document.getElementById('fbBody').hidden")  # 矮屏幕默认收起指板
    top, dock = b.js(
        "[document.querySelector('#score .at-surface').getBoundingClientRect().top, document.getElementById('fbDock').getBoundingClientRect().top]"
    )
    assert dock - top >= 560, (top, dock)
    rows = b.js(
        "new Set([...document.querySelectorAll('#secBtns button')].map(x => Math.round(x.getBoundingClientRect().top))).size"
    )
    assert rows == 1
    assert not b.errors, b.errors


def test_control_bar_stays_on_one_row_from_1000_to_1440(server, browser):
    """窗口 1000–1440：控制条一行放得下（按钮依次收成图标），不会有孤零零换到第二行的按钮，也不超出卡片。"""
    b = _open_at(server, browser, 1440, 900)
    check = """(() => { const row = document.querySelector('#transport .t-row'), rr = row.getBoundingClientRect();
      const cs = [...row.children].filter(x => x.offsetParent).map(x => x.getBoundingClientRect());
      const mid = cs.map(c => c.top + c.height / 2);
      return [Math.max(...mid) - Math.min(...mid) <= 12, cs.every(c => c.right <= rr.right + 1)]; })()"""
    for playing in (False, True):
        b.js(
            f"document.getElementById('transport').classList.toggle('playing', {str(playing).lower()}); true"
        )
        for w in (1000, 1040, 1100, 1150, 1199, 1200, 1280, 1360, 1440):
            _viewport(b, w, 900)
            time.sleep(0.15)
            assert b.js(check) == [True, True], (w, playing)
    assert not b.errors, b.errors


def test_narrow_window_puts_the_bar_card_in_a_drawer(server, browser):
    """窗口窄于 1000px：小节卡片是右边滑出的抽屉——按钮打开、焦点移进去，Esc 收起、焦点回到按钮；
    抽屉开着时播放快捷键照常；打开敲节奏时抽屉跟着打开。"""
    b = _open_at(server, browser, 990, 800)
    hidden = "getComputedStyle(document.getElementById('side')).visibility === 'hidden'"
    b.wait_for(hidden)
    assert b.js("document.getElementById('side').inert")
    b.js("document.getElementById('drawerBtn').click(); true")
    b.wait_for(f"!({hidden})")
    assert b.js("document.getElementById('side').contains(document.activeElement)")
    left = b.js("document.getElementById('side').getBoundingClientRect().left")
    assert 990 - 400 < left < 990
    bpm = b.js("tt.state.bpm")
    b.js("document.activeElement.blur(); true")
    b.key("ArrowUp", vk=38)  # 不是浮层：快捷键照常
    assert b.js("tt.state.bpm") == bpm + 4
    b.js("document.getElementById('drawerClose').focus(); true")
    b.key("Escape", vk=27)
    b.wait_for(hidden)
    assert b.js("document.activeElement.id") == "drawerBtn"
    b.js("document.getElementById('rhythmBtn').click(); true")
    b.wait_for(f"!({hidden})")
    assert b.js("tt.get('ui.drawer')")
    b.js("tt.rhythm.toggle(false); true")
    _viewport(b, 1200, 800)  # 变宽：不再是抽屉
    b.wait_for(f"!({hidden}) && !document.getElementById('side').inert")
    assert not b.errors, b.errors


def test_bar_card_order_and_one_correction_section(trainer):
    """小节卡片：原谱紧跟在提示后面，问答不自己滚；纠错集中在一块（改音 / 改数据 → 哪里不对 + AI 核对 → 重新识别整行）；
    待核对的小节标题栏有“核对”；“改音”进入改音模式，谱面标题栏只剩“完成”。"""
    b = trainer()
    b.js("tt.set('ui.bar', 1); true")
    order = b.js(
        "[...document.querySelectorAll('.bar-card .body > *')].filter(x => !x.hidden).map(x => x.id || x.className)"
    )
    assert (
        order.index("tipText") < order.index("cropBox") < order.index("practiceLine") < order.index("fixBox")
    )
    assert b.js("getComputedStyle(document.getElementById('qaList')).overflowY") == "visible"
    fix = b.js(
        "[...document.querySelectorAll('#fixBox button, #fixBox input')].filter(x => x.offsetParent).map(x => x.id)"
    )
    assert fix == ["barEditBtn", "editBtn", "hintInput", "refineBarBtn", "refineRowBtn", "reparseBtn"]
    assert "本小节" in b.js("document.getElementById('practiceLine').textContent")
    assert b.js("document.getElementById('barCheckBtn').hidden")
    b.js(
        """tt.set('song', {...tt.song, bars: tt.song.bars.map((x, i) => i === 1 ? {...x, needs_review: true} : x)}); true"""
    )
    b.wait_for("!document.getElementById('barCheckBtn').hidden")
    assert "核对" in b.js("document.getElementById('barCheckBtn').textContent")
    assert b.js("document.getElementById('editModeBtn').hidden")
    b.js("document.getElementById('barEditBtn').click(); true")
    assert b.js("tt.get('ui.editMode')") and not b.js("document.getElementById('editModeBtn').hidden")
    assert "改音" in b.js("document.getElementById('scoreHelp').textContent")
    b.js("document.getElementById('editModeBtn').click(); true")
    assert not b.js("tt.get('ui.editMode')") and b.js("document.getElementById('editModeBtn').hidden")
    assert "快捷键" in b.js("document.getElementById('scoreHelp').textContent")
    # 整首练了多久：副标题最后，悬停说明不带日期的旧记录
    b.js(
        "tt.set('song.practice', {...tt.song.practice, time: {total_seconds: 600, undated_seconds: 120}}); true"
    )
    assert b.js("document.getElementById('songSub').textContent").endswith("练了 10 分钟")
    assert "旧版本" in b.js("document.getElementById('songSub').title")
    assert not b.errors, b.errors


def test_gp_bar_card_shows_its_plan_section(server, trainer):
    """GP 曲子：没有纠错（小节来自原文件），卡片上显示这一小节所在的分段，可以直接循环它。"""
    b = trainer(server["gp"])
    b.wait_for("tt.song.bars.length === 3")
    assert b.js("document.getElementById('fixBox').hidden")
    b.js("""tt.set('song', {...tt.song, plan: {overview: '', steps: [{bars: '2-3', title: '后半', advice: '慢练换把'}]}});
      tt.set('ui.bar', 2); true""")
    b.wait_for("!document.getElementById('barPlan').hidden")
    assert "后半" in b.js("document.getElementById('barPlan').textContent")
    b.js("document.querySelector('#barPlan button').click(); true")
    assert [b.js("tt.state.a"), b.js("tt.state.b")] == [1, 2]
    b.js("tt.set('ui.bar', 0); true")
    assert b.js("document.getElementById('barPlan').hidden")
    assert not b.errors, b.errors


def test_tuning_is_spelled_one_way(trainer):
    """调弦只用一种写法：alphaTab 自己的调弦说明（降号：③=Gb）不画，非标准调弦在谱面标题栏用我们的升号写法写出来。"""
    b = trainer()
    assert (
        b.js("tt.api.settings.notation.elements.get(tt.player.alphaTab.NotationElement.GuitarTuning)")
        is False
    )
    assert b.js("document.getElementById('tuningLine').textContent") == ""  # 标准调弦不写
    b.js("tt.set('song', {...tt.song, meta: {...tt.song.meta, tuning_midi: [62, 57, 54, 49, 45, 38]}}); true")
    b.wait_for("document.getElementById('tuningLine').textContent !== ''")
    assert b.js("document.getElementById('tuningLine').textContent") == "⑥D ⑤A ④C♯ ③F♯ ②A ①D"
    assert "C♯ F♯" in b.js("document.getElementById('songSub').textContent")
    assert not b.errors, b.errors


def test_section_chips_switch_between_rows_and_plan_sections(server, trainer):
    """选段可以按原谱的行或 AI 分的段落（记住选择）；段落的按钮直接循环那一段；没有练习建议时没有这个开关。"""
    b = trainer()
    assert not b.js("document.getElementById('secMode').hidden")
    chips = "[...document.querySelectorAll('#secBtns button')].map(x => x.textContent)"
    assert b.js(chips) == ["全曲", "1–3", "4–6"]
    b.js("document.querySelector('#secMode [data-v=plan]').click(); true")
    assert b.js(chips) == ["全曲", "1 第一行"]
    assert b.js("localStorage.getItem('tt.secMode')") == "plan"
    b.js("document.querySelectorAll('#secBtns button')[1].click(); true")
    assert [b.js("tt.state.a"), b.js("tt.state.b")] == [0, 2]
    assert b.js("tt.get('ui.secs')") == [{"a": 0, "z": 2}]
    b = trainer()  # 重新打开：还是按段落
    assert b.js(chips) == ["全曲", "1 第一行"]
    b.js("document.querySelector('#secMode [data-v=rows]').click(); true")
    b = trainer(server["gp"])
    b.wait_for("tt.song.bars.length === 3")
    assert b.js("document.getElementById('secMode').hidden")
    assert not b.errors, b.errors


def test_ai_actions_are_greyed_out_without_an_api_key(trainer):
    """没有 API key：AI 的按钮灰掉，悬停提示说先去设置里填，点了不发请求、弹出说明（可以去设置）。"""
    b = trainer()
    b.wait_for("tt.get('ui.hasKey') === false")  # e2e 的服务没有 key
    assert b.js("document.documentElement.classList.contains('no-ai-key')")
    b.js("""(() => { const of = window.fetch; window.__ai = [];
      window.fetch = (u, i) => { if (/refine|ask|reparse|explain/.test(String(u))) __ai.push(String(u)); return of(u, i); };
      return true; })()""")
    b.js("tt.set('ui.bar', 1); true")
    b.wait_for("!document.getElementById('refineBarBtn').hidden")
    assert float(b.js("getComputedStyle(document.getElementById('refineBarBtn')).opacity")) < 0.6
    b.js(
        "document.getElementById('refineBarBtn').dispatchEvent(new PointerEvent('pointerover', {bubbles: true})); true"
    )
    assert b.js("document.getElementById('refineBarBtn').title") == "先在设置里填 API key"
    b.js("document.getElementById('refineBarBtn').click(); true")
    b.wait_for("!!document.querySelector('.dlg-msg[open]')")
    assert "API key" in b.js("document.querySelector('.dlg-msg[open]').textContent")
    b.key("Escape", vk=27)
    b.js(
        "document.getElementById('askInput').value = '怎么练'; document.getElementById('askBtn').click(); true"
    )
    b.wait_for("!!document.querySelector('.dlg-msg[open]')")
    b.key("Escape", vk=27)
    time.sleep(0.3)
    assert b.js("__ai") == []
    assert all(
        b.js(f"document.getElementById('{i}').hasAttribute('data-ai')")
        for i in ("refineBtn", "reparseBtn", "askBtn", "barCheckBtn")
    )
    assert not b.errors, b.errors
