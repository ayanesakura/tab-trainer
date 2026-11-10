"""图片谱子的识别流程（替身模型）：整首、单行重新识别、取消、并发上限。"""

import itertools
import threading
from collections import Counter

import pytest

from app import config, errors, jobs, music, practice, recognize, songs, state, store
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
    monkeypatch.setattr(config.settings, "row_concurrency", 1)
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
    monkeypatch.setattr(config.settings, "row_concurrency", 1)
    with pytest.raises(jobs.Cancelled):
        recognize.run_parse(sid, cancel)
    song = store.load(sid)
    assert len([c for c in FakeClaude.calls if not c["retry"]]) == 1  # 第二行没有再发请求
    assert any(s["error"] == "已取消" for s in song["systems"])


def test_systems_share_a_global_limit_across_songs(data_dir, monkeypatch):
    """两首曲子同时识别：同时在识别的行数不超过全局上限。"""
    import time

    monkeypatch.setattr(config.settings, "max_rows_global", 1)  # 名额按当前设置算，改了马上生效
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
    monkeypatch.setattr(config.settings, "max_rows_global", 1)
    jobs.slots.acquire()  # 名额被别的曲子占着
    try:
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(jobs.Cancelled):
            recognize.transcribe_queued(None, "sid", {}, 0, 1, cancel=cancel)
        assert jobs.slots.used == 1  # 没拿走名额
    finally:
        jobs.slots.release()


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
    monkeypatch.setattr(config.settings, "row_concurrency", 1)
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


def test_edit_just_before_the_carried_ts_rerun_is_not_overwritten(data_dir, monkeypatch):
    """挑要按变拍号重做的行和锁住它们在同一次 store.update 里：用户在两者之间改了那一行，也不会被重做覆盖。"""
    transcribe, calls = _cramming_transcribe()
    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    sid = songs.create_song([("p1.png", score_png())])
    real_update = store.update
    edited = []

    def update(song_id, fn):
        if getattr(fn, "__name__", "") == "begin_redo" and not edited:  # 恰好在锁住之前，用户改了第 2 行
            bar = next(b for b in store.load(song_id)["bars"] if b["system"] == 1)
            edited.append(bar["id"])
            songs.edit_bar(song_id, bar["id"], [{"dur": "3", "notes": [{"string": 2, "fret": 9}]}], None)
        return real_update(song_id, fn)

    monkeypatch.setattr(store, "update", update)
    recognize.run_parse(sid)
    song = store.load(sid)
    bar = next(b for b in song["bars"] if b["id"] == edited[0])
    assert bar["edited"] and bar["events"][0]["notes"][0]["fret"] == 9
    assert [c["start_ts"] for c in calls if c["first_bar"] == 4] == [None]  # 第 2 行没有重做
    assert all(s["status"] == "done" for s in song["systems"])


@pytest.mark.parametrize("how", ["error", "cancel"])
def test_failed_carried_ts_rerun_keeps_the_first_pass_bars(data_dir, monkeypatch, how):
    """按变拍号重做的行失败 / 被取消：只标这一行的状态，第一遍识别出的小节留着（不能整行清空）。"""
    transcribe, _ = _cramming_transcribe()
    cancel = threading.Event()

    def flaky(self, img, meta, first_bar, spans, **kw):
        if kw.get("start_ts") == [3, 4]:
            if how == "cancel":
                cancel.set()
                raise jobs.Cancelled()
            raise RuntimeError("overloaded mid-stream")
        return transcribe(self, img, meta, first_bar, spans, **kw)

    monkeypatch.setattr(FakeClaude, "transcribe", flaky)
    sid = songs.create_song([("p1.png", score_png())])
    if how == "cancel":
        with pytest.raises(jobs.Cancelled):
            recognize.run_parse(sid, cancel)
    else:
        recognize.run_parse(sid)
    song = store.load(sid)
    assert [b["system"] for b in song["bars"]] == [0, 0, 0, 1, 1, 1]
    assert [b["n"] for b in song["bars"]] == [1, 2, 3, 4, 5, 6]
    assert song["systems"][1]["status"] == "error"


def test_failed_fix_up_attempt_keeps_the_first_attempt(data_dir, monkeypatch):
    """返修那次调用出错（过载、断线）：用第一次的结果（带着警告），不让整行失败。"""
    real = FakeClaude.transcribe

    def flaky(self, img, meta, first_bar, spans, previous=None, **kw):
        if previous is not None:
            raise RuntimeError("overloaded")
        return real(self, img, meta, first_bar, spans, previous=previous, **kw)

    monkeypatch.setattr(FakeClaude, "transcribe", flaky)
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    song = store.load(sid)
    assert song["status"] == "ready" and song["notice"] is None
    assert all(s["status"] == "done" for s in song["systems"])
    assert [b["n"] for b in song["bars"]] == [1, 2, 3, 4, 5, 6]
    assert all(w["code"] == "length" for b in song["bars"][:3] for w in b["warn"]) and song["bars"][0]["warn"]


def test_cancel_before_the_fix_up_attempt_still_cancels(data_dir, monkeypatch):
    real = FakeClaude.transcribe
    cancel = threading.Event()

    def cancelling(self, img, meta, first_bar, spans, previous=None, **kw):
        if first_bar == 1:
            cancel.set()  # 第一次识别期间点了取消：不再返修
        return real(self, img, meta, first_bar, spans, previous=previous, **kw)

    monkeypatch.setattr(FakeClaude, "transcribe", cancelling)
    sid = songs.create_song([("p1.png", score_png())])
    with pytest.raises(jobs.Cancelled):
        recognize.run_parse(sid, cancel)
    assert not any(c["retry"] for c in FakeClaude.calls if c.get("first_bar") == 1)
    assert store.load(sid)["systems"][0]["error"] == "已取消"


