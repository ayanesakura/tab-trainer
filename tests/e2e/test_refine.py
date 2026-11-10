"""精修的端到端测试：练习页点“精修整首” → 确认框给出预估 → 进度 → 小节卡片显示改了什么 → 撤销精修；
小节卡片上的“精修这个小节 / 这一行”（不弹确认框，进度、谱面上的标记）；GP 曲子不显示。只用替身模型。"""

import json
import threading
import urllib.request

import pytest

from app import config
from tests.e2e.conftest import _seed
from tests.fakes import FakeClaude

pytestmark = pytest.mark.e2e


def _ai_on(b):
    """e2e 的服务没有 API key，AI 按钮默认灰掉（点了只说明去设置）：当作有 key（模型调用由测试换成假的或拦下）。"""
    b.wait_for("tt.get('ui.hasKey') !== undefined")
    b.js("tt.set('ui.hasKey', true); true")


def _second_bar_is_wrong(gate: threading.Event):
    """替身模型：只有第 2 小节（3 弦 2 品开头）读错了，应是 7 品。gate 没放开时核对停着，页面上能看到进度。"""

    def verify(img, meta, span, events, previous, ts):
        gate.wait(30)
        first = events[0]["notes"][0]
        if (first["string"], first["fret"]) != (3, 2):
            return {"diff": "", "changed": False, "events": events, "uncertain": ""}
        fixed = [{**e, "notes": [{**n, "fret": 7} for n in e["notes"]]} for e in events]
        return {"diff": "第 1 拍应是 3 弦 7 品", "changed": True, "events": fixed, "uncertain": ""}

    return verify


def test_refine_from_the_practice_page(server, trainer, monkeypatch):
    store = server["store"]
    sid = _seed()
    gate = threading.Event()

    monkeypatch.setattr(config, "llm_factory", FakeClaude)
    monkeypatch.setattr(FakeClaude, "verify_result", _second_bar_is_wrong(gate))
    try:
        b = trainer(sid)
        _ai_on(b)
        b.wait_for("!document.getElementById('refineBtn').hidden")
        assert b.js("document.getElementById('refineBtn').textContent.trim()") == "精修"
        b.js("document.getElementById('refineBtn').click()")
        b.wait_for("!!document.querySelector('.dlg-msg[open] [data-ok]')")
        text = b.js("document.querySelector('.dlg-msg').textContent")
        assert "精修整首曲子？" in text
        assert "6 个小节" in text and "$1.20" in text and "约 3 分钟" in text  # 6 × $0.2，4 个并行
        b.js("document.querySelector('.dlg-msg [data-ok]').click(); true")
        notices = "document.getElementById('notices').textContent"
        b.wait_for(f"{notices}.includes('正在精修')", timeout=10)
        assert "取消精修" in b.js(notices)
        assert b.js("document.getElementById('refineBtn').disabled") is True  # 进行中不能再点
        gate.set()
        b.wait_for("!tt.get('song').busy && !!tt.get('song').bars[1].refine", timeout=20)
        b.wait_for(f"{notices}.includes('精修完成：检查了 6 个小节，改了 1 个')", timeout=5)

        b.js("tt.set('ui.bar', 1)")
        b.wait_for("!document.getElementById('refineRow').hidden")
        assert b.js("document.getElementById('refineDiff').textContent") == "精修改过：第 1 拍应是 3 弦 7 品"
        assert "3弦2品" in b.js("document.getElementById('refinePrev').textContent")  # 精修之前的读法
        b.js("tt.set('ui.bar', 0)")
        b.wait_for("document.getElementById('refineRow').hidden")  # 没改的小节不显示

        b.js("tt.set('ui.bar', 1)")
        b.wait_for("!document.getElementById('refineRow').hidden")
        b.js("document.getElementById('undoRefineBtn').click()")
        b.wait_for(
            "!tt.get('song').bars[1].refine && document.getElementById('refineRow').hidden", timeout=10
        )
        saved = json.load(urllib.request.urlopen(f"{server['base']}/api/songs/{sid}"))["bars"][1]
        assert saved["events"][0]["notes"][0]["fret"] == 2 and saved["tip"] == "第 2 小节的提示"
        assert not b.errors, b.errors
    finally:
        gate.set()
        store.delete(sid)


