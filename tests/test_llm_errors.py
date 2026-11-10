import anthropic
import httpx
import pytest

from app import llm


def _status_error(cls, status: int, body):
    req = httpx.Request("POST", "http://127.0.0.1:3425/v1/messages")
    resp = httpx.Response(status, request=req, json=body)
    return cls(f"Error code: {status} - {body!r}", response=resp, body=body)


def test_403_shows_upstream_reason():
    """网关把请求转给只接受 Claude Code 的渠道时会 403：要把原因给用户看，而不是只说 key 没权限。"""
    body = {
        "type": "error",
        "error": {
            "type": "permission_error",
            "message": "4router.net: this channel only serves official Claude Code CLI requests",
        },
    }
    msg = llm.friendly(_status_error(anthropic.PermissionDeniedError, 403, body))
    assert "4router.net: this channel only serves official Claude Code CLI requests" in msg
    assert "403" in msg


def test_403_without_reason_falls_back_to_permission_hint():
    msg = llm.friendly(_status_error(anthropic.PermissionDeniedError, 403, None))
    assert "权限" in msg


def test_upstream_reason_is_capped_and_only_uses_message_field():
    body = {"error": {"message": "x" * 1000}, "request": {"api_key": "sk-ant-secret"}}
    reason = llm.upstream_reason(_status_error(anthropic.PermissionDeniedError, 403, body))
    assert len(reason) <= llm.REASON_MAX
    assert "sk-ant" not in reason


def test_connection_dropped_mid_stream_is_retried_and_explained(monkeypatch):
    """回复传到一半连接断开：SDK 不包装，抛的是它自带的 httpx 的 RemoteProtocolError / ReadError 等。
    要当临时错误重试，最后还失败时给中文说明，而不是原样的英文异常。"""
    from app.llm import _http

    calls = []

    class Body(_http.SyncByteStream):
        def __iter__(self):
            yield (
                b'event: message_start\ndata: {"type":"message_start","message":{"id":"m","type":"message",'
                b'"role":"assistant","model":"claude-opus-5-5","content":[],"stop_reason":null,'
                b'"stop_sequence":null,"usage":{"input_tokens":5,"output_tokens":1}}}\n\n'
            )
            raise _http.RemoteProtocolError("peer closed connection without sending complete message body")

    def handler(req):
        calls.append(1)
        return _http.Response(200, headers={"content-type": "text/event-stream"}, stream=Body())

    monkeypatch.setattr(llm, "STREAM_RETRY_WAIT", 0)
    c = llm.Claude(llm.LLMConfig(api_key="sk-fake", base_url="http://mock"))
    c.client = anthropic.Anthropic(
        api_key="sk-fake",
        base_url="http://mock",
        max_retries=0,
        http_client=_http.Client(transport=_http.MockTransport(handler)),
    )
    kwargs = {"model": "claude-opus-5-5", "max_tokens": 10, "messages": [{"role": "user", "content": "x"}]}
    with pytest.raises(_http.RemoteProtocolError) as err:
        c._stream(kwargs, fallbacks=False)
    assert len(calls) == llm.STREAM_RETRIES + 1
    assert (
        llm.friendly(err.value)
        == "和模型服务的连接中断了，请稍后重试（网络不稳定时可以检查网络或 Base URL）。"
    )
    for e in (_http.ReadError("x"), _http.ReadTimeout("x")):
        assert llm._dropped_mid_stream(e) and "连接中断" in llm.friendly(e)
