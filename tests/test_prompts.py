"""给模型的提示词快照：每一类调用发出去的文字、schema、精细度和长度上限和 tests/fixtures/prompts.json 逐字一致。
改提示词是有意的就重新生成快照：UPDATE_PROMPTS=1 uv run pytest tests/test_prompts.py，然后在 diff 里核对改动。"""

import json
import os
from pathlib import Path

from PIL import Image

from app import music, prompts
from app.llm import Claude, LLMConfig

FIXTURE = Path(__file__).parent / "fixtures" / "prompts.json"


def _capture(monkeypatch) -> list[dict]:
    calls = []

    def fake_call(self, content, schema, effort=None, max_tokens=32000):
        calls.append(
            {
                "content": [b["text"] if b["type"] == "text" else f"<{b['type']}>" for b in content],
                "schema": schema,
                "system": prompts.SYSTEM,
                "effort": effort,
                "max_tokens": max_tokens,
            }
        )
        return {"bars": [], "answer": "好", "title": "ok"}

    monkeypatch.setattr(Claude, "_call", fake_call)
    return calls


def _all_prompts(monkeypatch) -> dict:
    calls = _capture(monkeypatch)
    c = Claude(LLMConfig(api_key="sk-ant-test"))
    meta = music.normalize_meta(
        {
            "title": "T",
            "tuning": ["D4", "A3", "F#3", "D3", "A2", "D2"],
            "capo": 2,
            "time_signature": [4, 4],
            "notes": "5、6 弦叠在一起的 x 是拍弦",
        }
    )
    img = Image.new("RGB", (1200, 200), "white")
    bars = [
        {
            "n": 3,
            "events": [
                {"dur": "1", "notes": [{"string": 2, "fret": 3, "tech": ["h"], "grace": -1, "bend": 0}]}
            ]
            * 4,
            "tip": "慢练",
        },
        {"n": 4, "time_signature": [3, 4], "events": [{"dur": "3", "notes": []}], "tip": ""},
    ]
    out = {}
    for name, run in {
        "ping": lambda: c.ping(),
        "read_meta": lambda: c.read_meta(img),
        "transcribe": lambda: c.transcribe(img, meta, 3, [(10, 400), (400, 800)], labeled_strings=True),
        "transcribe_retry": lambda: c.transcribe(
            img,
            meta,
            3,
            None,
            hint="第 3 拍是 2 弦",
            previous={"bars": []},
            problems=["第 3 小节：时值不对"],
            start_ts=[3, 4],
        ),
        "explain_bars": lambda: c.explain_bars(meta, bars),
        "verify_bar": lambda: c.verify_bar(
            Image.new("RGB", (400, 200), "white"),
            meta,
            (60, 340),
            [{**bars[0]["events"][0], "chord": "Am"}] * 4,
            previous=bars[1]["events"],
            time_signature=[3, 4],
        ),
        "verify_bar_hint": lambda: c.verify_bar(
            Image.new("RGB", (400, 200), "white"),
            meta,
            (60, 340),
            bars[0]["events"],
            hint="第 3 拍是 2 弦 4 品",
        ),
        "coach": lambda: c.coach(
            meta, bars[0], [bars[1]], "怎么弹", [{"q": "前一个问题", "a": "回答"}], "练了 3 遍", img
        ),
        "practice_plan": lambda: c.practice_plan(meta, bars),
    }.items():
        calls.clear()
        run()
        out[name] = calls[0]
    return out


def test_prompts_match_the_snapshot(monkeypatch):
    got = json.loads(json.dumps(_all_prompts(monkeypatch), ensure_ascii=False))
    if os.environ.get("UPDATE_PROMPTS"):
        FIXTURE.write_text(json.dumps(got, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    want = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for name in want:
        assert got[name] == want[name], name
    assert set(got) == set(want)


def test_note_text_shows_the_tapped_fret_like_the_tab():
    """给模型的文字版小节：点弦泛音写成谱上的样子（按的品 + <点的品>），别的音不变。"""
    bar = {
        "n": 12,
        "events": [
            {
                "dur": "4",
                "notes": [
                    {"string": 1, "fret": 0, "tech": ["th"], "grace": -1, "bend": 0, "harm": 19},
                    {"string": 4, "fret": 2, "tech": [], "grace": -1, "bend": 0},
                ],
            }
        ],
    }
    assert prompts.bar_text(bar) == "第12小节: 4拍 1弦0品<19>[th] 4弦2品"
    assert "th tapped harmonic" in prompts.NOTATION_KEY
