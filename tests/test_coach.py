"""AI 讲解与小节问答，以及练习记录。"""

import json

import pytest

from app import coach, config, errors, jobs, music, practice, recognize, songs, store, views
from tests.fakes import FakeClaude, derived_bars, score_png


def test_set_derived_and_explain(data_dir, sync_jobs):
    sid = songs.create_song([("x.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    bars = derived_bars(20)
    bars[3] = {"events": [{"dur": "4", "notes": [{"string": 9, "fret": 0}]}]}  # 坏数据：弦号 9
    meta = {"tuning": ["E4", "B3", "G3", "D3", "A2", "E2"], "capo": 0, "time_signature": [4, 4], "tempo": 90}
    song = songs.set_derived(sid, meta, bars, track=1)
    assert len(song["bars"]) == 20 and song["track"] == 1
    assert (
        song["bars"][3]["events"] == [{"dur": "4", "notes": []}]
        and song["bars"][3]["warn"][0]["code"] == "other"
    )
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

    monkeypatch.setattr(config, "llm_factory", llm.Claude)
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
    practice.record(sid, [bar["id"]], 3, 50, 60)
    r = coach.ask_bar(sid, bar["id"], "这里左手怎么按？")
    assert r["answer"] == "回答：这里左手怎么按？" and len(r["qa"]) == 1
    call = FakeClaude.calls[-1]
    assert call["coach"] == 2 and call["neighbours"] == [1, 3] and call["image"]  # 图片曲子带上原谱截图
    assert "3 遍" in call["practice"] and "50 BPM" in call["practice"]
    coach.ask_bar(sid, bar["id"], "第二个问题")
    assert FakeClaude.calls[-1]["history"] == 1
    assert [q["q"] for q in store.load(sid)["bars"][1]["qa"]] == ["这里左手怎么按？", "第二个问题"]
    with pytest.raises(errors.BarNotFound):
        coach.ask_bar(sid, "nope", "问题")


def test_ask_bar_without_key_is_a_user_error(data_dir, monkeypatch):
    from app import llm  # 用真正的客户端（没有 key 时构造就会失败）

    monkeypatch.setattr(config, "llm_factory", llm.Claude)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sid = songs.create_song([("曲子.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    songs.set_derived(sid, {"tuning": music.STANDARD}, derived_bars(2))
    bar_id = store.load(sid)["bars"][0]["id"]
    with pytest.raises(ValueError, match="API key") as e:
        coach.ask_bar(sid, bar_id, "怎么练？")
    assert e.value.status == 400


def test_practice_log_accumulates_per_bar(data_dir):
    sid = songs.create_song([("曲子.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    songs.set_derived(sid, {"tuning": music.STANDARD}, derived_bars(4))
    ids = [b["id"] for b in store.load(sid)["bars"]]
    practice.record(sid, ids[1:3], 4, 48, 120)
    p = practice.record(sid, [*ids[1:3], "gone"], 2, 56, 40)
    assert p["bars"][ids[1]]["loops"] == 6 and p["bars"][ids[1]]["best_bpm"] == 56
    assert p["bars"][ids[2]]["seconds"] == 80  # 时间平摊到两个小节
    assert ids[0] not in p["bars"]
    assert p["last"]["a"] == ids[1] and p["last"]["b"] == ids[2] and p["last"]["bpm"] == 56
    assert (p["last"]["a_n"], p["last"]["b_n"]) == (2, 3)
    assert p["total_seconds"] == 160
    # 只听了一会儿没循环完：记时间，不更新最快速度
    p = practice.record(sid, [ids[3]], 0, 90, 10)
    assert p["bars"][ids[3]]["best_bpm"] == 0 and p["bars"][ids[3]]["seconds"] == 10
    # 追加写 practice.jsonl，不改 song.json
    rev = store.load(sid)["rev"]
    practice.record(sid, [ids[0]], 1, 60, 5)
    assert store.load(sid)["rev"] == rev
    lines = (store.song_dir(sid) / "practice.jsonl").read_text().splitlines()
    assert len(lines) == 4 and '"gone"' not in lines[1]
    assert practice.record(sid, ["gone"], 1, 60, 5)["total_seconds"] == 175  # 全都不存在：不记


def test_practice_ignores_duplicate_bar_ids(data_dir):
    sid = songs.create_song([("曲子.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    songs.set_derived(sid, {"tuning": music.STANDARD}, derived_bars(2))
    ids = [b["id"] for b in store.load(sid)["bars"]]
    p = practice.record(sid, [ids[0], ids[0], ids[1]], 1, 60, 20)
    assert p["bars"][ids[0]] == {**p["bars"][ids[0]], "loops": 1, "seconds": 10}


def test_practice_cache_follows_the_file(data_dir):
    sid = songs.create_song([("曲子.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    songs.set_derived(sid, {"tuning": music.STANDARD}, derived_bars(2))
    song = store.load(sid)
    ids = [b["id"] for b in song["bars"]]
    practice.record(sid, ids, 1, 60, 20)
    assert practice.aggregate(sid, song)["total_seconds"] == 20
    with open(store.song_dir(sid) / "practice.jsonl", "a") as f:  # 别处追加的、断电留下的半行
        rec = {"at": "2026-01-01 10:00", "ids": [ids[0]], "loops": 1, "bpm": 50, "seconds": 7}
        f.write(json.dumps(rec) + '\n{"at": ')
    assert practice.aggregate(sid, song)["total_seconds"] == 27


def test_explain_runs_beside_the_song_status(data_dir, sync_jobs, monkeypatch):
    """讲解期间不动 status（曲子照常能练、曲库不会显示成识别中），进度记在 job / progress。"""
    sid = songs.create_song([("x.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    songs.set_derived(sid, {"tuning": music.STANDARD}, derived_bars(3))
    seen = []
    real = FakeClaude.explain_bars

    def explain(self, meta, bars):
        s = store.load(sid)
        row = views.list_library()[0]
        seen.append((s["status"], s["activity"]["kind"], row["busy"], row["recognizing"]))
        if len(seen) == 1:
            raise RuntimeError("网络断了")
        return real(self, meta, bars)

    monkeypatch.setattr(FakeClaude, "explain_bars", explain)
    coach.start_explain(sid)
    s = store.load(sid)
    assert seen[0] == ("ready", "explain", True, False)
    assert s["status"] == "ready" and s["activity"] is None
    assert s["notice"]["kind"] == "error" and s["notice"]["msg"].startswith("生成讲解失败")
    coach.start_explain(sid)  # 再试一次成功：上次失败的提示清掉
    s = store.load(sid)
    assert s["status"] == "ready" and not s["notice"] and s["bars"][0]["tip"]


def test_restart_during_explain_clears_the_job(data_dir):
    sid = songs.create_song([("x.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    songs.set_derived(sid, {"tuning": music.STANDARD}, derived_bars(2))
    store.update(
        sid,
        lambda s: s.update(
            activity={"kind": "explain", "restaging": False, "systems": []}, progress={"stage": "生成讲解"}
        ),
    )
    songs.recover_interrupted()
    s = store.load(sid)
    assert s["status"] == "ready" and s["activity"] is None and not s["progress"]
    assert "讲解生成中断" in s["notice"]["msg"]


def test_claim_is_released_when_the_job_cannot_start(data_dir, monkeypatch):
    sid = songs.create_song([("p1.png", score_png())])

    def broken(*a, **kw):
        raise OSError("磁盘满了")

    monkeypatch.setattr(store, "update", broken)
    with pytest.raises(OSError):
        recognize.start_parse(sid)
    assert not jobs.jobs.running(sid)


def test_renaming_during_explain_or_recognition_keeps_ai_content(data_dir, sync_jobs, monkeypatch):
    """改曲名（派生的 meta.title 跟着变）不影响提示和练习建议；改调弦才算设定变了。"""
    sid = songs.create_song([("x.gp5", b"\x18FICHIER GUITAR PRO v5.10")])
    meta = {"tuning": ["E4", "B3", "G3", "D3", "A2", "E2"], "time_signature": [4, 4]}
    songs.set_derived(sid, meta, derived_bars(20))
    real = FakeClaude.explain_bars

    def rename_while_explaining(self, meta, bars):
        songs.edit_meta(sid, {"title": f"新名字 {bars[0]['n']}"})
        return real(self, meta, bars)

    monkeypatch.setattr(FakeClaude, "explain_bars", rename_while_explaining)
    real_plan = FakeClaude.practice_plan
    monkeypatch.setattr(
        FakeClaude,
        "practice_plan",
        lambda self, meta, bars: (
            songs.edit_meta(sid, {"title": "最后的名字"}) and real_plan(self, meta, bars)
        ),
    )
    coach.start_explain(sid)
    song = store.load(sid)
    assert all(b["tip"] for b in song["bars"]), [b["tip"] for b in song["bars"]]
    assert song["title"] == "最后的名字" and not song["plan"]["stale"]

    # 图片曲子整首识别：生成练习建议期间改了曲名，建议不算过时
    img = songs.create_song([("p1.png", score_png())])
    monkeypatch.setattr(
        FakeClaude,
        "practice_plan",
        lambda self, meta, bars: songs.edit_meta(img, {"title": "改名"}) and real_plan(self, meta, bars),
    )
    recognize.run_parse(img)
    assert not store.load(img)["plan"]["stale"]

    # 改了变调夹：按旧设定写的这批提示作废（练习建议是之后按新设定生成的，不过时）
    sid2 = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid2)
    store.update(sid2, lambda s: [b.update(tip="") for b in s["bars"]])
    monkeypatch.setattr(FakeClaude, "practice_plan", real_plan)

    def retune_while_explaining(self, meta, bars):
        songs.edit_meta(sid2, {"capo": 5})
        return real(self, meta, bars)

    monkeypatch.setattr(FakeClaude, "explain_bars", retune_while_explaining)
    coach.start_explain(sid2)
    song = store.load(sid2)
    assert not any(b["tip"] for b in song["bars"]) and song["meta"]["capo"] == 5


def test_failed_explain_still_records_its_cost(data_dir, sync_jobs, monkeypatch):
    """讲解某一批失败：这一批的请求也花了钱，要记到曲子上（已经完成的批次不重复记）。"""
    sid = songs.create_song([("x.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    songs.set_derived(sid, {}, derived_bars(20))
    real = FakeClaude.explain_bars

    def explain_bars(self, meta, bars):
        self.usage["input_tokens"] += 1000
        if bars[0]["n"] > 1:
            raise errors.Upstream("输出被截断")
        return real(self, meta, bars)

    monkeypatch.setattr(FakeClaude, "explain_bars", explain_bars)
    coach.start_explain(sid)
    song = store.load(sid)
    assert song["notice"]["kind"] == "error"
    assert song["usage"]["input_tokens"] == 2010  # 替身一开始就带 10：新建的客户端用掉的全算


@pytest.mark.parametrize("answer", [None, "  "])
def test_failed_question_still_records_its_cost(data_dir, monkeypatch, answer):
    sid = songs.create_song([("x.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    song = songs.set_derived(sid, {}, derived_bars(2))
    before = song["usage"]["input_tokens"]

    def coach_(self, *a, **kw):
        self.usage["input_tokens"] += 1000
        if answer is None:
            raise RuntimeError("connection reset")
        yield answer

    monkeypatch.setattr(FakeClaude, "coach_stream", coach_)
    with pytest.raises(errors.Upstream):
        coach.ask_bar(sid, song["bars"][0]["id"], "怎么弹")
    assert store.load(sid)["usage"]["input_tokens"] == before + 1010  # 替身一开始就带 10
