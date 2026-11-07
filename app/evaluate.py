"""识别准确率评测：拿一首人工校对过的曲子当标准答案，重新识别它的每一行，逐小节比对。

用法：
  uv run python -m app.evaluate example-wuting            # 只打印计划和预估费用，不调用模型
  uv run python -m app.evaluate example-wuting --yes      # 真正运行（会产生 API 费用）
  uv run python -m app.evaluate example-wuting --yes --systems 0,5 --model claude-sonnet-5-5

标准答案的调弦 / 变调夹 / 拍号直接沿用，只评测“逐行识别”这一步。
结果写到 data/evals/<时间>-<曲子>/：report.json（全部数据）和 report.md（摘要 + 错得最多的小节）。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path

from app import jobs, recognize, store
from app.llm import Claude, LLMConfig

log = logging.getLogger("tab_trainer.evaluate")

# 每百万 token 的美元价格（输入, 输出），用于估算费用
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
EST_TOKENS_PER_CALL = (6000, 6000)  # 粗估：一行谱子的图片 + 提示词，以及含思考的输出


# ---------------------------------------------------------------------------
# 比对
# ---------------------------------------------------------------------------
def _onsets(events: list[dict]):
    t = Fraction(0)
    for e in events:
        yield t, e
        t += Fraction(str(e["dur"]))


def bar_sets(events: list[dict]) -> dict[str, set]:
    """把一个小节拆成可比较的集合：起拍位置（不含休止）、(起拍, 弦, 品)、(起拍, 弦, 技巧)。"""
    onsets, notes, techs = set(), set(), set()
    for t, e in _onsets(events):
        if e["notes"]:
            onsets.add(t)
        for n in e["notes"]:
            notes.add((t, n["string"], n["fret"]))
            for k in n.get("tech", []):
                techs.add((t, n["string"], k))
    return {"onsets": onsets, "notes": notes, "techs": techs}


def _prf(truth: set, pred: set) -> tuple[int, int, int]:
    """返回 (命中, 标准答案数, 识别结果数)，汇总后再算 precision / recall / F1。"""
    return len(truth & pred), len(truth), len(pred)


def compare_bar(truth: list[dict], pred: list[dict] | None) -> dict:
    t = bar_sets(truth)
    p = bar_sets(pred or [])
    out = {k: _prf(t[k], p[k]) for k in t}
    out["exact"] = pred is not None and t["notes"] == p["notes"] and t["onsets"] == p["onsets"]
    out["missing"] = sorted(t["notes"] - p["notes"])
    out["extra"] = sorted(p["notes"] - t["notes"])
    return out


def f1(hit: int, n_truth: int, n_pred: int) -> dict:
    prec = hit / n_pred if n_pred else (1.0 if not n_truth else 0.0)
    rec = hit / n_truth if n_truth else 1.0
    return {
        "precision": round(prec, 4),
        "recall": round(rec, 4),
        "f1": round(2 * prec * rec / (prec + rec), 4) if prec + rec else 0.0,
    }


def summarize(rows: list[dict]) -> dict:
    tot = {k: [0, 0, 0] for k in ("onsets", "notes", "techs")}
    for r in rows:
        for k, acc in tot.items():
            for i in range(3):
                acc[i] += r["cmp"][k][i]
    n = len(rows)
    return {
        "bars": n,
        "bar_exact_rate": round(sum(r["cmp"]["exact"] for r in rows) / n, 4) if n else 0.0,
        "duration_ok_rate": round(sum(r["duration_ok"] for r in rows) / n, 4) if n else 0.0,
        "notes": f1(*tot["notes"]),
        "rhythm": f1(*tot["onsets"]),
        "techniques": f1(*tot["techs"]),
    }


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------
def _fmt_note(x) -> str:
    t, s, f = x
    return f"{float(t):g}拍:{s}弦{f}品"


def run(sid: str, cfg: LLMConfig, systems: list[int] | None, concurrency: int, price_model: str = "") -> dict:
    truth = store.load(sid)
    sys_idx = systems if systems is not None else list(range(len(truth["systems"])))
    claude = Claude(cfg)
    raw_calls: list[dict] = []  # 模型原始输出（含五线谱音名），清洗后的数据里没有，排查校验时用
    real_transcribe = claude.transcribe

    def recording_transcribe(img, meta, first_bar, *args, **kw):
        out = real_transcribe(img, meta, first_bar, *args, **kw)
        raw_calls.append(
            {"first_bar": first_bar, "retry": kw.get("previous") is not None, "bars": out.get("bars", [])}
        )
        return out

    claude.transcribe = recording_transcribe
    by_sys: dict[int, list[dict]] = {}
    for b in truth["bars"]:
        by_sys.setdefault(b["system"], []).append(b)

    def one(si: int):
        first = by_sys[si][0]["n"] if by_sys.get(si) else 1
        try:
            return si, recognize.transcribe_system(claude, sid, truth, si, first), None
        except Exception as e:  # noqa: BLE001 - 单行失败照样出报告
            return si, [], jobs.friendly(e)

    started = time.time()
    with ThreadPoolExecutor(concurrency) as pool:
        results = list(pool.map(one, sys_idx))

    rows, errors = [], {}
    for si, pred_bars, err in results:
        if err:
            errors[si] = err
        tbars = by_sys.get(si, [])
        for k, tb in enumerate(tbars):
            pb = pred_bars[k] if k < len(pred_bars) else None
            cmp = compare_bar(tb["events"], pb["events"] if pb else None)
            rows.append(
                {
                    "bar": tb["n"],
                    "system": si,
                    "cmp": cmp,
                    "duration_ok": bool(pb) and not any("时值" in w for w in pb.get("warn", [])),
                    "pred_events": pb["events"] if pb else None,
                    "pred_uncertain": pb.get("uncertain", "") if pb else "",
                    "pred_warn": pb.get("warn", []) if pb else [],
                }
            )
    price = PRICES.get(price_model or cfg.model, (0, 0))
    usage = claude.usage
    return {
        "song": sid,
        "model": cfg.model,
        "effort": cfg.effort,
        "systems": sys_idx,
        "bar_count_mismatch": {
            si: [len(by_sys.get(si, [])), len(p)] for si, p, _ in results if len(p) != len(by_sys.get(si, []))
        },
        "errors": errors,
        "summary": summarize(rows),
        "usage": usage,
        "cost_usd": round(
            usage["input_tokens"] / 1e6 * price[0] + usage["output_tokens"] / 1e6 * price[1], 4
        ),
        "seconds": round(time.time() - started, 1),
        "bars": rows,
        "raw_calls": raw_calls,
    }


def rescore(rep: dict, truth: dict) -> dict:
    """标准答案修正后，用报告里保存的识别结果重新计分（不调用模型）。"""
    by_n = {b["n"]: b for b in truth["bars"]}
    for r in rep["bars"]:
        r["cmp"] = compare_bar(by_n[r["bar"]]["events"], r["pred_events"])
    rep["summary"] = summarize(rep["bars"])
    return rep


def to_markdown(rep: dict, worst: int = 12) -> str:
    s = rep["summary"]
    lines = [
        f"# 识别评测：{rep['song']}",
        "",
        (
            f"- 模型 `{rep['model']}`，精细度 `{rep['effort']}`，{len(rep['systems'])} 行 / {s['bars']} 小节，"
            f"用时 {rep['seconds']} 秒，约 ${rep['cost_usd']}（{rep['usage']['input_tokens']} 输入 + "
            f"{rep['usage']['output_tokens']} 输出 token）"
        ),
        "",
        "| 指标 | 精确率 | 召回率 | F1 |",
        "|---|---|---|---|",
    ]
    for name, key in (
        ("音符（起拍+弦+品）", "notes"),
        ("节奏（起拍位置）", "rhythm"),
        ("技巧记号", "techniques"),
    ):
        m = s[key]
        lines.append(f"| {name} | {m['precision']:.1%} | {m['recall']:.1%} | {m['f1']:.1%} |")
    lines += [
        "",
        f"- 小节完全一致：{s['bar_exact_rate']:.1%}",
        f"- 时值凑满拍号：{s['duration_ok_rate']:.1%}",
    ]
    if rep["bar_count_mismatch"]:
        lines.append(f"- 小节数不一致的行（标准答案, 识别结果）：{rep['bar_count_mismatch']}")
    if rep["errors"]:
        lines.append(f"- 识别失败的行：{rep['errors']}")
    bad = sorted(rep["bars"], key=lambda r: len(r["cmp"]["missing"]) + len(r["cmp"]["extra"]), reverse=True)
    bad = [r for r in bad if r["cmp"]["missing"] or r["cmp"]["extra"]][:worst]
    if bad:
        lines += ["", "## 错得最多的小节", ""]
        for r in bad:
            miss = "、".join(_fmt_note(x) for x in r["cmp"]["missing"]) or "无"
            extra = "、".join(_fmt_note(x) for x in r["cmp"]["extra"]) or "无"
            lines.append(f"- 第 {r['bar']} 小节：漏了 {miss}；多了 {extra}")
            if r["pred_uncertain"]:
                lines.append(f"  - 模型自己标注的不确定：{r['pred_uncertain']}")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="用人工校对过的曲子评测识别准确率")
    ap.add_argument("song", help="作为标准答案的曲子 id（data/songs 下的文件夹名）")
    ap.add_argument("--yes", action="store_true", help="确认调用模型（会产生费用）；不加只打印计划")
    ap.add_argument("--systems", help="只评测这些行（从 0 开始，逗号分隔），省钱用")
    ap.add_argument("--model", help="覆盖设置里的模型")
    ap.add_argument("--effort", help="覆盖设置里的识别精细度")
    ap.add_argument("--base-url", help="覆盖设置里的 Base URL（key 用设置里的或环境变量 ANTHROPIC_API_KEY）")
    ap.add_argument("--price-as", help="按这个模型的价格估算费用（模型名不在价格表里时用）")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument(
        "--rescore", metavar="REPORT", help="用已有的 report.json 按当前标准答案重新计分，不调用模型"
    )
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    try:
        truth = store.load(a.song)
    except KeyError:
        sys.exit(f"找不到曲子 {a.song}")
    if a.rescore:
        rep = rescore(json.loads(Path(a.rescore).read_text()), truth)
        print(to_markdown(rep))
        return
    systems = [int(x) for x in a.systems.split(",")] if a.systems else None
    n_sys = len(systems) if systems is not None else len(truth["systems"])
    cfg = jobs.llm_config()
    if a.model:
        cfg.model = a.model
    if a.effort:
        cfg.effort = a.effort
    if a.base_url:
        cfg.base_url = a.base_url
    price_model = a.price_as or cfg.model
    price = PRICES.get(price_model)
    est = (
        f"约 ${n_sys * (EST_TOKENS_PER_CALL[0] * price[0] + EST_TOKENS_PER_CALL[1] * price[1]) / 1e6:.2f}"
        f"–{2 * n_sys * (EST_TOKENS_PER_CALL[0] * price[0] + EST_TOKENS_PER_CALL[1] * price[1]) / 1e6:.2f}"
        if price
        else "未知（模型不在价格表里）"
    )
    print(f"评测 {a.song}：{n_sys} 行谱子，模型 {cfg.model}，精细度 {cfg.effort}")
    print(f"将调用模型 {n_sys}–{2 * n_sys} 次（时值不对的行会返修一次），预估费用 {est}")
    if not a.yes:
        print("这只是计划。确认要运行请加 --yes。")
        return

    rep = run(a.song, cfg, systems, a.concurrency, price_model)
    model_tag = (a.model or cfg.model or "").split("/")[-1]
    out = store.DATA / "evals" / f"{time.strftime('%Y%m%d-%H%M%S')}-{a.song}-{model_tag}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
    md = to_markdown(rep)
    (out / "report.md").write_text(md)
    print(md)
    print(f"完整结果：{out}")


if __name__ == "__main__":
    main()
