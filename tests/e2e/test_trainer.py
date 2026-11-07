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
    assert b.js("document.getElementById('songSource').textContent") == "图片"
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
    assert b.js("tt.state.a") == 1  # 弹窗里换小节不影响练习页的选段（←→ 不会被当成换段）
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
    label = "document.getElementById('loopLabel').textContent"
    assert b.js(label) == "第 1–6 小节"
    b.key("ArrowRight", vk=39)
    assert b.js(label) == "第 1–3 小节"
    b.key("ArrowRight", vk=39)
    assert b.js(label) == "第 4–6 小节"
    b.key("ArrowLeft", vk=37)
    assert b.js(label) == "第 1–3 小节"
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
    assert len(demos) == 10
    for d in demos:  # 每个示范都点一遍：闷音、滑音、击勾弦、两种泛音、倚音各条发声路径都会走到
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
    b.js("document.getElementById('fbBtn').click(); true")
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
    try:
        # 模拟问 AI：后端存了一次（写 qa，顶层 rev 变了），前端把返回的 qa 并进 store
        store.update(sid, lambda s: s["bars"][0].update(qa=[{"q": "Q", "a": "A", "at": "now"}]))
        b.js("""(() => { const of = window.fetch; window.fetch = (u, init) => String(u).includes('/ask')
          ? Promise.resolve(new Response(JSON.stringify({answer: 'A', qa: [{q: 'Q', a: 'A', at: 'now'}]}), {status: 200, headers: {'Content-Type': 'application/json'}}))
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
    """GP 曲子的谱面是异步加载的：接着练定下的速度不能被“原速六成”的默认值盖掉（地址带 resume=1 和点“接着练”都一样）。"""
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
        b.wait_for("document.getElementById('notices').innerText.includes('数据损坏')", timeout=20)
        time.sleep(6)  # 退避重试的话这段时间里至少还会再请求一次
        assert b.js("window.__gets") == 1
        text = b.js("document.getElementById('notices').innerText")
        assert "自动重试" not in text and "重试" in text
        assert b.js("document.getElementById('songTitle').textContent") == "加载失败"
    finally:
        shutil.rmtree(store.song_dir(sid), ignore_errors=True)


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
    assert "怎么练" in b.js("document.getElementById('rhResult').textContent")

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
    """曲子信息：图片曲子能改艺人；GP 曲子只能改曲名，提示以原文件为准。"""
    sid, store = server["image"], server["store"]
    old = store.load(sid)["meta"].get("artist", "")
    b = trainer()
    try:
        assert not b.js("document.getElementById('mArtistLbl').hidden")
        b.js(
            "document.getElementById('mArtist').value = '测试艺人'; document.getElementById('metaSave').click(); true"
        )
        b.wait_for("tt.song.meta.artist === '测试艺人'")
        assert _song(server, sid)["meta"]["artist"] == "测试艺人"
    finally:
        store.update(
            sid, lambda s: (s["meta"].update(artist=old), s.get("meta_user", {}).pop("artist", None))
        )
    b = trainer(server["gp"])
    b.wait_for("tt.song.status === 'ready'")
    assert b.js("document.getElementById('mArtistLbl').hidden")
    assert "原文件" in b.js("document.getElementById('metaMsg').textContent")
    assert not b.errors, b.errors


def test_tuner_mic_stops_when_leaving_tune_page(trainer):
    """调音器开了麦克风：离开调弦页（或再点一次按钮）就关掉，麦克风不会一直开着。"""
    b = trainer()
    b.js("""(() => { window.__streams = []; const md = navigator.mediaDevices, of = md.getUserMedia.bind(md);
      md.getUserMedia = async c => { const s = await of(c); __streams.push(s); return s; }; return true; })()""")
    b.js("location.hash = '#tune'; true")
    b.wait_for("tt.get('ui.view') === 'tune'")
    b.js("document.getElementById('micBtn').click(); true")
    b.wait_for("document.getElementById('micBtn').textContent.includes('已开启')")
    b.js("location.hash = '#practice'; true")
    b.wait_for("__streams[0].getTracks().every(t => t.readyState === 'ended')")
    assert "开启麦克风" in b.js("document.getElementById('micBtn').textContent")
    # 再开一次，用按钮关
    b.js("location.hash = '#tune'; true")
    b.js("document.getElementById('micBtn').click(); true")
    b.wait_for("__streams.length === 2 && document.getElementById('micBtn').textContent.includes('已开启')")
    b.js("document.getElementById('micBtn').click(); true")
    b.wait_for("__streams[1].getTracks().every(t => t.readyState === 'ended')")
    assert not b.errors, b.errors


def _viewport(b, w, h):
    b._run(
        b._send("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=1, mobile=False)
    )


def test_follow_scrolls_cursor_row_into_view(trainer):
    """自动跟随（默认开）：循环跨行时，光标走到下面看不见的那一行，谱面自己滚过去；手动滚动后停几秒不抢。"""
    b = trainer()
    _viewport(b, 1400, 640)
    b.js("document.body.style.paddingBottom = '2000px'; true")  # 只有两行的曲子：页面加高，最后一行也能滚到控制条下面
    time.sleep(1)  # 窗口变了，谱面重画
    b.wait_for("tt.followDelta(4) !== null")
    # 这个宽度下第 1–5 小节一行、第 6 小节在下一行
    b.js("tt.set('play.countIn', false); tt.setLoop(4, 5); tt.setTempo(240); true")
    b.js("scrollTo(0, Math.max(0, scrollY + tt.followDelta(4))); true")  # 第 5 小节那一行刚好在控制条下面
    time.sleep(0.3)
    assert b.js("tt.followDelta(4)") == 0 and b.js("tt.followDelta(5)") > 0  # 第 6 小节在下一行，看不见
    y = b.js("scrollY")
    b.js("tt.play(); true")
    b.wait_for("tt.state.playing")
    b.wait_for("tt.followDelta(5) === 0", timeout=8)  # 进了下一行：自己滚过去了
    assert b.js("scrollY") > y
    # 手动滚轮往上翻：停几秒，不会马上又被拽回去
    b._run(b._send("Input.dispatchMouseEvent", type="mouseWheel", x=600, y=400, deltaX=0, deltaY=-2000))
    b.wait_for("tt.follow.pausedUntil > Date.now()")
    time.sleep(0.4)
    y2 = b.js("scrollY")
    time.sleep(1.5)
    assert b.js("scrollY") == y2
    b.js("tt.stop(); true")
    assert not b.errors, b.errors


def test_play_settings_survive_reload(trainer):
    """节拍器、预备拍、音量、自动提速、自动跟随记在本机，换一首 / 重新打开还在；存坏了的值用默认值。"""
    b = trainer()
    b.js("""(() => { const ch = (id, v) => { const el = document.getElementById(id);
        if (el.type === 'checkbox') { el.checked = v; el.dispatchEvent(new Event('change')); }
        else { el.value = v; el.dispatchEvent(new Event('input')); } };
      ch('metro', false); ch('countIn', false); ch('gtVol', 33); ch('metroVol', 12);
      ch('auto', true); ch('autoEvery', 3); ch('autoStep', 6); ch('follow', false); return true; })()""")
    b = trainer()
    got = b.js(
        "(({metro, countIn, gtVol, metroVol, auto, autoEvery, autoStep, follow}) => "
        "({metro, countIn, gtVol, metroVol, auto, autoEvery, autoStep, follow}))(tt.state)"
    )
    assert got == {
        "metro": False,
        "countIn": False,
        "gtVol": 33,
        "metroVol": 12,
        "auto": True,
        "autoEvery": 3,
        "autoStep": 6,
        "follow": False,
    }
    assert (
        not b.js("document.getElementById('follow').checked")
        and b.js("document.getElementById('gtVol').value") == "33"
    )
    assert "flag" in b.js("document.getElementById('moreBtn').className")  # 自动提速开着：按钮上有点
    b.js(
        """localStorage.setItem('tt.playPrefs', '{"gtVol": "x", "metro": 1, "autoStep": 999, "follow": false}'); true"""
    )
    b = trainer()
    assert b.js("[tt.state.gtVol, tt.state.metro, tt.state.autoStep, tt.state.follow]") == [
        80,
        True,
        4,
        False,
    ]
    b.js("localStorage.setItem('tt.playPrefs', 'not json'); true")
    b = trainer()
    assert b.js("tt.state.follow") is True and b.js("tt.state.gtVol") == 80
    assert not b.errors, b.errors
