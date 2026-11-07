"""调整图片曲子的页面：换顺序、删页、补页（只识别新页上的行），以及标签和花费。"""

import pytest
from fastapi.testclient import TestClient

from app import errors, jobs, main, pages, practice, recognize, songs, store, views
from tests.fakes import FakeClaude, derived_bars, score_png


def _two_page_song():
    sid = songs.create_song([("a.png", score_png()), ("b.png", score_png())])
    recognize.run_parse(sid)
    song = store.load(sid)
    assert [p["file"] for p in song["pages"]] == ["p1.png", "p2.png"] and len(song["bars"]) == 12
    return sid, song


def test_reorder_moves_bars_without_recognizing(data_dir):
    sid, before = _two_page_song()
    practice.record(sid, [before["bars"][7]["id"]], 1, 60, 10)
    calls = len(FakeClaude.calls)

    after = pages.arrange_pages(sid, ["p2.png", "p1.png"], [])

    assert len(FakeClaude.calls) == calls  # 不花钱
    assert [p["file"] for p in after["pages"]] == ["p2.png", "p1.png"]
    ids_before = [b["id"] for b in before["bars"]]
    assert [b["id"] for b in after["bars"]] == ids_before[6:] + ids_before[:6]
    assert [b["n"] for b in after["bars"]] == list(range(1, 13))
    assert [s["page"] for s in after["systems"]] == [0, 0, 1, 1]
    assert all(b["page"] == after["systems"][b["system"]]["page"] for b in after["bars"])
    assert before["bars"][7]["id"] in practice.aggregate(sid, after)["bars"]
    assert after["plan"]["stale"]  # 小节号变了


def test_deleting_a_page_drops_its_bars_and_file(data_dir):
    sid, before = _two_page_song()
    practice.record(sid, [before["bars"][7]["id"]], 1, 60, 10)

    after = pages.arrange_pages(sid, ["p1.png"], [])

    assert [b["id"] for b in after["bars"]] == [b["id"] for b in before["bars"][:6]]
    assert len(after["systems"]) == 2 and not practice.aggregate(sid, after)["bars"]
    assert not (store.song_dir(sid) / "pages" / "p2.png").exists()


def test_added_page_is_recognized_alone(data_dir, sync_jobs, monkeypatch):
    real = FakeClaude.transcribe

    def transcribe(self, *a, **kw):  # 替身模型每次调用记一点用量
        self.usage = {**self.usage, "input_tokens": self.usage["input_tokens"] + 100, "cost_usd": 0.5}
        return real(self, *a, **kw)

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    sid = songs.create_song([("a.png", score_png())])
    recognize.run_parse(sid)
    before = store.load(sid)
    FakeClaude.calls = []

    after = pages.arrange_pages(sid, ["new:0", "p1.png"], [("c.png", score_png())])

    after = store.load(sid)
    assert [p["file"] for p in after["pages"]] == ["p2.png", "p1.png"]
    assert len(after["bars"]) == 12 and [b["n"] for b in after["bars"]] == list(range(1, 13))
    assert [b["id"] for b in after["bars"][6:]] == [b["id"] for b in before["bars"]]  # 原来的小节没动
    assert all(s["status"] == "done" for s in after["systems"])
    # 只识别了新页的两行（第 1 行故意写错时值会返修一次），没有重新读谱头
    assert {c["first_bar"] for c in FakeClaude.calls} == {1, 4}
    assert after["activity"] is None and after["progress"] is None and after["plan"]["stale"]
    assert after["usage"]["input_tokens"] > before["usage"]["input_tokens"]
    assert after["usage"]["cost_usd"] > before["usage"]["cost_usd"]


def test_new_page_on_song_without_bars_runs_full_recognition(data_dir, sync_jobs, monkeypatch):
    sid = songs.create_song([("a.png", score_png())])
    store.patch(sid, status="error", notice={"kind": "error", "msg": "没有识别出任何小节", "source": "parse"})
    started = []
    real = recognize.start_parse

    def spy(s, cancel=None):
        started.append((s, jobs.jobs.running(s)))  # 调整页面占着的任务登记一直没放手
        return real(s, cancel)

    monkeypatch.setattr(recognize, "start_parse", spy)
    pages.arrange_pages(sid, ["p1.png", "new:0"], [("c.png", score_png())])
    assert started == [(sid, True)]
    song = store.load(sid)
    assert len(song["pages"]) == 2 and len(song["bars"]) == 12 and song["status"] == "ready"
    assert song["notice"] is None and all(x["status"] == "done" for x in song["systems"])
    assert not jobs.jobs.running(sid)


