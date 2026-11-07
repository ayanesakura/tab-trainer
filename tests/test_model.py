"""app/model.py 是唯一的数据定义：给模型的 schema 从它生成，而且和重构前手写的逐字一致（提示词 / schema 不能变）。"""

import json
from pathlib import Path

import pytest

from app import llm, model, music
from tests.fakes import FakeClaude

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "llm_schemas.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["NOTE", "EVENT", "BAR", "TRANSCRIBE", "META", "EXPLAIN", "COACH", "PLAN"])
def test_llm_schemas_are_unchanged(name):
    """tests/fixtures/llm_schemas.json 由重构前（6957d49）的 llm.py 导出；按 JSON 文本比较，连键的顺序都要一样。"""
    got = json.dumps(getattr(llm, f"{name}_SCHEMA"), ensure_ascii=False)
    assert got == json.dumps(FIXTURE[name], ensure_ascii=False)


def test_note_and_event_schemas_come_from_the_model():
    assert llm.NOTE_SCHEMA == model.llm_schema(model.Note)
    assert llm.EVENT_SCHEMA == model.llm_schema(model.Event)
    assert llm.NOTE_SCHEMA["properties"]["string"]["enum"] == list(range(1, music.STRINGS + 1))


def test_constants_are_unchanged():
    assert llm.DIRECTIONS == FIXTURE["DIRECTIONS"]
    assert music.TECHS == FIXTURE["TECHS"]
    assert list(music.DIRECTIONS) == FIXTURE["MUSIC_DIRECTIONS"]
    assert music.STANDARD == FIXTURE["STANDARD"]


def test_fake_llm_implements_the_protocol():
    assert llm.LLM in FakeClaude.__mro__ and llm.LLM in llm.Claude.__mro__  # 显式实现接口
    assert isinstance(FakeClaude(None), llm.LLM)
    for name in ("ping", "read_meta", "transcribe", "explain_bars", "coach", "practice_plan"):
        assert callable(getattr(FakeClaude, name))


def test_stored_song_validates_against_the_model(data_dir):
    """存盘的 song.json 符合 model.Song（曲子包导入、迁移之后的数据都是这个形状）。"""
    from app import recognize, songs, store
    from tests.fakes import score_png

    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    raw = json.loads((store.song_dir(sid) / "song.json").read_text())
    s = model.Song.model_validate(raw)
    assert s.schema_version == 2 and s.source_type == "image" and s.status == "ready"
    assert "n" not in raw["bars"][0] and "tuning_midi" not in raw["meta"]


def test_derive_and_strip_round_trip():
    meta = music.normalize_meta({"title": "读出来的"})
    song = {
        "title": "",
        "meta": meta,
        "bars": [{"id": "a", "events": [{"dur": "3", "notes": []}], "warn": [], "_ocr": 1}],
        "plan": {"changed_bars": ["a"]},
        "_migrated": True,
        "practice": {"bars": {}},
    }
    model.derive(song)
    assert song["bars"][0]["n"] == 1 and song["bars"][0]["warn"][0]["code"] == "length"
    assert song["plan"]["stale"] is True
    out = model.strip(song)
    assert out["title"] == "读出来的"  # 没起名字：用谱头读出来的
    assert "title" not in out["meta"] and "tuning_midi" not in out["meta"]
    assert out["bars"][0] == {"id": "a", "events": [{"dur": "3", "notes": []}], "warn": []}
    assert "stale" not in out["plan"] and "_migrated" not in out and "practice" not in out


def test_add_usage_accumulates():
    s = {"usage": {"input_tokens": 1, "output_tokens": 2}}
    model.add_usage(s, {"input_tokens": 10, "output_tokens": 20, "cost_usd": 0.5}, {"input_tokens": 4})
    assert s["usage"] == {"input_tokens": 7, "output_tokens": 22, "cost_usd": 0.5, "unpriced_tokens": 0}
