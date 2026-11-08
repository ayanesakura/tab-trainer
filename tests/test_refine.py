"""精修（app/refine.py）：挑哪些小节、写回时的 rev 检查、取消、费用、自动精修、设置、撤销精修。只用替身模型。"""

import pytest
from fastapi.testclient import TestClient

from app import config, jobs, main, model, recognize, refine, songs, state, store, views
from app.model import notes_key
from tests.fakes import FakeClaude, derived_bars, score_png


@pytest.fixture
def client(data_dir, monkeypatch):
    monkeypatch.setattr(recognize, "start_parse", lambda sid: True)  # 上传后不自动识别
    return TestClient(main.app, base_url="http://127.0.0.1:8765")


def _song() -> str:
    """识别好的图片曲子：两行各 3 小节，每行第 k 个小节是 4 个 1 弦 k 品（替身模型的结果）。"""
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    FakeClaude.calls.clear()
    return sid


def _verifies() -> list[dict]:
    return [c for c in FakeClaude.calls if "verify" in c]


def change_fret(old: int, new: int, diff: str = "第 1 拍应是 7 品"):
    """替身模型的核对结果：1 弦 old 品的小节改成 new 品，别的说没问题。"""

    def fn(img, meta, span, events, previous, ts):
        if events[0]["notes"][0]["fret"] != old:
            return {"diff": "", "changed": False, "events": events, "uncertain": ""}
        fixed = [{**e, "notes": [{**n, "fret": new} for n in e["notes"]]} for e in events]
        return {"diff": diff, "changed": True, "events": fixed, "uncertain": "第 2 拍看不清"}

    return fn


def test_refine_rewrites_changed_bars_and_keeps_the_old_reading(client, sync_jobs, monkeypatch):
    sid = _song()

    def prepare(s):
        b = s["bars"][1]
        b["qa"] = [{"q": "怎么弹", "a": "慢练", "at": "2026-11-01 10:00"}]
        b["warn"] = [{"code": "ocr_mismatch", "msg": "本地数字识别和 AI 结果不一致"}]
        b["uncertain"] = "第 1 拍可能是 2 品"
        s["plan"] = {"overview": "慢练", "steps": []}

    store.update(sid, prepare)
    before = store.load(sid)
    monkeypatch.setattr(FakeClaude, "verify_result", change_fret(1, 7))
    est = client.get(f"/api/songs/{sid}").json()["refine_estimate"]
    assert est == {
        "bars": 6,
        "cost_usd": 1.2,
        "minutes": 3,
        "seconds": 150,
    }  # 6 × $0.2；4 个并行、两批，每批约 75 秒

    assert client.post(f"/api/songs/{sid}/refine").json() == {"ok": True}
    v = client.get(f"/api/songs/{sid}").json()
    model.SongView.model_validate(v)
    b = v["bars"][1]
    assert [n["fret"] for e in b["events"] for n in e["notes"]] == [7, 7, 7, 7]
    assert b["refine"]["diff"] == "第 1 拍应是 7 品"
    assert b["refine"]["prev"]["events"] == before["bars"][1]["events"]
    assert b["refine"]["prev"]["tip"] == "tip 2"
    assert b["tip"] == "" and b["uncertain"] == "第 2 拍看不清"  # 提示按旧音写的，清掉；不确定说明换成新的
    assert not any(w["code"] == "ocr_mismatch" for w in b["warn"])  # OCR 比的是旧音
    assert b["edited"] is False  # 不算用户手改
    assert b["qa"][0]["stale"] is True
    assert b["id"] in v["plan"]["changed_bars"] and v["plan"]["stale"] is True
    assert v["bars"][4]["refine"]["diff"]  # 第 2 行第 2 小节一样是 1 品
    assert all("refine" not in x for i, x in enumerate(v["bars"]) if i not in (1, 4))
    assert v["bars"][0]["tip"] == "tip 1"  # 没改的小节不动
    assert not any("refine_key" in x for x in v["bars"])  # 内部字段不给前端
    assert v["refined"]["checked"] == 6 and v["refined"]["changed"] == 2 and v["refined"]["at"]
    assert v["notice"] == {"kind": "info", "msg": v["notice"]["msg"]}
    assert v["notice"]["msg"].startswith("精修完成：检查了 6 个小节，改了 2 个")
    assert v["activity"] is None and v["busy"] is False and v["progress"] is None
    # 费用记到曲子上（替身每次核对 $0.05）：6 个小节各一次，改了的 2 个再核对一次确认
    assert round(v["usage"]["cost_usd"] - before["usage"].get("cost_usd", 0), 4) == 0.4
    calls = _verifies()
    assert len(calls) == 8
    assert sorted(c["previous"] for c in calls) == [False] + [True] * 7  # 第 1 小节前面没有小节
    for c in calls:
        a, z = c["span"]
        assert 0 < a < z < c["size"][0]  # 左右各多带了一点邻居
    row = next(x for x in client.get("/api/songs").json() if x["id"] == sid)
    model.LibraryItem.model_validate(row)
    assert (
        row["refined"]["changed"] == 2 and row["refine_estimate"]["bars"] == 6
    )  # 全都核对过：再点就整首重来


