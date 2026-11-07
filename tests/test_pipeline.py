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

    def explain_bars(self, meta, bars):
        FakeClaude.calls.append({"explain": [b["n"] for b in bars]})
        return {"bars": [{"number": b["n"], "tip": f"讲解 {b['n']}"} for b in bars]}

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

    bar_id = store.load(sid)["bars"][1]["id"]
    song = pipeline.edit_bar(sid, bar_id, [{"dur": "3", "notes": []}], tip="改过")
    bar = song["bars"][1]
    assert bar["tip"] == "改过" and bar["warn"] == ["时值合计 3 拍，应为 4 拍"]

    with pytest.raises(ValueError):
        pipeline.edit_bar(sid, bar_id, [{"dur": "abc", "notes": []}], tip=None)

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


def test_bar_ids_are_stable_and_unique(data_dir):
    sid = pipeline.create_song([("p1.png", _png())])
    pipeline._run_parse(sid)
    before = store.load(sid)["bars"]
    assert len({b["id"] for b in before}) == len(before)
    pipeline.reparse_system(sid, 1)
    after = store.load(sid)["bars"]
    assert [b["id"] for b in after[:3]] == [b["id"] for b in before[:3]]  # 没重识别的行，id 不变
    with pytest.raises(pipeline.BarNotFound):
        pipeline.edit_bar(sid, before[4]["id"], [{"dur": "4", "notes": []}], tip=None)  # 旧 id 已失效


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
        pipeline.clean_events(events)


def test_empty_bar_becomes_rest(data_dir):
    sid = pipeline.create_song([("p1.png", _png())])
    pipeline._run_parse(sid)
    bar_id = store.load(sid)["bars"][0]["id"]
    song = pipeline.edit_bar(sid, bar_id, [], tip=None)
    assert song["bars"][0]["events"] == [{"dur": "4", "notes": []}]
    assert song["bars"][0]["warn"] == []


def test_edit_meta_rejects_bad_values(data_dir):
    sid = pipeline.create_song([("p1.png", _png())])
    pipeline._run_parse(sid)
    for bad in ({"time_signature": [None, 4]}, {"capo": 20}, {"tuning": ["E4", "B3", "G3", "D3", "A2", "Q"]}):
        with pytest.raises(ValueError):
            pipeline.edit_meta(sid, bad)


def test_recover_interrupted_parse(data_dir):
    sid = pipeline.create_song([("p1.png", _png())])
    pipeline._run_parse(sid)

    def stuck(s):
        s["status"] = "parsing"
        s["systems"][1]["status"] = "parsing"
        s["bars"][0].pop("id")

    store.update(sid, stuck)
    pipeline.recover_interrupted()
    song = store.load(sid)
    assert song["status"] == "error" and "中断" in song["error"]
    assert song["systems"][1]["status"] == "error"
    assert song["bars"][0]["id"]


def test_reparse_claim_rejects_concurrent_job(data_dir):
    sid = pipeline.create_song([("p1.png", _png())])
    pipeline._run_parse(sid)
    assert pipeline.jobs.claim(sid) is not None  # 模拟整首识别正在进行
    with pytest.raises(RuntimeError):
        pipeline.start_reparse(sid, 0)
    assert store.load(sid)["systems"][0]["status"] == "done"  # 没有被误标成识别中
    pipeline.jobs.release(sid)


def test_cancel_stops_remaining_systems(data_dir, monkeypatch):
    sid = pipeline.create_song([("p1.png", _png())])
    cancel = pipeline.threading.Event()
    real = FakeClaude.transcribe

    def transcribe_then_cancel(self, *a, **kw):
        cancel.set()  # 第一行识别完成后用户点了取消
        return real(self, *a, **kw)

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe_then_cancel)
    monkeypatch.setattr(pipeline, "CONCURRENCY", 1)
    with pytest.raises(pipeline.Cancelled):
        pipeline._run_parse(sid, cancel)
    song = store.load(sid)
    assert len([c for c in FakeClaude.calls if not c["retry"]]) == 1  # 第二行没有再发请求
    assert any(s["error"] == "已取消" for s in song["systems"])


def test_reparse_failed_line_uses_real_bar_numbers(data_dir):
    sid = pipeline.create_song([("p1.png", _png())])
    pipeline._run_parse(sid)
    store.update(
        sid, lambda s: s.update(bars=[b for b in s["bars"] if b["system"] != 1])
    )  # 第 2 行之前失败了
    pipeline.reparse_system(sid, 1)
    assert FakeClaude.calls[-1]["first_bar"] == 4


def _derived_bars(n):
    return [{"events": [{"dur": "4", "notes": [{"string": 2, "fret": i % 5, "tech": []}]}]} for i in range(n)]


