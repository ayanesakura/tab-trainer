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
