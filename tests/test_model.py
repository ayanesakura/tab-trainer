"""app/model.py 是唯一的数据定义：给模型的 schema 从它生成，而且和重构前手写的逐字一致（提示词 / schema 不能变）。"""

import json
from pathlib import Path

import pytest

from app import llm, model, music, prompts
from tests.fakes import FakeClaude

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "llm_schemas.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["NOTE", "EVENT", "BAR", "TRANSCRIBE", "META", "EXPLAIN", "COACH", "PLAN"])
def test_llm_schemas_are_unchanged(name):
    """tests/fixtures/llm_schemas.json 是给模型的 schema 快照（最初由重构前 6957d49 的 llm.py 导出，弹唱谱加了 chord / strum / chords）；
    按 JSON 文本比较，连键的顺序都要一样。有意改 schema 时重新导出。"""
    got = json.dumps(getattr(prompts, f"{name}_SCHEMA"), ensure_ascii=False)
    assert got == json.dumps(FIXTURE[name], ensure_ascii=False)


def test_note_and_event_schemas_come_from_the_model():
    assert prompts.NOTE_SCHEMA == model.llm_schema(model.Note)
    assert prompts.EVENT_SCHEMA == model.llm_schema(model.Event)
    assert prompts.NOTE_SCHEMA["properties"]["string"]["enum"] == list(range(1, music.STRINGS + 1))


def test_constants_are_unchanged():
    assert prompts.DIRECTIONS == FIXTURE["DIRECTIONS"]
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
    assert s["usage"] == {
        "input_tokens": 7,
        "output_tokens": 22,
        "cost_usd": 0.5,
        "unpriced_tokens": 0,
        "legacy_input_tokens": 1,
        "legacy_output_tokens": 2,
    }
    model.add_usage(s, {"input_tokens": 1, "cost_usd": 0.1})  # 已经有 cost_usd 的不会再改 legacy
    assert s["usage"]["legacy_input_tokens"] == 1 and s["usage"]["cost_usd"] == 0.6


def test_legacy_cost_stays_estimated_after_a_new_call():
    from app import views

    s = {"usage": {"input_tokens": 2_000_000, "output_tokens": 100_000}}
    before = views.cost_summary(s["usage"])
    model.add_usage(s, {"input_tokens": 100, "output_tokens": 10, "cost_usd": 0.01})
    after = views.cost_summary(s["usage"])
    assert after["cost_estimated"] and after["cost_usd"] == pytest.approx(before["cost_usd"] + 0.01, abs=1e-3)


def test_harm_is_only_on_tapped_harmonics():
    """Note.harm（点弦泛音点的品）：曲子包导入补默认值时不给别的音补 0（存盘的数据不变），超出范围的拒收。"""
    import pydantic

    note = {"string": 1, "fret": 0, "tech": []}
    ev = model.Event.model_validate(
        {"dur": "1", "notes": [note, {**note, "string": 2, "tech": ["th"], "harm": 19}]}
    )
    raw = {"dur": "1", "notes": [dict(note), {**note, "string": 2, "tech": ["th"], "harm": 19}]}
    model._fill_defaults(raw, ev)
    assert "harm" not in raw["notes"][0] and raw["notes"][1]["harm"] == 19
    assert raw["notes"][0]["grace"] == -1  # 别的默认值照常补
    with pytest.raises(pydantic.ValidationError):
        model.Note.model_validate({**note, "tech": ["th"], "harm": 37})
