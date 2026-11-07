import io
from typing import ClassVar

import pytest

from app import pipeline, store
from tests.test_layout import _score


class FakeClaude:
    """替身模型：每行返回检测到的小节数；第一次识别第 1 行时故意把时值写错，验证返修。"""

    calls: ClassVar[list] = []

    def __init__(self, cfg):
        self.usage = {"input_tokens": 10, "output_tokens": 20}

    def read_meta(self, page):
        return {
            "title": "测试曲",
            "artist": "",
            "arranger": "",
            "tuning": ["D", "A", "F#", "C#", "A", "D"],  # 不带八度，应按标准调弦补全
            "capo": 2,
            "time_signature": [4, 4],
            "tempo": 0,
            "notes": "",
        }

    def transcribe(self, img, meta, first_bar, spans, hint="", previous=None, problems=None):
        FakeClaude.calls.append({"first_bar": first_bar, "retry": previous is not None, "hint": hint})
        bad = first_bar == 1 and previous is None
        bars = []
        for i in range(len(spans)):
            dur = "1/2" if bad else "1"
            bars.append(
                {
                    "number": first_bar + i,
                    "events": [
                        {
                            "dur": dur,
                            "notes": [{"string": 1, "fret": i, "tech": ["h"], "grace": -1, "bend": 0}],
                        }
                        for _ in range(4)
                    ],
                    "tip": f"tip {first_bar + i}",
                    "uncertain": "",
                }
            )
        return {"bars": bars}

    def practice_plan(self, meta, bars):
        return {"overview": "ok", "steps": [{"bars": "1-6", "title": "全曲", "advice": "慢练"}]}


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA", tmp_path)
    monkeypatch.setattr(store, "SONGS", tmp_path / "songs")
    monkeypatch.setattr(store, "CONFIG", tmp_path / "config.json")
    monkeypatch.setattr(pipeline, "Claude", FakeClaude)
    monkeypatch.setattr(pipeline, "start_parse", lambda sid: None)  # 测试里同步执行
    FakeClaude.calls = []
    return tmp_path


def _png() -> bytes:
    buf = io.BytesIO()
    _score().convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def test_full_parse(data_dir):
    sid = pipeline.create_song([("p1.png", _png())])
    pipeline._run_parse(sid)
    song = store.load(sid)

    assert song["status"] == "ready", song["error"]
    assert song["meta"]["tuning"] == ["D4", "A3", "F#3", "C#3", "A2", "D2"]
    assert len(song["systems"]) == 2
    assert [b["n"] for b in song["bars"]] == [1, 2, 3, 4, 5, 6]
    assert all(not b["warn"] for b in song["bars"])  # 第 1 行返修后时值正确
    assert any(c["retry"] for c in FakeClaude.calls)
    assert song["bars"][3]["system"] == 1 and song["bars"][3]["tip"] == "tip 4"
    assert song["plan"]["overview"] == "ok"


def test_edit_bar_and_meta(data_dir):
    sid = pipeline.create_song([("p1.png", _png())])
    pipeline._run_parse(sid)

    song = pipeline.edit_bar(sid, 2, [{"dur": "3", "notes": []}], tip="改过")
    bar = song["bars"][1]
    assert bar["tip"] == "改过" and bar["warn"] == ["时值合计 3 拍，应为 4 拍"]

    with pytest.raises(ValueError):
        pipeline.edit_bar(sid, 2, [{"dur": "abc", "notes": []}], tip=None)

    song = pipeline.edit_meta(sid, {"time_signature": [3, 4], "tuning": ["E4", "B3", "G3", "D3", "A2", "E2"]})
    assert song["meta"]["tuning_midi"] == [64, 59, 55, 50, 45, 40]
    assert song["bars"][1]["warn"] == []  # 3 拍的小节在 3/4 下合法
    assert song["bars"][0]["warn"]  # 4 拍的小节在 3/4 下不合法


def test_reparse_system_passes_hint(data_dir):
    sid = pipeline.create_song([("p1.png", _png())])
    pipeline._run_parse(sid)
    pipeline.reparse_system(sid, 1, hint="第 4 小节第 2 拍是 2 弦")
    assert FakeClaude.calls[-1] == {"first_bar": 4, "retry": False, "hint": "第 4 小节第 2 拍是 2 弦"}
    assert [b["n"] for b in store.load(sid)["bars"]] == [1, 2, 3, 4, 5, 6]


def test_note_names():
    assert pipeline.note_to_midi("C#3") == 49
    assert pipeline.note_to_midi("D♭3") == 49
    assert pipeline.note_to_midi("D", near=40) == 38  # 6 弦写 D：降到 D2，而不是升到 D3
    assert pipeline.note_to_midi("F#", near=55) == 54
