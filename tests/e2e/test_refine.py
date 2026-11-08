"""精修的端到端测试：练习页点“精修” → 确认框给出预估 → 进度 → 小节卡片显示改了什么 → 撤销精修。只用替身模型。"""

import json
import threading
import urllib.request

import pytest

from app import config
from tests.e2e.conftest import _seed
from tests.fakes import FakeClaude

pytestmark = pytest.mark.e2e


def test_refine_from_the_practice_page(server, trainer, monkeypatch):
    store = server["store"]
    sid = _seed()
    gate = threading.Event()  # 核对停在这里，页面上能看到进度

    def verify(img, meta, span, events, previous, ts):
        gate.wait(30)
        first = events[0]["notes"][0]
        if (first["string"], first["fret"]) != (3, 2):  # 只有第 2 小节读错了
            return {"diff": "", "changed": False, "events": events, "uncertain": ""}
        fixed = [{**e, "notes": [{**n, "fret": 7} for n in e["notes"]]} for e in events]
        return {"diff": "第 1 拍应是 3 弦 7 品", "changed": True, "events": fixed, "uncertain": ""}

    monkeypatch.setattr(config, "llm_factory", FakeClaude)
    monkeypatch.setattr(FakeClaude, "verify_result", verify)
    try:
        b = trainer(sid)
        b.wait_for("!document.getElementById('refineBtn').hidden")
        b.js("document.getElementById('refineBtn').click()")
        b.wait_for("!!document.querySelector('.dlg-msg[open] [data-ok]')")
        text = b.js("document.querySelector('.dlg-msg').textContent")
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
