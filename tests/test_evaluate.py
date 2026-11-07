from app import evaluate, pipeline, store
from tests.test_pipeline import FakeClaude, _png, data_dir  # noqa: F401 - pytest fixture


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


def test_missing_bar_scores_zero_recall():
    c = evaluate.compare_bar([ev("4", (2, 0))], None)
    assert c["notes"] == (0, 1, 0) and not c["exact"]


def test_run_end_to_end_with_fake_model(data_dir, monkeypatch):  # noqa: F811
    sid = pipeline.create_song([("p1.png", _png())])
    pipeline._run_parse(sid)  # 用替身模型生成的结果当“标准答案”
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
