"""练习页的播放：谱子重新加载（改音、后台识别出新的一行）时的播放位置和遍数，播放中重选同一段，预备拍里换位置。"""

import json
import time
import urllib.request

import pytest

pytestmark = pytest.mark.e2e


def _song(server, sid):
    return json.load(urllib.request.urlopen(f"{server['base']}/api/songs/{sid}"))


@pytest.fixture
def image_song(server):
    """改图片曲子的测试：结束后把小节恢复原样（其他测试也用这首）。"""
    sid, store = server["image"], server["store"]
    raw = store.load(sid)["bars"]
    yield sid, store
    store.update(sid, lambda s: s.update(bars=raw))


def _loops(server, sid, ids):
    bars = _song(server, sid)["practice"]["bars"]
    return {i: bars.get(i, {}).get("loops", 0) for i in ids}


def test_edit_while_looping_keeps_the_cursor_in_the_loop(trainer, image_song):
    """循环第 4–5 小节时改了一个音（谱子重新加载）：再播放从段首开始，不是从第 1 小节。"""
    b = trainer()
    b.js("tt.set('play.countIn', false); tt.setLoop(3, 4); tt.setTempo(120); true")
    start = b.js("tt.api.playbackRange.startTick")
    b.wait_for(f"Math.abs(tt.api.tickPosition - {start}) <= 2")
    b.js("""(() => { tt.setEditMode(true);
      const beat = tt.api.score.tracks[0].staves[0].bars[3].voices[0].beats.find(x => x.notes.length);
      tt.openNotePop(beat, beat.notes[0]); document.getElementById('npFret').value = '9'; return true; })()""")
    b.key("Enter", vk=13)
    b.wait_for("!tt.editor.saving && tt.editor.undo === 1")
    b.wait_for("tt.view.source.tex.includes('9.')")
    time.sleep(1.0)  # 重新加载完
    assert abs(b.js("tt.api.tickPosition") - start) <= 2
    b.js("tt.setEditMode(false); tt.play(); true")
    b.wait_for("tt.state.playing")
    time.sleep(0.4)
    assert b.js("document.getElementById('statusL1').textContent").startswith("第 4 小节")
    b.js("tt.stop(); true")
    assert not b.errors, b.errors


def test_background_update_while_playing_is_not_a_finished_loop(server, trainer, image_song):
    """播放中后台识别出新内容（谱子重新加载）：接着播放，不算弹完一遍、不记练习。"""
    sid, store = image_song
    ids = ["bar3", "bar4", "bar5"]
    before = _loops(server, sid, ids)
    b = trainer()
    b.js("tt.set('play.countIn', false); tt.setLoop(2, 4); tt.setTempo(100); tt.play(); true")
    b.wait_for("tt.state.playing")
    time.sleep(1.0)
    t1 = b.js("tt.api.tickPosition")
    store.update(sid, lambda s: s["bars"][0]["events"][0]["notes"][0].update(fret=9))
    b.js("tt.refresh(); true")
    b.wait_for("tt.view.source.tex.includes('9.')")
    time.sleep(1.0)
    assert b.js("tt.state.playing")
    assert b.js("tt.state.loops") == 0 and b.js("tt.state.bpm") == 100
    r = b.js("[tt.api.playbackRange.startTick, tt.api.playbackRange.endTick]")
    assert (
        r[0] <= b.js("tt.api.tickPosition") < r[1] and b.js("tt.api.tickPosition") > t1
    )  # 从原来的位置接着走
    b.js("tt.stop(); true")
    b.wait_for("!tt.state.playing")
    time.sleep(0.8)
    assert _loops(server, sid, ids) == before
    assert not b.errors, b.errors


def test_reselecting_the_current_section_while_playing_is_not_a_loop(server, trainer):
    """播放中再点一次正在循环的段：从段首重新开始，不算弹完一遍。"""
    sid = server["image"]
    ids = ["bar4", "bar5", "bar6"]
    before = _loops(server, sid, ids)
    b = trainer()
    b.js("tt.set('play.countIn', false); tt.setLoop(3, 5); tt.setTempo(60); tt.play(); true")
    b.wait_for("tt.state.playing")
    time.sleep(1.5)
    b.js("""document.querySelector('#secBtns button[data-a="3"]').click(); true""")
    time.sleep(1.0)
    assert b.js("tt.state.loops") == 0
    b.js("tt.stop(); true")
    b.wait_for("!tt.state.playing")
    time.sleep(0.8)
    assert _loops(server, sid, ids) == before
    assert not b.errors, b.errors