def test_full_rerecognition_keeps_the_artist_set_by_hand(data_dir, monkeypatch):
    """艺人和调弦 / 变调夹 / 拍号一样：手改过的以用户的为准，重新识别读出的谱头不覆盖（包括识别期间改的）。"""
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    songs.edit_meta(sid, {"artist": "Yiruma", "capo": 3})
    recognize.run_parse(sid)
    meta = store.load(sid)["meta"]
    assert (meta["artist"], meta["capo"]) == ("Yiruma", 3)

    real = FakeClaude.transcribe

    def edit_during_run(self, *a, **kw):
        if not kw.get("previous"):
            songs.edit_meta(sid, {"artist": "久石让"})
        return real(self, *a, **kw)

    monkeypatch.setattr(FakeClaude, "transcribe", edit_during_run)
    recognize.run_parse(sid)
    meta = store.load(sid)["meta"]
    assert (meta["artist"], meta["capo"]) == ("久石让", 3)


@pytest.mark.parametrize(
    "head, kept",
    [
        ({"capo": 0}, True),  # 品位和变调夹 / 调弦无关：手改的音照样对
        ({"tuning": ["E", "B", "G", "D", "A", "E"]}, True),
        ({"time_signature": [2, 2]}, True),  # 小节长度没变
        ({"time_signature": [3, 4]}, False),  # 小节长度变了：手改的音凑不满 / 超出，按新结果
    ],
)
def test_reparse_reading_other_settings_keeps_hand_edits(data_dir, sync_jobs, monkeypatch, head, kept):
    """重新识别时模型把变调夹 / 调弦 / 拍号读得和上次不一样：手改的小节照样保留（能撤销回新识别结果），
    提示、问答按旧设定写的照样作废。"""
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    bar = store.load(sid)["bars"][1]
    songs.edit_bar(sid, bar["id"], [{"dur": "4", "notes": [{"string": 2, "fret": 7}]}], None)
    store.update(sid, lambda s: s["bars"][1].update(tip="手写提示", qa=[{"q": "怎么弹", "a": "慢慢弹"}]))
    real = FakeClaude.read_meta
    monkeypatch.setattr(FakeClaude, "read_meta", lambda self, p: {**real(self, p), **head})
    assert recognize.start_parse(sid)
    after = store.load(sid)
    nb = next(b for b in after["bars"] if b["id"] == bar["id"])
    assert nb["qa"][0]["stale"]
    if kept:
        assert nb["edited"] and nb["events"][0]["notes"][0]["fret"] == 7 and nb["_orig"]["events"]
        assert nb["tip"] == "" and not after["notice"]
    else:
        assert not nb.get("edited") and "_orig" not in nb
        assert "手改过的小节" in after["notice"]["msg"]


@pytest.mark.parametrize("how", ["error", "cancel"])
def test_failed_row_reparse_still_records_its_cost(data_dir, sync_jobs, monkeypatch, how):
    """单行重新识别失败 / 被取消：已经发出去的请求也花了钱，要记到曲子上。"""
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    before = store.load(sid)["usage"]

    def costly_fail(self, *a, **kw):
        self.usage["input_tokens"] += 1000
        raise jobs.Cancelled() if how == "cancel" else errors.Upstream("输出被截断")

    monkeypatch.setattr(FakeClaude, "transcribe", costly_fail)
    recognize.start_reparse(sid, 0)
    after = store.load(sid)
    assert after["systems"][0]["status"] == "error"
    assert after["usage"]["input_tokens"] == before["input_tokens"] + 1010  # 替身一开始就带 10
    assert after["usage"]["output_tokens"] == before["output_tokens"] + 20


def test_cost_is_not_lost_when_saving_the_result_fails(data_dir, sync_jobs, monkeypatch):
    """写回结果时存盘失败：这次的用量没写进去，之后的写入（失败处理）要把它补上，不能当成已经记过。"""
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    before = store.load(sid)["usage"]
    real_save, real_transcribe = store.save, FakeClaude.transcribe
    fails = []

    def transcribe(self, *a, **kw):
        fails.append("armed")  # 识别完的第一次写回存盘失败
        return real_transcribe(self, *a, **kw)

    def flaky_save(song, *a, **kw):
        if fails == ["armed"]:
            fails.append("failed")
            raise OSError("磁盘满了")
        return real_save(song, *a, **kw)

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    monkeypatch.setattr(store, "save", flaky_save)
    recognize.start_reparse(sid, 1)
    after = store.load(sid)
    assert fails and after["systems"][1]["status"] == "error"
    assert after["usage"]["input_tokens"] == before["input_tokens"] + 10  # 新建的替身一开始就带 10
    assert after["usage"]["output_tokens"] == before["output_tokens"] + 20


def test_usage_meter_commits_only_after_the_write(monkeypatch):
    """charge 记上的部分要等写盘成功才算数：写盘失败，下一次 charge 连同这次一起记。"""
    meter = jobs.UsageMeter(FakeClaude(None))
    s = {}

    def failing_update(sid, fn):
        fn({})
        raise OSError("磁盘满了")

    monkeypatch.setattr(store, "update", failing_update)
    with pytest.raises(OSError):
        meter.update("x", meter.charge)
    meter.charge(s)
    assert s["usage"]["input_tokens"] == 10 and s["usage"]["output_tokens"] == 20


@pytest.mark.parametrize("how", ["error", "cancel"])
def test_header_cost_is_recorded_when_the_parse_stops_before_any_row(data_dir, sync_jobs, monkeypatch, how):
    real = FakeClaude.read_meta

    def read_meta(self, page):
        self.usage["input_tokens"] += 1000
        if how == "error":
            raise errors.Upstream("输出被截断")
        cancel.set()
        return real(self, page)

    monkeypatch.setattr(FakeClaude, "read_meta", read_meta)
    sid = songs.create_song([("p1.png", score_png())])
    cancel = jobs.jobs.claim(sid)
    assert recognize.start_parse(sid, cancel)
    song = store.load(sid)
    assert song["status"] == ("cancelled" if how == "cancel" else "error")
    # 替身一开始就带 10 / 20：新建的客户端用掉的全算这首曲子的
    assert song["usage"]["input_tokens"] == 1010 and song["usage"]["output_tokens"] == 20


