import io
import itertools
from typing import ClassVar

import pytest

from app import pipeline, store
from tests.test_layout import _score


class FakeClaude:
    """替身模型：每行返回检测到的小节数；第一次识别第 1 行时故意把时值写错，验证返修。"""

    calls: ClassVar[list] = []
    tab_lines: ClassVar[list] = []
    last_request_id = "req_fake"

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

    def transcribe(
        self, img, meta, first_bar, spans, hint="", previous=None, problems=None, labeled_strings=False
    ):
        FakeClaude.calls.append({"first_bar": first_bar, "retry": previous is not None, "hint": hint})
        FakeClaude.tab_lines.append((first_bar, labeled_strings))
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

    def coach(self, meta, bar, neighbours, question, history, practice="", image=None):
        FakeClaude.calls.append(
            {
                "coach": bar["n"],
                "neighbours": [b["n"] for b in neighbours],
                "history": len(history),
                "practice": practice,
                "image": image.size if image else None,
            }
        )
        return f"回答：{question}"

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
    FakeClaude.tab_lines = []
    monkeypatch.setattr(pipeline, "OCR_CHECK", False)  # 合成谱子上的数字和替身模型的结果对不上；OCR 单独测
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


def test_systems_share_a_global_limit_across_songs(data_dir, monkeypatch):
    """两首曲子同时识别：同时在识别的行数不超过全局上限。"""
    import threading
    import time

    monkeypatch.setattr(pipeline, "_system_slots", threading.BoundedSemaphore(1))
    real = FakeClaude.transcribe
    active, peak, lock = [0], [0], threading.Lock()

    def slow(self, *a, **kw):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with lock:
            active[0] -= 1
        return real(self, *a, **kw)

    monkeypatch.setattr(FakeClaude, "transcribe", slow)
    sids = [pipeline.create_song([(f"p{k}.png", _png())]) for k in range(2)]
    threads = [threading.Thread(target=pipeline._run_parse, args=(sid,)) for sid in sids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1
    assert all(store.load(sid)["status"] == "ready" for sid in sids)


def test_cancel_while_waiting_for_a_slot(data_dir, monkeypatch):
    import threading

    sem = threading.BoundedSemaphore(1)
    sem.acquire()  # 名额被别的曲子占着
    monkeypatch.setattr(pipeline, "_system_slots", sem)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(pipeline.Cancelled):
        pipeline._transcribe_queued(None, "sid", {}, 0, 1, cancel=cancel)


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
    sid = pipeline.create_song([("我的曲子.gp5", b"\x18FICHIER GUITAR PRO v5.10")])
    song = store.load(sid)
    assert song["kind"] == "score" and song["status"] == "imported" and song["title"] == "我的曲子"
    assert (store.song_dir(sid) / song["source"]).read_bytes() == b"\x18FICHIER GUITAR PRO v5.10"
    assert not FakeClaude.calls
    with pytest.raises(ValueError):
        pipeline.create_song([("a.gp5", b"x"), ("b.png", _png())])


@pytest.mark.parametrize(
    ("name", "data", "ok"),
    [
        ("a.gp5", b"\x18FICHIER GUITAR PRO v5.10", True),
        ("a.gp5", b"not a gp file", False),
        ("a.gpx", b"BCFZ....", True),
        ("a.gp", b"PK\x03\x04....", True),
        ("a.gp", b"%PDF-1.7", False),
        ("a.musicxml", b'<?xml version="1.0"?>\n<!DOCTYPE x>\n<score-partwise version="4.0">', True),
        ("a.musicxml", b"<html>hello</html>", False),
        ("a.mxl", b"PK\x03\x04", True),
    ],
)
def test_score_upload_checks_file_header(data_dir, name, data, ok):
    if ok:
        assert store.load(pipeline.create_song([(name, data)]))["status"] == "imported"
    else:
        with pytest.raises(ValueError, match="不是有效"):
            pipeline.create_song([(name, data)])


def test_import_lifecycle(data_dir):
    sid = pipeline.create_song([("x.gp5", b"\x18FICHIER GUITAR PRO v5.10")])
    song = pipeline.import_failed(sid, "Unsupported format")  # 浏览器解析失败
    assert song["status"] == "error" and song["error"] == "Unsupported format"
    song = pipeline.set_derived(
        sid, {"tuning": pipeline.STANDARD}, _derived_bars(2)
    )  # 之后成功解析（比如换了音轨）
    assert song["status"] == "ready" and song["error"] is None
    with pytest.raises(ValueError):
        pipeline.import_failed(pipeline.create_song([("p1.png", _png())]), "x")


def test_set_derived_and_explain(data_dir, monkeypatch):
    sid = pipeline.create_song([("x.gp", b"\x18FICHIER GUITAR PRO v5.10")])
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
    sid = pipeline.create_song([("x.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    pipeline.set_derived(sid, {}, _derived_bars(2))
    store.update(sid, lambda s: s.update(status="parsing"))
    pipeline.recover_interrupted()
    song = store.load(sid)
    assert song["status"] == "ready" and "中断" in song["error"]


def test_explain_without_key_fails_fast(data_dir, monkeypatch):
    from app import llm

    monkeypatch.setattr(pipeline, "Claude", llm.Claude)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sid = pipeline.create_song([("x.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    pipeline.set_derived(sid, {}, _derived_bars(2))
    with pytest.raises(ValueError, match="API key"):
        pipeline.start_explain(sid)
    assert store.load(sid)["status"] == "ready" and not pipeline.jobs.running(sid)


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
    sid = pipeline.create_song([("曲子.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    bars = _derived_bars(2)
    bars[0]["repeat_start"] = True
    bars[1].update(repeat_end=3, directions=["fine"])
    song = pipeline.set_derived(sid, {"tuning": pipeline.STANDARD}, bars)
    assert song["bars"][0]["repeat_start"] is True
    assert song["bars"][1]["repeat_end"] == 3 and song["bars"][1]["directions"] == ["fine"]


def test_set_derived_per_bar_time_signature(data_dir):
    sid = pipeline.create_song([("曲子.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    bars = _derived_bars(4)
    bars[1] = {"time_signature": [3, 4], "events": [{"dur": "3", "notes": [{"string": 1, "fret": 27}]}]}
    bars[2] = {"time_signature": [6, 8], "events": [{"dur": "3", "notes": []}]}
    bars[3]["time_signature"] = [4, 4]  # 和全曲一样：不单独存
    song = pipeline.set_derived(sid, {"tuning": pipeline.STANDARD, "time_signature": [4, 4]}, bars)
    b = song["bars"]
    assert [x.get("time_signature") for x in b] == [None, [3, 4], [6, 8], None]
    assert all(not x["warn"] for x in b), [x["warn"] for x in b]  # 按各自的拍号检查时值
    assert b[1]["events"][0]["notes"][0]["fret"] == 27
    # 改音、改全曲拍号时也按小节自己的拍号检查
    song = pipeline.edit_bar(sid, b[1]["id"], [{"dur": "4", "notes": []}], None)
    assert song["bars"][1]["warn"] == ["时值合计 4 拍，应为 3 拍"]
    song = pipeline.edit_meta(sid, {"time_signature": [2, 4]})
    assert not song["bars"][2]["warn"] and song["bars"][0]["warn"]
    # 清空的小节按它自己的长度补休止；AI 看到的文字里带拍号
    song = pipeline.edit_bar(sid, b[2]["id"], [], None)
    assert song["bars"][2]["events"] == [{"dur": "3", "notes": []}]
    from app.llm import bar_text

    assert bar_text(song["bars"][2]).startswith("第3小节（6/8 拍）:")


def test_rederive_keeps_unchanged_bars(data_dir):
    """转换器 / alphaTab 升级后重新转换：内容没变的小节保留 id、提示、问答和练习记录，变了的按改过音处理。"""
    sid = pipeline.create_song([("曲子.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    meta = {"tuning": pipeline.STANDARD}
    song = pipeline.set_derived(sid, meta, _derived_bars(3), derived={"version": 1, "alphatab": "1.8.0"})
    assert song["derived"] == {"version": 1, "alphatab": "1.8.0"}
    ids = [b["id"] for b in song["bars"]]

    def seed(s):
        for i, b in enumerate(s["bars"]):
            b["tip"] = f"提示 {i}"
            b["qa"] = [{"q": "问", "a": "答"}]
        s["plan"] = {"overview": "ok", "steps": []}
        s["practice"] = {"bars": {i: {"loops": 3} for i in ids}}

    store.update(sid, seed)
    pipeline.edit_meta(sid, {"title": "我改的名字"})
    bars = _derived_bars(3)
    bars[1]["events"][0]["notes"][0]["fret"] = 9  # 新的转换规则读出来不一样
    song = pipeline.set_derived(sid, meta, bars, derived={"version": 2, "alphatab": "1.9.0"})
    b = song["bars"]
    assert [x["id"] for x in b] == ids
    assert b[0]["tip"] == "提示 0" and not b[0]["qa"][0].get("stale")
    assert b[1]["tip"] == "" and b[1]["qa"][0]["stale"]
    assert song["plan"]["stale"] and song["plan"]["changed_bars"] == [ids[1]]
    assert set(song["practice"]["bars"]) == set(ids)
    assert song["meta"]["title"] == "我改的名字"
    assert song["derived"] == {"version": 2, "alphatab": "1.9.0"}

    # 换音轨：是另一份谱子，全部重来
    song = pipeline.set_derived(sid, meta, bars, track=1, derived={"version": 2, "alphatab": "1.9.0"})
    assert not {x["id"] for x in song["bars"]} & set(ids)
    assert song["plan"] is None and not song["practice"]["bars"]
    assert all(x["tip"] == "" for x in song["bars"])


def test_tab_lines_are_found_and_strings_labelled(data_dir):
    sid = pipeline.create_song([("p1.png", _png())])
    pipeline._run_parse(sid)
    song = store.load(sid)
    assert all(labeled for _, labeled in FakeClaude.tab_lines)
    # 合成谱子的六线谱：第一行从 y=230 开始、第二行从 y=630 开始，线距 14
    for sysinfo in song["systems"]:
        assert sysinfo["lines"][0] in (230, 630)
        lines = pipeline._tab_lines(sid, song, sysinfo)
        assert lines == [y - sysinfo["crop"][2] for y in sysinfo["lines"]]
        assert [round(b - a) for a, b in itertools.pairwise(lines)] == [14] * 5
    # 旧曲子没存 lines：对整页重新检测
    s0 = dict(song["systems"][0])
    lines0 = s0.pop("lines")
    assert pipeline._tab_lines(sid, song, s0) == [y - s0["crop"][2] for y in lines0]
    # 标出来的弦号是红色的，画在第一个小节左边
    x0, x1, y0, y1 = s0["crop"]
    img = pipeline._page_image(sid, song["pages"][0]).crop((x0, y0, x1, y1))
    out = pipeline._label_strings(img, pipeline._tab_lines(sid, song, s0), s0["spans"][0][0] - x0)
    px = out.load()
    reds = [
        (x, y) for x in range(out.width) for y in range(out.height) if px[x, y][0] > 180 and px[x, y][1] < 90
    ]
    assert reds and max(x for x, _ in reds) < s0["spans"][0][0] - x0


def test_ask_bar_keeps_history_and_sends_context(data_dir):
    sid = pipeline.create_song([("p1.png", _png())])
    pipeline._run_parse(sid)
    bar = store.load(sid)["bars"][1]
    pipeline.record_practice(sid, {"bar_ids": [bar["id"]], "loops": 3, "bpm": 50, "seconds": 60})
    r = pipeline.ask_bar(sid, bar["id"], "  这里左手怎么按？ ")
    assert r["answer"] == "回答：这里左手怎么按？" and len(r["qa"]) == 1
    call = FakeClaude.calls[-1]
    assert call["coach"] == 2 and call["neighbours"] == [1, 3] and call["image"]  # 图片曲子带上原谱截图
    assert "3 遍" in call["practice"] and "50 BPM" in call["practice"]
    pipeline.ask_bar(sid, bar["id"], "第二个问题")
    assert FakeClaude.calls[-1]["history"] == 1
    assert [q["q"] for q in store.load(sid)["bars"][1]["qa"]] == ["这里左手怎么按？", "第二个问题"]
    with pytest.raises(ValueError):
        pipeline.ask_bar(sid, bar["id"], "   ")
    with pytest.raises(pipeline.BarNotFound):
        pipeline.ask_bar(sid, "nope", "问题")


def test_ask_bar_without_key_is_a_user_error(data_dir, monkeypatch):
    from app import llm  # 用真正的客户端（没有 key 时构造就会失败）

    monkeypatch.setattr(pipeline, "Claude", llm.Claude)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sid = pipeline.create_song([("曲子.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    pipeline.set_derived(sid, {"tuning": pipeline.STANDARD}, _derived_bars(2))
    bar_id = store.load(sid)["bars"][0]["id"]
    with pytest.raises(ValueError, match="API key"):
        pipeline.ask_bar(sid, bar_id, "怎么练？")


def test_practice_log_accumulates_per_bar(data_dir):
    sid = pipeline.create_song([("曲子.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    pipeline.set_derived(sid, {"tuning": pipeline.STANDARD}, _derived_bars(4))
    ids = [b["id"] for b in store.load(sid)["bars"]]
    pipeline.record_practice(sid, {"bar_ids": ids[1:3], "loops": 4, "bpm": 48, "seconds": 120})
    p = pipeline.record_practice(sid, {"bar_ids": ids[1:3] + ["gone"], "loops": 2, "bpm": 56, "seconds": 40})
    assert p["bars"][ids[1]]["loops"] == 6 and p["bars"][ids[1]]["best_bpm"] == 56
    assert p["bars"][ids[2]]["seconds"] == 80  # 时间平摊到两个小节
    assert ids[0] not in p["bars"]
    assert p["last"]["a"] == ids[1] and p["last"]["b"] == ids[2] and p["last"]["bpm"] == 56
    assert p["total_seconds"] == 160
    # 只听了一会儿没循环完：记时间，不更新最快速度
    p = pipeline.record_practice(sid, {"bar_ids": [ids[3]], "loops": 0, "bpm": 90, "seconds": 10})
    assert p["bars"][ids[3]]["best_bpm"] == 0 and p["bars"][ids[3]]["seconds"] == 10
    for bad in (
        {},
        {"bar_ids": ids, "bpm": 0},
        {"bar_ids": ids, "bpm": 60, "loops": -1},
        {"bar_ids": ids, "bpm": "x"},
    ):
        with pytest.raises(ValueError):
            pipeline.record_practice(sid, bad)


def _explain_sync(monkeypatch):
    monkeypatch.setattr(
        pipeline.threading, "Thread", lambda target, daemon: type("T", (), {"start": lambda self: target()})()
    )


def test_editing_notes_invalidates_tip_qa_and_plan_but_keeps_practice(data_dir, monkeypatch):
    sid = pipeline.create_song([("p1.png", _png())])
    pipeline._run_parse(sid)
    song = store.load(sid)
    bar = song["bars"][1]
    pipeline.ask_bar(sid, bar["id"], "怎么练？")
    pipeline.record_practice(sid, {"bar_ids": [bar["id"]], "loops": 2, "bpm": 50, "seconds": 30})
    assert store.load(sid)["plan"] and not store.load(sid)["plan"].get("stale")

    # 内容没变的保存：什么都不失效
    song = pipeline.edit_bar(sid, bar["id"], bar["events"], None)
    assert song["bars"][1]["tip"] == "tip 2" and not song["plan"].get("stale")

    song = pipeline.edit_bar(sid, bar["id"], [{"dur": "4", "notes": [{"string": 2, "fret": 3}]}], None)
    b = song["bars"][1]
    assert b["tip"] == "" and b["uncertain"] == "" and all(q["stale"] for q in b["qa"])
    assert song["plan"]["stale"] and song["bars"][0]["tip"] == "tip 1"  # 别的小节不受影响
    assert song["practice"]["bars"][bar["id"]]["loops"] == 2  # 同一个小节，练习记录保留

    # 之后再问：改谱之前的问答不再作为上下文
    pipeline.ask_bar(sid, bar["id"], "现在呢？")
    assert FakeClaude.calls[-1]["history"] == 0

    # 补写：只写缺失的小节，并重新生成计划
    _explain_sync(monkeypatch)
    FakeClaude.calls.clear()
    pipeline.start_explain(sid)
    song = store.load(sid)
    assert [c["explain"] for c in FakeClaude.calls if "explain" in c] == [[2]]
    assert song["bars"][1]["tip"] == "讲解 2" and song["bars"][0]["tip"] == "tip 1"
    assert not song["plan"].get("stale")
    with pytest.raises(ValueError, match="最新"):
        pipeline.start_explain(sid)


def test_changing_tuning_or_time_signature_invalidates_all_tips(data_dir):
    sid = pipeline.create_song([("p1.png", _png())])
    pipeline._run_parse(sid)
    song = pipeline.edit_meta(sid, {"title": "新名字"})
    assert all(b["tip"] for b in song["bars"]) and not song["plan"].get("stale")  # 只改名字
    song = pipeline.edit_meta(sid, {"capo": 4})
    assert not any(b["tip"] for b in song["bars"]) and song["plan"]["stale"]


def test_reparse_line_marks_plan_stale_and_drops_practice_of_replaced_bars(data_dir):
    sid = pipeline.create_song([("p1.png", _png())])
    pipeline._run_parse(sid)
    bars = store.load(sid)["bars"]
    pipeline.record_practice(sid, {"bar_ids": [bars[0]["id"]], "loops": 1, "bpm": 50, "seconds": 10})
    pipeline.record_practice(sid, {"bar_ids": [bars[4]["id"]], "loops": 1, "bpm": 60, "seconds": 10})
    pipeline.reparse_system(sid, 1)  # 第 2 行（小节 4–6）换成了新的小节
    song = store.load(sid)
    assert song["plan"]["stale"]
    assert set(song["practice"]["bars"]) == {bars[0]["id"]}
    assert "last" not in song["practice"]  # 最近一次练的是被替换的小节
    assert song["practice"]["total_seconds"] == 20


def test_practice_ignores_duplicate_bar_ids(data_dir):
    sid = pipeline.create_song([("曲子.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    pipeline.set_derived(sid, {"tuning": pipeline.STANDARD}, _derived_bars(2))
    ids = [b["id"] for b in store.load(sid)["bars"]]
    p = pipeline.record_practice(
        sid, {"bar_ids": [ids[0], ids[0], ids[1]], "loops": 1, "bpm": 60, "seconds": 20}
    )
    assert p["bars"][ids[0]] == {**p["bars"][ids[0]], "loops": 1, "seconds": 10}


def test_check_bar_flags_durations_the_player_cannot_write():
    meta = pipeline.normalize_meta({})
    ok = [{"dur": "1/3", "notes": []}] * 3 + [
        {"dur": "7/48", "notes": []},
        {"dur": "41/48", "notes": []},
        {"dur": "2", "notes": []},
    ]
    assert pipeline.check_bar(ok, meta) == []
    quint = [{"dur": "1/5", "notes": []}] * 5 + [{"dur": "3", "notes": []}]
    probs = pipeline.check_bar(quint, meta)
    assert len(probs) == 1 and "1/5" in probs[0] and "近似" in probs[0]


def test_explain_does_not_write_old_tips_onto_bars_edited_meanwhile(data_dir, monkeypatch):
    sid = pipeline.create_song([("x.gp5", b"\x18FICHIER GUITAR PRO v5.10")])
    pipeline.set_derived(sid, {"tuning": pipeline.STANDARD}, _derived_bars(3))
    bar_id = store.load(sid)["bars"][0]["id"]
    real = FakeClaude.explain_bars

    def explain_while_user_edits(self, meta, bars):
        out = real(self, meta, bars)
        pipeline.edit_bar(
            sid, bar_id, [{"dur": "4", "notes": [{"string": 1, "fret": 9}]}], None
        )  # 用户这时改了音
        return out

    monkeypatch.setattr(FakeClaude, "explain_bars", explain_while_user_edits)
    _explain_sync(monkeypatch)
    pipeline.start_explain(sid)
    song = store.load(sid)
    assert song["bars"][0]["tip"] == "" and song["bars"][1]["tip"] == "讲解 2"
    assert song["status"] == "ready"


def test_plan_built_from_old_notes_stays_stale(data_dir, monkeypatch):
    sid = pipeline.create_song([("x.gp5", b"\x18FICHIER GUITAR PRO v5.10")])
    pipeline.set_derived(sid, {"tuning": pipeline.STANDARD}, _derived_bars(2))
    bar_id = store.load(sid)["bars"][1]["id"]
    real = FakeClaude.practice_plan

    def plan_while_user_edits(self, meta, bars):
        out = real(self, meta, bars)
        pipeline.edit_bar(sid, bar_id, [{"dur": "4", "notes": [{"string": 3, "fret": 5}]}], None)
        return out

    monkeypatch.setattr(FakeClaude, "practice_plan", plan_while_user_edits)
    _explain_sync(monkeypatch)
    pipeline.start_explain(sid)
    plan = store.load(sid)["plan"]
    assert plan["stale"] and plan["changed_bars"] == [bar_id]


def test_full_parse_plan_built_from_old_notes_stays_stale(data_dir, monkeypatch):
    """整首识别最后生成计划期间用户改了音：计划还没写回（plan 为空）时改动标不上过时，写回时要补上。"""
    sid = pipeline.create_song([("p1.png", _png())])
    real = FakeClaude.practice_plan
    edited = []

    def plan_while_user_edits(self, meta, bars):
        out = real(self, meta, bars)
        edited.append(bars[2]["id"])
        pipeline.edit_bar(sid, bars[2]["id"], [{"dur": "4", "notes": [{"string": 3, "fret": 5}]}], None)
        return out

    monkeypatch.setattr(FakeClaude, "practice_plan", plan_while_user_edits)
    pipeline._run_parse(sid)
    song = store.load(sid)
    assert song["status"] == "ready"
    assert song["plan"]["stale"] and song["plan"]["changed_bars"] == edited and not song["plan"]["stale_all"]
    # 撤销回原样：计划又是最新的
    orig = song["bars"][2]["_orig"]["events"]
    song = pipeline.edit_bar(sid, edited[0], orig, None)
    assert not song["plan"]["stale"]


def test_undoing_an_edit_restores_tip_qa_and_plan(data_dir):
    sid = pipeline.create_song([("p1.png", _png())])
    pipeline._run_parse(sid)
    bar = store.load(sid)["bars"][1]
    pipeline.ask_bar(sid, bar["id"], "旧问题")
    pipeline.edit_bar(sid, bar["id"], [{"dur": "4", "notes": [{"string": 2, "fret": 3}]}], None)
    pipeline.ask_bar(sid, bar["id"], "改了之后的问题")
    pipeline.edit_bar(sid, bar["id"], [{"dur": "4", "notes": [{"string": 2, "fret": 4}]}], None)  # 再改一次
    song = pipeline.edit_bar(sid, bar["id"], bar["events"], None)  # 撤销回原样
    b = song["bars"][1]
    assert b["tip"] == "tip 2" and "_orig" not in b
    assert [q.get("stale", False) for q in b["qa"]] == [False, True]  # 原来的问答恢复，改动期间的变成过时
    assert not song["plan"]["stale"]


def test_meta_change_keeps_uncertain_notes(data_dir):
    sid = pipeline.create_song([("p1.png", _png())])
    pipeline._run_parse(sid)
    store.update(sid, lambda s: s["bars"][0].update(uncertain="第 3 拍看不清"))
    song = pipeline.edit_meta(sid, {"capo": 3})
    assert song["bars"][0]["uncertain"] == "第 3 拍看不清" and song["bars"][0]["tip"] == ""


def test_off_grid_durations_do_not_trigger_a_retry(data_dir, monkeypatch):
    def transcribe(
        self, img, meta, first_bar, spans, hint="", previous=None, problems=None, labeled_strings=False
    ):
        FakeClaude.calls.append({"first_bar": first_bar, "retry": previous is not None})
        quint = [
            {"dur": "1/5", "notes": [{"string": 1, "fret": k, "tech": [], "grace": -1, "bend": 0}]}
            for k in range(5)
        ]
        rest = [{"dur": "3", "notes": [{"string": 2, "fret": 0, "tech": [], "grace": -1, "bend": 0}]}]
        return {
            "bars": [
                {"number": first_bar + i, "events": quint + rest, "tip": "", "uncertain": ""}
                for i in range(len(spans))
            ]
        }

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    sid = pipeline.create_song([("p1.png", _png())])
    pipeline._run_parse(sid)
    assert not any(c.get("retry") for c in FakeClaude.calls)
    assert all(any("网格" in w for w in b["warn"]) for b in store.load(sid)["bars"])  # 只是提醒


def test_import_error_is_ignored_once_the_song_was_parsed(data_dir):
    sid = pipeline.create_song([("x.gp5", b"\x18FICHIER GUITAR PRO v5.10")])
    pipeline.set_derived(sid, {"tuning": pipeline.STANDARD}, _derived_bars(2))
    assert pipeline.import_failed(sid, "临时的网络错误")["status"] == "ready"


def test_restart_during_explain_restores_previous_status(data_dir):
    sid = pipeline.create_song([("p1.png", _png())])
    pipeline._run_parse(sid)
    store.update(
        sid,
        lambda s: s.update(
            status="parsing",
            job="explain",
            job_prev={"status": "cancelled", "error": "已取消"},
            progress={"stage": "生成讲解"},
        ),
    )
    pipeline.recover_interrupted()
    song = store.load(sid)
    assert song["status"] == "cancelled" and "讲解生成中断" in song["error"] and "job" not in song
