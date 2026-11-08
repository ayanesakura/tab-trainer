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
    """播放中后台识别出新内容（谱子重新加载）：接着播放，不算弹完一遍、不记练习、不自动提速。"""
    sid, store = image_song
    ids = ["bar3", "bar4", "bar5"]
    before = _loops(server, sid, ids)
    b = trainer()
    b.js(
        "tt.set('play.countIn', false); tt.set('play.auto', true); tt.set('play.autoEvery', 1);"
        "tt.set('play.autoStep', 10); tt.set('play.autoTarget', 200); tt.setLoop(2, 4); tt.setTempo(100); tt.play(); true"
    )
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
    b.js("tt.set('play.auto', false); true")
    assert not b.errors, b.errors


def test_reselecting_the_current_section_while_playing_is_not_a_loop(server, trainer):
    """播放中再点一次正在循环的段：从段首重新开始，不算弹完一遍。"""
    sid = server["image"]
    ids = ["bar4", "bar5", "bar6"]
    before = _loops(server, sid, ids)
    b = trainer()
    b.js(
        "tt.set('play.countIn', false); tt.set('play.auto', false); tt.setLoop(3, 5); tt.setTempo(60); tt.play(); true"
    )
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
