"""HTTP 接口：业务错误按类型映射成状态码，上传后只有图片曲子会启动识别。"""

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import config, main, recognize
from tests.fakes import FakeClaude, derived_bars, score_png


@pytest.fixture
def client(data_dir, monkeypatch):
    started = []
    monkeypatch.setattr(recognize, "start_parse", lambda sid: started.append(sid) or True)
    c = TestClient(main.app, base_url="http://127.0.0.1:8765")
    c.started = started
    return c


def test_upload_starts_recognition_only_for_images(client):
    img = client.post("/api/songs", files={"files": ("p1.png", score_png())}).json()["id"]
    gp = client.post("/api/songs", files={"files": ("x.gp5", b"\x18FICHIER GUITAR PRO v5.10")}).json()["id"]
    assert client.started == [img]
    assert client.get(f"/api/songs/{gp}").json()["status"] == "imported"


def test_upload_rejects_bad_files(client):
    r = client.post("/api/songs", files={"files": ("x.gp5", b"not a score")})
    assert r.status_code == 400 and "不是有效" in r.json()["detail"]
    r = client.post("/api/songs", files={"files": ("p.png", b"not an image")})
    assert r.status_code == 400 and "文件无法读取" in r.json()["detail"]
    tiny = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(tiny, "PNG")
    r = client.post("/api/songs", files={"files": ("p.png", tiny.getvalue())})
    assert r.status_code == 400 and "太小" in r.json()["detail"]
    assert client.started == []


def _image_song(client):
    sid = client.post("/api/songs", files={"files": ("p1.png", score_png())}).json()["id"]
    recognize.run_parse(sid)
    return sid


def test_domain_errors_map_to_status_codes(client):
    sid = client.post("/api/songs", files={"files": ("x.gp5", b"\x18FICHIER GUITAR PRO v5.10")}).json()["id"]
    assert client.put(f"/api/songs/{sid}/derived", json={"bars": derived_bars(2)}).status_code == 200
    gp_bar = client.get(f"/api/songs/{sid}").json()["bars"][0]["id"]
    r = client.put(f"/api/songs/{sid}/bars/{gp_bar}", json={"events": []})
    assert r.status_code == 400 and "来自原文件" in r.json()["detail"]  # GP 曲子的小节不能手改

    img = _image_song(client)
    r = client.put(f"/api/songs/{img}/bars/nope", json={"events": []})
    assert r.status_code == 404 and "小节不存在" in r.json()["detail"]
    bar_id = client.get(f"/api/songs/{img}").json()["bars"][0]["id"]
    r = client.put(f"/api/songs/{img}/bars/{bar_id}", json={"events": [{"dur": "x"}]})
    assert r.status_code == 400 and "时值必须是大于 0 的分数" in r.json()["detail"]  # model.Event 检查
    assert client.put(f"/api/songs/{sid}/meta", json={"capo": 99}).status_code == 400
    assert client.post(f"/api/songs/{sid}/practice", json={}).status_code == 400
    assert client.post(f"/api/songs/{sid}/cancel").status_code == 409
    assert client.get("/api/songs/20260101-000000-abcdef").status_code == 404


def _pdf(pages: int) -> bytes:
    import fitz

    doc = fitz.open()
    for _ in range(pages):
        doc.new_page().insert_text((72, 72), "tab")
    return doc.tobytes()


