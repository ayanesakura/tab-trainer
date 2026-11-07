"""HTTP 接口：业务错误按类型映射成状态码，上传后只有图片曲子会启动识别。"""

import pytest
from fastapi.testclient import TestClient

from app import config, main, recognize
from tests.fakes import derived_bars, score_png


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
    assert r.status_code == 400 and r.json()["detail"].startswith("小节数据格式不对")
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
    assert v["notice"] is None and v["error"] is None
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
    sid = _image_song(client)
    v = client.get(f"/api/songs/{sid}").json()
    bar = v["bars"][0]
    body = {"events": bar["events"], "tip": "新提示", "rev": v["rev"]}
    r = client.put(f"/api/songs/{sid}/bars/{bar['id']}", json=body)
    assert r.status_code == 200 and r.json()["rev"] == v["rev"] + 1
    r = client.put(f"/api/songs/{sid}/bars/{bar['id']}", json=body)  # 还是旧的 rev
    assert r.status_code == 409 and r.json()["detail"] == "曲子在别处改过了，已刷新，请再改一次"
    r = client.put(f"/api/songs/{sid}/meta", json={"title": "x", "rev": v["rev"]})
    assert r.status_code == 409
    r = client.put(f"/api/songs/{sid}/meta", json={"title": "x", "rev": v["rev"] + 1})
    assert r.status_code == 200 and r.json()["title"] == "x"
    assert client.put(f"/api/songs/{sid}/meta", json={"title": "y"}).status_code == 200  # 不带 rev 照旧


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
    assert row["last_practiced"] == row["practice"]["last_at"]
    assert client.get("/api/songs/20260101-000000-abcdef/practice/history").status_code == 404