def test_row_with_no_bars_fails_instead_of_wiping_the_row(data_dir, sync_jobs, monkeypatch):
    """模型对一行（返修后也）返回 0 个小节：这一行算失败。重新识别时原来的结果整首保留。"""
    sid, before = _parsed_song_with_user_data()
    real = FakeClaude.transcribe

    def transcribe(self, img, meta, first_bar, *a, **kw):
        out = real(self, img, meta, first_bar, *a, **kw)
        return {"bars": []} if first_bar > 1 else out

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    assert recognize.start_parse(sid)
    after = store.load(sid)
    assert after["bars"] == before["bars"] and "原来的结果保留不变" in after["notice"]["msg"]
    assert "没有读出这一行的小节" in after["notice"]["msg"]

    fresh = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(fresh)
    s = store.load(fresh)
    assert [x["status"] for x in s["systems"]] == ["done", "error"]
    assert "没有读出" in s["systems"][1]["error"]


def test_fixing_every_failed_row_makes_the_song_ready(data_dir, sync_jobs, monkeypatch):
    """第一次识别中途服务重启（曲子显示识别失败），把这一行单独重新识别成功后，曲子回到“完成”。"""
    real = FakeClaude.transcribe

    def transcribe(self, img, meta, first_bar, *a, **kw):
        if first_bar > 1:
            raise RuntimeError("网络断了")
        return real(self, img, meta, first_bar, *a, **kw)

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    # 服务在识别中途重启：recover 把曲子标成识别失败（已经识别出的行留着）
    store.update(sid, lambda x: x.update(status="parsing", activity={"kind": "parse", "systems": []}))
    store.update(sid, state.recover)
    s = store.load(sid)
    assert s["status"] == "error" and s["notice"]["source"] == "parse"
    monkeypatch.setattr(FakeClaude, "transcribe", real)
    recognize.start_reparse(sid, 1)
    s = store.load(sid)
    assert s["status"] == "ready" and s["notice"] is None


def test_chord_sheet_rows_keep_chord_names_strums_and_shapes(data_dir, monkeypatch):
    """弹唱谱：事件上的和弦名、扫弦方向留着；这一行的和弦图按小节存进 shapes（只存这个小节用到的）。"""

    def transcribe(self, img, meta, first_bar, spans, **kw):
        g = [-1, 3, 2, 0, 3, 3]  # 不是真的 G，随便一个指法
        bars = []
        for i in range(len(spans)):
            ev = {
                "dur": "1",
                "notes": [{"string": s, "fret": 0, "tech": [], "grace": -1, "bend": 0} for s in (1, 2)],
            }
            bars.append(
                {
                    "number": first_bar + i,
                    "events": [
                        {**ev, "chord": "Em" if i == 0 else "", "strum": "down"},
                        ev,
                        ev,
                        {**ev, "strum": "up"},
                    ],
                    "tip": "",
                    "uncertain": "",
                }
            )
        return {
            "bars": bars,
            "chords": [{"name": "Em", "frets": [0, 0, 0, 2, 2, 0]}, {"name": "G?", "frets": g}],
        }

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    bars = store.load(sid)["bars"]
    first = bars[0]
    assert first["events"][0]["chord"] == "Em" and first["events"][0]["strum"] == "down"
    assert first["events"][3]["strum"] == "up" and "chord" not in first["events"][1]
    assert first["shapes"] == {"Em": [0, 0, 0, 2, 2, 0]} and "shapes" not in bars[1]


