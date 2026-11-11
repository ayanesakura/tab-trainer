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


def test_requests_carry_the_app_user_agent():
    """网关按 User-Agent 区分调用方：每个请求都带 tab-trainer，不是 SDK 默认的。"""
    from app.llm import USER_AGENT, _http

    sent = []

    def handler(req):
        sent.append(req.headers.get("user-agent"))
        return _sse_response(_reply(["好"]))

    c = Claude(LLMConfig(api_key="sk-test"))
    c.use_fallbacks = False
    assert c.client.default_headers.get("User-Agent") == USER_AGENT == "tab-trainer"
    # 换成假的传输层，其他设置（含 default_headers）照旧
    c.client = c.client.with_options(
        http_client=_http.Client(transport=_http.MockTransport(handler)), max_retries=0
    )
    list(c.coach_stream(META, BAR, [], "怎么弹", []))
    assert sent == [USER_AGENT]


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
    busy = {"type": "error", "error": {"type": "overloaded_error", "message": "overloaded mid-reply"}}
    fails = [_status_error(200, lost), _status_error(200, busy)]

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


def test_errors_the_sdk_already_retried_are_not_retried_again(monkeypatch):
    """建立连接时的 429 / 529 / 5xx、连不上：SDK 自己已经按 max_retries 重试过，这里不再整轮重来（否则一次调用
    最多会发十几个请求）。只有回复传到一半断开才由 _stream 再试。"""
    import anthropic
    import httpx

    from app import llm

    monkeypatch.setattr(llm, "STREAM_RETRY_WAIT", 0)
    c = Claude(LLMConfig(api_key="sk-test"))
    req = httpx.Request("POST", "http://127.0.0.1:3425/v1/messages")
    for err in (
        _status_error(429, {"error": {"type": "rate_limit_error"}}),
        _status_error(529, {"error": {"type": "overloaded_error"}}),
        _status_error(503, {"error": {"type": "api_error"}}),
        anthropic.APIConnectionError(request=req),
    ):
        calls = []

        def once(kwargs, fallbacks=None, err=err, calls=calls):
            calls.append(1)
            raise err

        monkeypatch.setattr(c, "_stream_once", once)
        with pytest.raises(type(err)):
            c._call([], {})
        assert len(calls) == 1, err


# ---------------------------------------------------------------------------
# 流式问答（纯文字）：用假的 SSE 回复测
# ---------------------------------------------------------------------------
def _sse(*events) -> bytes:
    import json

    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


def _reply(
    texts,
    stop="end_turn",
    tin=5,
    tout=7,
    model="claude-opus-5-5",
    fallback_at=None,
    to="claude-sonnet-5-5",
    category=None,
    iterations=None,
):
    """一个完整的流式回复：每段文字一个 text_delta；fallback_at=k：第 k 段之前插一个兜底模型（to）接手的块
    （category：原模型拒绝的类别）。iterations：最后的 message_delta 里带的 usage.iterations（逐次尝试的用量）。"""
    ev = [
        {
            "type": "message_start",
            "message": {
                "id": "m",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": tin, "output_tokens": 1},
            },
        }
    ]
    idx = 0
    ev.append({"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}})
    for k, t in enumerate(texts):
        if k == fallback_at:
            ev.append({"type": "content_block_stop", "index": idx})
            block = {
                "type": "fallback",
                "from": {"model": model},
                "to": {"model": to},
                "trigger": {"type": "refusal", "category": category},
            }
            ev.append({"type": "content_block_start", "index": idx + 1, "content_block": block})
            ev.append({"type": "content_block_stop", "index": idx + 1})
            idx += 2
            ev.append(
                {"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}}
            )
        ev.append({"type": "content_block_delta", "index": idx, "delta": {"type": "text_delta", "text": t}})
    ev.append({"type": "content_block_stop", "index": idx})
    ev.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"output_tokens": tout, **({"iterations": iterations} if iterations else {})},
        }
    )
    ev.append({"type": "message_stop"})
    return _sse(*ev)


def _sse_response(body):
    from app.llm import _http

    stream = _http.ByteStream(body) if isinstance(body, bytes) else body
    return _http.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)


