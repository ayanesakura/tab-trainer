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


def test_playback_does_not_scroll_until_asked(trainer):
    b = trainer()
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
    b.wait_for("!document.getElementById('origBtn').hidden")
    b.js("document.getElementById('origBtn').click(); true")
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
    assert "E2E 图片曲" in text and "曲谱读取失败" in text
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
    store = server["store"]

    def last(s):
        s.setdefault("practice", {})["last"] = {"a": "bar4", "b": "bar6", "bpm": 66, "at": "昨天"}

    store.update(server["image"], last)
    b = browser
    # 页面会把地址里的 resume 去掉，所以不用 open()（它要等地址一致）
    b._run(b._send("Page.navigate", url=f"{server['base']}/trainer?id={server['image']}&resume=1"))
    b.wait_for("tt.state.a === 3 && tt.state.b === 5")
    assert b.js("tt.state.bpm") == 66
    assert "resume" not in b.js("location.search")  # 刷新页面不会再跳
    assert not b.js("document.querySelector('#notices [data-id=resume]')")
