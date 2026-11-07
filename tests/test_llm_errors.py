import anthropic
import httpx

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