def test_guitar_pro_upload_skips_ai_and_keeps_file(data_dir):
    sid = pipeline.create_song([("我的曲子.gp5", b"FICHIER GUITAR PRO")])
    song = store.load(sid)
    assert song["kind"] == "score" and song["status"] == "ready" and song["title"] == "我的曲子"
    assert (store.song_dir(sid) / song["source"]).read_bytes() == b"FICHIER GUITAR PRO"
    assert not FakeClaude.calls
    with pytest.raises(ValueError):
        pipeline.create_song([("a.gp5", b"x"), ("b.png", _png())])


def test_set_derived_and_explain(data_dir, monkeypatch):
    sid = pipeline.create_song([("x.gp", b"..")])
    bars = _derived_bars(20)
    bars[3] = {"events": [{"dur": "4", "notes": [{"string": 9, "fret": 0}]}]}  # 坏数据：弦号 9
    meta = {"tuning": ["E4", "B3", "G3", "D3", "A2", "E2"], "capo": 0, "time_signature": [4, 4], "tempo": 90}
    song = pipeline.set_derived(sid, meta, bars, track=1)
    assert len(song["bars"]) == 20 and song["track"] == 1
    assert song["bars"][3]["events"] == [{"dur": "4", "notes": []}] and song["bars"][3]["warn"]
    assert song["bars"][5]["system"] == 1

    monkeypatch.setattr(
        pipeline.threading, "Thread", lambda target, daemon: type("T", (), {"start": lambda self: target()})()
    )
    pipeline.start_explain(sid)
    song = store.load(sid)
    assert (
        song["status"] == "ready"
        and song["bars"][19]["tip"] == "讲解 20"
        and song["plan"]["overview"] == "ok"
    )
    assert [c["explain"] for c in FakeClaude.calls if "explain" in c] == [
        list(range(1, 17)),
        [17, 18, 19, 20],
    ]

    with pytest.raises(ValueError):
        pipeline.set_derived(pipeline.create_song([("p1.png", _png())]), meta, bars)  # 图片曲子不能这样改


def test_recover_interrupted_explain_keeps_score_song_playable(data_dir):
    sid = pipeline.create_song([("x.gp", b"..")])
    pipeline.set_derived(sid, {}, _derived_bars(2))
    store.update(sid, lambda s: s.update(status="parsing"))
    pipeline.recover_interrupted()
    song = store.load(sid)
    assert song["status"] == "ready" and "中断" in song["error"]


def test_explain_without_key_fails_fast(data_dir, monkeypatch):
    from app import llm

    monkeypatch.setattr(pipeline, "Claude", llm.Claude)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sid = pipeline.create_song([("x.gp", b"..")])
    pipeline.set_derived(sid, {}, _derived_bars(2))
    with pytest.raises(ValueError, match="API key"):
        pipeline.start_explain(sid)
    assert store.load(sid)["status"] == "ready" and not pipeline.jobs.running(sid)


STD_META = pipeline.normalize_meta({"tuning": pipeline.STANDARD, "capo": 2, "time_signature": [4, 4]})


def _raw(notes_per_bar):
    """[[(string, fret, pitch, tech), ...], ...] -> 模型原始输出的 bars（每个音一个起拍）。"""
    return [
        {
            "events": [
                {"dur": "1", "notes": [{"string": s, "fret": f, "pitch": p, "tech": t}]} for s, f, p, t in bar
            ]
        }
        for bar in notes_per_bar
    ]


def test_pitch_check_flags_note_read_onto_wrong_string():
    # 五线谱按实际发音记（变调夹 2 品 → 每个音比指法高 2 个半音，且高八度记谱）
    ok = [(5, 2, "C#4", []), (4, 2, "F#4", []), (3, 2, "B4", []), (2, 3, "E5", [])]
    assert pipeline.pitch_mismatches(_raw([ok]), STD_META) == {}

    # 第 2 小节：原谱是 6 弦 9 品（C#3，加变调夹记成 D#3），模型把数字读到了 5 弦（按调弦应是 G#）
    wrong = [(5, 9, "D#3", [])]
    out = pipeline.pitch_mismatches(_raw([ok, wrong]), STD_META)
    assert list(out) == [1]
    assert "5 弦 9 品" in out[1][0] and "G#" in out[1][0]


def test_pitch_check_skips_harmonics_and_sparse_or_inconsistent_staff():
    ok = [(5, 2, "C#4", []), (4, 2, "F#4", []), (3, 2, "B4", []), (2, 3, "E5", [])]
    harmonic = [(3, 12, "G6", ["nh"]), (1, 0, "x", ["x"])]
    assert pipeline.pitch_mismatches(_raw([ok, harmonic]), STD_META) == {}
    # 没有五线谱（pitch 为空）或音太少：不判断
    assert pipeline.pitch_mismatches(_raw([[(5, 2, "", []), (4, 2, "", [])]]), STD_META) == {}
    assert pipeline.pitch_mismatches(_raw([[(5, 2, "C#4", []), (4, 2, "C4", [])]]), STD_META) == {}
    # 五线谱读得乱七八糟（没有一致的偏移）：不下结论，免得误报
    chaos = [(5, 0, "C4", []), (5, 0, "D4", []), (5, 0, "E4", []), (5, 0, "F#4", []), (5, 0, "G#4", [])]
    assert pipeline.pitch_mismatches(_raw([chaos]), STD_META) == {}