def test_clicking_an_earlier_bar_during_count_in_then_pausing(trainer):
    """预备拍里点了前面的小节：停下以后位置留在点的小节，不回到开始播放的地方。"""
    b = trainer()
    b.wait_for("!!(tt.api.tracks && tt.api.boundsLookup)")
    b.js("tt.set('play.countIn', true); tt.setLoop(2, 3); tt.setTempo(60); true")
    start = b.js("tt.api.playbackRange.startTick")
    b.wait_for(f"Math.abs(tt.api.tickPosition - {start}) <= 2")
    b.js("tt.play(); true")
    time.sleep(0.4)  # 60 BPM 一小节预备拍 4 秒：还在预备拍里
    b.js("tt.onBeatClick(tt.api.tracks[0].staves[0].bars[0].voices[0].beats[0]); true")
    time.sleep(0.3)
    b.js("tt.stop(); true")
    b.wait_for("!tt.state.playing")
    time.sleep(1.0)  # 预备拍里停下要重新生成 MIDI
    assert b.js("tt.api.tickPosition") < start - 960
    assert not b.errors, b.errors


def test_status_counts_beats_in_the_time_signature(server, trainer, image_song):
    """6/8 拍的小节：状态栏数到第 6 拍（八分音符一拍），不是按四分音符数成 1–3 拍。"""
    sid, store = image_song

    def six_eight(s):
        s["bars"][0]["time_signature"] = [6, 8]
        s["bars"][0]["events"] = [
            {"dur": "1/2", "notes": [{"string": 1, "fret": k, "tech": [], "grace": -1, "bend": 0}]}
            for k in range(6)
        ]

    store.update(sid, six_eight)
    b = trainer()
    b.js("""(() => { window.__st = new Set(); const el = document.getElementById('statusL1');
      new MutationObserver(() => __st.add(el.textContent)).observe(el, { childList: true, characterData: true, subtree: true });
      tt.set('play.countIn', false); tt.setLoop(0, 0); tt.setTempo(120); tt.play(); return true; })()""")
    b.wait_for("__st.has('第 1 小节 · 第 6 拍')", timeout=8)
    b.js("tt.stop(); true")
    assert not any("第 7 拍" in x for x in b.js("[...__st]"))
    assert not b.errors, b.errors


def _range(b):
    return b.js("['loopA', 'loopB'].map(id => document.getElementById(id).value).join('–')")


def test_typing_the_loop_range(trainer):
    """控制条上的起止小节可以直接输入：回车生效，起点改到终点后面时终点跟过去，乱打的改回原值，↑↓ 加减一小节。"""
    b = trainer()
    b.wait_for("tt.view && tt.view.bars.length === 6 && !document.getElementById('loopA').disabled")

    def type_(el_id, text):
        b.js(
            f"(() => {{ const el = document.getElementById('{el_id}'); el.focus(); el.select(); return true; }})()"
        )
        b.type_text(text)
        b.key("Enter", vk=13)
        return b.js("[tt.state.a, tt.state.b]")

    assert type_("loopA", "3") == [2, 5]
    assert type_("loopB", "4") == [2, 3]
    assert type_("loopA", "5") == [4, 4]  # 终点跟到起点
    assert type_("loopB", "2") == [1, 1]  # 起点跟到终点
    assert type_("loopB", "99") == [1, 5]  # 夹到最后一小节
    assert type_("loopA", "abc") == [1, 5] and _range(b) == "2–6"  # 不是数字：不改，输入框改回去
    assert b.js("document.activeElement.id") != "loopA"  # 回车后离开输入框，空格等快捷键照常
    # 用鼠标点进输入框：原来的数被全选，打的字替换掉它（不是接在后面变成 "23"）
    r = b.js(
        "(() => { const r = document.getElementById('loopA').getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()"
    )
    b.mouse("mousePressed", *r)
    b.mouse("mouseReleased", *r)
    b.type_text("3")
    b.key("Enter", vk=13)
    assert b.js("[tt.state.a, tt.state.b]") == [2, 5]
    bpm = b.js("tt.state.bpm")
    b.js("document.getElementById('loopB').focus(); true")
    b.key("ArrowDown", vk=40)
    assert (
        b.js("[tt.state.a, tt.state.b]") == [2, 4] and b.js("tt.state.bpm") == bpm
    )  # ↑↓ 在输入框里改小节，不改速度
    assert not b.errors, b.errors


