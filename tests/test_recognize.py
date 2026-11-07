"""图片谱子的识别流程（替身模型）：整首、单行重新识别、取消、并发上限。"""

import itertools
import threading

import pytest

from app import errors, jobs, recognize, songs, store
from tests.fakes import FakeClaude, score_png


def test_full_parse(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    song = store.load(sid)

    assert song["status"] == "ready", song["error"]
    assert song["meta"]["tuning"] == ["D4", "A3", "F#3", "C#3", "A2", "D2"]
    assert len(song["systems"]) == 2
    assert [b["n"] for b in song["bars"]] == [1, 2, 3, 4, 5, 6]
    assert all(not b["warn"] for b in song["bars"])  # 第 1 行返修后时值正确
    assert any(c["retry"] for c in FakeClaude.calls)
    assert song["bars"][3]["system"] == 1 and song["bars"][3]["tip"] == "tip 4"
    assert song["plan"]["overview"] == "ok"


def test_reparse_system_passes_hint(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    recognize.reparse_system(sid, 1, hint="第 4 小节第 2 拍是 2 弦")
    assert FakeClaude.calls[-1] == {"first_bar": 4, "retry": False, "hint": "第 4 小节第 2 拍是 2 弦"}
    assert [b["n"] for b in store.load(sid)["bars"]] == [1, 2, 3, 4, 5, 6]


def test_bar_ids_are_stable_and_unique(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    before = store.load(sid)["bars"]
    assert len({b["id"] for b in before}) == len(before)
    recognize.reparse_system(sid, 1)
    after = store.load(sid)["bars"]
    assert [b["id"] for b in after[:3]] == [b["id"] for b in before[:3]]  # 没重识别的行，id 不变
    with pytest.raises(errors.BarNotFound):
        songs.edit_bar(sid, before[4]["id"], [{"dur": "4", "notes": []}], tip=None)  # 旧 id 已失效


def test_empty_bar_becomes_rest(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    bar_id = store.load(sid)["bars"][0]["id"]
    song = songs.edit_bar(sid, bar_id, [], tip=None)
    assert song["bars"][0]["events"] == [{"dur": "4", "notes": []}]
    assert song["bars"][0]["warn"] == []


def test_reparse_claim_rejects_concurrent_job(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    assert jobs.jobs.claim(sid) is not None  # 模拟整首识别正在进行
    with pytest.raises(errors.Busy):
        recognize.start_reparse(sid, 0)
    assert store.load(sid)["systems"][0]["status"] == "done"  # 没有被误标成识别中
    jobs.jobs.release(sid)


def test_cancel_stops_remaining_systems(data_dir, monkeypatch):
    sid = songs.create_song([("p1.png", score_png())])
    cancel = threading.Event()
    real = FakeClaude.transcribe

    def transcribe_then_cancel(self, *a, **kw):
        cancel.set()  # 第一行识别完成后用户点了取消
        return real(self, *a, **kw)

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe_then_cancel)
    monkeypatch.setattr(recognize, "CONCURRENCY", 1)
    with pytest.raises(jobs.Cancelled):
        recognize.run_parse(sid, cancel)
    song = store.load(sid)
    assert len([c for c in FakeClaude.calls if not c["retry"]]) == 1  # 第二行没有再发请求
    assert any(s["error"] == "已取消" for s in song["systems"])


def test_systems_share_a_global_limit_across_songs(data_dir, monkeypatch):
    """两首曲子同时识别：同时在识别的行数不超过全局上限。"""
    import time

    monkeypatch.setattr(recognize, "_system_slots", threading.BoundedSemaphore(1))
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
    sids = [songs.create_song([(f"p{k}.png", score_png())]) for k in range(2)]
    threads = [threading.Thread(target=recognize.run_parse, args=(sid,)) for sid in sids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1
    assert all(store.load(sid)["status"] == "ready" for sid in sids)


def test_cancel_while_waiting_for_a_slot(data_dir, monkeypatch):

    sem = threading.BoundedSemaphore(1)
    sem.acquire()  # 名额被别的曲子占着
    monkeypatch.setattr(recognize, "_system_slots", sem)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(jobs.Cancelled):
        recognize.transcribe_queued(None, "sid", {}, 0, 1, cancel=cancel)


def test_reparse_failed_line_uses_real_bar_numbers(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    store.update(
        sid, lambda s: s.update(bars=[b for b in s["bars"] if b["system"] != 1])
    )  # 第 2 行之前失败了
    recognize.reparse_system(sid, 1)
    assert FakeClaude.calls[-1]["first_bar"] == 4


def test_tab_lines_are_found_and_strings_labelled(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    song = store.load(sid)
    assert all(labeled for _, labeled in FakeClaude.tab_lines)
    # 合成谱子的六线谱：第一行从 y=230 开始、第二行从 y=630 开始，线距 14
    for sysinfo in song["systems"]:
        assert sysinfo["lines"][0] in (230, 630)
        lines = recognize.tab_lines(sid, song, sysinfo)
        assert lines == [y - sysinfo["crop"][2] for y in sysinfo["lines"]]
        assert [round(b - a) for a, b in itertools.pairwise(lines)] == [14] * 5
    # 旧曲子没存 lines：对整页重新检测
    s0 = dict(song["systems"][0])
    lines0 = s0.pop("lines")
    assert recognize.tab_lines(sid, song, s0) == [y - s0["crop"][2] for y in lines0]
    # 标出来的弦号是红色的，画在第一个小节左边
    x0, x1, y0, y1 = s0["crop"]
    img = store.page_image(sid, song["pages"][0]).crop((x0, y0, x1, y1))
    out = recognize.label_strings(img, recognize.tab_lines(sid, song, s0), s0["spans"][0][0] - x0)
    px = out.load()
    reds = [
        (x, y) for x in range(out.width) for y in range(out.height) if px[x, y][0] > 180 and px[x, y][1] < 90
    ]
    assert reds and max(x for x, _ in reds) < s0["spans"][0][0] - x0


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
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    assert not any(c.get("retry") for c in FakeClaude.calls)
    assert all(any("网格" in w for w in b["warn"]) for b in store.load(sid)["bars"])  # 只是提醒


def test_full_parse_plan_built_from_old_notes_stays_stale(data_dir, monkeypatch):
    """整首识别最后生成计划期间用户改了音：计划还没写回（plan 为空）时改动标不上过时，写回时要补上。"""
    sid = songs.create_song([("p1.png", score_png())])
    real = FakeClaude.practice_plan
    edited = []

    def plan_while_user_edits(self, meta, bars):
        out = real(self, meta, bars)
        edited.append(bars[2]["id"])
        songs.edit_bar(sid, bars[2]["id"], [{"dur": "4", "notes": [{"string": 3, "fret": 5}]}], None)
        return out

    monkeypatch.setattr(FakeClaude, "practice_plan", plan_while_user_edits)
    recognize.run_parse(sid)
    song = store.load(sid)
    assert song["status"] == "ready"
    assert song["plan"]["stale"] and song["plan"]["changed_bars"] == edited and not song["plan"]["stale_all"]
    # 撤销回原样：计划又是最新的
    orig = song["bars"][2]["_orig"]["events"]
    song = songs.edit_bar(sid, edited[0], orig, None)
    assert not song["plan"]["stale"]


def test_reparse_line_marks_plan_stale_and_drops_practice_of_replaced_bars(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    bars = store.load(sid)["bars"]
    songs.record_practice(sid, {"bar_ids": [bars[0]["id"]], "loops": 1, "bpm": 50, "seconds": 10})
    songs.record_practice(sid, {"bar_ids": [bars[4]["id"]], "loops": 1, "bpm": 60, "seconds": 10})
    recognize.reparse_system(sid, 1)  # 第 2 行（小节 4–6）换成了新的小节
    song = store.load(sid)
    assert song["plan"]["stale"]
    assert set(song["practice"]["bars"]) == {bars[0]["id"]}
    assert "last" not in song["practice"]  # 最近一次练的是被替换的小节
    assert song["practice"]["total_seconds"] == 20


def _parsed_song_with_user_data():
    """识别完的曲子，带上用户的手改、改过的曲名和调弦、练习记录。"""
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    bars = store.load(sid)["bars"]
    songs.edit_bar(sid, bars[1]["id"], [{"dur": "4", "notes": [{"string": 2, "fret": 7}]}], None)
    songs.edit_meta(sid, {"title": "我起的名字", "capo": 3})
    songs.record_practice(sid, {"bar_ids": [bars[0]["id"]], "loops": 2, "bpm": 60, "seconds": 30})
    return sid, store.load(sid)


@pytest.mark.parametrize("how", ["row fails", "cancel", "crash"])
def test_failed_or_cancelled_reparse_keeps_the_old_result(data_dir, sync_jobs, monkeypatch, how):
    sid, before = _parsed_song_with_user_data()
    real = FakeClaude.transcribe

    def transcribe(self, img, meta, first_bar, *a, **kw):
        if first_bar > 1:  # 第二行
            if how == "row fails":
                raise RuntimeError("网络断了")
            if how == "cancel":
                jobs.jobs.cancel(sid)
        return real(self, img, meta, first_bar, *a, **kw)

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    if how == "crash":
        monkeypatch.setattr(FakeClaude, "read_meta", lambda self, page: 1 / 0)
    monkeypatch.setattr(recognize, "CONCURRENCY", 1)
    assert recognize.start_parse(sid)
    after = store.load(sid)
    assert after["status"] == "ready" and "原来的结果保留不变" in after["error"]
    for k in ("bars", "meta", "systems", "practice", "plan"):
        assert after[k] == before[k], k
    assert not after.get("restaging")
    songs.edit_bar(sid, after["bars"][0]["id"], [{"dur": "4", "notes": []}], None)  # 之后照常能改


def test_successful_reparse_replaces_everything_but_the_title(data_dir, sync_jobs):
    sid, before = _parsed_song_with_user_data()
    assert recognize.start_parse(sid)
    after = store.load(sid)
    assert after["status"] == "ready" and not after["error"] and not after.get("restaging")
    assert not {b["id"] for b in after["bars"]} & {b["id"] for b in before["bars"]}
    assert after["meta"]["capo"] == 2 and after["meta"]["title"] == "我起的名字"
    assert after["plan"]["overview"] == "ok" and not after["practice"]["bars"]


def test_edits_that_recognition_would_overwrite_are_refused(data_dir, monkeypatch):
    sid, song = _parsed_song_with_user_data()
    bar0, bar4 = song["bars"][0]["id"], song["bars"][4]["id"]
    # 单行重新识别第 2 行时：这一行的小节、调弦拍号不能改；别的行、曲名照常
    store.update(sid, lambda s: s["systems"][1].update(status="parsing"))
    with pytest.raises(errors.Busy, match="这一行正在重新识别"):
        songs.edit_bar(sid, bar4, [{"dur": "4", "notes": []}], None)
    with pytest.raises(errors.Busy, match="调弦"):
        songs.edit_meta(sid, {"capo": 5})
    songs.edit_bar(sid, bar0, [{"dur": "4", "notes": []}], None)
    songs.edit_meta(sid, {"title": "又改名"})
    # 整首重新识别时：所有小节都不能改
    store.update(
        sid, lambda s: (s["systems"][1].update(status="done"), s.update(status="parsing", restaging=True))
    )
    with pytest.raises(errors.Busy, match="重新识别整首"):
        songs.edit_bar(sid, bar0, [{"dur": "1", "notes": []}], None)
    with pytest.raises(errors.Busy):
        songs.edit_meta(sid, {"time_signature": [3, 4]})


def test_restart_during_reparse_keeps_the_old_result(data_dir):
    sid, before = _parsed_song_with_user_data()
    store.update(sid, lambda s: s.update(status="parsing", restaging=True, progress={"stage": "逐行识别"}))
    songs.recover_interrupted()
    after = store.load(sid)
    assert after["status"] == "ready" and "原来的结果保留不变" in after["error"]
    assert after["bars"] == before["bars"] and "restaging" not in after
