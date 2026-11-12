"""首页的“问 AI”对话抽屉：开关、流式回答、回答中不能重复发送、曲子链接、多轮和保存、新对话、出错、没有 key。"""

import json
import urllib.request

import pytest

from tests.e2e.test_library import _fake_key

pytestmark = pytest.mark.e2e


def _api(server, path, method="GET"):
    req = urllib.request.Request(server["base"] + path, method=method)
    return json.loads(urllib.request.urlopen(req).read())


@pytest.fixture
def fake_model(server, monkeypatch):
    from app import config
    from tests.fakes import FakeClaude

    monkeypatch.setattr(config, "llm_factory", FakeClaude)
    _api(server, "/api/coach", "DELETE")
    yield FakeClaude
    _api(server, "/api/coach", "DELETE")


def _open_chat(b, server, view="library"):
    _fake_key(b, True)
    b.open(f"{server['base']}/#{view}")
    b.wait_for("document.querySelectorAll('.song-row').length >= 2")
    b.js("document.getElementById('chatBtn').click(); true")
    b.wait_for("document.body.classList.contains('chat-open') && !document.getElementById('chat').inert")


def test_chat_drawer_streams_answers_and_keeps_them(server, browser, fake_model, monkeypatch):
    """点“问 AI”从右边滑出（不是模态，页面照常能点）；点示例问题，回答一段段出来，回答中不能再发；
    回答里的曲子换成练习页链接；Esc 收起再打开、刷新页面，对话都还在；新对话清空。"""
    b = browser
    img = server["image"]
    monkeypatch.setattr(fake_model, "stream_delay", 0.3)
    monkeypatch.setattr(fake_model, "chat_answer", f"先练 [[{img}|E2E 图片曲]]，再看 [[nope-0000|编的]]。")
    _open_chat(b, server)
    assert b.js("document.getElementById('chatBtn').getAttribute('aria-expanded')") == "true"
    assert b.js("document.activeElement.id") == "chatInput"
    assert b.js("document.querySelectorAll('#chatEmpty .chat-examples button').length") == 4
    assert b.js("document.getElementById('chatNew').disabled") is True
    # 不是模态：曲库的搜索框照样能用，抽屉不收
    b.js("document.getElementById('search').focus(); true")
    assert b.js("document.activeElement.id") == "search" and b.js(
        "document.body.classList.contains('chat-open')"
    )

    calls = len(fake_model.calls)
    b.js("document.querySelector('#chatEmpty .chat-examples button').click(); true")
    pending = "(document.querySelector('#chatList .qa.pending .a') || {}).textContent || ''"
    b.wait_for(f"{pending}.startsWith('先练')")
    assert b.js("document.getElementById('chatEmpty').hidden") is True
    assert b.js("document.getElementById('chatSend').disabled") is True
    assert "回答中" in b.js("document.getElementById('chatSend').textContent")
    assert "[[" not in b.js(pending)  # 半截标记不显示
    # 回答中按回车不会再发一个
    b.js("const i = document.getElementById('chatInput'); i.value = '再问一个'; i.focus(); true")
    b.key("Enter", vk=13)
    assert b.js("document.getElementById('chatInput').value") == "再问一个"

    b.wait_for("!document.querySelector('#chatList .qa.pending')", timeout=30)
    assert len(fake_model.calls) == calls + 1
    assert b.js(
        "[...document.querySelectorAll('#chatList .qa .q > span:not(.t):not(.ic)')].map(x => x.textContent)"
    ) == ["今天先练哪首？"]
    assert (
        b.js("document.querySelector('#chatList a.song-link').getAttribute('href')") == f"/trainer?id={img}"
    )
    assert b.js("document.querySelector('#chatList a.song-link').textContent") == "E2E 图片曲"
    assert b.js("document.querySelector('#chatList .song-ref').textContent") == "编的"
    assert b.js("document.getElementById('chatSend').disabled") is False
    assert "$" in b.js("document.getElementById('chatTitle').title")
    saved = _api(server, "/api/coach")
    assert [t["q"] for t in saved["turns"]] == ["今天先练哪首？"]

    # 第二个问题：回车发送，带上前面的问答
    monkeypatch.setattr(fake_model, "stream_delay", 0)
    b.js("document.getElementById('chatInput').focus(); true")
    b.key("Enter", vk=13)
    b.wait_for("document.querySelectorAll('#chatList .qa:not(.pending)').length === 2")
    assert [h["q"] for h in fake_model.calls[-1]["history"]] == ["今天先练哪首？"]
    assert b.js("document.getElementById('chatInput').value") == ""

    # Esc 收起（焦点回到按钮），再打开对话还在；切到练琴页也能用
    b.key("Escape", vk=27)
    b.wait_for("!document.body.classList.contains('chat-open')")
    assert b.js("document.getElementById('chat').inert") is True
    b.js("location.hash = 'study'; document.getElementById('chatBtn').click(); true")
    b.wait_for("document.body.classList.contains('chat-open')")
    assert b.js("document.querySelectorAll('#chatList .qa').length") == 2
    assert b.js("document.getElementById('view-study').hidden") is False

    # 刷新：对话从服务器读回来，抽屉还开着（这个标签页里开着时离开的）
    b.open(f"{server['base']}/#study")
    b.wait_for(
        "document.body.classList.contains('chat-open') && document.querySelectorAll('#chatList .qa').length === 2"
    )

    # 新对话
    b.js("document.getElementById('chatNew').click(); true")
    b.wait_for(
        "document.querySelectorAll('#chatList .qa').length === 0 && !document.getElementById('chatEmpty').hidden"
    )
    assert _api(server, "/api/coach")["turns"] == []
    b.js("document.getElementById('chatClose').click(); true")
    b.wait_for("!document.body.classList.contains('chat-open')")
    assert not b.errors, b.errors