def test_pitch_mismatch_triggers_retry_and_warns_if_unfixed(data_dir, monkeypatch):
    def transcribe(self, img, meta, first_bar, spans, hint="", previous=None, problems=None):
        FakeClaude.calls.append({"first_bar": first_bar, "retry": previous is not None, "problems": problems})
        bars = []
        for i in range(len(spans)):
            # 调弦 D A F# C# A D，五线谱按指法记。第 1 行第 2 小节最后一个音（2 弦 1 品 = A#）被读到了 3 弦
            notes = [(1, 0, "D", []), (2, 1, "A#", []), (3, 2, "G#", []), (2, 1, "A#", [])]
            if first_bar == 1 and i == 1 and previous is None:
                notes[3] = (3, 1, "A#", [])
            if first_bar == 4 and i == 0:  # 第 2 行第 1 小节两次都错：返修后仍应标出来
                notes[3] = (3, 1, "A#", [])
            bars.append(
                {"number": first_bar + i, "events": _raw([notes])[0]["events"], "tip": "", "uncertain": ""}
            )
        return {"bars": bars}

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    sid = pipeline.create_song([("p1.png", _png())])
    pipeline._run_parse(sid)
    song = store.load(sid)
    retries = [c for c in FakeClaude.calls if c.get("retry")]
    assert {c["first_bar"] for c in retries} == {1, 4}
    assert any("3 弦 1 品" in p for c in retries for p in c["problems"])
    assert song["bars"][1]["warn"] == []  # 返修改对了
    assert any("3 弦 1 品" in w for w in song["bars"][3]["warn"])  # 仍然不对：练习页标橙
    assert all(not b["warn"] for b in song["bars"] if b["n"] not in (4,))


def test_clean_repeats_keeps_only_valid_marks():
    assert pipeline.clean_repeats({}) == {}
    assert pipeline.clean_repeats(
        {
            "repeat_start": True,
            "repeat_end": 1,
            "endings": [2, 1, 1, 9],
            "directions": ["segno", "bogus", "segno"],
        }
    ) == {"repeat_start": True, "repeat_end": 2, "endings": [1, 2], "directions": ["segno"]}
    assert pipeline.clean_repeats({"repeat_start": "yes", "repeat_end": "x", "endings": ["a"]}) == {}


def test_set_derived_keeps_repeats(data_dir):
    sid = pipeline.create_song([("曲子.gp", b"x")])
    bars = _derived_bars(2)
    bars[0]["repeat_start"] = True
    bars[1].update(repeat_end=3, directions=["fine"])
    song = pipeline.set_derived(sid, {"tuning": pipeline.STANDARD}, bars)
    assert song["bars"][0]["repeat_start"] is True
    assert song["bars"][1]["repeat_end"] == 3 and song["bars"][1]["directions"] == ["fine"]


def test_pitch_check_treats_whole_string_offset_as_tuning_not_misread():
    # 实际是 Drop D（6 弦 D2），谱头却读成了标准调弦：6 弦上的音全部偏 2 个半音，不应让模型去“改”六线谱
    notes = [
        (6, 0, "D", []),
        (6, 2, "E", []),
        (6, 3, "F", []),
        (5, 2, "B", []),
        (4, 2, "E", []),
        (3, 2, "A", []),
    ]
    notes += [(2, 1, "C", []), (1, 0, "E", []), (5, 0, "A", []), (4, 0, "D", [])]
    assert (
        pipeline.pitch_mismatches(_raw([notes]), pipeline.normalize_meta({"tuning": pipeline.STANDARD})) == {}
    )
    # 但同一根弦上只有个别音不对，仍然是识读错误
    one_off = [*notes, (6, 5, "G", [])]
    one_off[0] = (6, 0, "E", [])
    one_off[1] = (6, 2, "F#", [])
    one_off[2] = (6, 3, "G", [])
    out = pipeline.pitch_mismatches(_raw([one_off]), pipeline.normalize_meta({"tuning": pipeline.STANDARD}))
    assert list(out) == [0] and "6 弦 5 品" in out[0][0]


def test_pitch_check_ignores_malformed_entries_and_bends():
    ok = [(5, 2, "C#4", []), (4, 2, "F#4", []), (3, 2, "B4", []), (2, 3, "E5", [])]
    raw = _raw([ok])
    raw[0]["events"].append(
        {"dur": "1", "notes": [{"string": 2, "fret": 5, "pitch": "G5", "tech": [], "bend": 2}]}
    )
    raw.append("garbage")
    raw[0]["events"].append("garbage")
    assert pipeline.pitch_mismatches(raw, STD_META) == {}
