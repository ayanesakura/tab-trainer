from scripts import tabomr_vote as tv

STD = [64, 59, 55, 50, 45, 40]


def test_to_bars_strings_ties_and_tuplets():
    toks = [
        "TS_4_4",
        "BAR",
        "BEAT",
        "DUR_4",
        "REST",
        "BEAT",
        "DUR_8",
        "N_S1_F3",  # 模型的 S1 是最低音弦 = 我们的 6 弦
        "BEAT",
        "DUR_8",
        "N_S6_F0",
        "NTECH_TIE_START",
        "BEAT",
        "DUR_4",
        "N_S6_F0",
        "NTECH_TIE_DEST",  # 连线的后半个音：并进前一个事件
        "BEAT",
        "DUR_8",
        "TUPLET_3_2",
        "N_S3_FX",
        "END_BAR",
        "BAR",
        "BEAT",
        "DUR_1",
        "DOTS_1",
        "N_S2_F2",
        "END_BAR",
    ]
    bars = tv.to_bars(toks)
    assert len(bars) == 2
    b = bars[0]
    assert [e["dur"] for e in b] == ["1", "1/2", "3/2", "1/3"]
    assert b[0]["notes"] == []
    assert b[1]["notes"] == [{"string": 6, "fret": 3, "tech": []}]
    assert b[2]["notes"] == [{"string": 1, "fret": 0, "tech": []}]
    assert b[3]["notes"] == [{"string": 4, "fret": 0, "tech": ["x"]}]
    assert bars[1][0]["dur"] == "6"


def _ev(*notes):
    return {"dur": "1", "notes": [{"string": s, "fret": f, "tech": []} for s, f in notes]}


def test_disagree_ignores_onsets_and_muted_notes():
    ai = [_ev((1, 0)), _ev((2, 1), (3, 0))]
    omr = [_ev((1, 0), (2, 1)), _ev((3, 0)), {"dur": "1", "notes": [{"string": 4, "fret": 0, "tech": ["x"]}]}]
    assert tv.disagree(ai, omr) == {"only_ai": 0, "only_omr": 0, "set_diff": 0, "n_ai": 3, "n_omr": 3}


def test_disagree_by_pitch_forgives_the_same_note_on_another_string():
    ai = [_ev((2, 5))]  # B3 + 5 = E4
    omr = [_ev((1, 0))]  # E4
    assert tv.disagree(ai, omr)["only_ai"] == 1
    assert tv.disagree(ai, omr, STD)["only_ai"] == 0
    assert tv.disagree(ai, None) is None


def test_song_bars_abstains_when_the_row_bar_count_differs():
    song = {
        "systems": [{}, {}],
        "bars": [{"n": 1, "system": 0}, {"n": 2, "system": 0}, {"n": 3, "system": 1}],
    }
    one = ["BAR", "BEAT", "DUR_1", "N_S1_F0", "END_BAR"]
    pred = {"predictions": [{"clipId": "s__000", "tokens": one}, {"clipId": "s__001", "tokens": one}]}
    out = tv.song_bars(pred, song, "s")
    assert out[1] is None and out[2] is None  # 第 1 行模型只读出 1 个小节，应该是 2 个
    assert out[3][0]["notes"] == [{"string": 6, "fret": 0, "tech": []}]