def test_which_bars_are_refined(data_dir):
    sid = _song()
    s = store.load(sid)
    ids = [b["id"] for b in s["bars"]]
    assert refine.pick(s) == ids
    songs.edit_bar(sid, ids[0], [{"dur": "4", "notes": [{"string": 2, "fret": 3}]}], None)  # 手改过
    songs.review_bar(sid, ids[3], True)  # 确认过没问题

    def mark(x):
        x["bars"][1]["placeholder"] = True  # 没识别出来、按休止占位
        x["bars"][2]["refine_key"] = notes_key(x["bars"][2])  # 核对过、音没再变

    store.update(sid, mark)
    s = store.load(sid)
    assert refine.pick(s) == [ids[4], ids[5]]
    assert refine.pick(s, {ids[2], ids[4]}) == [ids[4]]
    s["systems"][1]["status"] = "parsing"  # 第 2 行正在重新识别
    assert refine.pick(s) == []  # 剩下的都核对过了：自动精修不重来
    assert refine.pick(s, recheck=True) == [ids[2]]  # 用户点“精修整首”：整首重来
    assert refine.pick(s, {ids[2]}) == []
    s["systems"][1]["status"] = "done"
    s["systems"][1]["spans"] = None  # 没检测到小节线：整行一个小节，裁不出来
    assert refine.pick(s, recheck=True) == [ids[2]]

    gp = songs.create_song([("x.gp5", b"\x18FICHIER GUITAR PRO v5.10")])
    songs.set_derived(gp, {}, derived_bars(3))
    g = store.load(gp)
    assert refine.pick(g) == [] and refine.estimate(g) is None


def test_refine_errors(client, monkeypatch):
    gp = client.post("/api/songs", files={"files": ("x.gp5", b"\x18FICHIER GUITAR PRO v5.10")}).json()["id"]
    client.put(f"/api/songs/{gp}/derived", json={"bars": derived_bars(2)})
    r = client.post(f"/api/songs/{gp}/refine")
    assert r.status_code == 400 and "不需要精修" in r.json()["detail"]
    sid = _song()
    assert jobs.jobs.claim(sid)  # 已经有任务在跑
    try:
        assert client.post(f"/api/songs/{sid}/refine").status_code == 423
    finally:
        jobs.jobs.release(sid)

    def all_edited(s):
        for b in s["bars"]:
            b["edited"] = True

    store.update(sid, all_edited)
    r = client.post(f"/api/songs/{sid}/refine")
    assert r.status_code == 400 and "没有可以精修的小节" in r.json()["detail"]
    assert jobs.jobs.running(sid) is False  # 开始失败时释放登记
    assert store.load(sid)["activity"] is None


def test_write_back_skips_a_bar_the_user_edited_meanwhile(client, sync_jobs, monkeypatch):
    sid = _song()
    target = store.load(sid)["bars"][1]["id"]
    mine = [{"dur": "4", "notes": [{"string": 3, "fret": 5}]}]
    fix = change_fret(1, 7)
    edited = []

    def verify(img, meta, span, events, previous, ts):
        if not edited:  # 核对期间用户在另一个页面改了第 2 小节
            edited.append(songs.edit_bar(sid, target, mine, None))
        return fix(img, meta, span, events, previous, ts)

    monkeypatch.setattr(FakeClaude, "verify_result", verify)
    client.post(f"/api/songs/{sid}/refine")
    s = store.load(sid)
    b = s["bars"][1]
    assert b["events"][0]["notes"][0]["fret"] == 5 and "refine" not in b and b["edited"] is True
    assert s["bars"][4]["refine"]  # 别的小节照常写回
    assert s["refined"]["changed"] == 1 and s["refined"]["checked"] == 5