def _mock_client(c, handler):
    import anthropic

    from app.llm import _http

    c.client = anthropic.Anthropic(
        api_key="sk-fake",
        base_url="http://mock",
        max_retries=0,
        http_client=_http.Client(transport=_http.MockTransport(handler)),
    )


META = {"title": "T", "tuning": ["E4", "B3", "G3", "D3", "A2", "E2"], "capo": 0, "time_signature": [4, 4]}
BAR = {"n": 1, "events": [], "tip": ""}


def test_coach_stream_yields_text_and_counts_usage():
    """问答要纯文字（不带 JSON schema），边收边给出片段；用量按最后的消息记，非流式的 coach 拼出同样的回答。"""
    import json

    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        return _sse_response(_reply(["用食指", "按 2 弦"]))

    c = Claude(LLMConfig(api_key="sk-test"))
    c.use_fallbacks = False
    _mock_client(c, handler)
    assert list(c.coach_stream(META, BAR, [], "怎么弹", [])) == ["用食指", "按 2 弦"]
    assert sent[0]["output_config"] == {"effort": "medium"} and sent[0]["max_tokens"] == 8000  # 没有 format
    assert c.usage["input_tokens"] == 5 and c.usage["output_tokens"] == 7
    assert c.usage["cost_usd"] == pytest.approx((5 * 4 + 7 * 20) / 1e6)
    assert c.coach(META, BAR, [], "怎么弹", []) == "用食指按 2 弦"


def test_coach_stream_restarts_when_a_fallback_model_takes_over():
    """服务端换兜底模型从头回答：给出 None（前面的作废），费用按实际回答的模型算。"""
    from app.llm import join_text

    def handler(req):
        assert "server-side-fallback" in req.headers.get("anthropic-beta", "")
        return _sse_response(_reply(["半句", "完整的回答"], fallback_at=1))

    c = Claude(LLMConfig(api_key="sk-test"))
    _mock_client(c, handler)
    got = list(c.coach_stream(META, BAR, [], "怎么弹", []))
    assert got == ["半句", None, "完整的回答"] and join_text(got) == "完整的回答"
    assert c.usage["cost_usd"] == pytest.approx((5 * 2 + 7 * 10) / 1e6)  # Sonnet 的价格


@pytest.mark.parametrize(
    ("texts", "stop", "word"),
    [(["不"], "refusal", "拒绝"), (["很长"], "max_tokens", "截断"), (["  "], "end_turn", "没有给出回答")],
)
def test_coach_stream_failures_match_the_json_calls(texts, stop, word):
    """拒绝、截断、空回答都是 LLMError（片段可能已经给出去了），用量照记。"""
    c = Claude(LLMConfig(api_key="sk-test"))
    c.use_fallbacks = False
    _mock_client(c, lambda req: _sse_response(_reply(texts, stop=stop)))
    with pytest.raises(LLMError, match=word):
        list(c.coach_stream(META, BAR, [], "怎么弹", []))
    assert c.usage["output_tokens"] == 7


def test_coach_stream_drops_unknown_fallbacks_and_retries_transient_errors(monkeypatch):
    """网关不认识 fallbacks 参数：不带它重来；回复到一半断线：重试，重试前给出 None。"""
    from app import llm
    from app.llm import _http

    monkeypatch.setattr(llm, "STREAM_RETRY_WAIT", 0)
    calls = []

    class Broken(_http.SyncByteStream):
        def __iter__(self):
            yield _reply(["前半"]).split(b"event: content_block_stop")[0]
            raise _http.RemoteProtocolError("peer closed connection")

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            err = {
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "unknown field fallbacks"},
            }
            return _http.Response(400, json=err)
        return _sse_response(Broken() if len(calls) == 2 else _reply(["完整"]))

    c = Claude(LLMConfig(api_key="sk-test"))
    _mock_client(c, handler)
    assert list(c.coach_stream(META, BAR, [], "怎么弹", [])) == ["前半", None, "完整"]
    assert not c.use_fallbacks and "fallback" not in calls[2].headers.get("anthropic-beta", "")
    assert c.usage["input_tokens"] == 10  # 断掉的那次也收到了 message_start：它的输入 token 也记上


