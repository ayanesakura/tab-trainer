"""谱面改动后 AI 内容（提示、问答、练习建议）的失效与恢复。"""

import pytest

from app import coach, practice, recognize, songs, staleness, store
from tests.fakes import FakeClaude, score_png


def _image_song():
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    store.update(sid, lambda s: [b.update(tip="") for b in s["bars"]] and s.update(plan=None))
    return sid


def test_editing_notes_invalidates_tip_qa_and_plan_but_keeps_practice(data_dir, sync_jobs):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    song = store.load(sid)
    bar = song["bars"][1]
    coach.ask_bar(sid, bar["id"], "怎么练？")
    practice.record(sid, [bar["id"]], 2, 50, 30)
    assert store.load(sid)["plan"] and not store.load(sid)["plan"].get("stale")

    # 内容没变的保存：什么都不失效
    song = songs.edit_bar(sid, bar["id"], bar["events"], None)
    assert song["bars"][1]["tip"] == "tip 2" and not song["plan"].get("stale")

    song = songs.edit_bar(sid, bar["id"], [{"dur": "4", "notes": [{"string": 2, "fret": 3}]}], None)
    b = song["bars"][1]
    assert b["tip"] == "" and b["uncertain"] == "" and all(q["stale"] for q in b["qa"])
    assert song["plan"]["stale"] and song["bars"][0]["tip"] == "tip 1"  # 别的小节不受影响
    assert b["edited"] and b["id"] == bar["id"]
    assert practice.aggregate(sid, song)["bars"][bar["id"]]["loops"] == 2  # 同一个小节，练习记录保留

    # 之后再问：改谱之前的问答不再作为上下文
    coach.ask_bar(sid, bar["id"], "现在呢？")
    assert FakeClaude.calls[-1]["history"] == 0

    # 补写：只写缺失的小节，并重新生成计划
    FakeClaude.calls.clear()
    coach.start_explain(sid)
    song = store.load(sid)
    assert [c["explain"] for c in FakeClaude.calls if "explain" in c] == [[2]]
    assert song["bars"][1]["tip"] == "讲解 2" and song["bars"][0]["tip"] == "tip 1"
    assert not song["plan"].get("stale")
    with pytest.raises(ValueError, match="最新"):
        coach.start_explain(sid)


def test_changing_tuning_or_time_signature_invalidates_all_tips(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    song = songs.edit_meta(sid, {"title": "新名字"})
    assert all(b["tip"] for b in song["bars"]) and not song["plan"].get("stale")  # 只改名字
    song = songs.edit_meta(sid, {"capo": 4})
    assert not any(b["tip"] for b in song["bars"]) and song["plan"]["stale"]


def test_undoing_an_edit_restores_tip_qa_and_plan(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    bar = store.load(sid)["bars"][1]
    coach.ask_bar(sid, bar["id"], "旧问题")
    songs.edit_bar(sid, bar["id"], [{"dur": "4", "notes": [{"string": 2, "fret": 3}]}], None)
    coach.ask_bar(sid, bar["id"], "改了之后的问题")
    songs.edit_bar(sid, bar["id"], [{"dur": "4", "notes": [{"string": 2, "fret": 4}]}], None)  # 再改一次
    song = songs.edit_bar(sid, bar["id"], bar["events"], None)  # 撤销回原样
    b = song["bars"][1]
    assert b["tip"] == "tip 2" and "_orig" not in b
    assert [q.get("stale", False) for q in b["qa"]] == [False, True]  # 原来的问答恢复，改动期间的变成过时
    assert not song["plan"]["stale"]


def test_meta_change_keeps_uncertain_notes(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    store.update(sid, lambda s: s["bars"][0].update(uncertain="第 3 拍看不清"))
    song = songs.edit_meta(sid, {"capo": 3})
    assert song["bars"][0]["uncertain"] == "第 3 拍看不清" and song["bars"][0]["tip"] == ""


def test_explain_does_not_write_old_tips_onto_bars_edited_meanwhile(data_dir, monkeypatch, sync_jobs):
    sid = _image_song()
    bar_id = store.load(sid)["bars"][0]["id"]
    real = FakeClaude.explain_bars

    def explain_while_user_edits(self, meta, bars):
        out = real(self, meta, bars)
        songs.edit_bar(
            sid, bar_id, [{"dur": "4", "notes": [{"string": 1, "fret": 9}]}], None
        )  # 用户这时改了音
        return out

    monkeypatch.setattr(FakeClaude, "explain_bars", explain_while_user_edits)
    coach.start_explain(sid)
    song = store.load(sid)
    assert song["bars"][0]["tip"] == "" and song["bars"][1]["tip"] == "讲解 2"
    assert song["status"] == "ready"


def test_plan_built_from_old_notes_stays_stale(data_dir, monkeypatch, sync_jobs):
    sid = _image_song()
    bar_id = store.load(sid)["bars"][1]["id"]
    real = FakeClaude.practice_plan

    def plan_while_user_edits(self, meta, bars):
        out = real(self, meta, bars)
        songs.edit_bar(sid, bar_id, [{"dur": "4", "notes": [{"string": 3, "fret": 5}]}], None)
        return out

    monkeypatch.setattr(FakeClaude, "practice_plan", plan_while_user_edits)
    coach.start_explain(sid)
    plan = store.load(sid)["plan"]
    assert plan["stale"] and plan["changed_bars"] == [bar_id]


def test_explain_keeps_the_edited_flag_and_undo_baseline(data_dir, sync_jobs):
    """讲解给手改过的小节补写提示后，它仍然是“手改过”（整首重新识别时保留用户的音），撤销照样能回到识别结果。"""
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    bar = store.load(sid)["bars"][1]
    songs.edit_bar(sid, bar["id"], [{"dur": "4", "notes": [{"string": 2, "fret": 3}]}], None)
    coach.start_explain(sid)
    b = store.load(sid)["bars"][1]
    assert b["tip"] == "讲解 2" and b["edited"] and b["_orig"]["events"] == bar["events"]
    b = songs.edit_bar(sid, bar["id"], bar["events"], None)["bars"][1]
    assert not b["edited"] and b["tip"] == "tip 2"


def test_kept_hand_edit_is_no_longer_a_placeholder_rest():
    """重新识别时这个小节没读出来（按休止占位），保留下来的是用户的音：之后按拍号重算时不能被换成休止。"""
    from app import music

    mine = [{"dur": "1", "notes": [{"string": 2, "fret": 3, "tech": [], "grace": -1, "bend": 0}]}] * 4
    prev = {"id": "a", "events": mine, "tip": "t", "edited": True}
    new = {"id": "b", "events": [{"dur": "4", "notes": []}], "placeholder": True, "warn": []}
    staleness._keep_edit(prev, new)
    assert new["events"] == mine and "placeholder" not in new
    meta = music.normalize_meta({"title": "x", "tuning": music.STANDARD, "time_signature": [4, 4]})
    bars = [new]
    music.apply_time_signatures(bars, meta)
    assert bars[0]["events"] == mine
