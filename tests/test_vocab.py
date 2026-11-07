"""前后端共用的词表 app/static/js/vocab.json：后端的常量都从它来，字段齐全、互相一致。"""

import json
from fractions import Fraction
from pathlib import Path

from app import llm, model, music, prompts

VOCAB = json.loads((Path(music.__file__).parent / "static" / "js" / "vocab.json").read_text(encoding="utf-8"))


def test_backend_constants_come_from_vocab():
    inst = VOCAB["instrument"]
    assert music.STRINGS == inst["strings"] == len(inst["standard_tuning"]) == 6
    assert music.STANDARD == inst["standard_tuning"]
    assert music.MAX_FRET == inst["max_fret_written"] >= inst["max_fret"]
    assert music.TECHS == [t["code"] for t in VOCAB["techs"]]
    assert list(music.DIRECTIONS) == [d["code"] for d in VOCAB["directions"]]
    assert music.GRID == Fraction(VOCAB["grid_ticks"], VOCAB["ticks_per_quarter"]) == Fraction(1, 48)
    assert music.WARN_CODES == VOCAB["warn_codes"]


def test_vocab_is_well_formed():
    for t in VOCAB["techs"]:
        assert set(t) == {"code", "sym", "name"} and all(t.values())
    assert len({t["code"] for t in VOCAB["techs"]}) == len(VOCAB["techs"])
    prompt = [d for d in VOCAB["directions"] if d["prompt"]]
    assert sorted(d["prompt_order"] for d in prompt) == list(range(len(prompt)))
    assert prompts.DIRECTIONS == music.PROMPT_DIRECTIONS and set(prompts.DIRECTIONS) <= set(music.DIRECTIONS)
    assert set(VOCAB["sources"]) == set(model.SourceType.__args__)
    assert set(model.SCORE_SOURCES) == {"gp", "musicxml"} <= set(VOCAB["sources"])
    assert set(music.GBK_EXT) <= set(music.SCORE_FILES["gp"])


def test_every_warn_code_the_backend_emits_is_in_vocab():
    emitted = set(music.DERIVED_WARNS) | {"ocr_mismatch", "other"}
    assert emitted == set(VOCAB["warn_codes"])
    assert set(model.WarnCode.__args__) == set(VOCAB["warn_codes"])


def test_model_presets_on_the_settings_page_have_prices():
    import re

    html = (Path(music.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    seg = re.search(r'id="modelSeg".*?</div>', html, re.DOTALL).group(0)
    buttons = [v for v in re.findall(r'data-v="([^"]+)"', seg) if v != "custom"]
    assert tuple(buttons) == llm.PRESET_MODELS
    assert all(llm.price_of(m) for m in llm.PRESET_MODELS) and llm.DEFAULT_MODEL in llm.PRESET_MODELS
