"""练习页的冒烟测试：加载、播放和练习记录、改音和撤销、方向键换段、调弦和记号示范音、GP 导入（成功 / 失败）、曲库页。"""

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


def test_playback_does_not_scroll_until_asked(trainer):
    b = trainer()
    # 页面加高，滚到最下面：播放光标在视野上方
    b.js("document.body.style.paddingBottom = '4000px'; scrollTo(0, document.body.scrollHeight); true")
    y = b.js("scrollY")
    assert y > 0 and b.js("document.getElementById('followBtn').hidden")
    b.js("document.getElementById('countIn').checked = false; applyAudio(); setLoop(3, 3); play(); true")
    b.wait_for("state.playing && !document.getElementById('followBtn').hidden")
    time.sleep(1.5)
    assert b.js("scrollY") == y  # 播放时不会自己滚到光标处
    b.js("document.getElementById('followBtn').click(); true")
    b.wait_for("document.getElementById('followBtn').hidden")  # 滚到光标处以后按钮消失
    assert b.js("scrollY") < y and not b.js("cursorOutOfView()")
    b.js("stop(); true")
    assert not b.errors, b.errors


def test_selected_bar_shows_original_image(trainer):
    b = trainer()
    assert b.js("document.getElementById('songSource').textContent") == "图片"
    b.wait_for("!!(api.tracks && api.tracks[0].staves[0].bars.length === 6 && api.boundsLookup)")
    b.js("onBeatClick(api.tracks[0].staves[0].bars[1].voices[0].beats[0]); true")
    b.wait_for("!document.getElementById('origBtn').hidden")
    b.js("document.getElementById('origBtn').click(); true")
    assert not b.js("document.getElementById('origModal').hidden")
    assert b.js("document.getElementById('origTitle').textContent") == "原图 · 第 2 小节"
    b.wait_for("document.getElementById('origCanvas').width > 0")
    b.js(
        "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowRight', code: 'ArrowRight'})); true"
    )
    assert b.js("document.getElementById('origTitle').textContent") == "原图 · 第 3 小节"
    assert b.js("state.a") == 1  # 弹窗里换小节不影响练习页的选段（←→ 不会被当成换段）
    b.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', code: 'Escape'})); true")
    assert b.js("document.getElementById('origModal').hidden")
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
    assert b.js("ac.state") == "running"
    assert not b.errors, b.errors


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
    badges = browser.js(
        "[...document.querySelectorAll('#songs .name .badge')].map(x => x.textContent).sort().join(',')"
    )
    assert badges == "GP,GP,图片"  # 导入渠道：一首图片曲、两首 GP（一首读取失败）
    assert not browser.errors, browser.errors


def test_rhythm_drill_scores_taps(trainer):
    """敲节奏：打开后代替原谱和指板；敲击按音频时钟配对，每遍打分；空格是敲击不是停止，Esc 停止；关掉后播放键回到 alphaTab。"""
    b = trainer()
    b.js("document.getElementById('countIn').checked = false; setLoop(0, 0); true")
    b.js("document.getElementById('rhythmBtn').click(); true")
    assert b.js("getComputedStyle(document.getElementById('rhythmCard')).display") == "block"
    assert b.js("getComputedStyle(document.getElementById('infoGrid')).display") == "none"
    assert b.js("document.querySelectorAll('#rhSvg .rh-note').length") == 4  # 第 1 小节四个四分音符
    assert b.js("document.getElementById('rhRange').textContent") == "第 1 小节 · 4 个音"
    assert "怎么练" in b.js("document.getElementById('rhResult').textContent")

    # 开始（播放键 / 空格开始的是敲节奏，不是 alphaTab）
    b.js("setTempo(120); play(); true")
    b.wait_for("RH.run && RH.run.loops.some(l => l.kind === 'loop')")
    assert not b.js("state.playing")
    # 第一遍：前三个音敲准（晚 10 ms），第四个不敲，另外在两个音中间多敲一下
    b.js(
        "const L = RH.run.loops.find(l => l.kind === 'loop');"
        "[0, 1, 2].forEach(i => rhythmTapAt(L.notes[i].T + 0.01)); rhythmTapAt((L.notes[1].T + L.notes[2].T) / 2); true"
    )
    b.wait_for("RH.last && RH.last.n === 1", timeout=10)
    s = b.js("RH.last")
    assert (s["good"], s["ok"], s["miss"], s["extra"], s["score"], s["mean"]) == (3, 0, 1, 1, 60, 10)
    text = b.js("document.getElementById('rhResult').textContent")
    assert "60" in text and "漏了" not in text and "多敲了 1 下" in text
    # 空格是敲击，不会停下；Esc 才停
    b.key(" ", code="Space", vk=32, text=" ")
    assert b.js("!!RH.run")
    b.key("Escape", vk=27)
    b.wait_for("!RH.run")
    # 换段：成绩清掉，节奏条换成新的段
    b.js("setLoop(1, 2); true")
    assert b.js("RH.last") is None and b.js("document.querySelectorAll('#rhSvg .rh-note').length") == 8
    # 关掉：原谱和指板回来，播放键又是 alphaTab
    b.js("document.getElementById('rhClose').click(); true")
    assert b.js("getComputedStyle(document.getElementById('infoGrid')).display") != "none"
    b.js("play(); true")
    b.wait_for("state.playing")
    b.js("stop(); true")
    assert not b.errors, b.errors


def test_rhythm_calibration_measures_latency(trainer):
    b = trainer()
    b.js("document.getElementById('rhythmBtn').click(); document.getElementById('rhCalib').click(); true")
    b.wait_for("RH.run && RH.run.calib && RH.run.loops.some(l => l.kind === 'loop')")
    # 每一下都晚 80 ms：补偿值就是 +80 ms
    b.js(
        "const L = RH.run.loops.find(l => l.kind === 'loop'); L.notes.forEach(n => rhythmTapAt(n.T + 0.08)); true"
    )
    b.wait_for("!RH.run", timeout=20)
    assert b.js("RH.latency") == 80 and b.js("localStorage.getItem('tt.tapLatency')") == "80"
    assert "+80 ms" in b.js("document.getElementById('rhCalib').textContent")
    # 校准完回到原来的节奏条，校准那一遍不算成绩
    assert b.js("RH.last") is None
    assert b.js("document.querySelectorAll('#rhSvg .rh-note').length") == b.js("RH.prog.onsets.length")
    b.js("localStorage.removeItem('tt.tapLatency'); true")
    assert not b.errors, b.errors
