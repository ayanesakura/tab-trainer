"""AI 讲解与小节问答，以及练习记录。"""

import pytest

from app import coach, errors, jobs, music, recognize, songs, store
from tests.fakes import FakeClaude, derived_bars, score_png


def test_set_derived_and_explain(data_dir, sync_jobs):
    sid = songs.create_song([("x.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    bars = derived_bars(20)
    bars[3] = {"events": [{"dur": "4", "notes": [{"string": 9, "fret": 0}]}]}  # 坏数据：弦号 9
    meta = {"tuning": ["E4", "B3", "G3", "D3", "A2", "E2"], "capo": 0, "time_signature": [4, 4], "tempo": 90}
    song = songs.set_derived(sid, meta, bars, track=1)
    assert len(song["bars"]) == 20 and song["track"] == 1
    assert song["bars"][3]["events"] == [{"dur": "4", "notes": []}] and song["bars"][3]["warn"]
    assert song["bars"][5]["system"] == 1

    coach.start_explain(sid)
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
        songs.set_derived(songs.create_song([("p1.png", score_png())]), meta, bars)  # 图片曲子不能这样改


def test_explain_without_key_fails_fast(data_dir, monkeypatch):
    from app import llm

    monkeypatch.setattr(jobs, "Claude", llm.Claude)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sid = songs.create_song([("x.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    songs.set_derived(sid, {}, derived_bars(2))
    with pytest.raises(ValueError, match="API key"):
        coach.start_explain(sid)
    assert store.load(sid)["status"] == "ready" and not jobs.jobs.running(sid)


def test_ask_bar_keeps_history_and_sends_context(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    bar = store.load(sid)["bars"][1]
    songs.record_practice(sid, {"bar_ids": [bar["id"]], "loops": 3, "bpm": 50, "seconds": 60})
    r = coach.ask_bar(sid, bar["id"], "  这里左手怎么按？ ")
    assert r["answer"] == "回答：这里左手怎么按？" and len(r["qa"]) == 1
    call = FakeClaude.calls[-1]
    assert call["coach"] == 2 and call["neighbours"] == [1, 3] and call["image"]  # 图片曲子带上原谱截图
    assert "3 遍" in call["practice"] and "50 BPM" in call["practice"]
    coach.ask_bar(sid, bar["id"], "第二个问题")
    assert FakeClaude.calls[-1]["history"] == 1
    assert [q["q"] for q in store.load(sid)["bars"][1]["qa"]] == ["这里左手怎么按？", "第二个问题"]
    with pytest.raises(ValueError):
        coach.ask_bar(sid, bar["id"], "   ")
    with pytest.raises(errors.BarNotFound):
        coach.ask_bar(sid, "nope", "问题")


def test_ask_bar_without_key_is_a_user_error(data_dir, monkeypatch):
    from app import llm  # 用真正的客户端（没有 key 时构造就会失败）

    monkeypatch.setattr(jobs, "Claude", llm.Claude)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sid = songs.create_song([("曲子.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    songs.set_derived(sid, {"tuning": music.STANDARD}, derived_bars(2))
    bar_id = store.load(sid)["bars"][0]["id"]
    with pytest.raises(ValueError, match="API key"):
        coach.ask_bar(sid, bar_id, "怎么练？")


def test_practice_log_accumulates_per_bar(data_dir):
    sid = songs.create_song([("曲子.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    songs.set_derived(sid, {"tuning": music.STANDARD}, derived_bars(4))
    ids = [b["id"] for b in store.load(sid)["bars"]]
    songs.record_practice(sid, {"bar_ids": ids[1:3], "loops": 4, "bpm": 48, "seconds": 120})
    p = songs.record_practice(sid, {"bar_ids": ids[1:3] + ["gone"], "loops": 2, "bpm": 56, "seconds": 40})
    assert p["bars"][ids[1]]["loops"] == 6 and p["bars"][ids[1]]["best_bpm"] == 56
    assert p["bars"][ids[2]]["seconds"] == 80  # 时间平摊到两个小节
    assert ids[0] not in p["bars"]
    assert p["last"]["a"] == ids[1] and p["last"]["b"] == ids[2] and p["last"]["bpm"] == 56
    assert p["total_seconds"] == 160
    # 只听了一会儿没循环完：记时间，不更新最快速度
    p = songs.record_practice(sid, {"bar_ids": [ids[3]], "loops": 0, "bpm": 90, "seconds": 10})
    assert p["bars"][ids[3]]["best_bpm"] == 0 and p["bars"][ids[3]]["seconds"] == 10
    for bad in (
        {},
        {"bar_ids": ids, "bpm": 0},
        {"bar_ids": ids, "bpm": 60, "loops": -1},
        {"bar_ids": ids, "bpm": "x"},
    ):
        with pytest.raises(ValueError):
            songs.record_practice(sid, bad)


def test_practice_ignores_duplicate_bar_ids(data_dir):
    sid = songs.create_song([("曲子.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    songs.set_derived(sid, {"tuning": music.STANDARD}, derived_bars(2))
    ids = [b["id"] for b in store.load(sid)["bars"]]
    p = songs.record_practice(
        sid, {"bar_ids": [ids[0], ids[0], ids[1]], "loops": 1, "bpm": 60, "seconds": 20}
    )
    assert p["bars"][ids[0]] == {**p["bars"][ids[0]], "loops": 1, "seconds": 10}


def test_explain_runs_beside_the_song_status(data_dir, sync_jobs, monkeypatch):
    """讲解期间不动 status（曲子照常能练、曲库不会显示成识别中），进度记在 job / progress。"""
    sid = songs.create_song([("x.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    songs.set_derived(sid, {"tuning": music.STANDARD}, derived_bars(3))
    seen = []
    real = FakeClaude.explain_bars

    def explain(self, meta, bars):
        s = store.load(sid)
        seen.append((s["status"], s.get("job"), [x["job"] for x in store.list_songs()]))
        if len(seen) == 1:
            raise RuntimeError("网络断了")
        return real(self, meta, bars)

    monkeypatch.setattr(FakeClaude, "explain_bars", explain)
    coach.start_explain(sid)
    s = store.load(sid)
    assert seen[0] == ("ready", "explain", ["explain"])
    assert s["status"] == "ready" and "job" not in s and s["error"].startswith("生成讲解失败")
    coach.start_explain(sid)  # 再试一次成功：上次失败的提示清掉
    s = store.load(sid)
    assert s["status"] == "ready" and not s["error"] and s["bars"][0]["tip"]


def test_restart_during_explain_clears_the_job(data_dir):
    sid = songs.create_song([("x.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    songs.set_derived(sid, {"tuning": music.STANDARD}, derived_bars(2))
    store.update(sid, lambda s: s.update(job="explain", progress={"stage": "生成讲解"}))
    songs.recover_interrupted()
    s = store.load(sid)
    assert s["status"] == "ready" and "job" not in s and not s["progress"] and "讲解生成中断" in s["error"]


def test_claim_is_released_when_the_job_cannot_start(data_dir, monkeypatch):
    sid = songs.create_song([("p1.png", score_png())])

    def broken(*a, **kw):
        raise OSError("磁盘满了")

    monkeypatch.setattr(store, "patch", broken)
    with pytest.raises(OSError):
        recognize.start_parse(sid)
    assert not jobs.jobs.running(sid)