def test_cancel_stops_the_remaining_bars(client, sync_jobs, monkeypatch):
    sid = _song()
    monkeypatch.setattr(config.settings, "row_concurrency", 1)
    cost = store.load(sid)["usage"].get("cost_usd", 0)

    def verify(img, meta, span, events, previous, ts):
        jobs.jobs.cancel(sid)  # 核对第一个小节时用户点了取消
        return {"diff": "", "changed": False, "events": events, "uncertain": ""}

    monkeypatch.setattr(FakeClaude, "verify_result", verify)
    client.post(f"/api/songs/{sid}/refine")
    s = store.load(sid)
    assert len(_verifies()) == 1  # 后面的不再发请求
    assert s["bars"][0]["refine_key"] == notes_key(s["bars"][0])  # 已经核对完的照常写回
    assert s["activity"] is None and s["progress"] is None
    assert s["notice"]["msg"] == "精修已取消：检查了 1 个小节，改了 0 个，改好的会保留。"
    assert "refined" not in s  # 没做完整首
    assert round(s["usage"]["cost_usd"] - cost, 4) == 0.05  # 发出去的请求照样记账
    assert jobs.jobs.running(sid) is False


def test_a_change_is_only_made_when_two_reads_agree(client, sync_jobs, monkeypatch):
    """模型提出改动时再独立核对：两遍提出一模一样的改动才改（中间几遍说没问题也不要紧，最多核对 MAX_READS 遍）；
    都对不上就不改，标成待核对。"""
    sid = _song()
    # 1 品的小节五遍里没有两遍一样的改动；2 品的第一、四遍一样（中间两遍说没问题：探查里真实出现过的顺序）
    proposals = {1: iter([7, 8, None]), 2: iter([5, None, None, 5])}

    def verify(img, meta, span, events, previous, ts):
        fret = events[0]["notes"][0]["fret"]
        new = next(proposals[fret], None) if fret in proposals else None
        if new is None:
            return {"diff": "", "changed": False, "events": events, "uncertain": ""}
        fixed = [{**e, "notes": [{**n, "fret": new} for n in e["notes"]]} for e in events]
        return {"diff": f"应是 {new} 品", "changed": True, "events": fixed, "uncertain": ""}

    monkeypatch.setattr(config.settings, "row_concurrency", 1)  # 按顺序核对，两行同样品位的小节各用一次 iter
    monkeypatch.setattr(FakeClaude, "verify_result", verify)
    client.post(f"/api/songs/{sid}/refine")
    s = store.load(sid)
    b1, b2 = s["bars"][1], s["bars"][2]
    assert b1["events"][0]["notes"][0]["fret"] == 1 and "refine" not in b1  # 没改
    assert "几次核对的结果不一致" in b1["uncertain"] and "应是 7 品" in b1["uncertain"]
    v = client.get(f"/api/songs/{sid}").json()
    assert v["bars"][1]["needs_review"] is True  # 标成待核对
    assert (
        b2["events"][0]["notes"][0]["fret"] == 5 and b2["refine"]["diff"] == "应是 5 品"
    )  # 第四遍和第一遍一样
    assert (
        len([c for c in _verifies() if c["verify"][0][0]["fret"] == 1]) == refine.MAX_READS + 1
    )  # 1 品的小节核对满 MAX_READS 遍；第 2 行的 1 品小节一遍就说没问题
    assert "1 个小节几次核对的结果不一致" in s["notice"]["msg"]

    monkeypatch.setattr(refine, "CONFIRM_CHANGES", False)  # 关掉确认：第一遍提出的改动直接采用
    store.update(sid, lambda x: x["bars"][4].pop("refine_key"))
    proposals[1] = iter([9])
    FakeClaude.calls.clear()
    client.post(f"/api/songs/{sid}/refine")
    assert store.load(sid)["bars"][4]["events"][0]["notes"][0]["fret"] == 9 and len(_verifies()) == 1


