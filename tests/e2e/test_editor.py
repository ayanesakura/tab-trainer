"""练习页：改谱的乐观锁（JSON 编辑器、撤销、同一小节连着改）和通知区的顺序。"""

import json
import sys
import time
import urllib.request

import pytest

pytestmark = pytest.mark.e2e


def _song(server, sid):
    return json.load(urllib.request.urlopen(f"{server['base']}/api/songs/{sid}"))


def test_notices_keep_order_regardless_of_arrival(trainer):
    b = trainer()
    order = b.js("""import('/static/js/trainer/notices.js').then(({ notice }) => {
      const ids = ['progress', 'offline', 'error', 'resume', 'explain', 'metaNote'];
      ids.forEach(id => notice(id, null));
      for (const id of ['metaNote', 'resume', 'offline', 'explain', 'progress']) notice(id, { kind: 'info', html: id });
      const got = [...document.getElementById('notices').children].map(x => x.dataset.id);
      ids.forEach(id => notice(id, null));
      return got;
    })""")
    assert order == ["progress", "offline", "resume", "explain", "metaNote"]
    assert not b.errors, b.errors


UNDO_MOD = 4 if sys.platform == "darwin" else 2  # ⌘ / Ctrl
CONFLICT = "[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('别处改过'))"


def _open_note(b, bi, k=0, fret=None):
    """在谱面上点第 bi 小节的第 k 个音（打开改音面板），可选填好品位。"""
    b.js(f"""(() => {{ tt.setEditMode(true);
      const beat = tt.api.score.tracks[0].staves[0].bars[{bi}].voices[0].beats.filter(x => x.notes.length)[{k}];
      tt.openNotePop(beat, beat.notes[0]);
      if ({json.dumps(fret)} !== null && tt.editor.np) document.getElementById('npFret').value = '{fret}';
      return true; }})()""")


def _edit_fret(b, bi, fret, k=0):
    _open_note(b, bi, k, fret)
    b.key("Enter", vk=13)
    b.wait_for("!tt.editor.saving && !tt.editor.np")
    time.sleep(0.6)  # 谱面按新数据重画完


def _undo(b):
    b.js("document.activeElement.blur(); true")
    b.key("z", code="KeyZ", vk=90, modifiers=UNDO_MOD)
    b.wait_for("!tt.editor.saving")
    time.sleep(0.3)


@pytest.fixture
def image_song(server):
    """改图片曲子的测试：结束后把小节恢复原样（其他测试也用这首）。"""
    sid, store = server["image"], server["store"]
    raw = store.load(sid)["bars"]
    yield sid, store
    store.update(sid, lambda s: s.update(bars=raw))


def test_json_editor_saves_against_the_rev_it_was_opened_with(server, trainer, image_song):
    sid, store = image_song
    b = trainer()
    b.js("tt.set('ui.bar', 1); document.getElementById('editBtn').click(); true")
    b.wait_for("!document.getElementById('editor').hidden")
    # 编辑器开着的时候，这个小节在别处改了（页面也已经刷新到新内容）
    store.update(sid, lambda s: s["bars"][1]["events"][0]["notes"][0].update(fret=12))
    b.js("tt.refresh().then(() => true)")
    b.wait_for("tt.song.bars[1].events[0].notes[0].fret === 12")
    b.js("document.getElementById('edSave').click(); true")
    b.wait_for(CONFLICT)
    assert _song(server, sid)["bars"][1]["events"][0]["notes"][0]["fret"] == 12  # 没被编辑器里的旧内容盖掉
    # 冲突后编辑器换成了刷新后的内容和版本：改了再保存就能成功，不会一直 409
    assert not b.js("document.getElementById('editor').hidden")
    assert '"fret":12' in b.js("document.getElementById('edText').value")
    b.js("""(() => { const t = document.getElementById('edText'); t.value = t.value.replace('"fret":12', '"fret":9');
      document.getElementById('edSave').click(); return true; })()""")
    b.wait_for("document.getElementById('editor').hidden")
    assert _song(server, sid)["bars"][1]["events"][0]["notes"][0]["fret"] == 9
    assert not b.errors, b.errors