def test_chord_names_and_strums_survive_editing(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    bar = store.load(sid)["bars"][0]
    ev = [{"dur": "4", "notes": [{"string": 1, "fret": 0}], "chord": " Am7 ", "strum": "up"}]
    out = songs.edit_bar(sid, bar["id"], ev, None)
    assert out["bars"][0]["events"] == [
        {
            "dur": "4",
            "notes": [{"string": 1, "fret": 0, "tech": [], "grace": -1, "bend": 0}],
            "chord": "Am7",
            "strum": "up",
        }
    ]
    assert music.clean_events([{"dur": "1", "notes": [], "strum": "sideways"}]) == [{"dur": "1", "notes": []}]


def test_rows_that_read_a_chord_diagram_differently_are_redone_with_the_majority_shape():
    """三行都有 D11：两行读成 x5403 0，一行把 3fr. 算错了。那一行带上多数的读法再识别；拍号没问题的行不动。"""
    meta = music.normalize_meta({"title": "x", "tuning": music.STANDARD, "time_signature": [4, 4]})
    good, bad = [0, 3, 0, 4, 5, -1], [0, 1, 0, 2, 3, -1]
    bars = [
        {
            "system": si,
            "events": [{"dur": "4", "notes": []}],
            "shapes": {"D11": shape, "Em": [0, 0, 0, 2, 2, 0]},
        }
        for si, shape in enumerate([good, bad, good])
    ]
    used = {0: [4, 4], 1: [4, 4], 2: [4, 4]}
    redo = recognize._rows_to_redo(bars, meta, used)
    assert [(si, ts) for si, ts, _ in redo] == [(1, [4, 4])]
    assert "D11 = frets for strings 1-6 [0, 3, 0, 4, 5, -1]" in redo[0][2] and "Em" not in redo[0][2]
    bars[1]["edited"] = True  # 用户改过的行不覆盖
    assert recognize._rows_to_redo(bars, meta, used) == []
    # 只有两行、各读各的：没有多数，不重做
    assert recognize._rows_to_redo(bars[:2], meta, {0: [4, 4], 1: [4, 4]}, skip_edited=False) == []


def test_wrong_bar_count_after_the_fix_up_is_read_once_more(data_dir, monkeypatch):
    """返修后小节数还是不对：不带上次结果重新读一遍；这次对了就用它。"""
    real = FakeClaude.transcribe
    calls = []

    def transcribe(self, img, meta, first_bar, spans, *a, **kw):
        out = real(self, img, meta, first_bar, spans, *a, **kw)
        calls.append(first_bar)
        if first_bar == 1 and calls.count(1) <= 2:  # 第一次和返修都只给 1 个小节
            out["bars"] = out["bars"][:1]
        return out

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    s = store.load(sid)
    assert calls.count(1) == 3
    assert len([b for b in s["bars"] if b["system"] == 0]) == 3


def test_double_check_votes_bars_that_two_reads_disagree_on(data_dir, monkeypatch):
    """双重校对：第二遍和第一遍有一个小节不一样，读第三遍；第三遍和第二遍一样就用第二遍的。
    三遍都不一样的小节用第一遍，标上不确定。"""
    real = FakeClaude.transcribe
    reads = []

    def transcribe(self, img, meta, first_bar, spans, *a, **kw):
        out = real(self, img, meta, first_bar, spans, *a, **kw)
        if first_bar != 1:
            return out
        if kw.get("previous") is None:
            reads.append(1)
        k = len(reads)  # 返修（previous 不是 None）算在同一遍里
        # 第 1 小节：第一遍读成 5 品，第二、三遍读成 0 品；第 2 小节：三遍各不相同
        for e in out["bars"][0]["events"]:
            e["notes"][0]["fret"] = 5 if k == 1 else 0
        for e in out["bars"][1]["events"]:
            e["notes"][0]["fret"] = k
        return out

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    sid = songs.create_song([("p1.png", score_png())])
    song = store.load(sid)
    song["meta"] = music.normalize_meta({"title": "x", "tuning": music.STANDARD, "time_signature": [4, 4]})
    song["systems"] = recognize.plan_systems(sid, song)
    claude = FakeClaude(None)
    bars = recognize.transcribe_system(claude, sid, song, 0, 1, double_check="all")
    assert len(reads) == 3
    assert bars[0]["events"][0]["notes"][0]["fret"] == 0  # 第二、三遍一致
    assert bars[1]["events"][0]["notes"][0]["fret"] == 1 and "三次识别" in bars[1]["uncertain"]
    assert not bars[2].get("uncertain")


def test_header_time_signature_misread_is_fixed_from_the_first_bar(data_dir, monkeypatch):
    """读谱头时把 3/4 读成了 4/4，第一行的第 1 小节写着变拍号 3/4：全曲改成 3/4，第 1 小节不算变拍号；
    后面按 4/4 识别的行按 3/4 重做。"""
    monkeypatch.setattr(
        FakeClaude,
        "read_meta",
        lambda self, page: {
            "title": "",
            "tuning": music.STANDARD,
            "capo": 0,
            "time_signature": [4, 4],
            "tempo": 0,
        },
    )
    real = FakeClaude.transcribe
    starts = []

    def transcribe(self, img, meta, first_bar, spans, *a, **kw):
        starts.append((first_bar, kw.get("start_ts") or meta["time_signature"]))
        out = real(self, img, meta, first_bar, spans, *a, **kw)
        for b in out["bars"]:
            b["events"] = b["events"][:3]  # 每小节 3 拍
        if first_bar == 1:
            out["bars"][0]["time_signature"] = [3, 4]
        return out

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    s = store.load(sid)
    assert s["meta"]["time_signature"] == [3, 4]
    assert "ts_change" not in s["bars"][0] and all("time_signature" not in b for b in s["bars"])
    assert (4, [3, 4]) in starts  # 第二行按 3/4 重做了
    assert starts.count((1, None)) == starts.count((1, None)) and not any(
        f == 1 and t == [3, 4] for f, t in starts
    )


def test_double_check_doubts_mode_only_rereads_rows_with_doubts(data_dir, monkeypatch):
    """doubts：第一遍没有警告、没有“不确定”的行不再读；有的才再读。"""
    real = FakeClaude.transcribe
    reads = []

    def transcribe(self, img, meta, first_bar, spans, *a, **kw):
        out = real(self, img, meta, first_bar, spans, *a, **kw)
        if kw.get("previous") is None:
            reads.append(first_bar)
        if first_bar > 1:
            out["bars"][0]["uncertain"] = "第 1 拍看不清"
        return out

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    sid = songs.create_song([("p1.png", score_png())])
    song = store.load(sid)
    song["meta"] = music.normalize_meta({"title": "x", "tuning": music.STANDARD, "time_signature": [4, 4]})
    song["systems"] = recognize.plan_systems(sid, song)
    claude = FakeClaude(None)
    recognize.transcribe_system(claude, sid, song, 0, 1, double_check="doubts")
    recognize.transcribe_system(claude, sid, song, 1, 4, double_check="doubts")
    assert reads == [1, 4, 4]  # 第一行没疑问：只读一遍；第二行有：再读一遍（两遍一样，不用第三遍）


def test_split_row_reads_halves_and_carries_the_chord_into_the_right_half(data_dir, monkeypatch):
    """切成两半：左半 2 个小节、右半 1 个；右半带上左半最后的和弦（和指法），小节号接着排。"""
    real = FakeClaude.transcribe
    calls = []

    def transcribe(self, img, meta, first_bar, spans, *a, **kw):
        out = real(self, img, meta, first_bar, spans, *a, **kw)
        calls.append((first_bar, len(spans), kw.get("context", ""), img.width))
        if first_bar == 1:
            out["bars"][0]["events"][0]["chord"] = "Em"
            out["chords"] = [{"name": "Em", "frets": [0, 0, 0, 2, 2, 0]}]
        return out

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    sid = songs.create_song([("p1.png", score_png())])
    song = store.load(sid)
    song["meta"] = music.normalize_meta({"title": "x", "tuning": music.STANDARD, "time_signature": [4, 4]})
    song["systems"] = recognize.plan_systems(sid, song)
    full_width = song["systems"][0]["crop"][1] - song["systems"][0]["crop"][0]
    bars = recognize.transcribe_system(FakeClaude(None), sid, song, 0, 1, split=True, double_check="off")
    assert len(bars) == 3
    firsts = [(f, n) for f, n, _, _ in calls]
    assert (1, 2) in firsts and (3, 1) in firsts
    right = next(c for c in calls if c[0] == 3)
    assert "Em" in right[2] and "[0, 0, 0, 2, 2, 0]" in right[2]
    assert all(w < full_width * 0.75 for *_, w in calls)
    assert [b["region"][0] for b in bars] == sorted(b["region"][0] for b in bars)


def test_double_check_without_a_majority_prefers_the_read_without_a_length_error():
    """两遍只差最后一个音的时值也算不一样；第三遍没读出来、没有多数时，用没有时值错误的那遍，说明写“两次”。"""
    meta = music.normalize_meta({"title": "x", "tuning": music.STANDARD, "time_signature": [4, 4]})

    def bar(last):
        events = [{"dur": d, "notes": [{"string": 1, "fret": 0, "tech": []}]} for d in ("1", "1", "1", last)]
        return {"events": events, "uncertain": "", "warn": music.check_bar(events, meta)}

    reads = iter([([bar("1")], []), RuntimeError("断了")])

    def read_once():
        r = next(reads)
        if isinstance(r, Exception):
            raise r
        return r

    out = recognize._double_check([bar("1/2")], read_once, 1, "sid", 0)
    assert out[0]["events"][-1]["dur"] == "1" and not out[0]["warn"]
    assert "两次识别" in out[0]["uncertain"]


def test_ocr_tie_from_the_left_half_is_not_a_disagreement():
    """右半行开头写成括号的延续音（接左半最后的 2 弦 5 品）：OCR 读到、AI 不写，不算不一致。"""
    bars = [{"events": [{"dur": "1", "notes": [{"string": 1, "fret": 3, "tech": []}]}]}]
    seen = [[(1, 3), (2, 5)]]
    assert recognize.ocr_disagreements(bars, seen, tied_in={}) != {}
    assert recognize.ocr_disagreements(bars, seen, tied_in={2: 5}) == {}
    left = [
        {
            "events": [
                {
                    "dur": "1",
                    "notes": [{"string": 2, "fret": 5, "tech": []}, {"string": 3, "fret": 9, "tech": ["x"]}],
                }
            ]
        }
    ]
    assert recognize._last_frets(left) == {2: 5}


def test_doubts_mode_rereads_rows_whose_only_doubt_is_an_ocr_mismatch(data_dir, monkeypatch):
    """doubts：返修后剩下的疑问只有 OCR 不一致（警告要到最后才加上），也算有疑问、要再读一遍。"""
    real = FakeClaude.transcribe
    reads = []

    def transcribe(self, img, meta, first_bar, spans, *a, **kw):
        if kw.get("previous") is None:
            reads.append(first_bar)
        return real(self, img, meta, first_bar, spans, *a, **kw)

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    monkeypatch.setattr(recognize, "OCR_CHECK", True)
    monkeypatch.setattr(recognize.ocr, "read_system", lambda img, lines, spans: [[(1, 1)] for _ in spans])
    monkeypatch.setattr(
        recognize, "ocr_disagreements", lambda bars, *a, **kw: {0: (Counter({(1, 1): 1}), Counter())}
    )
    sid = songs.create_song([("p1.png", score_png())])
    song = store.load(sid)
    song["meta"] = music.normalize_meta({"title": "x", "tuning": music.STANDARD, "time_signature": [4, 4]})
    song["systems"] = recognize.plan_systems(sid, song)
    bars = recognize.transcribe_system(FakeClaude(None), sid, song, 0, 1, double_check="doubts", split=False)
    assert reads == [1, 1] and bars[0]["warn"][-1]["code"] == "ocr_mismatch"


def test_chord_diagram_majority_goes_to_the_model_as_context_not_as_the_users_hint(data_dir, monkeypatch):
    seen = []
    real = FakeClaude.transcribe

    def transcribe(self, img, meta, first_bar, spans, *a, **kw):
        seen.append((kw.get("hint", ""), kw.get("context", "")))
        return real(self, img, meta, first_bar, spans, *a, **kw)

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    sid = songs.create_song([("p1.png", score_png())])
    song = store.load(sid)
    song["meta"] = music.normalize_meta({"title": "x", "tuning": music.STANDARD, "time_signature": [4, 4]})
    song["systems"] = recognize.plan_systems(sid, song)
    note = "The chord diagrams below were read the same way in the other lines"
    recognize._recognize_rows(
        FakeClaude(None), sid, song, [(0, None, note)], threading.Event(), lambda *a: None
    )
    assert seen and all(h == "" and note in c for h, c in seen)


def _planned_song():
    sid = songs.create_song([("p1.png", score_png())])
    song = store.load(sid)
    song["meta"] = music.normalize_meta({"title": "x", "tuning": music.STANDARD, "time_signature": [4, 4]})
    song["systems"] = recognize.plan_systems(sid, song)
    return sid, song


def _note(s, f):
    return {"string": s, "fret": f, "tech": [], "grace": -1, "bend": 0}


def _bars(first_bar, n, durs, fret=0):
    return {
        "bars": [
            {
                "number": first_bar + i,
                "events": [{"dur": d, "notes": [_note(1, fret + i)]} for d in durs(i)],
                "tip": "",
                "uncertain": "",
            }
            for i in range(n)
        ]
    }


def test_ocr_tie_at_the_start_of_a_row_is_not_a_disagreement():
    """行首写成括号的延续音（接上一行最后的音）：上一行的音已知时按它判断；
    不知道（各行并行识别）时，第一个小节 AI 这边没有对不上的音，OCR 多读的音不算不一致（和后面小节同一个规则）。"""
    bars = [{"events": [{"dur": "1", "notes": [{"string": 1, "fret": 3, "tech": []}]}]}]
    seen = [[(1, 3), (2, 5)]]
    assert recognize.ocr_disagreements(bars, seen) == {}  # 不知道上一行
    assert recognize.ocr_disagreements(bars, seen, tied_in={2: 5}) == {}
    assert recognize.ocr_disagreements(bars, seen, tied_in={}) != {}  # 第一行：前面没有音
    assert recognize.ocr_disagreements(bars, seen, tied_in={2: 7}) != {}
    # AI 在小节开头的 6 弦上已经写了 5 品，OCR 在这根弦上多读一个 5：不是延续音（延续音占着开头这根弦），
    # 是小节中间漏写的音（eval-r-imagine 第 10 小节第 3 拍）：照样报
    ghost = [{"events": [{"dur": "2", "notes": [_note(6, 5)]}, {"dur": "2", "notes": [_note(6, 5)]}]}]
    assert recognize.ocr_disagreements(ghost, [[(6, 5), (6, 5), (6, 5)]]) != {}
    # AI 这边也有对不上的音（1 弦 3 品其实在 2 弦）：照样报
    chord = [{"events": [{"dur": "1", "notes": [_note(1, 3), _note(3, 2), _note(4, 2), _note(5, 0)]}]}]
    out = recognize.ocr_disagreements(chord, [[(2, 3), (3, 2), (4, 2), (5, 0), (2, 5)]])
    assert dict(out[0][0]) == {(2, 3): 1, (2, 5): 1}


def test_row_recognition_passes_the_previous_rows_last_notes_for_ocr(data_dir, monkeypatch):
    """前一行已经有结果时（单行重新识别、补页、重试），把它最后的音告诉 OCR 校对；第一行前面什么都没有。"""
    seen = []
    monkeypatch.setattr(
        recognize, "transcribe_queued", lambda *a, **kw: seen.append((a[3], kw.get("tied_in"))) or []
    )
    sid, song = _planned_song()
    song["bars"] = [
        {"system": 0, "events": [{"dur": "1", "notes": [_note(2, 5), _note(3, 7)]}]},
        {"system": 0, "events": [{"dur": "1", "notes": [_note(2, 8)]}]},
    ]
    recognize._recognize_rows(
        FakeClaude(None), sid, song, [(0, None), (1, None)], threading.Event(), lambda *a: None
    )
    assert sorted(seen, key=lambda x: x[0]) == [(0, {}), (1, {2: 8, 3: 7})]


def test_single_bar_row_with_wrong_length_gets_the_normal_length_problem(data_dir, monkeypatch):
    """只有 1 个小节的行（短的末行、1 小节的右半行）时值不对：按普通的时值问题返修，
    不能说“整行每个小节都一样长、可能前面变了拍号”（模型会写个错的拍号，带到后面所有行）。"""
    sid, song = _planned_song()
    song["systems"][0]["spans"] = song["systems"][0]["spans"][:1]
    problems = []

    def transcribe(self, img, meta, first_bar, spans, *a, **kw):
        if kw.get("problems"):
            problems.append(kw["problems"])
        return _bars(first_bar, len(spans), lambda i: ["3"])

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    recognize.transcribe_system(FakeClaude(None), sid, song, 0, 1, double_check="off", split=False)
    assert problems and all("变了拍号" not in p for p in problems[0])
    assert any("时值合计 3 拍" in p for p in problems[0])


def test_retry_with_the_wrong_bar_count_does_not_replace_a_right_count_read(data_dir, monkeypatch):
    """第一遍小节数对、有两个时值问题；返修少了一个小节（OCR 不比，问题反而少）：小节数对的优先。
    之后重读又出错时，也不能留下小节数不对的结果。"""
    sid, song = _planned_song()
    reads = []

    def transcribe(self, img, meta, first_bar, spans, *a, previous=None, **kw):
        reads.append(previous is not None)
        if len(reads) == 3:
            raise RuntimeError("断了")
        if previous is None:  # 第一遍：前两个小节少一拍
            return _bars(first_bar, len(spans), lambda i: ["1"] * (3 if i < 2 else 4))
        return _bars(first_bar, len(spans) - 1, lambda i: ["1"] * 4)

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    bars = recognize.transcribe_system(FakeClaude(None), sid, song, 0, 1, double_check="off", split=False)
    assert len(bars) == 3


def test_doubts_mode_ignores_info_only_warnings(data_dir, monkeypatch):
    """doubts：五连音之类“不在播放网格上”的提醒读对了也会有，不算疑问，不为它再读一遍。"""
    sid, song = _planned_song()
    reads = []

    def transcribe(self, img, meta, first_bar, spans, *a, **kw):
        reads.append(first_bar)
        return _bars(first_bar, len(spans), lambda i: ["1/5"] * 5 + ["3"])

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    bars = recognize.transcribe_system(FakeClaude(None), sid, song, 0, 1, double_check="doubts", split=False)
    assert reads == [1] and all(w["code"] == "off_grid" for b in bars for w in b["warn"])


def test_failed_row_write_stops_the_other_rows_and_leaves_no_row_pending(data_dir, sync_jobs, monkeypatch):
    """第一次识别时写回一行出错（存盘失败之类）：还没开始的行不再发请求；结束后没有行停在“等待 / 识别中”，
    都标成失败（“重试失败的行”才找得到它们）。"""
    monkeypatch.setattr(config.settings, "row_concurrency", 1)
    real = recognize.apply_time_signatures
    boom = []

    def flaky(bars, meta):
        if not boom:
            boom.append(1)
            raise OSError("磁盘满了")
        return real(bars, meta)

    monkeypatch.setattr(recognize, "apply_time_signatures", flaky)
    real_queued = recognize.transcribe_queued

    def queued(*a, cancel=None, **kw):
        if a[3] == 1:  # 第二行等第一行写回（出错）之后才开始：不然要看两个线程谁快
            cancel.wait(2)
        return real_queued(*a, cancel=cancel, **kw)

    monkeypatch.setattr(recognize, "transcribe_queued", queued)
    sid = songs.create_song([("p1.png", score_png())])
    assert recognize.start_parse(sid)
    song = store.load(sid)
    assert boom and song["status"] == "error"
    assert [c["first_bar"] for c in FakeClaude.calls if not c["retry"]] == [1]  # 第二行没有再发请求
    assert [x["status"] for x in song["systems"]] == ["error", "error"]
    assert recognize.failed_rows(song) == [0, 1]


def test_rows_locked_for_a_redo_are_released_when_the_job_fails(data_dir, sync_jobs, monkeypatch):
    """重试失败的行之后要按新拍号重做别的行（这些行被标成识别中）：重做没跑成，它们也要标成失败，不能一直锁着。"""
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    store.update(sid, lambda s: s["systems"][0].update(status="error", error="断了"))
    real = recognize._recognize_rows
    n = []

    def rows(*a, **kw):
        n.append(1)
        if len(n) == 2:
            raise RuntimeError("断了")
        return real(*a, **kw)

    monkeypatch.setattr(recognize, "_recognize_rows", rows)
    monkeypatch.setattr(recognize, "_rows_to_redo", lambda *a, **kw: [(1, [3, 4], "")])
    recognize.start_retry_failed(sid)
    song = store.load(sid)
    assert song["activity"] is None
    assert [x["status"] for x in song["systems"]] == ["done", "error"]


def test_failed_rerecognition_keeps_the_previous_failed_status(data_dir, sync_jobs, monkeypatch):
    """原来有行失败（曲子是“识别失败”）时整首重新识别又失败：状态照旧是失败（不能变成“可练习”），提示说明还有失败的行。"""
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)

    def broken(s):
        s["systems"][1].update(status="error", error="断了")
        s["status"] = "error"

    store.update(sid, broken)
    monkeypatch.setattr(FakeClaude, "read_meta", lambda self, page: 1 / 0)
    assert recognize.start_parse(sid)
    after = store.load(sid)
    assert after["status"] == "error" and after["activity"] is None
    assert "原来的结果保留不变" in after["notice"]["msg"] and "重试失败的行" in after["notice"]["msg"]
    assert recognize.failed_rows(after) == [1]


