"""HTTP 接口：业务错误按类型映射成状态码，上传后只有图片曲子会启动识别。"""

import pytest
from fastapi.testclient import TestClient

from app import main, recognize
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


def test_domain_errors_map_to_status_codes(client):
    sid = client.post("/api/songs", files={"files": ("x.gp5", b"\x18FICHIER GUITAR PRO v5.10")}).json()["id"]
    assert client.put(f"/api/songs/{sid}/derived", json={"bars": derived_bars(2)}).status_code == 200

    r = client.put(f"/api/songs/{sid}/bars/nope", json={"events": []})
    assert r.status_code == 404 and "小节不存在" in r.json()["detail"]
    bar_id = client.get(f"/api/songs/{sid}").json()["bars"][0]["id"]
    r = client.put(f"/api/songs/{sid}/bars/{bar_id}", json={"events": [{"dur": "x"}]})
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
    from app import songs

    monkeypatch.setattr(songs, "MAX_FILES", 2)
    r = client.post("/api/songs", files=[("files", (f"p{i}.png", score_png())) for i in range(3)])
    assert r.status_code == 400 and "最多上传 2 个" in r.json()["detail"]

    monkeypatch.setattr(songs, "MAX_FILE_BYTES", 1000)
    r = client.post("/api/songs", files={"files": ("big.png", score_png())})
    assert r.status_code == 400 and "太大" in r.json()["detail"]
    monkeypatch.setattr(songs, "MAX_FILE_BYTES", 50 * 1024 * 1024)

    monkeypatch.setattr(songs, "MAX_PAGES", 2)
    r = client.post("/api/songs", files={"files": ("x.pdf", _pdf(3))})
    assert r.status_code == 400 and "最多 2 页" in r.json()["detail"]
    r = client.post("/api/songs", files=[("files", ("x.pdf", _pdf(2))), ("files", ("p.png", score_png()))])
    assert r.status_code == 400 and "最多 2 页" in r.json()["detail"]
    assert client.post("/api/songs", files={"files": ("x.pdf", _pdf(2))}).status_code == 200

    monkeypatch.setattr(songs, "MAX_PIXELS", 1000 * 1000)  # 合成谱页是 1200×900
    r = client.post("/api/songs", files={"files": ("p.png", score_png())})
    assert r.status_code == 400 and "分辨率太大" in r.json()["detail"]
    assert len(client.started) == 1  # 只有那份 2 页的 PDF 开始了识别
    from app import store

    assert len(list(store.SONGS.iterdir())) == 1  # 被拒的上传没有留下半成品目录


def test_oversized_request_is_rejected_before_parsing(client, monkeypatch):
    from app import songs

    monkeypatch.setattr(songs, "MAX_TOTAL_BYTES", 1000)
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
        ("post", f"/api/songs/{sid}/bars/{bar}/ask", {"question": "x" * 2000}, "太长"),
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