def test_dragging_across_bars_on_the_score_selects_a_loop(trainer):
    """在谱面上按住鼠标拖过几个小节：拖的时候控制条显示拖到的范围，松开后循环这几个小节；往回拖也行。"""
    b = trainer()
    b.wait_for("!!(tt.api.tracks && tt.api.boundsLookup) && tt.view && tt.view.bars.length === 6")

    def at(i):  # 第 i 个小节（下标）在视口里的中心
        return b.js(f"""(() => {{
          const r = tt.api.boundsLookup.findMasterBarByIndex({i}).realBounds;
          const s = document.querySelector('#score .at-surface').getBoundingClientRect();
          return [s.left + r.x + r.w / 2, s.top + r.y + r.h / 2]; }})()""")

    b.js("scrollTo(0, 0); true")
    p1, p2, p3 = at(1), at(2), at(3)
    b.mouse("mousePressed", *p1)
    b.mouse("mouseMoved", *p3, buttons=1)
    assert _range(b) == "2–4" and b.js("[tt.state.a, tt.state.b]") == [0, 5]  # 还没松开：只是预览
    b.mouse("mouseMoved", *p2, buttons=1)
    assert _range(b) == "2–3"
    b.mouse("mouseReleased", *p2)
    b.wait_for("tt.state.a === 1 && tt.state.b === 2")
    # 往回拖：从第 4 小节拖到第 1 小节
    p0 = at(0)
    b.mouse("mousePressed", *p3)
    b.mouse("mouseMoved", *p0, buttons=1)
    b.mouse("mouseReleased", *p0)
    b.wait_for("tt.state.a === 0 && tt.state.b === 3")
    # 只点一下（没拖到别的小节）：不改循环范围
    b.mouse("mousePressed", *p1)
    b.mouse("mouseReleased", *p1)
    time.sleep(0.2)
    assert b.js("[tt.state.a, tt.state.b]") == [0, 3]
    assert not b.errors, b.errors


def test_whole_song_stops_on_the_last_bar(server, trainer):
    """循环全曲：弹到曲末停在最后一小节（算弹完一遍、记练习），不跳回第一小节；再播放从头开始。
    用 GP 曲子：图片曲子的练习记录别的测试要查（不能多出这一遍）。"""
    sid = server["gp"]
    b = trainer(sid)
    n = b.js("tt.view.bars.length")
    last_id = b.js(f"tt.song.bars[{n - 1}].id")
    before = _loops(server, sid, [last_id])[last_id]
    b.js(f"tt.set('play.countIn', false); tt.setLoop(0, {n - 1}); tt.setTempo(240); tt.play(); true")
    b.wait_for("tt.state.playing")
    b.wait_for("!tt.state.playing", timeout=30)
    time.sleep(0.6)
    assert b.js("tt.state.loops") == 1
    assert b.js("tt.get('ui.bar')") == n - 1
    last_start = b.js(f"tt.api.tickCache.masterBars[{n - 1}].start")
    assert (
        abs(b.js("tt.api.tickPosition") - last_start) <= 2
    )  # 光标停在最后一小节开头（播放器取整差 1 tick），不是第一小节
    assert _loops(server, sid, [last_id])[last_id] == before + 1
    # 再播放：从头开始
    b.js("tt.play(); true")
    b.wait_for("tt.state.playing")
    time.sleep(0.3)
    assert b.js("tt.api.tickPosition") < last_start
    assert b.js("tt.get('ui.bar')") == 0
    b.js("tt.stop(); true")
    b.wait_for("!tt.state.playing")
    assert not b.errors, b.errors