def test_error_after_the_new_result_is_swapped_in_does_not_claim_the_old_one_was_kept(
    data_dir, sync_jobs, monkeypatch
):
    """重新识别的新结果已经换上之后才出错（收尾时存盘失败之类）：不能说“原来的结果保留不变”。"""
    sid, _ = _parsed_song_with_user_data()
    monkeypatch.setattr(recognize.staleness, "store_plan", lambda *a, **kw: 1 / 0)
    assert recognize.start_parse(sid)
    after = store.load(sid)
    assert after["activity"] is None and after["status"] == "ready"
    assert "原来的结果保留不变" not in after["notice"]["msg"]


def _ts_aware_transcribe(declare):
    """替身模型：第 1 行在 declare 给出的小节（行内序号 → 拍号）写变拍号；知道是 3/4 的小节写 3 拍，否则 4 拍。
    记下 (起始小节, 开头拍号)。"""
    starts = []

    def transcribe(self, img, meta, first_bar, spans, *a, start_ts=None, **kw):
        ts = start_ts or meta["time_signature"]
        starts.append((first_bar, start_ts))
        bars = []
        for i in range(len(spans)):
            mark = declare.get(i) if first_bar == 1 else None
            ts = mark or ts
            bars.append(
                {
                    "number": first_bar + i,
                    "time_signature": mark or [],
                    "events": [{"dur": "1", "notes": [_note(1, k)]} for k in range(ts[0])],
                    "tip": "",
                    "uncertain": "",
                }
            )
        return {"bars": bars}

    return transcribe, starts


