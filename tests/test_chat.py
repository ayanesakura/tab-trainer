"""首页的 AI 对话（app/chat.py）：给模型的上下文、多轮问答、存盘（data/coach.json）、费用、流式接口。"""

import json

import pytest
from fastapi.testclient import TestClient

from app import chat, config, main, music, practice, songs, store, study, viewmodels
from tests.fakes import FakeClaude, derived_bars, score_png


@pytest.fixture
def client(data_dir):
    return TestClient(main.app, base_url="http://127.0.0.1:8765")


def _gp(title: str, n: int = 4, **meta) -> str:
    sid = songs.create_song([("x.gp", b"\x18FICHIER GUITAR PRO v5.10")], title=title)
    songs.set_derived(sid, {"tuning": music.STANDARD, "time_signature": [4, 4], **meta}, derived_bars(n))
    return sid


def _stream(client, question: str):
    r = client.post("/api/coach/ask/stream", json={"question": question})
    if r.status_code != 200:
        return r.status_code, r.json()
    assert r.headers["content-type"].startswith("application/x-ndjson")
    return 200, [json.loads(line) for line in r.text.splitlines() if line]


def test_context_uses_the_library_plan_and_practice_numbers(data_dir):
    """上下文里每首曲子的概况（标记、艺人、标签、谱子信息、能不能练、练习、计划状态），今日队列，最近的练琴日历。"""
    a = _gp("晴天", 8, tempo=92, capo=2)
    store.update(a, lambda s: s.update(tags=["指弹"]) or s["meta"].update(artist="周杰伦"))
    b = _gp("卡农", 4, tuning=["D4", "B3", "G3", "D3", "A2", "D2"])
    c = _gp("旧曲", 2)
    pending = songs.create_song([("p1.png", score_png())], title="还没识别")  # 没开始识别：没有小节
    ids = [x["id"] for x in store.load(a)["bars"]]
    practice.record(a, ids[:2], 3, 70, 300)
    study.set_plan(a, True)
    study.set_plan(b, True)
    study.rate(a, 2, ["rhythm"], "第二段不稳", 70, stuck_bars=ids[3:5])
    songs.set_archived(c, True)

    ctx = chat.home_context()
    line = next(x for x in ctx.splitlines() if x.startswith(f"- [[{a}|晴天]]"))
    for part in (
        "周杰伦",
        "标签 指弹",
        "8 小节",
        "原速 92 BPM",
        "变调夹 2",
        "累计练了 5 分钟",
        "最快完整循环 70 BPM",
    ):
        assert part in line, part
    assert "4/4" not in line  # 不带拍号（GP 弱起小节的拍号会误导）
    for part in (
        "计划第 1 首",
        "学习中",
        "上次自评",
        "2 磕绊",
        "问题：节奏",
        "备注“第二段不稳”",
        "上次卡在第 4、5 小节",
    ):
        assert part in line, part
    assert "调弦 D4 B3 G3 D3 A2 D2" in ctx and "计划第 2 首；新曲（还没开始）" in ctx
    assert f"[[{c}|旧曲]]" in ctx and "已归档（不排进今日）" in ctx
    assert f"[[{pending}|还没识别]]" in ctx and "现在还不能练：" in ctx and "原谱识别" in ctx
    # 今日队列和 study.today 一样：评过的在“已练完”，新曲排进队列
    assert f"- [[{b}|卡农]] 新曲" in ctx and f"今天已练完并自评：[[{a}|晴天]]（2 磕绊）" in ctx
    assert "今天已练 5 分钟，每日目标 45 分钟" in ctx and "连续练琴 1 天" in ctx
    assert f"{study.practice_day(study.current())} 5 分钟（自评 1 首）" in ctx
    # 只读：问一次不改曲子、不改练琴数据
    revs = {sid: store.load(sid)["rev"] for sid in (a, b, c)}
    before = store.load_study()
    chat.ask("今天先练哪首？")
    assert {sid: store.load(sid)["rev"] for sid in (a, b, c)} == revs and store.load_study() == before
    call = FakeClaude.calls[-1]
    assert (
        call["chat"] == "今天先练哪首？"
        and call["context"] == chat.home_context()
        and "星期" in call["today"]
    )


def test_context_is_truncated_when_the_library_is_large(data_dir, monkeypatch):
    """曲子太多：计划里的先列详细信息，放不下的只列曲名，再放不下的只说还有几首，并告诉模型列表不全。"""
    sids = [_gp(f"曲{i:02d}", 2) for i in range(12)]
    study.set_plan(sids[0], True)
    one = len(chat._song_text(store.read(sids[0]), chat.views.library_item(store.read(sids[0])), 1))
    monkeypatch.setattr(chat, "SONGS_CHARS", one * 3)
    monkeypatch.setattr(chat, "NAMES_CHARS", 160)
    ctx = chat.home_context()
    songs_part = ctx.split("【曲库】\n")[1]
    detailed = [x for x in songs_part.splitlines() if x.startswith("- [[")]
    assert 1 <= len(detailed) <= 3 and detailed[0].startswith(f"- [[{sids[0]}|")  # 计划里的排在最前
    assert "曲库共 12 首" in songs_part and "只有上面" in songs_part and "连曲名也没列出" in songs_part
    assert len(songs_part) < one * 3 + 160 + 200