def test_a_failing_bar_does_not_stop_the_others(client, sync_jobs, monkeypatch):
    sid = _song()

    def verify(img, meta, span, events, previous, ts):
        if events[0]["notes"][0]["fret"] == 2:
            raise RuntimeError("过载")
        return {"diff": "", "changed": False, "events": events, "uncertain": ""}

    monkeypatch.setattr(FakeClaude, "verify_result", verify)
    client.post(f"/api/songs/{sid}/refine")
    s = store.load(sid)
    assert s["notice"]["kind"] == "warn" and "2 个小节没核对成" in s["notice"]["msg"]
    assert s["refined"]["checked"] == 4
    assert refine.pick(s) == [s["bars"][2]["id"], s["bars"][5]["id"]]  # 再点精修只核对没核对成的


def test_undo_refine(client, sync_jobs, monkeypatch):
    sid = _song()

    def prepare(s):
        s["bars"][1]["qa"] = [{"q": "怎么弹", "a": "慢练", "at": "2026-11-01 10:00"}]
        s["plan"] = {"overview": "慢练", "steps": []}

    store.update(sid, prepare)
    monkeypatch.setattr(FakeClaude, "verify_result", change_fret(1, 7))
    client.post(f"/api/songs/{sid}/refine")
    b = client.get(f"/api/songs/{sid}").json()["bars"][1]
    url = f"/api/songs/{sid}/bars/{b['id']}/undo-refine"
    r = client.put(url, json={"rev": b["rev"] - 1})
    assert r.status_code == 409  # 别处改过了
    v = client.put(url, json={"rev": b["rev"]}).json()
    u = v["bars"][1]
    assert [n["fret"] for e in u["events"] for n in e["notes"]] == [1, 1, 1, 1]
    assert "refine" not in u and u["tip"] == "tip 2" and u["uncertain"] == ""
    assert u["qa"][0].get("stale") is not True  # 因为精修才过时的问答恢复
    assert u["id"] not in v["plan"]["changed_bars"]
    assert v["bars"][4]["refine"]  # 只撤销这一个
    r = client.put(url, json={"rev": u["rev"]})
    assert r.status_code == 400 and "没有精修改动" in r.json()["detail"]
    s = store.load(sid)
    assert s["bars"][1]["id"] not in refine.pick(s)  # 撤销过的不再自动核对

    # 手改精修过的小节：撤销精修不再适用（会盖掉手改）
    b5 = v["bars"][4]
    songs.edit_bar(sid, b5["id"], [{"dur": "4", "notes": [{"string": 2, "fret": 3}]}], None, b5["rev"])
    assert "refine" not in store.load(sid)["bars"][4]

    gp = client.post("/api/songs", files={"files": ("x.gp5", b"\x18FICHIER GUITAR PRO v5.10")}).json()["id"]
    client.put(f"/api/songs/{gp}/derived", json={"bars": derived_bars(2)})
    gb = client.get(f"/api/songs/{gp}").json()["bars"][0]
    assert client.put(f"/api/songs/{gp}/bars/{gb['id']}/undo-refine", json={}).status_code == 400


def test_refine_setting_round_trips(client):
    assert client.get("/api/settings").json()["refine"] == "manual"
    assert client.put("/api/settings", json={"refine": "auto"}).json()["refine"] == "auto"
    assert config.refine_mode() == "auto"
    assert client.put("/api/settings", json={"effort": "max"}).json()["refine"] == "auto"  # 没带的字段不变
    assert client.put("/api/settings", json={"refine": "sometimes"}).status_code == 400
    assert client.put("/api/settings", json={"refine": "manual"}).json()["refine"] == "manual"