def test_refine_one_bar_and_one_row_from_the_bar_card(server, trainer, monkeypatch):
    store = server["store"]
    sid = _seed()
    gate = threading.Event()
    monkeypatch.setattr(config, "llm_factory", FakeClaude)
    monkeypatch.setattr(FakeClaude, "verify_result", _second_bar_is_wrong(gate))
    FakeClaude.calls.clear()
    hint = "document.getElementById('refineScopeHint').textContent"
    marks = "document.querySelectorAll('#warnLayer .{}').length"
    notices = "document.getElementById('notices').textContent"
    try:
        b = trainer(sid)
        _ai_on(b)
        b.js("tt.set('ui.bar', 1)")
        b.wait_for("!document.getElementById('refineScope').hidden")
        assert b.js(hint) == ""  # 预估只在悬停提示里
        bar_title, row_title = (
            "document.getElementById('refineBarBtn').title",
            "document.getElementById('refineRowBtn').title",
        )
        assert b.js(bar_title) == "AI 放大核对原谱：这个小节约 $0.2，1–2 分钟"
        assert b.js(row_title) == "AI 放大核对原谱：这一行 3 个小节，约 $0.6，1–2 分钟"
        # “识别有误？”里写了哪里不对：精修时一起告诉 AI
        b.js(
            "(i => { i.value = '第 1 拍是 7 品'; i.dispatchEvent(new Event('input')); })"
            "(document.getElementById('hintInput')); true"
        )
        assert b.js(bar_title).endswith("，会带上“哪里不对”里写的说明")
        b.js("document.getElementById('refineBarBtn').click(); true")  # 不弹确认框，点了就开始
        b.wait_for("!document.getElementById('refineBusy').hidden", timeout=10)
        assert b.js("document.getElementById('refineScopeBtns').hidden") is True
        assert b.js("!!document.querySelector('.dlg-msg[open]')") is False
        b.wait_for(f"{notices}.includes('正在精修第 2 小节')")
        assert "取消精修" in b.js(notices)
        b.wait_for(f"{marks.format('refining-mark')} === 1")  # 谱面上正在精修的小节
        b.js("tt.set('ui.bar', 4)")  # 别的小节：按钮先禁用
        b.wait_for("document.getElementById('refineBusy').hidden")
        assert b.js("document.getElementById('refineBarBtn').disabled") is True
        assert b.js("document.getElementById('refineRowBtn').title") == "正在精修，完成后再试"
        b.js("tt.set('ui.bar', 1)")
        gate.set()
        b.wait_for("!tt.get('song').busy && !!tt.get('song').bars[1].refine", timeout=20)
        b.wait_for(f"{notices}.includes('第 2 小节精修完：改了')", timeout=5)
        assert [c["hint"] for c in FakeClaude.calls if "verify" in c] == [
            "第 1 拍是 7 品"
        ] * 2  # 改了：再确认一遍
        b.wait_for(
            "!document.getElementById('refineRow').hidden && document.getElementById('refineBusy').hidden"
        )
        assert b.js("document.getElementById('refineDiff').textContent") == "精修改过：第 1 拍应是 3 弦 7 品"
        b.wait_for(f"{marks.format('refine-mark')} === 1 && {marks.format('refining-mark')} === 0")
        assert "精修改过" in b.js("document.getElementById('scoreHelp').textContent")  # 谱面图例
        assert "refined" not in store.load(sid)  # 只精修了一个小节：不算精修过整首

        b.js("document.getElementById('undoRefineBtn').click()")
        b.wait_for(f"!tt.get('song').bars[1].refine && {marks.format('refine-mark')} === 0", timeout=10)

        # 精修这一行：第 1 行 3 个小节都核对（刚撤销过的也核对）
        b.js(
            "(i => { i.value = ''; i.dispatchEvent(new Event('input')); })(document.getElementById('hintInput')); true"
        )
        FakeClaude.calls.clear()
        b.js("document.getElementById('refineRowBtn').click(); true")
        b.wait_for(f"{notices}.includes('第 1 行精修完：检查了 3 个小节，改了 1 个')", timeout=20)
        assert len([c for c in FakeClaude.calls if "verify" in c]) == 4
        assert all(c["hint"] == "" for c in FakeClaude.calls if "verify" in c)
        b.wait_for(f"{marks.format('refine-mark')} === 1")
        assert not b.errors, b.errors
    finally:
        gate.set()
        store.delete(sid)


def test_no_bar_refine_for_gp_songs(server, trainer):
    b = trainer(server["gp"])
    b.wait_for("tt.song.bars.length === 3")
    b.js("tt.set('ui.bar', 1)")
    b.wait_for("document.getElementById('barNo').textContent === '2'")
    assert b.js("document.getElementById('refineScope').hidden") is True
    assert b.js("document.getElementById('refineBtn').hidden") is True
    assert not b.errors, b.errors