def test_conversation_is_multi_turn_kept_and_cleared(data_dir, monkeypatch):
    """多轮：后面的问题带上前面几轮；只留最近 chat_keep 轮；新对话清空问答、费用留着；坏了的文件从空对话开始。"""
    monkeypatch.setattr(config.settings, "chat_keep", 3)
    monkeypatch.setattr(chat, "HISTORY_TURNS", 2)
    monkeypatch.setattr(chat, "HISTORY_ANSWER_CHARS", 5)
    for q in ("一", "二", "三", "四"):
        r = chat.ask(q)
    assert [t["q"] for t in r["turns"]] == ["二", "三", "四"] and r["answer"] == "回答：四"
    assert [h["q"] for h in FakeClaude.calls[-1]["history"]] == ["二", "三"]
    assert FakeClaude.calls[-1]["history"][0]["a"].startswith("回答：二")  # 太长的回答截断
    assert r["cost"]["cost_usd"] == pytest.approx(0.04)
    viewmodels.ChatView.model_validate(chat.conversation())

    cleared = chat.new_conversation()
    assert (
        cleared["turns"] == []
        and cleared["id"] != r["id"]
        and cleared["cost"]["cost_usd"] == pytest.approx(0.04)
    )
    chat.ask("五")
    assert FakeClaude.calls[-1]["history"] == []

    # 回答期间点了“新对话”：回答不并进新对话，费用照记
    run = chat.prepare_ask("六")
    chat.new_conversation()
    out = run()
    assert out["turns"] == [] and out["cost"]["cost_usd"] == pytest.approx(0.06)

    # 格式不对的问答跳过；整个文件坏了：改名留着，从空对话开始
    store.update_coach(lambda d: d.update(turns=[{"q": "好的", "a": "回答"}, {"q": 1}, "x"]))
    assert [t["q"] for t in chat.conversation()["turns"]] == ["好的"]
    store.coach_file().write_text("{坏了", encoding="utf-8")
    store.coach_file().with_name("coach.json.bak").write_text("也坏了", encoding="utf-8")
    assert chat.conversation()["turns"] == []
    assert list(data_dir.glob("coach.corrupt-*.json"))
    assert chat.ask("七")["turns"][-1]["q"] == "七"


def test_stream_api_sends_deltas_then_saves(client):
    _gp("晴天")
    assert client.get("/api/coach").json() == {
        "id": "",
        "turns": [],
        "cost": viewmodels.ChatCost().model_dump(),
    }
    status, events = _stream(client, "今天先练哪首？")
    assert status == 200
    deltas = [e for e in events if e["type"] == "delta"]
    assert len(deltas) > 1 and "".join(e["text"] for e in deltas) == "回答：今天先练哪首？"
    done = events[-1]
    assert done["type"] == "done" and done["answer"] == "回答：今天先练哪首？"
    assert done["turns"][-1]["q"] == "今天先练哪首？" and done["cost"]["cost_usd"] == pytest.approx(0.01)
    got = client.get("/api/coach").json()
    viewmodels.ChatView.model_validate(got)
    assert got["turns"] == done["turns"] and got["id"] == done["id"]
    assert client.delete("/api/coach").json()["turns"] == []
    assert client.get("/api/coach").json()["cost"]["cost_usd"] == pytest.approx(0.01)


def test_stream_api_errors(client, monkeypatch):
    """开始之前的错误是 HTTP 状态码；回答到一半出错是一行 error 事件，不存半截回答，费用照记。"""
    from app import llm

    assert _stream(client, "   ")[0] == 400
    assert _stream(client, "x" * 501)[0] == 400

    def broken(self, *a, **kw):
        yield "先这样"
        self.usage = {**self.usage, "output_tokens": self.usage["output_tokens"] + 500}
        raise llm._http.ReadError("connection reset")

    monkeypatch.setattr(FakeClaude, "chat_stream", broken)
    status, events = _stream(client, "怎么练")
    assert status == 200 and [e["type"] for e in events] == ["delta", "error"]
    assert "连接中断" in events[1]["detail"]
    saved = store.load_coach()
    assert not saved.get("turns") and saved["usage"]["output_tokens"] == 520

    monkeypatch.setattr(config, "llm_factory", llm.Claude)  # 真正的客户端，没有 key
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    status, body = _stream(client, "怎么练")
    assert status == 400 and "API key" in body["detail"]


def test_chat_prompt_sends_the_context_and_rules(monkeypatch):
    """真正的客户端发出去的：首页对话自己的 system、数据、前面几轮、问题和只按数据说话的规则。"""
    from app import prompts
    from app.llm import Claude, LLMConfig

    sent = {}

    def fake_stream(self, content, effort=None, max_tokens=32000, system=None):
        sent.update(text=content[0]["text"], system=system, effort=effort)
        yield "好"

    monkeypatch.setattr(Claude, "_stream_call", fake_stream)
    c = Claude(LLMConfig(api_key="sk-ant-test"))
    assert list(
        c.chat_stream("先练哪首", "【曲库】\n- [[a1b2|晴天]]", [{"q": "前面", "a": "答"}], "2026-11-12")
    ) == ["好"]
    assert sent["system"] == prompts.CHAT_SYSTEM and sent["effort"] == "medium"
    text = sent["text"]
    assert "- [[a1b2|晴天]]" in text and "Earlier question: 前面\nYour answer: 答" in text
    assert (
        "Their question (in Chinese): 先练哪首" in text and "Never invent" in text and "[[id|title]]" in text
    )
