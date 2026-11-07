import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture(autouse=True)
def _reset_token():
    yield
    main.app.state.token = None


def _client(monkeypatch, token, host="192.168.1.20"):
    main.app.state.token = token
    return TestClient(
        main.app, base_url="http://127.0.0.1:8765", client=(host, 50000), follow_redirects=False
    )


def test_no_token_needed_when_bound_to_localhost(monkeypatch):
    c = _client(monkeypatch, None)
    assert c.get("/api/songs").status_code == 200


def test_lan_requests_need_the_token(monkeypatch):
    c = _client(monkeypatch, "s3cret")
    assert c.get("/api/songs").status_code == 401
    assert c.get("/api/settings").status_code == 401
    assert c.put("/api/settings", json={"api_key": ""}).status_code == 401
    assert c.delete("/api/songs/whatever").status_code == 401
    r = c.get("/")
    assert r.status_code == 401 and "需要访问令牌" in r.text
    assert c.get("/?token=wrong").status_code == 401
    # 带正确令牌打开一次：写进 Cookie，并跳转到不带令牌的地址
    r = c.get("/trainer?id=x&token=s3cret")
    assert r.status_code == 303 and r.headers["location"].endswith("/trainer?id=x")
    assert c.cookies.get("tt_token") == "s3cret"
    assert c.get("/api/songs").status_code == 200


def test_local_browser_skips_the_token_even_when_lan_is_open(monkeypatch):
    c = _client(monkeypatch, "s3cret", host="127.0.0.1")
    assert c.get("/api/songs").status_code == 200


def test_non_ascii_token_values_are_rejected_not_500(monkeypatch):
    c = _client(monkeypatch, "s3cret")
    assert c.get("/?token=%C3%A9").status_code == 401
    assert c.get("/api/songs?token=%E4%BD%A0").status_code == 401


def test_ipv6_mapped_loopback_is_local(monkeypatch):
    c = _client(monkeypatch, "s3cret", host="::ffff:127.0.0.1")
    assert c.get("/api/songs").status_code == 200


def test_dns_rebinding_hosts_are_rejected(monkeypatch):
    """恶意网页把自己的域名解析到 127.0.0.1：Host 是它的域名，一律拒绝（本机访问也一样）。"""
    c = _client(monkeypatch, None, host="127.0.0.1")
    for host in ("evil.example.com", "evil.example.com:8765", "127.0.0.1.evil.com"):
        assert c.get("/api/settings", headers={"host": host}).status_code == 403, host
    r = c.put(
        "/api/settings", json={"base_url": "https://evil.example.com"}, headers={"host": "evil.example.com"}
    )
    assert r.status_code == 403
    for host in ("127.0.0.1:8765", "localhost:8765", "[::1]:8765", "192.168.1.5:8765", "my-mac.local:8765"):
        assert c.get("/api/songs", headers={"host": host}).status_code == 200, host


def test_cross_site_writes_are_rejected(monkeypatch):
    c = _client(monkeypatch, None, host="127.0.0.1")
    evil = {"origin": "https://evil.example.com"}
    assert c.put("/api/settings", json={"model": "x"}, headers=evil).status_code == 403
    r = c.post("/api/songs", files={"files": ("p.png", b"x")}, headers=evil)
    assert r.status_code == 403 and "其他网站" in r.json()["detail"]
    assert (
        c.post("/api/songs", files={"files": ("p.png", b"x")}, headers={"origin": "null"}).status_code == 403
    )
    assert c.get("/api/songs", headers=evil).status_code == 200  # 读请求浏览器本身会挡（同源策略）
    # 同源页面发的请求、没有 Origin 的客户端（curl）照常
    same = {"origin": "http://127.0.0.1:8765"}
    assert (
        c.post("/api/songs", files={"files": ("p.png", b"x")}, headers=same).status_code == 400
    )  # 到了业务校验
    assert c.post("/api/songs", files={"files": ("p.png", b"x")}).status_code == 400