def test_coach_stream_closed_early_still_counts_usage():
    """调用方中途不读了（生成器被关掉）：已经收到的部分照记用量。"""
    c = Claude(LLMConfig(api_key="sk-test"))
    c.use_fallbacks = False
    _mock_client(c, lambda req: _sse_response(_reply(["一", "二", "三"])))
    g = c.coach_stream(META, BAR, [], "怎么弹", [])
    assert next(g) == "一"
    g.close()
    assert c.usage["input_tokens"] == 5 and c.usage["cost_usd"] > 0


def test_json_calls_count_usage_of_a_reply_broken_mid_stream(monkeypatch):
    """JSON 请求（识别、精修）回复到一半断了：重试成功或最终失败，断掉那次已经收到的部分都记上用量。"""
    from app import llm
    from app.llm import _http

    monkeypatch.setattr(llm, "STREAM_RETRY_WAIT", 0)

    class Broken(_http.SyncByteStream):
        def __iter__(self):
            yield _reply(['{"ok"']).split(b"event: content_block_stop")[0]
            raise _http.RemoteProtocolError("peer closed connection")

    calls = []

    def handler(req):
        calls.append(req)
        return _sse_response(Broken() if len(calls) == 1 else _reply(['{"ok": 1}']))

    c = Claude(LLMConfig(api_key="sk-test"))
    c.use_fallbacks = False
    _mock_client(c, handler)
    assert c._call([], {}) == {"ok": 1}
    assert c.usage["input_tokens"] == 10 and c.usage["output_tokens"] == 1 + 7

    c = Claude(LLMConfig(api_key="sk-test"))
    c.use_fallbacks = False
    _mock_client(c, lambda req: _sse_response(Broken()))
    with pytest.raises(_http.RemoteProtocolError):
        c._call([], {})
    assert c.usage["input_tokens"] == 5 * (llm.STREAM_RETRIES + 1) and c.usage["cost_usd"] > 0