def test_chat_error_mid_answer_restores_the_question(server, browser, fake_model):
    """回答到一半出错：提示错误，半截回答不留，问题放回输入框，能再发。"""
    b = browser
    _open_chat(b, server)
    b.js("""(() => { const of = window.fetch; window.fetch = (u, init) => String(u).includes('/api/coach/ask/stream')
      ? Promise.resolve(new Response('{"type":"delta","text":"先用"}\\n{"type":"error","detail":"和模型服务的连接中断了"}\\n', {status: 200}))
      : of(u, init); return true; })()""")
    b.js(
        "document.getElementById('chatInput').value = '怎么安排'; document.getElementById('chatSend').click(); true"
    )
    b.wait_for("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('连接中断'))")
    assert b.js("document.getElementById('chatInput').value") == "怎么安排"
    assert b.js("document.querySelectorAll('#chatList .qa').length") == 0
    assert b.js("document.getElementById('chatSend').disabled") is False
    assert not b.errors, b.errors


def test_chat_without_key_explains_instead_of_asking(server, browser, fake_model):
    """没有 API key：发问题先说明去设置填（和曲库页别的 AI 操作一样），不发请求。"""
    b = browser
    _fake_key(b, False)
    b.open(f"{server['base']}/#library")
    b.wait_for("!document.getElementById('keyNotice').hidden")
    calls = len(fake_model.calls)
    b.js("document.getElementById('chatBtn').click(); true")
    b.wait_for("document.body.classList.contains('chat-open')")
    b.js(
        "document.getElementById('chatInput').value = '先练哪首'; document.getElementById('chatSend').click(); true"
    )
    b.wait_for(
        "!!document.querySelector('dialog[open]') && document.querySelector('dialog[open]').textContent.includes('问 AI')"
    )
    assert len(fake_model.calls) == calls
    assert b.js("document.getElementById('chatInput').value") == "先练哪首"
    assert not b.errors, b.errors