def test_upload_limits(client, monkeypatch):
    st = config.settings
    monkeypatch.setattr(st, "max_files", 2)
    r = client.post("/api/songs", files=[("files", (f"p{i}.png", score_png())) for i in range(3)])
    assert r.status_code == 400 and "最多上传 2 个" in r.json()["detail"]

    monkeypatch.setattr(st, "max_file_bytes", 1000)
    r = client.post("/api/songs", files={"files": ("big.png", score_png())})
    assert r.status_code == 400 and "太大" in r.json()["detail"]
    monkeypatch.setattr(st, "max_file_bytes", 50 * 1024 * 1024)

    monkeypatch.setattr(st, "max_pages", 2)
    r = client.post("/api/songs", files={"files": ("x.pdf", _pdf(3))})
    assert r.status_code == 400 and "最多 2 页" in r.json()["detail"]
    r = client.post("/api/songs", files=[("files", ("x.pdf", _pdf(2))), ("files", ("p.png", score_png()))])
    assert r.status_code == 400 and "最多 2 页" in r.json()["detail"]
    assert client.post("/api/songs", files={"files": ("x.pdf", _pdf(2))}).status_code == 200

    monkeypatch.setattr(st, "max_pixels", 1000 * 1000)  # 合成谱页是 1200×900
    r = client.post("/api/songs", files={"files": ("p.png", score_png())})
    assert r.status_code == 400 and "分辨率太大" in r.json()["detail"]
    assert len(client.started) == 1  # 只有那份 2 页的 PDF 开始了识别
    from app import store

    assert len(list(store.SONGS.iterdir())) == 1  # 被拒的上传没有留下半成品目录


