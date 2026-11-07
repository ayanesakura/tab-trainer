"""数据模型：音名、事件和反复记号的整理与校验。"""

import pytest

from app import music


def test_note_names():
    assert music.note_to_midi("C#3") == 49
    assert music.note_to_midi("D♭3") == 49
    assert music.note_to_midi("D", near=40) == 38  # 6 弦写 D：降到 D2，而不是升到 D3
    assert music.note_to_midi("F#", near=55) == 54


@pytest.mark.parametrize(
    ("events", "msg"),
    [
        ([{"dur": "1/0", "notes": []}], "时值无法识别"),
        ([{"notes": []}], "缺少 dur"),
        ([{"dur": "4", "notes": [{"string": 7, "fret": 0}]}], "弦号 7"),
        ([{"dur": "4", "notes": [{"string": 1, "fret": 0, "bend": "nan"}]}], "推弦"),
        ([{"dur": "4", "notes": [{"fret": 0}]}], "格式不对"),
        ("oops", "列表"),
    ],
)
def test_clean_events_rejects_bad_input(events, msg):
    with pytest.raises(ValueError, match=msg):
        music.clean_events(events)


def test_clean_repeats_keeps_only_valid_marks():
    assert music.clean_repeats({}) == {}
    assert music.clean_repeats(
        {
            "repeat_start": True,
            "repeat_end": 1,
            "endings": [2, 1, 1, 9],
            "directions": ["segno", "bogus", "segno"],
        }
    ) == {"repeat_start": True, "repeat_end": 2, "endings": [1, 2], "directions": ["segno"]}
    assert music.clean_repeats({"repeat_start": "yes", "repeat_end": "x", "endings": ["a"]}) == {}


def test_check_bar_flags_durations_the_player_cannot_write():
    meta = music.normalize_meta({})
    ok = [{"dur": "1/3", "notes": []}] * 3 + [
        {"dur": "7/48", "notes": []},
        {"dur": "41/48", "notes": []},
        {"dur": "2", "notes": []},
    ]
    assert music.check_bar(ok, meta) == []
    quint = [{"dur": "1/5", "notes": []}] * 5 + [{"dur": "3", "notes": []}]
    probs = music.check_bar(quint, meta)
    assert len(probs) == 1 and probs[0]["code"] == "off_grid" and "1/5" in probs[0]["msg"]
    assert "近似" in probs[0]["msg"]


def test_warn_codes_and_old_string_warnings():
    meta = music.normalize_meta({})
    probs = music.check_bar([{"dur": "3", "notes": [{"string": 7, "fret": 31}]}], meta)
    assert [p["code"] for p in probs] == ["length", "string_range", "string_range"]
    assert music.check_bar([], meta)[-1] == {"code": "empty", "msg": "没有任何音符"}
    assert all(p["code"] in music.WARN_CODES for p in probs)
    # 旧数据里的一句话按开头的文字认出 code
    old = {
        "时值合计 3 拍，应为 4 拍": "length",
        "时值 1/5 拍不在播放器支持的节奏网格上": "off_grid",
        "本地数字识别和 AI 结果不一致：谱上读到 1弦3品": "ocr_mismatch",
        "没有识别出可用的音符，暂按整小节休止处理": "placeholder",
        "弦号 7 超出 1–6": "string_range",
        "品数 31 不合理": "string_range",
        "没有任何音符": "empty",
        "导入时这一小节的数据无法使用：x": "other",
    }
    for text, code in old.items():
        assert music.warn_code_of(text) == code
        assert music.as_warn(text) == {"code": code, "msg": text}
    assert music.as_warn({"code": "nope", "msg": "x"}) == {"code": "other", "msg": "x"}


def test_derived_warnings_are_recomputed_and_recorded_ones_kept():
    meta = music.normalize_meta({})
    bar = {
        "events": [{"dur": "3", "notes": []}],
        "placeholder": True,
        "warn": [{"code": "length", "msg": "旧的"}, {"code": "ocr_mismatch", "msg": "OCR"}],
    }
    music.derive_warns(bar, meta)
    assert [w["code"] for w in bar["warn"]] == ["length", "placeholder", "ocr_mismatch"]
    assert bar["warn"][0]["msg"] == "时值合计 3 拍，应为 4 拍"
    music.refresh_length_warn(bar, {**meta, "time_signature": [3, 4]})
    assert [w["code"] for w in bar["warn"]] == ["placeholder", "ocr_mismatch"]