def test_replacing_every_recognized_page_runs_full_recognition_without_stuck_rows(data_dir, monkeypatch):
    """原来的小节都在删掉的页上：按删完之后的小节判断（以前按删之前的，新行被标成 parsing），
    整首识别启动前一直占着任务登记；启动不了时新行也不会卡在“识别中”。"""
    sid = songs.create_song([("a.png", score_png())])
    recognize.run_parse(sid)
    seen = []

    def no_start(s, cancel=None):
        seen.append([x["status"] for x in store.load(s)["systems"]])
        raise RuntimeError("磁盘满了")

    monkeypatch.setattr(recognize, "start_parse", no_start)
    with pytest.raises(RuntimeError):
        pages.arrange_pages(sid, ["new:0"], [("c.png", score_png())])
    assert seen == [["pending", "pending"]]  # 不是 parsing：不会锁住、也不会一直显示在识别
    assert not jobs.jobs.running(sid)
    song = store.load(sid)
    assert song["bars"] == [] and not any(x["status"] == "parsing" for x in song["systems"])


@pytest.mark.parametrize(
    ("order", "n_up", "match"),
    [
        ([], 0, "至少要留一页"),
        (["p1.png", "p1.png"], 0, "重复"),
        (["nope.png"], 0, "不认识"),
        (["p1.png", "new:3"], 1, "对不上"),
        (["p1.png"], 1, "没有放进页序"),
    ],
)
def test_bad_orders_are_rejected(data_dir, order, n_up, match):
    sid = songs.create_song([("a.png", score_png())])
    recognize.run_parse(sid)
    with pytest.raises(errors.Invalid, match=match):
        pages.arrange_pages(sid, order, [("c.png", score_png())] * n_up)
    assert not jobs.jobs.running(sid)
    assert [p["file"] for p in store.load(sid)["pages"]] == ["p1.png"]
    assert sorted(f.name for f in (store.song_dir(sid) / "pages").iterdir()) == ["p1.png"]


def test_busy_and_score_songs_are_refused(data_dir):
    sid = songs.create_song([("a.png", score_png())])
    recognize.run_parse(sid)
    jobs.jobs.claim(sid)
    try:
        with pytest.raises(errors.Busy):
            pages.arrange_pages(sid, ["p1.png"], [])
    finally:
        jobs.jobs.release(sid)
    gp = songs.create_song([("x.gp5", b"\x18FICHIER GUITAR PRO v5.10")])
    with pytest.raises(errors.Invalid, match="没有页面"):
        pages.arrange_pages(gp, [], [])


def test_tags_and_cost_in_library(data_dir):
    sid = songs.create_song([("a.png", score_png())])
    c = TestClient(main.app, base_url="http://127.0.0.1:8765")
    r = c.put(f"/api/songs/{sid}/tags", json={"tags": [" 指弹 ", "练习中", "指弹", ""]})
    assert r.json() == {"tags": ["指弹", "练习中"]}
    store.patch(sid, usage={"input_tokens": 1_000_000, "output_tokens": 100_000})  # 早期只记 token
    row = next(r for r in views.list_library() if r["id"] == sid)
    assert row["tags"] == ["指弹", "练习中"]
    assert row["cost_usd"] == pytest.approx(4 + 2) and row["cost_estimated"]
    store.patch(sid, usage={"input_tokens": 5, "output_tokens": 5, "cost_usd": 1.5, "unpriced_tokens": 0})
    row = next(r for r in views.list_library() if r["id"] == sid)
    assert row["cost_usd"] == 1.5 and not row["cost_estimated"]


def test_pages_and_tags_over_http(data_dir, sync_jobs):
    c = TestClient(main.app, base_url="http://127.0.0.1:8765")
    sid = songs.create_song([("a.png", score_png()), ("b.png", score_png())])
    recognize.run_parse(sid)
    r = c.post(f"/api/songs/{sid}/pages", data={"order": '["p2.png"]'})
    assert r.status_code == 200 and [p["file"] for p in r.json()["pages"]] == ["p2.png"]
    r = c.post(
        f"/api/songs/{sid}/pages",
        data={"order": '["p2.png", "new:0"]'},
        files={"files": ("c.png", score_png())},
    )
    assert r.status_code == 200 and len(store.load(sid)["bars"]) == 12
    assert c.post(f"/api/songs/{sid}/pages", data={"order": "{"}).status_code == 400
    assert c.put(f"/api/songs/{sid}/tags", json={"tags": ["a"]}).json() == {"tags": ["a"]}
    assert c.put(f"/api/songs/{sid}/tags", json={"tags": "a"}).status_code == 400

    gp = songs.create_song([("x.gp5", b"\x18FICHIER GUITAR PRO v5.10")])
    songs.set_derived(gp, {}, derived_bars(2))
    assert c.post(f"/api/songs/{gp}/pages", data={"order": "[]"}).status_code == 400


def test_rename_before_the_header_is_read(data_dir):
    sid = songs.create_song([("a.png", score_png())])
    assert not store.load(sid)["meta"]
    songs.edit_meta(sid, {"title": "新名字"})
    assert next(r for r in views.list_library() if r["id"] == sid)["title"] == "新名字"