def test_auto_refine_after_whole_song_recognition(data_dir, sync_jobs):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.start_parse(sid)  # 默认手动：识别完不精修
    assert _verifies() == [] and "refined" not in store.load(sid)

    config.update_settings(None, False, {"refine": "auto"})
    FakeClaude.calls.clear()
    sid = songs.create_song([("p1.png", score_png())])
    recognize.start_parse(sid)
    s = store.load(sid)
    assert len(_verifies()) == 6
    assert s["status"] == "ready" and s["activity"] is None and s["refined"]["checked"] == 6
    assert s["notice"]["msg"].startswith("精修完成")

    FakeClaude.calls.clear()
    recognize.start_parse(sid)  # 整首重新识别，结果和原来一样：核对过的指纹跟着保留，不再核对
    assert _verifies() == [] and store.load(sid)["activity"] is None


def test_auto_refine_only_checks_the_bars_just_recognised(data_dir, sync_jobs):
    sid = _song()
    config.update_settings(None, False, {"refine": "auto"})

    def lose_row(s):  # 第 2 行识别失败
        s["bars"] = [b for b in s["bars"] if b["system"] != 1]
        s["systems"][1].update(status="error", error="过载")

    store.update(sid, lose_row)
    recognize.start_retry_failed(sid)
    s = store.load(sid)
    assert len(s["bars"]) == 6 and len(_verifies()) == 3  # 只核对重试出来的 3 个小节
    assert all(b.get("refine_key") for b in s["bars"][3:]) and not any(
        b.get("refine_key") for b in s["bars"][:3]
    )
    assert "refined" not in s  # 不是整首
    assert s["activity"] is None and not jobs.jobs.running(sid)

    def drift(s):  # 第 1 小节存的音和识别结果不一样了（重新识别第 1 行会换回识别结果）
        s["bars"][0]["events"] = [{"dur": "4", "notes": [{"string": 6, "fret": 3, "tech": []}]}]

    store.update(sid, drift)
    FakeClaude.calls.clear()
    recognize.start_reparse(sid, 0)
    assert len(_verifies()) == 1  # 第 1 行另外两个小节和原来一样，不再核对


def test_restart_marks_an_interrupted_refine(data_dir):
    sid = _song()
    store.update(sid, lambda s: state.begin(s, "refine", progress={"stage": "精修", "done": 2, "total": 6}))
    songs.recover_interrupted()
    s = store.load(sid)
    assert s["activity"] is None and s["progress"] is None
    assert "精修中断" in s["notice"]["msg"] and s["status"] == "ready"


# ---------------------------------------------------------------------------
# 精修某个小节 / 某一行（小节卡片上的按钮）
# ---------------------------------------------------------------------------
def test_scope_is_validated(client):
    sid = _song()
    url = f"/api/songs/{sid}/refine"
    r = client.post(url, json={"bars": ["nope"]})
    assert r.status_code == 400 and "小节不存在" in r.json()["detail"]
    r = client.post(url, json={"system": 5})
    assert r.status_code == 400 and "第 6 行不存在" in r.json()["detail"]
    assert client.post(url, json={"system": -1}).status_code == 400
    r = client.post(url, json={"bars": [], "system": 0})
    assert r.status_code == 400 and "只能指定一个" in r.json()["detail"]
    assert client.post(url, json={"bars": []}).status_code == 400
    assert jobs.jobs.running(sid) is False and store.load(sid)["activity"] is None

    s = store.load(sid)
    ids = [b["id"] for b in s["bars"]]
    assert refine.scope_ids(s) is None  # 整首
    assert refine.scope_ids(s, [ids[4], ids[1]]) == [ids[1], ids[4]]  # 按曲子里的顺序
    assert refine.scope_ids(s, system=1) == ids[3:]

    store.update(sid, lambda x: x["bars"][1].update(placeholder=True))  # 识别失败按休止占位：裁不出来
    r = client.post(url, json={"bars": [ids[1]]})
    assert r.status_code == 400 and "没有可以精修的小节" in r.json()["detail"]
    assert jobs.jobs.running(sid) is False and store.load(sid)["activity"] is None

    gp = client.post("/api/songs", files={"files": ("x.gp5", b"\x18FICHIER GUITAR PRO v5.10")}).json()["id"]
    client.put(f"/api/songs/{gp}/derived", json={"bars": derived_bars(2)})
    gb = client.get(f"/api/songs/{gp}").json()["bars"][0]["id"]
    r = client.post(f"/api/songs/{gp}/refine", json={"bars": [gb]})
    assert r.status_code == 400 and "不需要精修" in r.json()["detail"]


