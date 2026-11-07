import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture(autouse=True)
def _reset_token():
    yield
    main.app.state.token = None


def _client(monkeypatch, token, host="192.168.1.20"):
    main.app.state.token = token
    return TestClient(main.app, client=(host, 50000), follow_redirects=False)


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