def test_json_editor_stays_open_while_playback_moves_on(trainer, image_song):
    """播放中打开“修改本小节”：光标走到下一小节，侧栏不切换，编辑器不会关掉。"""
    b = trainer()
    b.js("tt.set('play.countIn', false); tt.setLoop(0, 2); tt.setTempo(240); tt.set('ui.bar', 0); true")
    b.js("document.getElementById('editBtn').click(); true")
    b.wait_for("!document.getElementById('editor').hidden")
    b.js("tt.play(); true")
    b.wait_for("tt.state.playing")
    b.wait_for("tt.api.tickPosition > tt.api.playbackRange.startTick + 4 * 960 + 100", timeout=8)  # 进了第 2 小节
    assert b.js("tt.get('ui.bar')") == 0
    assert not b.js("document.getElementById('editor').hidden")
    b.js("tt.stop(); true")
    assert not b.errors, b.errors


def test_undo_uses_the_rev_after_the_edit(server, trainer, image_song):
    sid, store = image_song
    orig = _song(server, sid)["bars"][2]["events"]
    b = trainer()
    # 同一小节改两次再撤销两次：每次撤销都带对的版本号，不报 409
    _edit_fret(b, 2, 7)
    _edit_fret(b, 2, 8)
    assert b.js("tt.editor.undoRevs.at(-1)") == _song(server, sid)["bars"][2]["rev"]
    _undo(b)
    _undo(b)
    assert not b.js(CONFLICT)
    assert _song(server, sid)["bars"][2]["events"] == orig
    # 改完以后这个小节在别处又改了：撤销报 409，不能把新内容冲掉
    _edit_fret(b, 2, 7)
    store.update(sid, lambda s: s["bars"][2]["events"][0]["notes"][0].update(fret=11))
    b.js("tt.refresh().then(() => true)")
    b.wait_for("tt.song.bars[2].events[0].notes[0].fret === 11")
    _undo(b)
    b.wait_for(CONFLICT)
    assert _song(server, sid)["bars"][2]["events"][0]["notes"][0]["fret"] == 11
    assert b.js("tt.editor.undo") == 0
    assert not b.errors, b.errors


def test_quick_second_edit_waits_for_the_first_save(server, trainer, image_song):
    sid, _ = image_song
    b = trainer()
    # 让保存慢一点，好在第一次保存还没回来时改同一小节的另一个音
    b.js("""(() => { const of = window.fetch; window.fetch = async (u, init) => {
      if (init && init.method === 'PUT' && String(u).includes('/bars/')) await new Promise(r => setTimeout(r, 800));
      return of(u, init); }; return true; })()""")
    _open_note(b, 2, 0, 7)
    b.key("Enter", vk=13)
    b.wait_for("tt.editor.saving === 1")
    _open_note(b, 2, 1, 5)
    b.key("Enter", vk=13)
    b.wait_for("!tt.editor.saving")
    time.sleep(0.6)
    assert not b.js(CONFLICT)
    assert b.js("tt.editor.undoBars") == ["bar3"]  # 第一次的撤销记录还在
    # 存好以后再改就正常
    _edit_fret(b, 2, 5, k=1)
    frets = [e["notes"][0]["fret"] for e in _song(server, sid)["bars"][2]["events"][:2]]
    assert frets == [7, 5] and b.js("tt.editor.undoBars") == ["bar3", "bar3"]
    assert not b.js(CONFLICT)
    assert not b.errors, b.errors


def test_typing_a_question_keeps_the_bar_while_playing(trainer):
    """播放中在“问 AI”里打字：侧栏停在正在看的小节，问题不会挂到光标后来走到的小节上。"""
    b = trainer()
    b.js("tt.set('play.countIn', false); tt.setLoop(0, 2); tt.setTempo(240); tt.set('ui.bar', 0); true")
    b.js("document.getElementById('askInput').focus(); tt.play(); true")
    b.wait_for("tt.state.playing")
    b.wait_for("tt.api.tickPosition > tt.api.playbackRange.startTick + 4 * 960 + 100", timeout=8)
    assert b.js("tt.get('ui.bar')") == 0
    b.js("document.getElementById('askInput').blur(); true")
    b.wait_for("tt.get('ui.bar') > 0", timeout=8)  # 不在输入了就照常跟着播放
    b.js("tt.stop(); true")
    assert not b.errors, b.errors