def test_reparse_of_the_first_row_fixes_the_header_time_signature_and_redoes_later_rows(
    data_dir, monkeypatch
):
    """单行重新识别第一行，这次第 1 小节写着 3/4（谱头读错了）：全曲拍号改成 3/4，后面按 4/4 识别的行按 3/4 重做。"""
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    transcribe, starts = _ts_aware_transcribe({0: [3, 4]})
    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    recognize.reparse_system(sid, 0)
    s = store.load(sid)
    assert s["meta"]["time_signature"] == [3, 4] and "ts_change" not in s["bars"][0]
    assert [f for f, _ in starts] == [1, 4]  # 第二行重做了（全曲已经是 3/4，不用再特别告诉模型）
    assert all(x["status"] == "done" for x in s["systems"]) and all(not b["warn"] for b in s["bars"])
    assert s["activity"] is None


def test_retrying_a_row_that_now_changes_the_time_signature_redoes_the_rows_after_it(
    data_dir, sync_jobs, monkeypatch
):
    """重试 / 补页识别的行写了变拍号：后面没有重试的行开头的拍号跟着变了，按新拍号重做（补页插在前面时同理）。"""
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    store.update(sid, lambda s: s["systems"][0].update(status="error", error="断了"))
    transcribe, starts = _ts_aware_transcribe({1: [3, 4]})
    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    recognize.start_retry_failed(sid)
    s = store.load(sid)
    assert (4, [3, 4]) in starts  # 第二行按 3/4 重做
    assert [b.get("time_signature") for b in s["bars"]] == [None] + [[3, 4]] * 5
    assert all(not b["warn"] for b in s["bars"]) and all(x["status"] == "done" for x in s["systems"])


