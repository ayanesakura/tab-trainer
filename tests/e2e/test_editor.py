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
    b.wait_for(
        "tt.api.tickPosition > tt.api.playbackRange.startTick + 4 * 960 + 100", timeout=8
    )  # 进了第 2 小节
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


def _toast(text):
    return f"[...document.querySelectorAll('.toast')].some(t => t.textContent.includes({json.dumps(text)}))"


def test_undo_for_a_bar_that_no_longer_exists_is_dropped(server, trainer, image_song):
    """撤销的小节已经不在了（这一行重新识别过，小节换了 id）：这条撤销记录作废，不挡着别的小节的撤销。"""
    sid, store = image_song
    orig = _song(server, sid)["bars"][2]["events"]
    b = trainer()
    _edit_fret(b, 2, 7)
    _edit_fret(b, 3, 7)
    assert b.js("tt.editor.undoBars") == ["bar3", "bar4"]
    store.update(sid, lambda s: s["bars"][3].update(id="bar4-new"))
    b.js("tt.refresh().then(() => true)")
    b.wait_for("tt.song.bars[3].id === 'bar4-new'")
    _undo(b)
    b.wait_for(_toast("撤销不了"))
    assert b.js("tt.editor.undoBars") == ["bar3"]
    _undo(b)  # 别的小节照常撤销
    assert b.js("tt.editor.undo") == 0
    assert _song(server, sid)["bars"][2]["events"] == orig
    assert not b.errors, b.errors


def test_bar_locked_423_keeps_undo_history(server, trainer, image_song):
    """423（有任务在跑 / 小节锁着）不是冲突：只提示服务的说明，不刷新、不作废撤销记录，过后还能撤销。409 照旧按冲突处理。"""
    sid, _ = image_song
    orig = _song(server, sid)["bars"][2]["events"]
    b = trainer()
    _edit_fret(b, 2, 7)
    b.js("""(() => { const of = window.fetch; window.__lock = 0; window.fetch = async (u, init) => {
      if (window.__lock && init && init.method === 'PUT' && String(u).includes('/bars/'))
        return new Response(JSON.stringify({ detail: window.__lock === 423 ? '这一行正在重新识别，稍后再改' : '曲子在别处改过了' }),
          { status: window.__lock, headers: { 'Content-Type': 'application/json' } });
      return of(u, init); }; return true; })()""")
    b.js("window.__lock = 423; true")
    _undo(b)
    b.wait_for(_toast("正在重新识别，稍后再改"))
    assert b.js("tt.editor.undoBars") == ["bar3"]  # 撤销记录放回去了
    _edit_fret(b, 2, 9)  # 锁着时改音：没存上，原来的撤销记录也还在
    assert b.js("tt.editor.undoBars") == ["bar3"]
    assert _song(server, sid)["bars"][2]["events"][0]["notes"][0]["fret"] == 7
    assert not b.js(CONFLICT)
    b.js("window.__lock = 0; true")
    _undo(b)
    assert b.js("tt.editor.undo") == 0
    assert _song(server, sid)["bars"][2]["events"] == orig
    # 409：仍是冲突（刷新、这个小节的撤销记录作废）
    _edit_fret(b, 2, 7)
    b.js("window.__lock = 409; true")
    _undo(b)
    b.wait_for(CONFLICT)
    assert b.js("tt.editor.undo") == 0
    b.js("window.__lock = 0; true")
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


def test_note_popup_sets_a_tapped_harmonic_with_its_tapped_fret(server, trainer, image_song):
    """改音面板选“点弦泛音”：出现“点弦”一栏，先填高 12 品，可以改；保存后音符带 th 和 harm，取消 th 后 harm 也去掉。"""
    sid, _ = image_song
    b = trainer()
    _open_note(b, 1, 0, 0)
    assert b.js("document.getElementById('npHarmRow').hidden")
    b.js("document.querySelector('#npTech button[data-t=th]').click(); true")
    assert not b.js("document.getElementById('npHarmRow').hidden")
    assert b.js("document.getElementById('npHarm').value") == "12"
    b.js("document.getElementById('npHarm').value = '19'; document.getElementById('npSave').click(); true")
    b.wait_for("!tt.editor.saving && !tt.editor.np")
    note = _song(server, sid)["bars"][1]["events"][0]["notes"][0]
    assert (note["fret"], note["tech"], note["harm"]) == (0, ["th"], 19)
    time.sleep(0.6)  # 谱面按新数据重画完
    _open_note(b, 1, 0)
    assert b.js("document.getElementById('npHarm').value") == "19"
    b.js("document.querySelector('#npTech button[data-t=ah]').click(); true")  # 换成人工泛音：点弦泛音取消
    assert b.js("document.getElementById('npHarmRow').hidden")
    b.js("document.getElementById('npSave').click(); true")
    b.wait_for("!tt.editor.saving && !tt.editor.np")
    note = _song(server, sid)["bars"][1]["events"][0]["notes"][0]
    assert note["tech"] == ["ah"] and "harm" not in note
    assert not b.errors, b.errors
