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
    assert c.usage["input_tokens"] == 3 * 1600 and c.usage["output_tokens"] == 5 * 1600
    # 默认模型 Opus：每百万 token 输入 $4、输出 $20
    assert c.usage["cost_usd"] == pytest.approx((3 * 4 + 5 * 20) * 1600 / 1e6)
    assert c.usage["unpriced_tokens"] == 0


def test_unknown_model_counts_unpriced_tokens(monkeypatch):
    from types import SimpleNamespace

    from app.llm import Claude, LLMConfig, price_of

    assert price_of("compass/claude-opus-5-5") == price_of("claude-opus-5-5")
    c = Claude(LLMConfig(api_key="sk-test", model="my-local-model"))
    msg = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=3, output_tokens=5),
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="{}")],
    )
    monkeypatch.setattr(c, "_stream", lambda kwargs, fallbacks=None: msg)
    c._call([], {})
    assert c.usage["cost_usd"] == 0 and c.usage["unpriced_tokens"] == 8


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


def _status_error(status, body):
    import anthropic
    import httpx

    req = httpx.Request("POST", "http://127.0.0.1:3425/v1/messages")
    return anthropic.APIStatusError("x", response=httpx.Response(status, request=req, json=body), body=body)


def test_transient_mid_stream_errors_are_retried(monkeypatch):
    """回复传到一半网关断线（流里的 api_error，状态码 200）：重试，不让整行识别失败。"""
    from types import SimpleNamespace

    from app import llm

    monkeypatch.setattr(llm, "STREAM_RETRY_WAIT", 0)
    c = Claude(LLMConfig(api_key="sk-test"))
    msg = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text='{"ok": 1}')],
    )
    lost = {"type": "error", "error": {"type": "api_error", "message": "connection lost mid-reply"}}
    fails = [_status_error(200, lost), _status_error(529, {"error": {"type": "overloaded_error"}})]

    def stream_once(kwargs, fallbacks=None):
        if fails:
            raise fails.pop(0)
        return msg

    monkeypatch.setattr(c, "_stream_once", stream_once)
    assert c._call([], {}) == {"ok": 1} and not fails

    # 一直断：重试两次后放弃；权限错误之类不重试
    calls = []

    def always(err):
        def f(kwargs, fallbacks=None):
            calls.append(1)
            raise err

        return f

    monkeypatch.setattr(c, "_stream_once", always(_status_error(200, lost)))
    import anthropic

    with pytest.raises(anthropic.APIStatusError):
        c._call([], {})
    assert len(calls) == 3
    calls.clear()
    monkeypatch.setattr(
        c, "_stream_once", always(_status_error(403, {"error": {"type": "permission_error"}}))
    )
    with pytest.raises(anthropic.APIStatusError):
        c._call([], {})
    assert len(calls) == 1