def test_tapped_harmonic_marks_are_not_ocr_disagreements_and_the_tap_is_part_of_the_bar_key():
    """点弦泛音 "5 <22>"：OCR 会把 <> 里的 22 也读成一个数字，不算 AI 漏了音；两遍识别点的品不同算不一致。"""

    def bar(harm):
        return {"events": [{"dur": "4", "notes": [{"string": 3, "fret": 5, "tech": ["th"], "harm": harm}]}]}

    seen = [[(3, 5), (3, 22)]]
    assert recognize.ocr_disagreements([bar(22)], seen, tied_in={}) == {}
    assert recognize.ocr_disagreements([bar(17)], seen, tied_in={}) != {}
    assert recognize._bar_key(bar(22)) != recognize._bar_key(bar(17))


def test_a_crash_outside_the_row_handlers_does_not_leave_the_song_recognizing(
    data_dir, sync_jobs, monkeypatch
):
    """单行重新识别在自己的错误处理之外出错（开头读曲子失败之类）：任务照样结束，行标成失败，
    曲子不会一直显示“识别中”。"""
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)

    def boom(song):
        raise OSError("读不出 song.json")

    monkeypatch.setattr(recognize, "_bar_keys", boom)
    recognize.start_reparse(sid, 1)
    s = store.load(sid)
    assert s["activity"] is None and not jobs.jobs.running(sid)
    assert s["systems"][1]["status"] == "error" and "读不出" in s["notice"]["msg"]