def test_section_loop_still_loops(trainer):
    """循环一段（最后一小节在段里也一样）：弹到段尾跳回段首接着弹，不停下。"""
    b = trainer()
    n = b.js("tt.view.bars.length")
    b.js(f"tt.set('play.countIn', false); tt.setLoop({n - 1}, {n - 1}); tt.setTempo(240); tt.play(); true")
    b.wait_for("tt.state.loops >= 1", timeout=20)
    assert b.js("tt.state.playing")
    b.js("tt.stop(); true")
    b.wait_for("!tt.state.playing")
    assert not b.errors, b.errors


def test_count_in_before_every_section_loop(trainer):
    """开着预备拍循环一段：每一遍前都数一小节预备拍（段尾停下、从段首带预备拍重新开始），
    中间播放键不闪成“停止”、遍数照常加；关掉预备拍就是 alphaTab 自己无缝循环。"""
    b = trainer()
    b.js("tt.set('play.countIn', true); tt.setLoop(1, 1); tt.setTempo(240); true")
    assert b.js("tt.api.isLooping") is False
    start = b.js("tt.api.playbackRange.startTick")
    b.js(
        "window.__stops = 0; tt.store.on('play.playing', v => { if (!v) window.__stops++; }); tt.play(); true"
    )
    b.wait_for("tt.state.loops >= 1", timeout=20)
    # 刚弹完第一遍：停在段首数预备拍（240 BPM 一小节 1 秒），位置不往前走
    time.sleep(0.3)
    assert abs(b.js("tt.api.tickPosition") - start) <= 2
    assert b.js("tt.state.playing")
    b.wait_for("tt.state.loops >= 2", timeout=20)
    assert b.js("window.__stops") == 0  # 一直是“播放中”
    b.js("tt.stop(); true")
    b.wait_for("!tt.state.playing")
    # 关掉预备拍：alphaTab 自己循环
    b.js("tt.set('play.countIn', false); true")
    assert b.js("tt.api.isLooping") is True
    assert not b.errors, b.errors


def test_tempo_change_near_the_end_of_a_pass_is_time_weighted(trainer):
    """一遍快弹完时才调快：这一遍记的是按时间加权的平均速度，不是新的速度（不然“最快 BPM”会虚高）。"""
    b = trainer()
    b.js("""(() => { window.__posts = []; const f = window.fetch;
      window.fetch = (u, o) => { if (String(u).endsWith('/practice')) __posts.push(JSON.parse(o.body)); return f(u, o); };
      return true; })()""")
    b.js("tt.set('play.countIn', false); tt.setLoop(0, 0); tt.setTempo(120); tt.play(); true")
    b.wait_for("tt.state.playing")
    time.sleep(1.5)  # 120 BPM 一小节 2 秒：快弹完了
    b.js("tt.setTempo(240); true")
    b.wait_for("__posts.some(p => p.loops === 1)", timeout=5)
    p = b.js("__posts.find(p => p.loops === 1)")
    b.js("tt.stop(); true")
    assert 120 <= p["bpm"] < 200, p
    assert not b.errors, b.errors