def test_derived_with_absurd_numbers_is_cleaned_not_a_500(client):
    """练习页转换出的数据里有超大的数（1e400 解析成 inf）、类型不对的字段：和别的坏值一样处理
    （坏小节换成休止并写明原因、谱头用默认值、反复记号不要），不能 500。"""
    sid = client.post("/api/songs", files={"files": ("x.gp5", b"\x18FICHIER GUITAR PRO v5.10")}).json()["id"]
    bad_bars = (
        '[{"events": [{"dur": "4", "notes": [{"string": 1, "fret": 1e400}]}]},'
        ' {"events": [{"dur": "4", "notes": 5}]},'
        ' {"repeat_end": 1e400, "endings": [1e400], "directions": [[1]], "events": []}]'
    )
    for meta in ('{"capo": 1e400, "tempo": 1e400}', '{"tuning": 5}', '{"time_signature": 5}'):
        r = client.put(
            f"/api/songs/{sid}/derived",
            content=f'{{"meta": {meta}, "bars": {bad_bars}}}',
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 200, (meta, r.status_code, r.text)
        song = r.json()
        assert song["meta"]["tuning"] == ["E4", "B3", "G3", "D3", "A2", "E2"]
        assert [b["warn"][0]["code"] for b in song["bars"][:2]] == ["other", "other"]
        assert "无法使用" in song["bars"][0]["warn"][0]["msg"]
        assert "repeat_end" not in song["bars"][2] and "endings" not in song["bars"][2]


def test_oversized_request_is_rejected_before_parsing(client, monkeypatch):
    monkeypatch.setattr(config.settings, "max_total_bytes", 1000)
    r = client.post("/api/songs", files={"files": ("p.png", b"x" * 3_000_000)})
    assert r.status_code == 413


def test_malformed_bodies_get_a_400_with_a_chinese_reason(client):
    sid = client.post("/api/songs", files={"files": ("x.gp5", b"\x18FICHIER GUITAR PRO v5.10")}).json()["id"]
    assert client.put(f"/api/songs/{sid}/derived", json={"bars": derived_bars(2)}).status_code == 200
    bar = client.get(f"/api/songs/{sid}").json()["bars"][0]["id"]
    cases = [
        ("put", "/api/settings", {"api_key": ["not", "a", "string"]}, "API key"),
        ("put", "/api/settings", {"effort": "turbo"}, "识别精细度"),
        ("put", f"/api/songs/{sid}/bars/{bar}", {"tip": "x"}, "缺少"),
        ("put", f"/api/songs/{sid}/bars/{bar}", {"events": "abc"}, "events"),
        ("put", f"/api/songs/{sid}/meta", {"capo": "三"}, "变调夹"),
        ("put", f"/api/songs/{sid}/derived", {"track": -1}, "音轨"),
        ("post", f"/api/songs/{sid}/practice", {"bar_ids": "b1", "loops": 1, "bpm": 60}, "小节"),
        ("post", f"/api/songs/{sid}/bars/{bar}/ask", {"question": "x" * 501}, "太长"),
        ("post", f"/api/songs/{sid}/bars/{bar}/ask", {"question": "   "}, "问题不能为空"),
        ("post", f"/api/songs/{sid}/practice", {"bar_ids": [bar], "bpm": 0}, "速度"),
        ("post", f"/api/songs/{sid}/practice", {"bar_ids": [bar], "bpm": 60, "loops": -1}, "遍数"),
        ("post", f"/api/songs/{sid}/practice", {"bar_ids": [bar], "bpm": "x"}, "速度"),
        ("post", f"/api/songs/{sid}/practice", {"bar_ids": [], "bpm": 60}, "小节"),
        ("put", f"/api/songs/{sid}/tags", {"tags": [f"t{i}" for i in range(13)]}, "最多 12 个"),
        ("put", f"/api/songs/{sid}/tags", {"tags": ["x" * 21]}, "最长 20"),
        (
            "put",
            f"/api/songs/{sid}/bars/{bar}",
            {"events": [{"dur": "1", "notes": [{"string": 9, "fret": 0}]}]},
            "超出范围",
        ),
    ]
    for method, url, body, word in cases:
        r = getattr(client, method)(url, json=body)
        assert r.status_code == 400, (url, body, r.status_code, r.text)
        assert r.json()["detail"].startswith("请求格式不对") and word in r.json()["detail"], r.json()
    # 正常的请求照常
    assert (
        client.put("/api/settings", json={"model": "claude-x", "effort": "high"}).json()["model"]
        == "claude-x"
    )
    r = client.post(
        f"/api/songs/{sid}/practice", json={"bar_ids": [bar], "loops": 1, "bpm": 61.5, "seconds": 9}
    )
    assert r.status_code == 200
    assert client.put(f"/api/songs/{sid}/meta", json={"title": "新名"}).json()["title"] == "新名"


def test_upload_decode_errors_are_400_but_other_errors_are_not(client, monkeypatch):
    """图片 / PDF 解码失败是用户的问题（400）；其他意外（磁盘满了之类）不再一律当成 400。"""
    r = client.post("/api/songs", files={"files": ("x.pdf", b"%PDF-1.7 garbage")})
    assert r.status_code == 400 and "文件无法读取" in r.json()["detail"]
    from app import songs

    def disk_full(*a, **kw):
        raise OSError("No space left on device")

    monkeypatch.setattr(songs.Image.Image, "save", disk_full)
    with pytest.raises(OSError) as e:  # 不是 errors.Invalid：接口返回 500，不会说成“文件无法读取”
        songs.create_song([("p1.png", score_png())])
    assert not isinstance(e.value, ValueError)


def test_settings_endpoints(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    s = client.get("/api/settings").json()
    assert s == {
        "api_key_masked": "",
        "env_key": False,
        "base_url": "",
        "model": s["model"],
        "effort": "high",
    }
    s = client.put(
        "/api/settings", json={"api_key": " sk-ant-1234567890abcdef ", "base_url": "http://gw"}
    ).json()
    assert s["api_key_masked"] == "sk-ant-…cdef" and s["base_url"] == "http://gw"
    from app import store

    assert store.load_config()["api_key"] == "sk-ant-1234567890abcdef"  # 去掉首尾空白，原样存
    assert oct(store.CONFIG.stat().st_mode & 0o777) == "0o600"  # 只有自己能读
    assert client.post("/api/settings/test").json() == {"ok": True}  # 替身模型
    s = client.put("/api/settings", json={"clear_api_key": True, "base_url": None}).json()
    assert s["api_key_masked"] == "" and s["base_url"] == ""
    from app import llm

    monkeypatch.setattr(config, "llm_factory", llm.Claude)
    r = client.post("/api/settings/test").json()
    assert r["ok"] is False and "API key" in r["error"]


def test_song_view_contract(client):
    """GET /api/songs/{id} 和 /api/songs 给前端的字段（见 README 的接口约定）。"""
    from app import model

    sid = _image_song(client)
    v = client.get(f"/api/songs/{sid}").json()
    model.SongView.model_validate(v)
    assert v["rev"] >= 1 and v["busy"] is False and v["recognizing"] is False and v["activity"] is None
    assert v["can_edit_bars"] is True and v["editable_meta"] == [
        "title",
        "artist",
        "tuning",
        "capo",
        "time_signature",
    ]
    assert v["notice"] is None
    assert not {"error", "running", "kind", "job"} & set(v)  # 兼容字段已删
    b = v["bars"][0]
    assert b["n"] == 1 and b["locked"] is None and b["edited"] is False and b["needs_review"] is False
    assert not any(k.startswith("_") for x in v["bars"] for k in x) and not any(k.startswith("_") for k in v)
    assert "meta_user" not in v and v["meta"]["title"] == v["title"] and "tuning_midi" in v["meta"]
    assert v["practice"] == {"bars": {}, "last": None, "total_seconds": 0}
    row = next(x for x in client.get("/api/songs").json() if x["id"] == sid)
    model.LibraryItem.model_validate(row)
    assert row["busy"] is False and row["needs_review_bars"] == 0 and row["practice"] is None

    gp = client.post("/api/songs", files={"files": ("x.gp5", b"\x18FICHIER GUITAR PRO v5.10")}).json()["id"]
    client.put(f"/api/songs/{gp}/derived", json={"bars": derived_bars(2)})
    v = client.get(f"/api/songs/{gp}").json()
    assert v["can_edit_bars"] is False and v["editable_meta"] == ["title"]
    assert all("原文件" in b["locked"] for b in v["bars"])


def test_rev_conflict_on_bar_and_meta(client):
    """乐观锁按对象：改小节比这个小节的 rev，改谱头比 meta_rev（顶层 rev 只是保存计数）。"""
    sid = _image_song(client)
    v = client.get(f"/api/songs/{sid}").json()
    bar, other = v["bars"][0], v["bars"][1]
    assert bar["rev"] == 0 and v["meta_rev"] >= 1  # 第一次识别读出谱头
    body = {"events": bar["events"], "tip": "新提示", "rev": bar["rev"]}
    r = client.put(f"/api/songs/{sid}/bars/{bar['id']}", json=body)
    assert r.status_code == 200 and r.json()["bars"][0]["rev"] == 1 and r.json()["rev"] == v["rev"] + 1
    assert r.json()["bars"][1]["rev"] == 0 and r.json()["meta_rev"] == v["meta_rev"]  # 别的没动
    r = client.put(f"/api/songs/{sid}/bars/{bar['id']}", json=body)  # 还是旧的 rev
    assert r.status_code == 409 and r.json()["detail"] == "曲子在别处改过了，已刷新，请再改一次"
    # 另一个小节、谱头不受影响：拿着改之前的版本号照样能改
    r = client.put(
        f"/api/songs/{sid}/bars/{other['id']}", json={"events": [{"dur": "4"}], "rev": other["rev"]}
    )
    assert r.status_code == 200 and r.json()["bars"][1]["rev"] == 1
    r = client.put(f"/api/songs/{sid}/meta", json={"title": "x", "rev": v["meta_rev"]})
    assert r.status_code == 200 and r.json()["meta_rev"] == v["meta_rev"] + 1
    r = client.put(f"/api/songs/{sid}/meta", json={"title": "z", "rev": v["meta_rev"]})  # 旧的 meta_rev
    assert r.status_code == 409
    r = client.put(f"/api/songs/{sid}/meta", json={"title": "x", "rev": v["meta_rev"] + 1})  # 没变：不加
    assert r.status_code == 200 and r.json()["meta_rev"] == v["meta_rev"] + 1
    assert client.put(f"/api/songs/{sid}/meta", json={"title": "y"}).status_code == 200  # 不带 rev 照旧
    assert client.put(f"/api/songs/{sid}/bars/{bar['id']}", json=body | {"rev": None}).status_code == 200


def test_background_writes_do_not_invalidate_the_edit_rev(client, sync_jobs, monkeypatch):
    """问 AI、讲解补提示、练习记录、改别的小节 / 识别别的行都会保存曲子，但不改这个小节的内容：
    前端拿着之前的 bar.rev / meta_rev 接着改，不应该 409。"""
    from app import coach, store

    sid = _image_song(client)
    store.update(sid, lambda s: [b.update(tip="") for b in s["bars"][:3]])  # 让讲解有活干
    v = client.get(f"/api/songs/{sid}").json()
    b0, b1, b4 = v["bars"][0], v["bars"][1], v["bars"][4]

    r = client.post(f"/api/songs/{sid}/bars/{b0['id']}/ask", json={"question": "怎么弹？"})
    assert r.status_code == 200
    r = client.post(
        f"/api/songs/{sid}/practice", json={"bar_ids": [b1["id"]], "loops": 1, "bpm": 60, "seconds": 5}
    )
    assert r.status_code == 200
    coach.start_explain(sid)
    after = store.load(sid)
    assert after["bars"][1]["tip"] == "讲解 2" and after["rev"] > v["rev"]
    recognize.reparse_system(sid, 1)  # 第二行重新识别（结果一样）
    client.put(f"/api/songs/{sid}/bars/{b4['id']}", json={"events": [{"dur": "4"}], "rev": b4["rev"]})

    r = client.put(f"/api/songs/{sid}/bars/{b1['id']}", json={"events": [{"dur": "4"}], "rev": b1["rev"]})
    assert r.status_code == 200, r.json()
    r = client.put(f"/api/songs/{sid}/meta", json={"artist": "某人", "rev": v["meta_rev"]})
    assert r.status_code == 200, r.json()


def test_practice_history_and_last_positions(client):
    sid = _image_song(client)
    ids = [b["id"] for b in client.get(f"/api/songs/{sid}").json()["bars"]]
    client.post(
        f"/api/songs/{sid}/practice", json={"bar_ids": ids[1:4], "loops": 2, "bpm": 60, "seconds": 30}
    )
    client.post(
        f"/api/songs/{sid}/practice", json={"bar_ids": [ids[4]], "loops": 0, "bpm": 72.4, "seconds": 5}
    )
    h = client.get(f"/api/songs/{sid}/practice/history").json()
    assert [(x["a"], x["b"], x["bpm"], x["loops"], x["seconds"]) for x in h["sessions"]] == [
        (ids[1], ids[3], 60, 2, 30),
        (ids[4], ids[4], 72, 0, 5),
    ]
    v = client.get(f"/api/songs/{sid}").json()
    assert v["practice"]["last"] == {
        "a": ids[4],
        "b": ids[4],
        "bpm": 72,
        "at": h["sessions"][1]["at"],
        "a_n": 5,
        "b_n": 5,
    }
    row = next(x for x in client.get("/api/songs").json() if x["id"] == sid)
    assert row["practice"]["total_seconds"] == 35 and row["practice"]["last"]["a_n"] == 5
    assert not {"error", "warn_bars", "last_practiced", "kind", "job"} & set(row)
    assert client.get("/api/songs/20260101-000000-abcdef/practice/history").status_code == 404


def test_song_view_while_recognizing(client):
    from app import store

    sid = _image_song(client)

    def row_parsing(s):
        s["systems"][1]["status"] = "parsing"
        s["activity"] = {"kind": "reparse_row", "restaging": False, "systems": [1]}

    store.update(sid, row_parsing)
    v = client.get(f"/api/songs/{sid}").json()
    assert v["busy"] and v["recognizing"] and v["activity"]["systems"] == [1]
    assert v["editable_meta"] == ["title", "artist"]
    assert [b["locked"] is not None for b in v["bars"]] == [False] * 3 + [True] * 3
    store.update(sid, lambda s: s.update(activity={"kind": "explain", "restaging": False, "systems": []}))
    store.update(sid, lambda s: s["systems"][1].update(status="done"))
    v = client.get(f"/api/songs/{sid}").json()
    assert v["busy"] and not v["recognizing"] and all(b["locked"] is None for b in v["bars"])
    row = next(x for x in client.get("/api/songs").json() if x["id"] == sid)
    assert row["busy"] and not row["recognizing"] and row["activity"]["kind"] == "explain"
    store.update(sid, lambda s: s.update(activity={"kind": "parse", "restaging": True, "systems": []}))
    v = client.get(f"/api/songs/{sid}").json()
    assert v["status"] == "ready" and v["recognizing"] and all("整首" in b["locked"] for b in v["bars"])
    # 警告和不确定说明 → needs_review
    store.update(sid, lambda s: (s.update(activity=None), s["bars"][0].update(uncertain="看不清")))
    store.update(sid, lambda s: s["bars"][1].update(events=[{"dur": "1", "notes": []}]))
    v = client.get(f"/api/songs/{sid}").json()
    assert [b["needs_review"] for b in v["bars"][:3]] == [True, True, False]
    assert v["bars"][1]["warn"][0]["code"] == "length"
    assert next(x for x in client.get("/api/songs").json() if x["id"] == sid)["needs_review_bars"] == 2


def test_job_and_file_routes_over_http(data_dir, sync_jobs):
    """整首 / 单行重新识别、取消、原文件和页面图片：走一遍真正的 HTTP 路由（不替换 start_parse）。"""
    c = TestClient(main.app, base_url="http://127.0.0.1:8765")
    sid = c.post("/api/songs", files={"files": ("p1.png", score_png())}).json()["id"]
    s = c.get(f"/api/songs/{sid}").json()
    assert s["status"] == "ready" and len(s["bars"]) == 6 and s["failed_rows"] == 0  # 上传后同步识别完了

    FakeClaude.calls = []
    assert c.post(f"/api/songs/{sid}/parse").json() == {"ok": True}
    assert len(FakeClaude.calls) >= 2 and c.get(f"/api/songs/{sid}").json()["status"] == "ready"

    FakeClaude.calls = []
    r = c.post(f"/api/songs/{sid}/systems/1/reparse", json={"hint": "第 4 小节第 2 拍是 2 弦"})
    assert r.json() == {"ok": True} and {x["hint"] for x in FakeClaude.calls} == {"第 4 小节第 2 拍是 2 弦"}
    assert c.post(f"/api/songs/{sid}/systems/9/reparse").status_code == 404
    assert c.post(f"/api/songs/{sid}/cancel").status_code == 409  # 没有在跑的任务

    page = s["pages"][0]["file"]
    r = c.get(f"/files/{sid}/{page}")
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    assert c.get(f"/files/{sid}/..%2Fsong.json").status_code == 404
    assert c.get(f"/files/{sid}/source").status_code == 404  # 图片曲子没有原文件

    gp = c.post("/api/songs", files={"files": ("x.gp5", b"\x18FICHIER GUITAR PRO v5.10")}).json()["id"]
    r = c.get(f"/files/{gp}/source")
    assert r.status_code == 200 and r.content.startswith(b"\x18FICHIER")
    assert c.post(f"/api/songs/{gp}/parse").status_code == 400  # GP 曲子不用识别
    assert c.get(f"/api/songs/{gp}").json()["is_score"]
