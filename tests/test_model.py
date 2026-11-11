"""app/model.py 是唯一的数据定义：给模型的 schema 从它生成，而且和重构前手写的逐字一致（提示词 / schema 不能变）。"""

import json
from pathlib import Path

import pytest

from app import llm, model, music, prompts
from tests.fakes import FakeClaude

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "llm_schemas.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["NOTE", "EVENT", "BAR", "TRANSCRIBE", "META", "EXPLAIN", "PLAN"])
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
    names = (
        "ping",
        "read_meta",
        "transcribe",
        "verify_bar",
        "explain_bars",
        "coach",
        "coach_stream",
        "practice_plan",
    )
    for name in names:
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


def test_add_usage_keeps_the_names_of_unpriced_models():
    """没计价的 token 是哪些模型的：合并进曲子（不重复），之后照常累加；客户端没记这一项（替身）时不添。
    （写盘时的不变量检查见 test_pages.test_tags_and_cost_in_library。）"""
    s = {"usage": {"input_tokens": 1, "output_tokens": 2, "cost_usd": 0.1}}
    model.add_usage(s, {"input_tokens": 1})
    assert "unpriced_models" not in s["usage"]
    model.add_usage(s, {"unpriced_tokens": 12, "unpriced_models": ["claude-no-price"]})
    model.add_usage(
        s, {"unpriced_tokens": 3, "unpriced_models": ["a-model", "claude-no-price"]}, {"unpriced_tokens": 1}
    )
    assert s["usage"]["unpriced_tokens"] == 14
    assert s["usage"]["unpriced_models"] == ["a-model", "claude-no-price"]


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


def test_file_names_follow_one_rule():
    """页面只能是 pN.png、原文件只能是 source.<GP / MusicXML 扩展名>：存储、接口、曲子包都按这一处检查。"""
    assert model.valid_page_file("p1.png") and model.valid_page_file("p12.png")
    for bad in ("x.html", "p1.png\n", "../p1.png", "p1.PNG", "p.png", "", None, 3):
        assert not model.valid_page_file(bad)
    assert model.valid_source_file("source.gp5") and model.valid_source_file("source.musicxml")
    for bad in ("source.html", "x.gp5", "source.gp5\n", "../source.gp5"):
        assert not model.valid_source_file(bad)
    assert model.page_number("p7.png") == 7 and model.page_number("x.png") is None
    bad = {"schema_version": 2, "id": "a", "source_type": "image", "status": "ready"}
    assert model.problems({**bad, "pages": [{"file": "x.html", "width": 1, "height": 1}]})
    assert model.problems({**bad, "source": "evil.html"})