def test_a_parse_whose_error_handling_fails_still_finishes(data_dir, sync_jobs, monkeypatch):
    """整首识别失败、收尾写回也失败（磁盘满之类）：最后一道兜底把任务结束掉。"""
    sid = songs.create_song([("p1.png", score_png())])

    def end_fails(*a):
        raise OSError("disk full")

    monkeypatch.setattr(recognize, "_end_parse", end_fails)
    monkeypatch.setattr(FakeClaude, "read_meta", lambda self, page: 1 / 0)
    assert recognize.start_parse(sid)
    s = store.load(sid)
    assert s["activity"] is None and s["status"] == "error" and "disk full" in s["notice"]["msg"]


def _blank_png() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (800, 1100), "white").save(buf, "PNG")
    return buf.getvalue()


def test_a_page_without_tab_is_a_done_row_with_no_bars(data_dir, sync_jobs, monkeypatch):
    """检测不到六线谱的页（封面、歌词页）整页当一行交给模型；模型说这一页没有六线谱（返回 0 个小节）时，
    这一行算识别完成、没有小节，而不是失败：不然这首曲子永远“有行没识别出来”，重试也只会再花钱失败。"""
    real = FakeClaude.transcribe

    def transcribe(self, img, meta, first_bar, spans, *a, **kw):
        if spans is None:
            FakeClaude.calls.append({"first_bar": first_bar, "retry": False, "hint": "", "page": True})
            return {"bars": []}
        return real(self, img, meta, first_bar, spans, *a, **kw)

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    sid = songs.create_song([("cover.png", _blank_png()), ("p1.png", score_png())])
    recognize.run_parse(sid)
    s = store.load(sid)
    assert s["systems"][0]["spans"] is None
    assert [x["status"] for x in s["systems"]] == ["done", "done", "done"]
    assert s["status"] == "ready" and s["notice"] is None
    assert [b["n"] for b in s["bars"]] == [1, 2, 3, 4, 5, 6] and {b["system"] for b in s["bars"]} == {1, 2}
    assert recognize.failed_rows(s) == []
    assert recognize.first_bar_of(s, 1) == 1  # 没有小节的整页不占小节号

    # 重新识别整首：封面页照样没有小节，不算失败，新结果照常换上
    assert recognize.start_parse(sid)
    s = store.load(sid)
    assert (
        s["activity"] is None
        and s["status"] == "ready"
        and not (s["notice"] or {}).get("msg", "").count("失败")
    )
    assert len(s["bars"]) == 6 and s["systems"][0]["status"] == "done"

    # 单独重新识别封面页这一行
    recognize.reparse_system(sid, 0)
    s = store.load(sid)
    assert s["systems"][0]["status"] == "done" and len(s["bars"]) == 6


def test_reparsing_a_row_with_a_hint_is_not_redone_without_it(data_dir, sync_jobs, monkeypatch):
    """用户带着提示重新识别一行：这一行的和弦图和别的行的多数读法不一样，也不再按多数读法（不带提示）重做一遍，
    用户的意思优先，也不多花一次钱。这一行只看拍号。"""
    good, bad = [0, 3, 0, 4, 5, -1], [0, 1, 0, 2, 3, -1]
    real = FakeClaude.transcribe

    def transcribe(self, img, meta, first_bar, spans, *a, **kw):
        out = real(self, img, meta, first_bar, spans, *a, **kw)
        for b in out["bars"]:
            b["events"][0]["chord"] = "D11"
        out["chords"] = [{"name": "D11", "frets": bad if kw.get("hint") else good}]
        return out

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    sid = songs.create_song([("p1.png", score_png()), ("p2.png", score_png())])
    recognize.run_parse(sid)
    assert len(store.load(sid)["systems"]) == 4
    FakeClaude.calls.clear()
    recognize.start_reparse(sid, 1, "第 2 小节是 D11，按谱上的和弦图")
    s = store.load(sid)
    assert [c["hint"] for c in FakeClaude.calls if not c["retry"]] == ["第 2 小节是 D11，按谱上的和弦图"]
    assert all(b["shapes"] == {"D11": bad} for b in s["bars"] if b["system"] == 1)
    assert s["activity"] is None and s["systems"][1]["status"] == "done"