def test_at_adapter_smoke(trainer):
    """at-adapter.js 里每个碰 alphaTab 内部结构的函数，对着真实加载好的谱子都调一遍（升级 alphaTab 后先跑这个）。"""
    b = trainer()
    b.wait_for("!!(tt.api.tracks && tt.api.boundsLookup) && tt.view && tt.view.bars.length === 6")
    b.wait_for("tt.at.playedBars(tt.api).length === 6")
    r = b.js("""(() => { const at = tt.at, api = tt.api, root = document.getElementById('score');
      const box = at.barBox(api, root, 1), row = at.scoreRow(api, root, 0);
      const played = at.playedBars(api), sec = at.sectionTicks(api, 1, 2);
      return {
        box: !!box && box.w > 0 && box.h > 0, noBox: at.barBox(api, root, 99),
        row: row && [row.i, row.a, row.b >= 0], noRow: at.scoreRow(api, root, 99),
        rows: [at.rowOfBar(api, 0), at.rowOfBar(api, 5)], noRowOf: at.rowOfBar(api, -1),
        lyrics: Array.isArray(at.lyricSpots(api, root)),
        played: played.map(p => p.bar), last: at.lastPlayedBar(api),
        sec: sec && [sec.start === played[1].start, sec.end === played[2].end],
        barStart: at.barStartTick(api, 3, null) === played[3].start,
        ctx: !!at.audioContext(api) && typeof at.audioContext(api).currentTime === 'number',
        sfEvent: at.onSoundFontLoadFailed(api, () => {}),
        chords: Array.isArray(at.scoreChords(api.score)),
        beats: at.trackBeats(api.tracks[0]).length > 0,
      }; })()""")
    assert r["box"] and r["noBox"] is None
    assert r["row"] == [0, 0, True] and r["noRow"] is None
    assert r["rows"] == [0, 1] and r["noRowOf"] == -1
    assert r["lyrics"] and r["chords"] and r["beats"] and r["sfEvent"] and r["ctx"]
    assert r["played"] == [0, 1, 2, 3, 4, 5] and r["last"]["bar"] == 5
    assert r["sec"] == [True, True] and r["barStart"]
    # 播放范围：设一段、和现在一样就不再设、清回全曲；谱面高亮
    r = b.js("""(() => { const at = tt.at, api = tt.api, p = at.playedBars(api);
      const want = { start: p[1].start, end: p[2].end };
      const first = at.setPlaybackRange(api, want), again = at.setPlaybackRange(api, want), got = at.playbackRange(api);
      at.highlightBars(api, 1, 2); at.highlightBars(api, 0, 5, true);
      const back = tt.setLoop(0, 5); return [first, again, got.start === want.start && got.end === want.end, at.playbackRange(api)]; })()""")
    assert r == [True, False, True, None]
    # 音色：换成尼龙弦再还原（导出 GP 用）
    r = b.js("""(() => { const at = tt.at, s = tt.api.score;
      at.applyTimbre(s, 24); const a = at.currentProgram(s);
      at.applyTimbre(s, 24, true); const orig = at.currentProgram(s);
      at.applyTimbre(s, 25); return [a, orig, at.currentProgram(s)]; })()""")
    assert r[0] == 24 and r[2] == 25 and 24 <= r[1] <= 31
    # 播放光标：开始播放以后有
    b.js("tt.set('play.countIn', false); tt.setLoop(0, 0); tt.play(); true")
    b.wait_for("tt.state.playing && !!tt.at.cursorBar(document.getElementById('score'))")
    b.js("tt.stop(); true")
    # 节拍器的声音：用 alphaTab 自己的合成器渲染（敲节奏的节拍器用它）
    b.js(
        "window.__click = null; tt.at.renderMetronomeClick(tt.api, 48000).then(x => { window.__click = x ? x.length : 0; }); true"
    )
    b.wait_for("window.__click !== null", timeout=10)
    assert b.js("window.__click") > 1000
    assert not b.errors, b.errors


def test_switching_to_an_earlier_section_during_playback_with_count_in(trainer):
    """开着预备拍、播放中换到前面的段：预备拍从新的段首数（以前记成了旧位置，预备拍一直“数不完”）；
    预备拍里停下，位置在新的段首（以前放回旧位置、被夹到段尾），再播放照常数预备拍，不会马上“弹完一遍”。"""
    b = trainer()
    b.js("tt.set('play.countIn', true); tt.setLoop(4, 5); tt.setTempo(240); tt.play(); true")
    b.wait_for(
        "tt.get('play.phase') === 'playing' && tt.api.tickPosition > tt.api.playbackRange.startTick + 480",
        timeout=10,
    )
    b.js("tt.setTempo(60); tt.setLoop(0, 1); true")  # 60 BPM：预备拍 4 秒
    start, end = b.js("[tt.api.playbackRange.startTick, tt.api.playbackRange.endTick]")
    b.wait_for("tt.get('play.phase') === 'countIn'", timeout=3)
    assert b.js("tt.machine.from") == start
    assert b.js("tt.state.playing")
    time.sleep(0.6)
    assert b.js("tt.get('play.phase')") == "countIn" and b.js("tt.state.loops") == 0
    b.js("tt.stop(); true")
    b.wait_for("tt.get('play.phase') === 'idle'", timeout=5)
    time.sleep(0.3)
    assert start <= b.js("tt.api.tickPosition") < end
    b.js("tt.play(); true")
    time.sleep(0.8)
    assert b.js("tt.get('play.phase')") == "countIn" and b.js("tt.state.loops") == 0
    b.js("tt.stop(); true")
    b.wait_for("tt.get('play.phase') === 'idle'", timeout=5)
    assert not b.errors, b.errors