def test_an_explicit_scope_checks_every_bar_in_it(client, sync_jobs, monkeypatch):
    """点了某个小节 / 某一行：手改过、确认过、核对过的也核对；占位的照样跳过。整首精修照旧跳过它们。"""
    sid = _song()
    ids = [b["id"] for b in store.load(sid)["bars"]]
    songs.edit_bar(
        sid, ids[3], [{"dur": "4", "notes": [{"string": 1, "fret": 1}]}], None
    )  # 手改过（改成 1 品）
    songs.review_bar(sid, ids[4], True)  # 确认过没问题

    def mark(x):
        x["bars"][5]["refine_key"] = notes_key(x["bars"][5])  # 核对过
        x["bars"][2]["placeholder"] = True

    store.update(sid, mark)
    s = store.load(sid)
    assert refine.pick(s) == ids[:2]  # 整首：跳过手改过、确认过、核对过、占位的
    assert refine.pick_scope(s, ids[3:]) == ids[3:]
    assert refine.pick_scope(s, ids[:3]) == ids[:2]

    monkeypatch.setattr(FakeClaude, "verify_result", change_fret(1, 9, "应是 9 品"))
    assert client.post(f"/api/songs/{sid}/refine", json={"system": 1}).json() == {"ok": True}
    assert len(_verifies()) == 5  # 3 个小节各一次，改了的 2 个再确认一次
    v = client.get(f"/api/songs/{sid}").json()
    b3, b4 = v["bars"][3], v["bars"][4]
    assert (
        b3["events"][0]["notes"][0]["fret"] == 9
        and b3["refine"]["prev"]["events"][0]["notes"][0]["fret"] == 1
    )
    assert b3["edited"] is True  # 手改过的照样改了，撤销精修恢复成手改的
    assert b4["events"][0]["notes"][0]["fret"] == 9  # 确认过的也改了
    assert "refine" not in v["bars"][5]  # 核对过的也核对了：没问题
    assert "refined" not in store.load(sid) and "refined" not in v  # 不是整首：不算“精修过整首”
    assert (
        v["notice"]["msg"]
        == "第 2 行精修完：检查了 3 个小节，改了 2 个（小节卡片上能看到改了什么，也能撤销）。"
    )
    assert v["activity"] is None and not jobs.jobs.running(sid)

    u = client.put(f"/api/songs/{sid}/bars/{ids[3]}/undo-refine", json={"rev": b3["rev"]}).json()["bars"][3]
    assert u["events"][0]["notes"][0]["fret"] == 1 and u["edited"] is True


def test_bar_scope_short_notice_and_hint(client, sync_jobs, monkeypatch):
    """一个小节：结束提示短（“第 2 小节精修完：没问题 / 改了”）；用户写的“哪里不对”每次核对都带上。"""
    sid = _song()
    ids = [b["id"] for b in store.load(sid)["bars"]]
    url = f"/api/songs/{sid}/refine"
    client.post(url, json={"bars": [ids[1]], "hint": "  第 3 拍是 2 弦 4 品 "})
    calls = _verifies()
    assert len(calls) == 1 and calls[0]["hint"] == "第 3 拍是 2 弦 4 品"
    v = client.get(f"/api/songs/{sid}").json()
    assert v["notice"]["msg"] == "第 2 小节精修完：没问题。" and "refined" not in v

    monkeypatch.setattr(FakeClaude, "verify_result", change_fret(1, 7))
    FakeClaude.calls.clear()
    client.post(url, json={"bars": [ids[1]], "hint": "第 1 拍不对"})  # 已经核对过：点了还是核对
    assert [c["hint"] for c in _verifies()] == ["第 1 拍不对"] * 2  # 提出改动后再确认一遍：也带上
    assert client.get(f"/api/songs/{sid}").json()["notice"]["msg"].startswith("第 2 小节精修完：改了")

    FakeClaude.calls.clear()
    client.post(url)  # 整首：不带说明
    assert len(_verifies()) == 5 + 1 and all(c["hint"] == "" for c in _verifies())
    assert store.load(sid)["refined"]["checked"] == 5  # 第 2 小节核对过、音没再变：整首时不再核对


