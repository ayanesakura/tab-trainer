"""练习页的冒烟测试：加载、播放和练习记录、改音和撤销、方向键换段、GP 导入（成功 / 失败）、曲库页。"""

import json
import sys
import time
import urllib.request

import pytest

pytestmark = pytest.mark.e2e
UNDO_MOD = 4 if sys.platform == "darwin" else 2  # ⌘ / Ctrl


def _song(server, sid):
    return json.load(urllib.request.urlopen(f"{server['base']}/api/songs/{sid}"))


def test_practice_page_loads_cleanly(trainer):
    b = trainer()
    assert b.js("window.scrollY") == 0  # 打开时不会跳到谱面
    assert b.js("document.querySelectorAll('#secBtns button').length") == 3  # 全曲 + 两行
    assert b.js("document.getElementById('tipText').textContent") == "第 1 小节的提示"
    assert b.js("SONG.bars.length") == 6
    assert not b.errors, b.errors


def test_playback_records_completed_loops(server, trainer):
    b = trainer()
    b.js(
        "document.getElementById('countIn').checked = false; applyAudio(); setLoop(0, 0); setTempo(240); play(); true"
    )
    time.sleep(4)
    b.js("stop(); true")
    b.wait_for("!state.playing")
    time.sleep(0.8)
    p = _song(server, server["image"])["practice"]["bars"]["bar1"]
    assert p["loops"] >= 2 and p["best_bpm"] == 240
    # 播放中换段：不能把跳回起点误记成弹完一遍
    before = p["loops"]
    b.js("setLoop(0, 0); play(); true")
    time.sleep(1.2)
    b.js("setLoop(3, 3); setLoop(4, 4); stop(); true")
    time.sleep(0.8)
    bars = _song(server, server["image"])["practice"]["bars"]
    assert bars.get("bar4", {}).get("loops", 0) == 0 and bars.get("bar5", {}).get("loops", 0) == 0
    assert bars["bar1"]["loops"] >= before
    assert not b.errors, b.errors


def test_edit_note_then_undo(server, trainer):
    b = trainer()
    orig = _song(server, server["image"])["bars"][1]
    b.js("""(() => { setEditMode(true);
      const beat = api.score.tracks[0].staves[0].bars[1].voices[0].beats.find(x => x.notes.length);
      openNotePop(beat, beat.notes[0]); document.getElementById('npFret').value = '9'; return true; })()""")
    b.key("Enter", vk=13)
    b.wait_for("!saving && undoStack.length === 1")
    edited = _song(server, server["image"])["bars"][1]
    assert edited["events"][0]["notes"][0]["fret"] == 9 and edited["tip"] == ""
    b.js("document.activeElement.blur(); true")
    b.key("z", code="KeyZ", vk=90, modifiers=UNDO_MOD)
    b.wait_for("!saving && undoStack.length === 0")
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
    bpm = b.js("state.bpm")
    b.key("ArrowUp", vk=38)
    assert b.js("state.bpm") == bpm + 4


def test_guitar_pro_import_becomes_ready(server, trainer):
    b = trainer(server["gp"])
    b.wait_for("SONG.bars.length === 3 && SONG.status === 'ready'")
    s = _song(server, server["gp"])
    assert s["status"] == "ready" and s["meta"]["tuning"][5] == "E2" and len(s["bars"]) == 3
    assert s["derived"]["version"] == b.js("scoreConv.DERIVE_VERSION") and s["derived"]["alphatab"]
    assert not b.errors, b.errors


def test_outdated_derived_data_is_rebuilt_once(server, trainer):
    """转换规则升级以后（这里把记下的版本改旧）：打开时重新转换一次，内容没变的小节保留 id 和提示。"""
    store = server["store"]
    trainer(server["gp"]).wait_for("SONG.bars.length === 3 && SONG.status === 'ready'")

    def age(s):
        s["derived"]["version"] = 0
        s["bars"][0]["tip"] = "保留的提示"

    store.update(server["gp"], age)
    before = _song(server, server["gp"])
    b = trainer(server["gp"])
    b.wait_for("SONG.derived && SONG.derived.version === scoreConv.DERIVE_VERSION && state.ready")
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
    browser.wait_for("typeof SONG !== 'undefined' && SONG && SONG.status === 'error'", timeout=30)
    assert "曲谱读取失败" in browser.js("document.getElementById('errorBox').innerText")
    assert _song(server, server["broken"])["status"] == "error"


def test_library_lists_songs(server, browser):
    browser.open(server["base"] + "/")
    browser.wait_for("document.getElementById('songs').innerText.includes('E2E 图片曲')")
    text = browser.js("document.getElementById('songs').innerText")
    assert "E2E 图片曲" in text and "曲谱读取失败" in text
    assert not browser.errors, browser.errors