def test_switching_timbre_while_playing_keeps_playing(trainer):
    """播放中换钢弦 / 尼龙弦：重新生成 MIDI 以后接着放（以前停下就不再继续）。"""
    b = trainer()
    b.js("tt.set('play.countIn', false); tt.setLoop(0, 5); tt.setTempo(100); tt.play(); true")
    b.wait_for("tt.get('play.phase') === 'playing'")
    time.sleep(0.5)
    b.js("""document.querySelector('#timbreSeg [data-v="nylon"]').click(); true""")
    b.wait_for("tt.at.currentProgram(tt.api.score) === 24")
    time.sleep(1.0)
    assert b.js("tt.state.playing") and b.js("tt.get('play.phase')") == "playing"
    t1 = b.js("tt.api.tickPosition")
    time.sleep(0.5)
    assert b.js("tt.api.tickPosition") > t1
    b.js("""tt.stop(); document.querySelector('#timbreSeg [data-v="steel"]').click(); true""")
    b.wait_for("!tt.state.playing")
    assert not b.errors, b.errors


def test_clean_pass_marker(trainer):
    """“这遍干净”（Enter / 按钮）：没用过时练习记录不带 clean；标了的那一遍弹完带 clean: true，
    之后没标的遍带 clean: false（连续干净才会清零）；停着时按不算。"""
    b = trainer()
    b.js("""(() => { window.__posts = []; const f = window.fetch;
      window.fetch = (u, o) => { if (String(u).endsWith('/practice')) __posts.push(JSON.parse(o.body)); return f(u, o); };
      return true; })()""")
    b.js("document.activeElement.blur(); true")
    b.key("Enter", vk=13)  # 停着：不标
    assert b.js("tt.state.clean") is False
    b.js("tt.set('play.countIn', false); tt.setLoop(0, 0); tt.setTempo(240); tt.play(); true")
    b.wait_for("__posts.filter(p => p.loops === 1).length >= 1", timeout=6)
    assert "clean" not in b.js("__posts.find(p => p.loops === 1)")
    b.key("Enter", vk=13)
    assert b.js("tt.state.clean") is True
    assert b.js("document.getElementById('cleanBtn').getAttribute('aria-pressed')") == "true"
    b.wait_for("__posts.some(p => p.clean === true)", timeout=6)
    assert b.js("tt.state.clean") is False  # 只管那一遍
    b.wait_for("__posts.some(p => p.clean === false && p.loops === 1)", timeout=6)
    b.js("tt.stop(); true")
    assert not b.errors, b.errors


def test_tempo_shows_the_original_tempo(server, trainer):
    """GP 曲子知道原速：BPM 后面是百分比（悬停看原速），滑块上有原速的刻度；图片曲子没标速度就只写 BPM。"""
    b = trainer()
    assert b.js("document.getElementById('bpmUnit').textContent") == "BPM"
    assert b.js("document.getElementById('bpmOrig').hidden")
    b = trainer(server["gp"])
    b.wait_for("tt.state.origBpm > 0")
    orig = b.js("tt.state.origBpm")
    b.js(f"tt.setTempo({orig} / 2); true")
    assert b.js("document.getElementById('bpmUnit').textContent") == "· 50%"
    assert f"原速 {orig}" in b.js("document.getElementById('bpmBox').title")
    assert not b.js("document.getElementById('bpmOrig').hidden")
    assert not b.errors, b.errors