def test_edit_just_before_the_new_page_ts_rerun_is_not_overwritten(data_dir, sync_jobs, monkeypatch):
    """补页后按变拍号重做的行：挑行和锁住在同一次 store.update 里（同整首识别的 begin_redo），
    用户在第一遍识别完、锁住之前改了那一行，也不会被重做覆盖。"""
    calls = []

    def transcribe(self, img, meta, first_bar, spans, start_ts=None, **kw):
        calls.append((first_bar, start_ts))
        new_page_row1 = first_bar == 7  # 新页第一行开头写着 3/4，后面的行并行识别时看不到
        beats = 3 if new_page_row1 or start_ts == [3, 4] else 4
        bars = [
            {
                "time_signature": [3, 4] if new_page_row1 and i == 0 else [],
                "events": [{"dur": "1", "notes": [{"string": 1, "fret": i}]} for _ in range(beats)],
                "tip": "",
                "uncertain": "",
            }
            for i in range(len(spans))
        ]
        return {"bars": bars}

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    sid = songs.create_song([("a.png", score_png())])
    recognize.run_parse(sid)
    real_load = store.load
    edited = []

    def load(song_id):
        song = real_load(song_id)
        systems = song["systems"]
        if not edited and len(systems) == 4 and all(x["status"] == "done" for x in systems):
            bar = next(b for b in song["bars"] if b["system"] == 3)  # 第一遍刚识别完，用户改了新页第二行
            edited.append(bar["id"])
            songs.edit_bar(song_id, bar["id"], [{"dur": "4", "notes": [{"string": 2, "fret": 9}]}], None)
        return song

    monkeypatch.setattr(store, "load", load)
    pages.arrange_pages(sid, ["p1.png", "new:0"], [("c.png", score_png())])
    song = real_load(sid)
    bar = next(b for b in song["bars"] if b["id"] == edited[0])
    assert bar["edited"] and bar["events"][0]["notes"][0]["fret"] == 9
    assert [ts for fb, ts in calls if fb == 10] == [None]  # 新页第二行没有重做（和全曲拍号一样时不传）
    assert all(s["status"] == "done" for s in song["systems"])


def test_retry_failed_rows_only_rereads_the_failed_ones(data_dir, sync_jobs, monkeypatch):
    """第一次识别第二行失败：“重试失败的行”只重新识别这一行，成功后曲子没有失败的行，提示也清掉。"""
    real = FakeClaude.transcribe
    broken = {"on": True}

    def transcribe(self, img, meta, first_bar, *a, **kw):
        if first_bar > 1 and broken["on"]:
            raise RuntimeError("网络断了")
        return real(self, img, meta, first_bar, *a, **kw)

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    c = TestClient(main.app, base_url="http://127.0.0.1:8765")
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    v = c.get(f"/api/songs/{sid}").json()
    assert v["failed_rows"] == 1 and not v["is_score"] and v["notice"]
    assert next(x for x in c.get("/api/songs").json() if x["id"] == sid)["failed_rows"] == 1
    first_row = [b["id"] for b in store.load(sid)["bars"]]

    broken["on"] = False
    FakeClaude.calls = []
    assert c.post(f"/api/songs/{sid}/retry-failed").status_code == 200
    assert {x["first_bar"] for x in FakeClaude.calls} == {4}  # 只识别了第二行（第一行没动、没花钱）
    s = store.load(sid)
    assert [x["status"] for x in s["systems"]] == ["done", "done"] and s["activity"] is None
    assert [b["id"] for b in s["bars"]][: len(first_row)] == first_row and len(s["bars"]) == 6
    v = c.get(f"/api/songs/{sid}").json()
    assert v["failed_rows"] == 0 and v["status"] == "ready" and v["notice"] is None

    r = c.post(f"/api/songs/{sid}/retry-failed")
    assert r.status_code == 400 and "没有识别失败的行" in r.json()["detail"]


def test_retry_failed_with_nothing_recognized_runs_the_whole_song(data_dir, sync_jobs, monkeypatch):
    real = FakeClaude.read_meta
    monkeypatch.setattr(FakeClaude, "read_meta", lambda self, page: 1 / 0)
    sid = songs.create_song([("p1.png", score_png())])
    assert recognize.start_parse(sid)
    assert store.load(sid)["status"] == "error"
    monkeypatch.setattr(FakeClaude, "read_meta", real)
    recognize.start_retry_failed(sid)
    s = store.load(sid)
    assert s["status"] == "ready" and len(s["bars"]) == 6


def test_retry_failed_rejects_gp_songs(data_dir):
    gp = songs.create_song([("x.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    with pytest.raises(errors.Invalid):
        recognize.start_retry_failed(gp)
