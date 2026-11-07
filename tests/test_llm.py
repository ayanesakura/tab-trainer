import pytest

from app.llm import DEFAULT_BASE_URL, Claude, LLMConfig, LLMError


def test_ignores_ambient_gateway_credentials(monkeypatch):
    """环境里有别的工具的 token / 网关地址时，不能被拿来用。"""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "someone-elses-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9/gateway")
    with pytest.raises(LLMError):
        Claude(LLMConfig())

    c = Claude(LLMConfig(api_key="sk-ant-test"))
    assert str(c.client.base_url).rstrip("/") == DEFAULT_BASE_URL
    assert c.client.api_key == "sk-ant-test"
    assert not c.client.auth_token


def test_env_api_key_and_custom_base_url(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    c = Claude(LLMConfig(base_url="https://example.com/proxy"))
    assert c.client.api_key == "sk-ant-env"
    assert str(c.client.base_url).startswith("https://example.com/proxy")


def test_usage_is_counted_exactly_under_concurrency(monkeypatch):
    import threading
    from types import SimpleNamespace

    from app.llm import Claude, LLMConfig

    c = Claude(LLMConfig(api_key="sk-test"))
    msg = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=3, output_tokens=5),
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="{}")],
    )
    monkeypatch.setattr(c, "_stream", lambda kwargs, fallbacks=None: msg)
    threads = [threading.Thread(target=lambda: [c._call([], {}) for _ in range(200)]) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert c.usage == {"input_tokens": 3 * 1600, "output_tokens": 5 * 1600}


def test_request_ids_reach_errors_and_logs(monkeypatch):
    from types import SimpleNamespace

    from app.llm import Claude, LLMConfig, LLMError, request_id_of

    c = Claude(LLMConfig(api_key="sk-test"))
    ok = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="{}")],
        _request_id="req_ok",
    )
    monkeypatch.setattr(c, "_stream", lambda kwargs, fallbacks=None: ok)
    c._call([], {})
    assert c.last_request_id == "req_ok"
    bad = SimpleNamespace(**{**ok.__dict__, "stop_reason": "refusal", "_request_id": "req_no"})
    monkeypatch.setattr(c, "_stream", lambda kwargs, fallbacks=None: bad)
    try:
        c._call([], {})
    except LLMError as e:
        assert request_id_of(e) == "req_no" and "req_no" in str(e)
    assert request_id_of(ValueError("x")) == "none"