def test_a_second_refine_while_one_runs_is_423(client):
    sid = _song()
    ids = [b["id"] for b in store.load(sid)["bars"]]
    url = f"/api/songs/{sid}/refine"
    for scope, row, bars, msg in [
        ("bars", None, [ids[1]], "正在精修第 2 小节，完成后再试"),
        ("row", 1, ids[3:], "正在精修第 2 行（3 个小节），完成后再试"),
        ("song", None, ids, "正在精修整首，完成后再试"),
    ]:
        store.update(sid, lambda x, a=scope, r=row, b=bars: refine._begin(x, b, a, r))
        assert jobs.jobs.claim(sid)
        try:
            for body in ({"bars": [ids[0]]}, {"system": 0}, None):
                r = client.post(url, json=body)
                assert r.status_code == 423 and r.json()["detail"] == msg
        finally:
            jobs.jobs.release(sid)
            store.update(sid, lambda x: state.finish(x))
    store.update(sid, lambda x: state.begin(x, "reparse_row", systems=[0]))
    assert jobs.jobs.claim(sid)
    try:
        r = client.post(url, json={"bars": [ids[4]]})
        assert r.status_code == 423 and "正在识别中" in r.json()["detail"]
    finally:
        jobs.jobs.release(sid)


def test_view_shows_which_bars_are_being_refined(client, sync_jobs, monkeypatch):
    """进行中：activity 给出范围（scope / row / bars）和还没核对完的小节（pending），练习页据此显示“精修中”。"""
    sid = _song()
    ids = [b["id"] for b in store.load(sid)["bars"]]
    seen = []
    monkeypatch.setattr(config.settings, "row_concurrency", 1)

    def verify(img, meta, span, events, previous, ts):
        v = views.song_view(store.load(sid))
        model.SongView.model_validate(v)
        seen.append((v["activity"], v["busy"], v["progress"]["done"]))
        return {"diff": "", "changed": False, "events": events, "uncertain": ""}

    monkeypatch.setattr(FakeClaude, "verify_result", verify)
    client.post(f"/api/songs/{sid}/refine", json={"system": 1})
    a = seen[0][0]
    assert a["kind"] == "refine" and a["scope"] == "row" and a["row"] == 1
    assert a["bars"] == ids[3:] and a["pending"] == ids[3:] and seen[0][1] is True
    assert seen[1][0]["pending"] == ids[4:] and seen[2][0]["pending"] == ids[5:]  # 核对完一个去掉一个
    assert [x[2] for x in seen] == [0, 1, 2]
    v = client.get(f"/api/songs/{sid}").json()
    assert v["activity"] is None and v["notice"]["msg"] == "第 2 行精修完：3 个小节都没问题。"


def test_view_gives_estimates_for_a_bar_and_each_row(client):
    """小节卡片上的预估：和整首用同一套算法（$0.2 / 小节，几个小节并行，每次约 75 秒）。"""
    sid = _song()
    v = client.get(f"/api/songs/{sid}").json()
    assert v["refine_scopes"] == {
        "bar": {"bars": 1, "cost_usd": 0.2, "minutes": 2, "seconds": 75},
        "rows": [{"bars": 3, "cost_usd": 0.6, "minutes": 2, "seconds": 75}] * 2,
    }
    assert all(b["refinable"] for b in v["bars"])

    def no_lines(s):
        s["systems"][1]["spans"] = None  # 第 2 行没检测到小节线：裁不出单个小节
        s["bars"][0]["placeholder"] = True

    store.update(sid, no_lines)
    v = client.get(f"/api/songs/{sid}").json()
    assert v["refine_scopes"]["rows"] == [{"bars": 2, "cost_usd": 0.4, "minutes": 2, "seconds": 75}, None]
    assert [b["refinable"] for b in v["bars"]] == [False, True, True, False, False, False]
    assert "refine_scopes" not in store.load(sid)  # 只在接口返回里

    gp = client.post("/api/songs", files={"files": ("x.gp5", b"\x18FICHIER GUITAR PRO v5.10")}).json()["id"]
    client.put(f"/api/songs/{gp}/derived", json={"bars": derived_bars(2)})
    g = client.get(f"/api/songs/{gp}").json()
    assert g["refine_scopes"] is None and not any(b["refinable"] for b in g["bars"])
