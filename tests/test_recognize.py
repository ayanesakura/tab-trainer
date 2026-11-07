"""图片谱子的识别流程（替身模型）：整首、单行重新识别、取消、并发上限。"""

import itertools
import threading

import pytest

from app import errors, jobs, music, practice, recognize, songs, store
from tests.fakes import FakeClaude, score_png


def test_full_parse(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    song = store.load(sid)

    assert song["status"] == "ready", song["notice"]
    assert song["activity"] is None and song["notice"] is None
    assert song["title"] == "测试曲" == song["meta"]["title"]  # 没起名字：用谱头读出来的
    assert song["meta"]["tuning"] == ["D4", "A3", "F#3", "C#3", "A2", "D2"]
    assert len(song["systems"]) == 2
    assert [b["n"] for b in song["bars"]] == [1, 2, 3, 4, 5, 6]
    assert all(not b["warn"] for b in song["bars"])  # 第 1 行返修后时值正确
    assert any(c["retry"] for c in FakeClaude.calls)
    assert song["bars"][3]["system"] == 1 and song["bars"][3]["tip"] == "tip 4"
    assert song["plan"]["overview"] == "ok"


def test_first_recognition_row_writes_keep_earlier_bar_revs(data_dir, monkeypatch):
    """第一次识别边识别边写：后面的行写进来不会让前面已经能改的小节的 rev 过期。"""
    sid = songs.create_song([("p1.png", score_png())])
    monkeypatch.setattr(recognize, "CONCURRENCY", 1)
    seen = []
    real = FakeClaude.transcribe

    def transcribe(self, img, meta, first_bar, *a, **kw):
        if first_bar > 1 and not seen:  # 第二行开始识别时，前端拿到的第一行
            seen.append(store.load(sid)["bars"][0])
        return real(self, img, meta, first_bar, *a, **kw)

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    recognize.run_parse(sid)
    bar = seen[0]
    after = songs.edit_bar(sid, bar["id"], [{"dur": "4", "notes": []}], None, rev=bar["rev"])
    assert after["bars"][0]["rev"] == bar["rev"] + 1


def test_reparse_system_passes_hint(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    recognize.reparse_system(sid, 1, hint="第 4 小节第 2 拍是 2 弦")
    assert FakeClaude.calls[-1] == {"first_bar": 4, "retry": False, "hint": "第 4 小节第 2 拍是 2 弦"}
    assert [b["n"] for b in store.load(sid)["bars"]] == [1, 2, 3, 4, 5, 6]


def test_bar_ids_are_stable_and_unique(data_dir, monkeypatch):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    before = store.load(sid)["bars"]
    assert len({b["id"] for b in before}) == len(before)
    store.update(sid, lambda s: s["bars"][4].update(qa=[{"q": "问", "a": "答"}]))
    recognize.reparse_system(sid, 1)  # 结果和原来一样：按内容对上，id 和问答都沿用
    after = store.load(sid)["bars"]
    assert [b["id"] for b in after] == [b["id"] for b in before]
    assert after[4]["qa"] == [{"q": "问", "a": "答"}]
    # 这一行这次少识别出一个小节：对得上的沿用 id，多出来的旧 id 失效
    real = FakeClaude.transcribe

    def fewer(self, img, meta, first_bar, spans, *a, **kw):
        out = real(self, img, meta, first_bar, spans, *a, **kw)
        out["bars"] = out["bars"][:-1]
        return out

    monkeypatch.setattr(FakeClaude, "transcribe", fewer)
    recognize.reparse_system(sid, 1)
    after = store.load(sid)["bars"]
    assert [b["id"] for b in after] == [b["id"] for b in before[:5]]
    with pytest.raises(errors.BarNotFound):
        songs.edit_bar(sid, before[5]["id"], [{"dur": "4", "notes": []}], tip=None)  # 旧 id 已失效


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
        self,
        img,
        meta,
        first_bar,
        spans,
        hint="",
        previous=None,
        problems=None,
        labeled_strings=False,
        start_ts=None,
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
    assert all(any(w["code"] == "off_grid" for w in b["warn"]) for b in store.load(sid)["bars"])  # 只是提醒


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
    assert song["bars"][2]["edited"]
    orig = song["bars"][2]["_orig"]["events"]
    song = songs.edit_bar(sid, edited[0], orig, None)
    assert not song["plan"]["stale"] and not song["bars"][2].get("edited") and "_orig" not in song["bars"][2]


def test_reparse_line_marks_plan_stale_and_practice_follows_bar_ids(data_dir, monkeypatch):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    bars = store.load(sid)["bars"]
    practice.record(sid, [bars[0]["id"]], 1, 50, 10)
    practice.record(sid, [bars[5]["id"]], 1, 60, 10)
    real = FakeClaude.transcribe

    def fewer(self, img, meta, first_bar, spans, *a, **kw):  # 第 2 行（小节 4–6）这次只识别出两个小节
        out = real(self, img, meta, first_bar, spans, *a, **kw)
        out["bars"] = out["bars"][:-1]
        return out

    monkeypatch.setattr(FakeClaude, "transcribe", fewer)
    recognize.reparse_system(sid, 1)
    song = store.load(sid)
    assert song["plan"]["stale"]
    p = practice.aggregate(sid, song)
    assert set(p["bars"]) == {bars[0]["id"]}  # 第 6 小节没了，它的记录不再算
    assert p["last"]["a"] == bars[5]["id"] and p["last"]["a_n"] is None  # 最近一次练的小节不在了
    assert p["total_seconds"] == 20


def _parsed_song_with_user_data():
    """识别完的曲子，带上用户的手改、改过的曲名和调弦、练习记录。"""
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    bars = store.load(sid)["bars"]
    songs.edit_bar(sid, bars[1]["id"], [{"dur": "4", "notes": [{"string": 2, "fret": 7}]}], None)
    songs.edit_meta(sid, {"title": "我起的名字", "capo": 3, "tuning": ["E4", "B3", "G3", "D3", "A2", "D2"]})
    practice.record(sid, [bars[0]["id"]], 2, 60, 30)
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
    assert after["status"] == "ready" and "原来的结果保留不变" in after["notice"]["msg"]
    for k in ("bars", "meta", "systems", "plan", "title"):
        assert after[k] == before[k], k
    assert practice.aggregate(sid, after) == practice.aggregate(sid, before)
    assert after["activity"] is None
    songs.edit_bar(sid, after["bars"][0]["id"], [{"dur": "4", "notes": []}], None)  # 之后照常能改


def test_successful_reparse_keeps_user_data_on_matching_bars(data_dir, sync_jobs):
    sid, before = _parsed_song_with_user_data()
    store.update(sid, lambda s: s["bars"][0].update(qa=[{"q": "怎么弹", "a": "慢慢弹"}]))
    assert recognize.start_parse(sid)
    after = store.load(sid)
    assert after["status"] == "ready" and not after["notice"] and after["activity"] is None
    # 新结果和原来的识别一样：小节 id、问答、练习记录都保留；手改的小节保留用户改的音
    assert [b["id"] for b in after["bars"]] == [b["id"] for b in before["bars"]]
    assert after["bars"][0]["qa"] == [{"q": "怎么弹", "a": "慢慢弹"}]
    assert after["bars"][1]["events"] == before["bars"][1]["events"] and "_orig" in after["bars"][1]
    assert after["bars"][1]["edited"] and not after["bars"][0].get("edited")
    assert practice.aggregate(sid, after)["bars"] == practice.aggregate(sid, before)["bars"]
    # 曲名、用户改过的变调夹以用户的为准（模型读出来是 2）
    assert after["meta"]["capo"] == 3 and after["meta"]["title"] == after["title"] == "我起的名字"
    assert after["meta"]["tuning"] == ["E4", "B3", "G3", "D3", "A2", "D2"]
    assert after["meta"]["tuning_midi"] == before["meta"]["tuning_midi"]
    assert after["plan"]["overview"] == "ok"


def test_reparse_with_different_result_drops_mismatched_data(data_dir, sync_jobs, monkeypatch):
    sid, before = _parsed_song_with_user_data()
    real = FakeClaude.transcribe

    def transcribe(self, img, meta, first_bar, spans, *a, **kw):  # 第二行这次少识别出一个小节
        out = real(self, img, meta, first_bar, spans, *a, **kw)
        if first_bar > 1:
            out["bars"] = out["bars"][:-1]
        return out

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    assert recognize.start_parse(sid)
    after = store.load(sid)
    assert len(after["bars"]) == len(before["bars"]) - 1
    assert [b["id"] for b in after["bars"]] == [b["id"] for b in before["bars"][:-1]]
    assert after["bars"][0]["id"] in practice.aggregate(sid, after)["bars"]


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
    restaging = {"kind": "parse", "restaging": True, "systems": []}
    store.update(sid, lambda s: (s["systems"][1].update(status="done"), s.update(activity=restaging)))
    with pytest.raises(errors.Busy, match="重新识别整首"):
        songs.edit_bar(sid, bar0, [{"dur": "1", "notes": []}], None)
    with pytest.raises(errors.Busy):
        songs.edit_meta(sid, {"time_signature": [3, 4]})


def test_restart_during_reparse_keeps_the_old_result(data_dir):
    sid, before = _parsed_song_with_user_data()
    restaging = {"kind": "parse", "restaging": True, "systems": []}
    store.update(sid, lambda s: s.update(activity=restaging, progress={"stage": "逐行识别"}))
    songs.recover_interrupted()
    after = store.load(sid)
    assert after["status"] == "ready" and "原来的结果保留不变" in after["notice"]["msg"]
    assert after["bars"] == before["bars"] and after["activity"] is None


def _ts_transcribe(declare_on_row2=False, row2_retry_declares=False):
    """替身模型：第 1 行第 2 小节写着变拍号 3/4，之后的小节都是 3 拍。"""
    calls = []

    def transcribe(
        self,
        img,
        meta,
        first_bar,
        spans,
        hint="",
        previous=None,
        problems=None,
        labeled_strings=False,
        start_ts=None,
    ):
        calls.append({"first_bar": first_bar, "retry": previous is not None, "problems": problems})
        bars = []
        for i in range(len(spans)):
            n = first_bar + i
            beats = 4 if n == 1 else 3
            ts = []
            if n == 2 or (
                i == 0
                and first_bar > 1
                and (declare_on_row2 or (row2_retry_declares and previous is not None))
            ):
                ts = [3, 4]
            bars.append(
                {
                    "number": n,
                    "time_signature": ts,
                    "events": [
                        {"dur": "1", "notes": [{"string": 1, "fret": k, "tech": [], "grace": -1, "bend": 0}]}
                        for k in range(beats)
                    ],
                    "tip": "",
                    "uncertain": "",
                }
            )
        return {"bars": bars}

    return transcribe, calls


def test_time_signature_change_inside_a_row(data_dir, monkeypatch):
    transcribe, calls = _ts_transcribe(declare_on_row2=True)
    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    bars = store.load(sid)["bars"]
    assert [b.get("time_signature") for b in bars] == [None] + [[3, 4]] * 5
    assert all(not b["warn"] for b in bars), [b["warn"] for b in bars]
    assert not any(c["retry"] for c in calls)  # 写了变拍号的 3 拍小节不算时值错误


@pytest.mark.parametrize("declares", [False, True])
def test_time_signature_change_in_an_earlier_row(data_dir, monkeypatch, declares):
    """第 2 行看不到第 1 行写的变拍号：整行都是 3 拍时返修，但告诉模型可能是前面变了拍号、不要硬凑；
    整首识别完按变拍号统一检查，不管模型返修时有没有在第 2 行写上拍号，都不该留下时值警告。"""
    transcribe, calls = _ts_transcribe(row2_retry_declares=declares)
    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    bars = store.load(sid)["bars"]
    retry = [c for c in calls if c["retry"]]
    assert [c["first_bar"] for c in retry] == [4]
    assert any("前面的行里变了拍号" in p for p in retry[0]["problems"])
    assert [b.get("time_signature") for b in bars] == [None] + [[3, 4]] * 5
    assert all(not b["warn"] for b in bars), [b["warn"] for b in bars]


def test_time_signatures_follow_edits_and_reparse(data_dir, monkeypatch):
    transcribe, _ = _ts_transcribe()
    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    # 改全曲拍号成 3/4：所有小节都是 3/4，第 1 小节（4 拍）有时值警告
    song = songs.edit_meta(sid, {"time_signature": [3, 4]})
    assert all("time_signature" not in b for b in song["bars"])
    assert song["bars"][0]["warn"] and not any(b["warn"] for b in song["bars"][1:])
    songs.edit_meta(sid, {"time_signature": [4, 4]})

    # 重新识别第 1 行时模型没看到变拍号：第 2 行的 3 拍小节恢复成按 4/4 检查，出现时值警告
    def no_change(self, img, meta, first_bar, spans, *a, **kw):
        out = transcribe(self, img, meta, first_bar, spans, *a, **kw)
        for b in out["bars"]:
            b["time_signature"] = []
            if b["number"] <= 3:
                b["events"] = [{"dur": "4", "notes": []}]
        return out

    monkeypatch.setattr(FakeClaude, "transcribe", no_change)
    recognize.reparse_system(sid, 0)
    bars = store.load(sid)["bars"]
    assert all("time_signature" not in b for b in bars)
    assert [bool(b["warn"]) for b in bars] == [False] * 3 + [True] * 3


def test_placeholder_rest_follows_the_time_signature(data_dir, monkeypatch):
    transcribe, _ = _ts_transcribe(declare_on_row2=True)

    def with_empty(self, img, meta, first_bar, spans, *a, **kw):
        out = transcribe(self, img, meta, first_bar, spans, *a, **kw)
        if first_bar > 1:
            out["bars"][1]["events"] = []  # 第 5 小节没识别出来
        return out

    monkeypatch.setattr(FakeClaude, "transcribe", with_empty)
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    bar5 = store.load(sid)["bars"][4]
    assert bar5["events"] == [{"dur": "3", "notes": []}] and bar5["placeholder"]
    assert bar5["warn"] == [{"code": "placeholder", "msg": "没有识别出可用的音符，暂按整小节休止处理"}]


def _cramming_transcribe():
    """替身模型：第 1 行第 2 小节写着 3/4，之后的小节其实都是 3 拍；但不知道变过拍号的行，
    会把 3 拍的内容硬凑成 4 拍（每小节一样长，单看这一行查不出来）。告诉它这一行开头的拍号才读对。"""
    calls = []

    def transcribe(
        self,
        img,
        meta,
        first_bar,
        spans,
        hint="",
        previous=None,
        problems=None,
        labeled_strings=False,
        start_ts=None,
    ):
        calls.append({"first_bar": first_bar, "start_ts": start_ts, "retry": previous is not None})
        bars = []
        for i in range(len(spans)):
            n = first_bar + i
            in_row_change = first_bar == 1 and n >= 2
            knows = in_row_change or start_ts == [3, 4]
            beats = 4 if n == 1 or not knows else 3
            dur = "1" if n == 1 or knows else "4/3"  # 不知道时：3 个音被拉长凑成 4 拍
            bars.append(
                {
                    "number": n,
                    "time_signature": [3, 4] if n == 2 else [],
                    "events": [
                        {"dur": dur, "notes": [{"string": 1, "fret": k, "tech": [], "grace": -1, "bend": 0}]}
                        for k in range(3 if n > 1 else beats)
                    ],
                    "tip": "",
                    "uncertain": "",
                }
            )
        return {"bars": bars}

    return transcribe, calls


def test_rows_after_an_earlier_time_signature_change_are_reread(data_dir, monkeypatch):
    transcribe, calls = _cramming_transcribe()
    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    song = store.load(sid)
    assert song["status"] == "ready", song["notice"]
    # 第 2 行先按全曲拍号识别（凑成了 4 拍），再带上 3/4 重新识别
    row2 = [c for c in calls if c["first_bar"] == 4]
    assert [c["start_ts"] for c in row2] == [None, [3, 4]]
    bars = song["bars"]
    assert [sum(music.parse_dur(e["dur"]) for e in b["events"]) for b in bars] == [4, 3, 3, 3, 3, 3]
    assert [b.get("time_signature") for b in bars] == [None] + [[3, 4]] * 5
    assert all(not b["warn"] for b in bars) and all(s["status"] == "done" for s in song["systems"])


def test_reparse_row_gets_the_carried_time_signature(data_dir, monkeypatch):
    transcribe, calls = _cramming_transcribe()
    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    calls.clear()
    recognize.reparse_system(sid, 1)
    assert calls[0]["start_ts"] == [3, 4]
    assert all(not b["warn"] for b in store.load(sid)["bars"])


def test_songs_without_time_signature_changes_cost_nothing_extra(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    first_calls = [c for c in FakeClaude.calls if "first_bar" in c and not c["retry"]]
    assert sorted(c["first_bar"] for c in first_calls) == [1, 4]  # 每行只识别一次


def test_row_reparse_compares_bars_before_time_signatures_are_derived():
    """reviewer 的 row_ts：单行重新识别时新小节还没算 time_signature（替换之后才 apply_time_signatures），
    不能拿它和原来派生出来的 time_signature 比，否则变过拍号的曲子里没变的小节也算“变了”。"""
    import copy

    ev = [{"dur": "3", "notes": [{"string": 1, "fret": 0, "tech": [], "grace": -1, "bend": 0}]}]
    s = {
        "meta": {"time_signature": [4, 4]},
        "plan": {"overview": "x", "steps": []},
        "bars": [
            {"id": "a", "system": 0, "events": [{"dur": "4", "notes": []}], "tip": "t0"},
            {
                "id": "b",
                "system": 1,
                "events": ev,
                "time_signature": [3, 4],
                "tip": "AI tip",
                "qa": [{"q": "q", "a": "a"}],
            },
        ],
    }
    recognize._replace_system_bars(
        s, 1, [{"id": "new1", "system": 1, "events": copy.deepcopy(ev), "tip": "model tip"}]
    )
    b = s["bars"][1]
    assert b["id"] == "b" and b["tip"] == "AI tip" and not b["qa"][0].get("stale")
    assert not s["plan"].get("changed_bars") and not s["plan"].get("stale_all")


def test_reparse_unchanged_row_after_time_signature_change_keeps_ai_content(data_dir, monkeypatch):
    transcribe, _ = _ts_transcribe()
    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)

    def ai(s):
        for b in s["bars"]:
            b["tip"] = f"讲解 {b['n']}"
            b["qa"] = [{"q": "怎么弹", "a": "慢慢弹"}]

    before = store.update(sid, ai)
    assert [b.get("time_signature") for b in before["bars"]] == [None] + [[3, 4]] * 5
    assert not before["plan"]["stale"]
    recognize.reparse_system(sid, 1)
    after = store.load(sid)
    assert [b["id"] for b in after["bars"]] == [b["id"] for b in before["bars"]]
    assert [b["tip"] for b in after["bars"]] == [b["tip"] for b in before["bars"]]
    assert not any(q.get("stale") for b in after["bars"] for q in b["qa"])
    assert not after["plan"]["stale"], after["plan"]
    assert [b["rev"] for b in after["bars"]] == [b["rev"] for b in before["bars"]]
