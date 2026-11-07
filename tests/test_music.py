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
    assert len(probs) == 1 and "1/5" in probs[0] and "近似" in probs[0]
