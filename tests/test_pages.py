"""调整图片曲子的页面：换顺序、删页、补页（只识别新页上的行），以及标签和花费。"""

import pytest
from fastapi.testclient import TestClient

from app import errors, jobs, main, recognize, songs, store
from tests.fakes import FakeClaude, derived_bars, score_png


def _two_page_song():
    sid = songs.create_song([("a.png", score_png()), ("b.png", score_png())])
    recognize.run_parse(sid)
    song = store.load(sid)
    assert [p["file"] for p in song["pages"]] == ["p1.png", "p2.png"] and len(song["bars"]) == 12
    return sid, song


def test_reorder_moves_bars_without_recognizing(data_dir):
    sid, before = _two_page_song()
    songs.record_practice(sid, {"bar_ids": [before["bars"][7]["id"]], "loops": 1, "bpm": 60, "seconds": 10})
    calls = len(FakeClaude.calls)

    after = recognize.arrange_pages(sid, ["p2.png", "p1.png"], [])

    assert len(FakeClaude.calls) == calls  # 不花钱
    assert [p["file"] for p in after["pages"]] == ["p2.png", "p1.png"]
    ids_before = [b["id"] for b in before["bars"]]
    assert [b["id"] for b in after["bars"]] == ids_before[6:] + ids_before[:6]
    assert [b["n"] for b in after["bars"]] == list(range(1, 13))
    assert [s["page"] for s in after["systems"]] == [0, 0, 1, 1]
    assert all(b["page"] == after["systems"][b["system"]]["page"] for b in after["bars"])
    assert before["bars"][7]["id"] in after["practice"]["bars"]
    assert after["plan"]["stale"]  # 小节号变了


def test_deleting_a_page_drops_its_bars_and_file(data_dir):
    sid, before = _two_page_song()
    songs.record_practice(sid, {"bar_ids": [before["bars"][7]["id"]], "loops": 1, "bpm": 60, "seconds": 10})

    after = recognize.arrange_pages(sid, ["p1.png"], [])

    assert [b["id"] for b in after["bars"]] == [b["id"] for b in before["bars"][:6]]
    assert len(after["systems"]) == 2 and not after["practice"]["bars"]
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

    after = recognize.arrange_pages(sid, ["new:0", "p1.png"], [("c.png", score_png())])

    after = store.load(sid)
    assert [p["file"] for p in after["pages"]] == ["p2.png", "p1.png"]
    assert len(after["bars"]) == 12 and [b["n"] for b in after["bars"]] == list(range(1, 13))
    assert [b["id"] for b in after["bars"][6:]] == [b["id"] for b in before["bars"]]  # 原来的小节没动
    assert all(s["status"] == "done" for s in after["systems"])
    # 只识别了新页的两行（第 1 行故意写错时值会返修一次），没有重新读谱头
    assert {c["first_bar"] for c in FakeClaude.calls} == {1, 4}
    assert "job" not in after and after["progress"] is None and after["plan"]["stale"]
    assert after["usage"]["input_tokens"] > before["usage"]["input_tokens"]
    assert after["usage"]["cost_usd"] > before["usage"]["cost_usd"]


def test_new_page_on_song_without_bars_runs_full_recognition(data_dir, monkeypatch):
    sid = songs.create_song([("a.png", score_png())])
    store.patch(sid, status="error", error="没有识别出任何小节")
    started = []
    monkeypatch.setattr(recognize, "start_parse", lambda s: started.append(s) or True)
    recognize.arrange_pages(sid, ["p1.png", "new:0"], [("c.png", score_png())])
    assert started == [sid] and len(store.load(sid)["pages"]) == 2
    assert not jobs.jobs.running(sid)


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
        recognize.arrange_pages(sid, order, [("c.png", score_png())] * n_up)
    assert not jobs.jobs.running(sid)
    assert [p["file"] for p in store.load(sid)["pages"]] == ["p1.png"]
    assert sorted(f.name for f in (store.song_dir(sid) / "pages").iterdir()) == ["p1.png"]


def test_busy_and_score_songs_are_refused(data_dir):
    sid = songs.create_song([("a.png", score_png())])
    recognize.run_parse(sid)
    jobs.jobs.claim(sid)
    try:
        with pytest.raises(errors.Busy):
            recognize.arrange_pages(sid, ["p1.png"], [])
    finally:
        jobs.jobs.release(sid)
    gp = songs.create_song([("x.gp5", b"\x18FICHIER GUITAR PRO v5.10")])
    with pytest.raises(errors.Invalid, match="没有页面"):
        recognize.arrange_pages(gp, [], [])


def test_tags_and_cost_in_library(data_dir):
    sid = songs.create_song([("a.png", score_png())])
    assert songs.set_tags(sid, [" 指弹 ", "练习中", "指弹", ""]) == ["指弹", "练习中"]
    with pytest.raises(errors.Invalid):
        songs.set_tags(sid, ["x" * 21])
    store.patch(sid, usage={"input_tokens": 1_000_000, "output_tokens": 100_000})  # 早期只记 token
    row = next(r for r in store.list_songs() if r["id"] == sid)
    assert row["tags"] == ["指弹", "练习中"]
    assert row["cost_usd"] == pytest.approx(4 + 2) and row["cost_estimated"]
    store.patch(sid, usage={"input_tokens": 5, "output_tokens": 5, "cost_usd": 1.5, "unpriced_tokens": 0})
    row = next(r for r in store.list_songs() if r["id"] == sid)
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
    assert next(r for r in store.list_songs() if r["id"] == sid)["title"] == "新名字"
