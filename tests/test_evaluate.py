from app import evaluate, recognize, songs, store
from tests.fakes import FakeClaude, score_png


def ev(dur, *notes, tech=()):
    return {"dur": dur, "notes": [{"string": s, "fret": f, "tech": list(tech)} for s, f in notes]}


def test_compare_bar_counts_notes_rhythm_and_techniques():
    truth = [ev("1/2", (1, 2), (5, 0)), ev("1/2", (1, 4), tech=["h"]), ev("3"), ev("0")]
    truth = truth[:3]  # 0.5 + 0.5 + 3（休止）= 4 拍
    pred = [ev("1/2", (1, 2), (5, 0)), ev("1/2", (1, 5)), ev("3")]
    c = evaluate.compare_bar(truth, pred)
    assert c["notes"] == (2, 3, 3)  # 1 弦 4 品读成了 5 品
    assert c["onsets"] == (2, 2, 2)  # 节奏全对（休止不算起拍）
    assert c["techs"] == (0, 1, 0)  # 漏了击弦
    assert not c["exact"]
    assert c["missing"] == [(evaluate.Fraction(1, 2), 1, 4)]


def test_tapped_harmonic_position_counts_as_part_of_the_technique():
    """点弦泛音：音照常按 (起拍, 弦, 品) 比（品 = 左手按的），点错了品算技巧错。"""

    def th(fret, harm):
        return [{"dur": "4", "notes": [{"string": 1, "fret": fret, "tech": ["th"], "harm": harm}]}]

    assert evaluate.compare_bar(th(0, 19), th(0, 19))["techs"] == (1, 1, 1)
    c = evaluate.compare_bar(th(0, 19), th(0, 12))
    assert c["notes"] == (1, 1, 1) and c["exact"]  # 音和节奏都对
    assert c["techs"] == (0, 1, 1)  # 点的位置错了
    # 以前的读法：把点的品当成按的品、没有技巧
    c = evaluate.compare_bar(th(0, 19), [ev("4", (1, 19))])
    assert c["notes"] == (0, 1, 1) and c["techs"] == (0, 1, 0)


def test_missing_bar_scores_zero_recall():
    c = evaluate.compare_bar([ev("4", (2, 0))], None)
    assert c["notes"] == (0, 1, 0) and not c["exact"]


def test_run_end_to_end_with_fake_model(data_dir, monkeypatch):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)  # 用替身模型生成的结果当“标准答案”
    monkeypatch.setattr(evaluate, "Claude", FakeClaude)
    rep = evaluate.run(sid, evaluate.LLMConfig(model="claude-opus-5-5"), None, 2)
    s = rep["summary"]
    assert s["bars"] == 6
    # 替身模型第一次识别第 1 行会故意写错时值，返修后与标准答案一致
    assert s["notes"]["f1"] == 1.0 and s["bar_exact_rate"] == 1.0
    assert "识别评测" in evaluate.to_markdown(rep)
    assert store.load(sid)["bars"][0]["tip"] == "tip 1"  # 评测不改动曲库里的数据


def test_rescore_uses_current_truth():
    truth = {"bars": [{"n": 1, "events": [ev("4", (2, 0))]}]}
    rep = {"bars": [{"bar": 1, "pred_events": [ev("4", (2, 0))], "cmp": {}, "duration_ok": True}]}
    assert evaluate.rescore(rep, truth)["summary"]["bar_exact_rate"] == 1.0


def test_only_verified_bars_are_scored(data_dir, monkeypatch):
    """真实谱子做的评测曲：song["eval_bars"] 里的小节才算，只评它们所在的行。"""
    from app import evaluate, recognize, songs, store
    from app.llm import LLMConfig
    from tests.fakes import FakeClaude, score_png

    monkeypatch.setattr(evaluate, "Claude", FakeClaude)
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    store.update(sid, lambda s: s.update(eval_bars=[2, 3]))
    rep = evaluate.run(sid, LLMConfig(api_key="x"), None, 1)
    assert sorted(r["bar"] for r in rep["bars"]) == [2, 3]
    assert rep["systems"] == [0]


def test_start_time_signature_is_the_one_in_effect_before_the_row(data_dir, monkeypatch):
    """每行开头的拍号按前面各行最后的拍号给；这一行自己第 1 小节的变拍号不算（应用里也看不到它，模型在图上看）。"""
    from app import music

    monkeypatch.setattr(evaluate, "Claude", FakeClaude)
    seen = {}
    real = FakeClaude.transcribe

    def transcribe(self, img, meta, first_bar, spans, *a, **kw):
        seen.setdefault(first_bar, kw.get("start_ts"))
        return real(self, img, meta, first_bar, spans, *a, **kw)

    monkeypatch.setattr(FakeClaude, "transcribe", transcribe)
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)

    def change_at_row_start(s):
        s["bars"][3]["ts_change"] = [3, 4]  # 第 2 行第 1 小节变成 3/4
        music.apply_time_signatures(s["bars"], s["meta"])

    store.update(sid, change_at_row_start)
    seen.clear()
    evaluate.run(sid, evaluate.LLMConfig(model="claude-opus-5-5"), [1], 1)
    assert seen[4] is None  # 按 4/4 开头（和全曲一样，不特别告诉模型）

    def change_in_row_one(s):
        s["bars"][3].pop("ts_change")
        s["bars"][2]["ts_change"] = [3, 4]  # 第 1 行最后一小节变成 3/4
        music.apply_time_signatures(s["bars"], s["meta"])

    store.update(sid, change_in_row_one)
    seen.clear()
    evaluate.run(sid, evaluate.LLMConfig(model="claude-opus-5-5"), [1], 1)
    assert seen[4] == [3, 4]


def test_report_records_settings_and_the_cost_the_client_measured(data_dir, monkeypatch):
    """报告里记下双重校对、切半的设置；费用用客户端按实际回答的模型记的（服务端兜底可能换了模型），--price-as 才重算。"""

    class Priced(FakeClaude):
        def __init__(self, cfg):
            super().__init__(cfg)
            self.usage = {"input_tokens": 1_000_000, "output_tokens": 0, "cost_usd": 1.23}

    monkeypatch.setattr(evaluate, "Claude", Priced)
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    rep = evaluate.run(sid, evaluate.LLMConfig(model="claude-opus-5-5"), [0], 1, double="doubts", split=False)
    assert rep["double_check"] == "doubts" and rep["split"] is False
    assert rep["cost_usd"] == 1.23
    assert "doubts" in evaluate.to_markdown(rep)
    rep = evaluate.run(
        sid, evaluate.LLMConfig(model="claude-opus-5-5"), [0], 1, price_model="claude-sonnet-5-5"
    )
    assert rep["cost_usd"] != 1.23


def test_estimate_counts_only_scored_rows_and_extra_reads(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    store.update(sid, lambda s: s.update(eval_bars=[2, 3]))
    truth = store.load(sid)
    rows = evaluate.rows_to_eval(truth, None)
    assert rows == [0]
    price = (5.0, 25.0)
    off = evaluate.estimate(truth, rows, price, "off", False)
    both = evaluate.estimate(truth, rows, price, "all", False)
    assert off[:2] == (1, 2) and both[0] == 2 and both[1] > off[1] and both[3] > off[3]