def _iteration(kind, model, tin, tout):
    return {
        "type": kind,
        "model": model,
        "input_tokens": tin,
        "output_tokens": tout,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


def test_json_calls_use_the_fallback_models_answer():
    """服务端换兜底模型：回复里先是原模型写了一半的文字，再是 fallback 块和兜底模型的回答，只解析后者。
    Claude Sonnet 5.5 用 fallbacks: "default" 时兜底到 Claude Sonnet 5（官方文档写明的目标）。费用逐次算：
    写了一半被拒的那次按原模型的价格、兜底的那次按兜底模型的价格（顶层 usage 只是最后那次的）。"""
    c = Claude(LLMConfig(api_key="sk-test", model="claude-sonnet-5-5"))
    its = [
        _iteration("message", "claude-sonnet-5-5", 40, 3),
        _iteration("fallback_message", "claude-sonnet-5", 5, 7),
    ]
    body = _reply(
        ['{"half": ', '{"ok": 2}'],
        model="claude-sonnet-5-5",
        fallback_at=1,
        to="claude-sonnet-5",
        iterations=its,
    )
    _mock_client(c, lambda req: _sse_response(body))
    assert c._call([], {}) == {"ok": 2}
    assert c.usage["input_tokens"] == 45 and c.usage["output_tokens"] == 10
    # Sonnet 5.5 和 Sonnet 5 都是每百万 token 输入 $2、输出 $10
    assert c.usage["cost_usd"] == pytest.approx(((40 + 5) * 2 + (3 + 7) * 10) / 1e6)
    assert c.usage["unpriced_tokens"] == 0 and c.usage["unpriced_models"] == []


def test_fallback_to_a_model_without_a_price_is_recorded_as_unpriced():
    """兜底模型不在价格表里（Claude Opus 5.5 的默认兜底目标官方没有公布，这里用一个价格表里没有的名字）：
    这一次不算 $0，记成“没计价”的 token 并记下是哪个模型；原模型写了一半被拒的那次照常按原模型计价。"""
    c = Claude(LLMConfig(api_key="sk-test"))
    its = [
        _iteration("message", "claude-opus-5-5", 40, 3),
        _iteration("fallback_message", "claude-no-price", 5, 7),
    ]
    body = _reply(
        ['{"half": ', '{"ok": 2}'], fallback_at=1, to="claude-no-price", category="cyber", iterations=its
    )
    _mock_client(c, lambda req: _sse_response(body))
    assert c._call([], {}) == {"ok": 2}
    assert c.usage["cost_usd"] == pytest.approx((40 * 4 + 3 * 20) / 1e6)  # 被拒的那次（Opus 5.5）
    assert c.usage["unpriced_tokens"] == 12 and c.usage["unpriced_models"] == ["claude-no-price"]


@pytest.mark.parametrize(("category", "billed"), [("cyber", False), (None, False), ("bio", True)])
def test_a_refusal_before_any_output_is_billed_only_in_the_billed_categories(category, billed):
    """原模型一个字都没写就拒绝了：官方只对 bio / frontier_llm / reasoning_extraction 这几类收费，
    别的类别（和 null）不收费，token 照样记。"""
    c = Claude(LLMConfig(api_key="sk-test", model="claude-sonnet-5-5"))
    its = [
        _iteration("message", "claude-sonnet-5-5", 40, 0),
        _iteration("fallback_message", "claude-sonnet-5", 5, 7),
    ]
    body = _reply(
        ['{"ok": 2}'],
        model="claude-sonnet-5",
        fallback_at=0,
        to="claude-sonnet-5",
        category=category,
        iterations=its,
    )
    _mock_client(c, lambda req: _sse_response(body))
    assert c._call([], {}) == {"ok": 2}
    assert c.usage["input_tokens"] == 45
    assert c.usage["cost_usd"] == pytest.approx(((40 if billed else 0) * 2 + 5 * 2 + 7 * 10) / 1e6)


def _bad_request(body):
    import anthropic
    import httpx

    req = httpx.Request("POST", "http://127.0.0.1:3425/v1/messages")
    return anthropic.BadRequestError("x", response=httpx.Response(400, request=req, json=body), body=body)


def test_fallbacks_are_dropped_only_when_the_400_is_about_them():
    """400 说的是 fallbacks 参数（错误体里的 param 或 message）才关掉兜底重来；别的参数错误照常报错。"""
    from app.llm import _about_fallbacks

    def err(**e):
        return _bad_request({"type": "error", "error": {"type": "invalid_request_error", **e}})

    assert _about_fallbacks(err(message="fallbacks: Extra inputs are not permitted"))
    assert _about_fallbacks(
        err(message="Unexpected value(s) `server-side-fallback-2026-07-01` for anthropic-beta")
    )
    assert _about_fallbacks(err(message="invalid request", param="fallbacks"))
    assert not _about_fallbacks(err(message="max_tokens: too large", param="max_tokens"))
    assert not _about_fallbacks(err(message="fallback mentioned elsewhere", param="max_tokens"))
    assert not _about_fallbacks(err(message="prompt is too long"))
    assert _about_fallbacks(_bad_request(None)) is False  # 没有错误体：看异常文字（这里是 "x"）

    c = Claude(LLMConfig(api_key="sk-test"))
    assert not c._drop_fallbacks(err(message="prompt is too long"), True) and c.use_fallbacks
    assert c._drop_fallbacks(err(message="unknown field fallbacks"), True) and not c.use_fallbacks


def test_verify_retries_at_the_normal_effort_only_when_truncated(monkeypatch):
    """精修最高精细度输出超长被截断（Truncated）：按设置的精细度再核对一次；拒绝等别的错误照常抛出。"""
    from PIL import Image

    from app.llm import VERIFY_EFFORT, Refused, Truncated

    c = Claude(LLMConfig(api_key="sk-test", effort="high"))
    efforts = []
    errors = [Truncated("输出超出长度上限，被截断")]

    def call(content, schema, effort=None, max_tokens=32000):
        efforts.append(effort)
        if errors:
            raise errors.pop(0)
        return {"changed": False}

    monkeypatch.setattr(c, "_call", call)
    meta = {**META, "notes": ""}
    img = Image.new("RGB", (200, 100), "white")
    assert c.verify_bar(img, meta, (10, 190), []) == {"changed": False}
    assert efforts == [VERIFY_EFFORT, None]
    efforts.clear()
    errors.append(Refused("模型拒绝了这次请求（不是截断）"))
    with pytest.raises(Refused):
        c.verify_bar(img, meta, (10, 190), [])
    assert efforts == [VERIFY_EFFORT]
