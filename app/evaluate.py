"""识别准确率评测：拿一首人工校对过的曲子当标准答案，重新识别它的每一行，逐小节比对。

用法：
  uv run python -m app.evaluate example-wuting            # 只打印计划和预估费用，不调用模型
  uv run python -m app.evaluate example-wuting --yes      # 真正运行（会产生 API 费用）
  uv run python -m app.evaluate example-wuting --yes --systems 0,5 --model claude-sonnet-5-5
  uv run python -m app.evaluate example-wuting --yes --refine   # 识别完再精修（逐小节放大核对），报告精修前后两份结果

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

from app import config, recognize, refine, store
from app.errors import NotFound
from app.llm import Claude, LLMConfig, friendly, price_of
from app.music import warn_code_of

log = logging.getLogger("tab_trainer.evaluate")

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
    """把一个小节拆成可比较的集合：起拍位置（不含休止）、(起拍, 弦, 品)、(起拍, 弦, 技巧)，
    以及弹唱谱的 (起拍, 和弦名)、(起拍, 扫弦方向)、(起拍, 第 1 段歌词)。
    点弦泛音的技巧连同右手点的品一起比（"th<19>"）：点错了位置算技巧错，品（左手按的）照常比。"""
    onsets, notes, techs, chords, strums, lyrics = set(), set(), set(), set(), set(), set()
    for t, e in _onsets(events):
        if e["notes"]:
            onsets.add(t)
        for n in e["notes"]:
            notes.add((t, n["string"], n["fret"]))
            for k in n.get("tech", []):
                techs.add((t, n["string"], f"th<{n.get('harm', 0)}>" if k == "th" else k))
        if e.get("chord"):
            chords.add((t, e["chord"]))
        if e.get("strum"):
            strums.add((t, e["strum"]))
        if e.get("lyrics") and e["lyrics"][0]:
            lyrics.add((t, e["lyrics"][0]))
    return {
        "onsets": onsets,
        "notes": notes,
        "techs": techs,
        "chords": chords,
        "strums": strums,
        "lyrics": lyrics,
    }


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
    tot = {k: [0, 0, 0] for k in ("onsets", "notes", "techs", "chords", "strums", "lyrics")}
    for r in rows:
        for k, acc in tot.items():
            for i in range(3):
                acc[i] += r["cmp"].get(k, (0, 0, 0))[i]
    n = len(rows)
    return {
        "bars": n,
        "bar_exact_rate": round(sum(r["cmp"]["exact"] for r in rows) / n, 4) if n else 0.0,
        "duration_ok_rate": round(sum(r["duration_ok"] for r in rows) / n, 4) if n else 0.0,
        "notes": f1(*tot["notes"]),
        "rhythm": f1(*tot["onsets"]),
        "techniques": f1(*tot["techs"]),
        **({"chords": f1(*tot["chords"])} if tot["chords"][1] else {}),
        **({"strums": f1(*tot["strums"])} if tot["strums"][1] else {}),
        **({"lyrics": f1(*tot["lyrics"])} if tot["lyrics"][1] else {}),
    }


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------
def _fmt_note(x) -> str:
    t, s, f = x
    return f"{float(t):g}拍:{s}弦{f}品"


def rows_to_eval(truth: dict, systems: list[int] | None) -> list[int]:
    """要评测的行：指定了就用指定的；真实谱子做的评测曲（scripts/make_real_eval.py）只有用户核对过的小节
    （eval_bars）算标准答案，只评这些小节所在的行；否则全部行。"""
    scored = set(truth.get("eval_bars") or [])
    if systems is not None:
        return systems
    if scored:
        return sorted({b["system"] for b in truth["bars"] if b["n"] in scored})
    return list(range(len(truth["systems"])))


def start_ts(truth: dict, si: int) -> list[int] | None:
    """第 si 行开头生效的拍号：前面各行最后一个小节的拍号（这一行自己第 1 小节写的变拍号不算，模型在图上看得到；
    和应用里一样，见 recognize._row_start_ts）。和全曲拍号一样时返回 None（应用里这时不特别告诉模型）。"""
    prev = [b for b in truth["bars"] if b["system"] < si]
    ts = (prev[-1].get("time_signature") if prev else None) or truth["meta"]["time_signature"]
    return None if ts == truth["meta"]["time_signature"] else ts


def estimate(
    truth: dict, sys_idx: list[int], price: tuple[float, float] | None, double: str, split: bool | None
):
    """预估调用次数和费用：(最少次数, 最多次数, 最少费用, 最多费用)；不知道价格时费用为 None。
    每读一遍 1–2 次调用（有问题时返修一次；返修后小节数还不对时再重读一次，很少见，不算）；切成两半的行每遍 2 倍；
    双重校对 all 每行至少读两遍、最多三遍，doubts 一到三遍。"""
    do_split = recognize.SPLIT_ROWS if split is None else split
    base = sum(
        2 if do_split and si < len(truth["systems"]) and recognize._should_split(truth["systems"][si]) else 1
        for si in sys_idx
    )
    reads = {"off": (1, 1), "doubts": (1, 3), "all": (2, 3)}[double]
    lo, hi = base * reads[0], base * reads[1] * 2
    if not price:
        return lo, hi, None, None
    per = (EST_TOKENS_PER_CALL[0] * price[0] + EST_TOKENS_PER_CALL[1] * price[1]) / 1e6
    return lo, hi, lo * per, hi * per


def relayout_truth(sid: str, truth: dict) -> int:
    """评测曲生成时存下的版面换成现在的版面检测结果（只在内存里）：版面检测改了以后，看新的行裁剪 / 小节线
    对识别有没有帮助。一页上检测出的行数、每行的小节数都和存下的一样才换（标准答案按行、按小节对得上），
    不一样的页保持原样。返回换了几行。"""
    by_page: dict[int, list[int]] = {}
    for i, s in enumerate(truth["systems"]):
        by_page.setdefault(s["page"], []).append(i)
    n = 0
    for pi, idx in by_page.items():
        new = recognize.plan_page_systems(sid, truth["pages"][pi], pi)
        old = [truth["systems"][i] for i in idx]
        if len(new) != len(old) or any(
            len(a.get("spans") or []) != len(b.get("spans") or []) for a, b in zip(old, new, strict=True)
        ):
            log.warning("song=%s page=%d relayout: rows/bars differ, keeping the stored layout", sid, pi)
            continue
        for i, b in zip(idx, new, strict=True):
            truth["systems"][i] = {
                **truth["systems"][i],
                "crop": b["crop"],
                "spans": b["spans"],
                "lines": b["lines"],
            }
            n += 1
    return n


def run(
    sid: str,
    cfg: LLMConfig,
    systems: list[int] | None,
    concurrency: int,
    price_model: str = "",
    double: str = "off",
    split: bool | None = None,  # None = 和应用里一样（recognize.SPLIT_ROWS）
    refine_pass: bool = False,
    relayout: bool = False,
) -> dict:
    """price_model：按这个模型的价格估算费用（--price-as）；不给就用模型客户端按实际回答的模型记的钱
    （服务端兜底换了模型时也对）。refine_pass：识别完再对要评分的小节做一遍精修（见 run_refine），报告里加上精修后的结果。"""
    truth = store.load(sid)
    replaced = relayout_truth(sid, truth) if relayout else 0
    scored = set(truth.get("eval_bars") or [])
    sys_idx = rows_to_eval(truth, systems)
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
            # 这一行开头的拍号按标准答案给（整首识别时由前面各行的结果带过来，见 recognize.run_parse）
            return (
                si,
                recognize.transcribe_system(
                    claude,
                    sid,
                    truth,
                    si,
                    first,
                    start_ts=start_ts(truth, si),
                    double_check=double,
                    split=split,
                ),
                None,
            )
        except Exception as e:  # noqa: BLE001 - 单行失败照样出报告
            return si, [], friendly(e)

    started = time.time()
    with ThreadPoolExecutor(concurrency) as pool:
        results = list(pool.map(one, sys_idx))

    refined: dict[tuple[int, int], dict] = {}
    refine_rep = None
    if refine_pass:
        refined, refine_rep = run_refine(sid, truth, results, scored, concurrency, cfg, price_model)

    rows, errors = [], {}
    for si, pred_bars, err in results:
        if err:
            errors[si] = err
        tbars = by_sys.get(si, [])
        for k, tb in enumerate(tbars):
            if scored and tb["n"] not in scored:
                continue
            pb = pred_bars[k] if k < len(pred_bars) else None
            cmp = compare_bar(tb["events"], pb["events"] if pb else None)
            row = {
                "bar": tb["n"],
                "system": si,
                "cmp": cmp,
                "duration_ok": duration_ok(pb),
                "pred_events": pb["events"] if pb else None,
                "pred_uncertain": pb.get("uncertain", "") if pb else "",
                "pred_warn": pb.get("warn", []) if pb else [],
            }
            if refine_pass:
                r = refined.get((si, k))
                after = r["events"] if r and r["events"] is not None else row["pred_events"]
                row.update(
                    refined_events=after,
                    cmp_refined=compare_bar(tb["events"], after) if pb else cmp,
                    refine_checked=r is not None and not r["error"],
                    refine_changed=bool(r and r["events"] is not None),
                    refine_diff=(r or {}).get("diff", ""),
                    refine_status=(r or {}).get("status", ""),
                    refine_error=(r or {}).get("error", ""),
                )
            rows.append(row)
    usage = claude.usage
    price = price_of(price_model) if price_model else None
    extra = {}
    if refine_rep is not None:
        before = sum(r["cmp"]["exact"] for r in rows)
        after = sum(r["cmp_refined"]["exact"] for r in rows)
        n = len(rows)
        extra = {
            "summary_refined": summarize([{**r, "cmp": r["cmp_refined"]} for r in rows]),
            "refine": {
                **refine_rep,
                "bars_checked": sum(r["refine_checked"] for r in rows),
                "changed": sum(r["refine_changed"] for r in rows),
                "fixed": sum(1 for r in rows if not r["cmp"]["exact"] and r["cmp_refined"]["exact"]),
                "broke": sum(1 for r in rows if r["cmp"]["exact"] and not r["cmp_refined"]["exact"]),
                "errors": sum(1 for r in rows if r["refine_error"]),
                # 模型提出的改动：再核对后确认了几个、几遍对不上没改几个（refine.CONFIRM_CHANGES）
                "confirm_changes": refine.CONFIRM_CHANGES,
                "confirmed": sum(r["refine_status"] == "confirmed" for r in rows),
                "rejected": sum(r["refine_status"] == "rejected" for r in rows),
                "bar_exact_before": round(before / n, 4) if n else 0.0,
                "bar_exact_after": round(after / n, 4) if n else 0.0,
            },
        }
    return {
        "song": sid,
        "model": cfg.model,
        "effort": cfg.effort,
        "double_check": double,
        "split": recognize.SPLIT_ROWS if split is None else split,
        "relayout_rows": replaced if relayout else None,
        "systems": sys_idx,
        "bar_count_mismatch": {
            si: [len(by_sys.get(si, [])), len(p)] for si, p, _ in results if len(p) != len(by_sys.get(si, []))
        },
        "errors": errors,
        "summary": summarize(rows),
        "usage": usage,
        "price_as": price_model or None,
        # 指定了 --price-as 就按它估算；否则用客户端按实际回答的模型记的钱（服务端兜底换了模型、网关前缀的模型名都对）
        "cost_usd": round(
            usage["input_tokens"] / 1e6 * price[0] + usage["output_tokens"] / 1e6 * price[1]
            if price
            else usage.get("cost_usd", 0),
            4,
        ),
        "seconds": round(time.time() - started, 1),
        "bars": rows,
        "raw_calls": raw_calls,
        **extra,
    }


def run_refine(
    sid: str,
    truth: dict,
    results: list[tuple[int, list[dict], str | None]],
    scored: set[int],
    concurrency: int,
    cfg: LLMConfig,
    price_model: str = "",
) -> tuple[dict[tuple[int, int], dict], dict]:
    """对识别结果做一遍精修（和应用里一样：app/refine.verify，按识别出的小节位置裁图，上一小节的识别结果当上下文）。
    只核对要评分的小节（有 eval_bars 时）；用单独的模型客户端，费用分开记。
    返回 ({(行号, 行内序号): {events（没改是 None）, diff, status（见 refine.verify）, error}}, {cost_usd, usage, seconds})。"""
    claude = Claude(cfg)
    meta = truth["meta"]
    by_sys: dict[int, list[dict]] = {}
    for b in truth["bars"]:
        by_sys.setdefault(b["system"], []).append(b)
    preds = {si: bars for si, bars, _ in results}
    pages: dict[int, object] = {}
    jobs = []
    for si, bars, _ in results:
        sysinfo = truth["systems"][si] if si < len(truth["systems"]) else None
        if not sysinfo or not sysinfo.get("spans"):
            continue  # 整行一个小节（没检测到小节线）：裁不出单个小节，应用里也不精修
        # 每个小节的拍号：这一行开头的（同 start_ts），走过识别结果里写的变拍号
        cur = start_ts(truth, si) or meta["time_signature"]
        for k, b in enumerate(bars):
            cur = b.get("ts_change") or cur
            tb = by_sys.get(si, [])[k] if k < len(by_sys.get(si, [])) else None
            if tb is None or (scored and tb["n"] not in scored) or b.get("placeholder"):
                continue
            prev = bars[k - 1] if k else (preds.get(si - 1) or [None])[-1]
            ts = cur if cur != meta["time_signature"] else None
            jobs.append((si, k, b, prev, ts))
            pages.setdefault(sysinfo["page"], None)
    for pi in pages:
        pages[pi] = store.page_image(sid, truth["pages"][pi])

    def one(job):
        si, k, b, prev, ts = job
        sysinfo = truth["systems"][si]
        try:
            r = refine.verify(
                claude, pages[sysinfo["page"]], sysinfo, b, meta, prev["events"] if prev else None, ts
            )
        except Exception as e:  # noqa: BLE001 - 一个小节失败照样出报告
            return (si, k), {"events": None, "diff": "", "status": "error", "error": friendly(e)}
        diff = str(r["out"].get("diff") or "")
        return (si, k), {"events": r["events"], "diff": diff, "status": r["status"], "error": ""}

    started = time.time()
    with ThreadPoolExecutor(concurrency) as pool:
        out = dict(pool.map(one, jobs))
    usage = claude.usage
    price = price_of(price_model) if price_model else None
    cost = (
        usage["input_tokens"] / 1e6 * price[0] + usage["output_tokens"] / 1e6 * price[1]
        if price
        else usage.get("cost_usd", 0)
    )
    return out, {"usage": usage, "cost_usd": round(cost, 4), "seconds": round(time.time() - started, 1)}


def refine_bars_to_check(truth: dict, sys_idx: list[int]) -> int:
    """--refine 要核对的小节数（预估用）：评测的行里要评分的小节，整行一个小节的行不算。"""
    scored = set(truth.get("eval_bars") or [])
    rows = {si for si in sys_idx if si < len(truth["systems"]) and truth["systems"][si].get("spans")}
    return sum(1 for b in truth["bars"] if b["system"] in rows and (not scored or b["n"] in scored))


DURATION_CODES = frozenset({"length", "placeholder"})  # 时值合计不对、整小节没识别出来（按休止占位）


def duration_ok(pred_bar: dict | None) -> bool:
    """识别结果的时值凑满了拍号：没有“时值合计”和“占位”这两种警告。
    旧报告里警告是一句话（不是 {code, msg}），按文字认出 code（见 music.warn_code_of）。"""
    if not pred_bar:
        return False
    return not ({warn_code_of(w) for w in pred_bar.get("warn") or []} & DURATION_CODES)


def rescore(rep: dict, truth: dict) -> dict:
    """标准答案修正后，用报告里保存的识别结果重新计分（不调用模型）。duration_ok 也按警告 code 重算。"""
    by_n = {b["n"]: b for b in truth["bars"]}
    for r in rep["bars"]:
        r["cmp"] = compare_bar(by_n[r["bar"]]["events"], r["pred_events"])
        if r.get("pred_events") is not None:
            r["duration_ok"] = duration_ok({"warn": r.get("pred_warn") or []})
    rep["summary"] = summarize(rep["bars"])
    return rep


def to_markdown(rep: dict, worst: int = 12) -> str:
    s = rep["summary"]
    lines = [
        f"# 识别评测：{rep['song']}",
        "",
        (
            f"- 模型 `{rep['model']}`，精细度 `{rep['effort']}`，双重校对 `{rep.get('double_check', 'off')}`，"
            f"切半 `{rep.get('split', '-')}`，{len(rep['systems'])} 行 / {s['bars']} 小节，"
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
        ("和弦名（起拍+名字）", "chords"),
        ("扫弦（起拍+方向）", "strums"),
        ("歌词（起拍+字）", "lyrics"),
    ):
        if key not in s:
            continue
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
    rf = rep.get("refine")
    if rf:
        sr = rep["summary_refined"]
        lines += [
            "",
            "## 精修（逐小节放大核对）",
            "",
            f"- 小节完全一致：{rf['bar_exact_before']:.1%} → {rf['bar_exact_after']:.1%}"
            f"（核对 {rf['bars_checked']} 个，改了 {rf['changed']} 个：改对 {rf['fixed']} 个，改坏 {rf['broke']} 个"
            + (f"，{rf['errors']} 个核对失败" if rf["errors"] else "")
            + "）",
            (
                f"- 提出的改动再核对：确认 {rf['confirmed']} 个，几遍对不上没改 {rf['rejected']} 个"
                if rf.get("confirm_changes")
                else "- 提出的改动没有再核对（CONFIRM_CHANGES 关着）"
            ),
            (
                f"- 音符 F1：{s['notes']['f1']:.1%} → {sr['notes']['f1']:.1%}；"
                f"节奏 F1：{s['rhythm']['f1']:.1%} → {sr['rhythm']['f1']:.1%}"
            ),
            f"- 精修费用约 ${rf['cost_usd']}，用时 {rf['seconds']} 秒",
        ]
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
        "--double-check",
        choices=["off", "doubts", "all"],
        default="off",
        help="双重校对：doubts 只重读第一遍有疑问的行，all 每行都重读；两遍不一致的小节读第三遍投票",
    )
    ap.add_argument(
        "--split",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="太宽的行切成两半识别（recognize._should_split）；默认和应用里一样（recognize.SPLIT_ROWS）",
    )
    ap.add_argument(
        "--refine",
        action="store_true",
        help="识别完再精修（逐小节放大核对，见 app/refine.py），报告精修前后两份结果（另加约 $0.2 / 小节）",
    )
    ap.add_argument(
        "--relayout",
        action="store_true",
        help="用现在的版面检测重新切行（行数、小节数和存下的一致的页才换），看版面改动对识别的影响",
    )
    ap.add_argument(
        "--rescore", metavar="REPORT", help="用已有的 report.json 按当前标准答案重新计分，不调用模型"
    )
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    try:
        truth = store.load(a.song)
    except NotFound:
        sys.exit(f"找不到曲子 {a.song}")
    if a.rescore:
        rep = rescore(json.loads(Path(a.rescore).read_text()), truth)
        print(to_markdown(rep))
        return
    systems = [int(x) for x in a.systems.split(",")] if a.systems else None
    sys_idx = rows_to_eval(truth, systems)
    cfg = config.llm_config()
    if a.model:
        cfg.model = a.model
    if a.effort:
        cfg.effort = a.effort
    if a.base_url:
        cfg.base_url = a.base_url
    lo, hi, c_lo, c_hi = estimate(truth, sys_idx, price_of(a.price_as or cfg.model), a.double_check, a.split)
    est = f"约 ${c_lo:.2f}–{c_hi:.2f}" if c_lo is not None else "未知（模型不在价格表里）"
    print(
        f"评测 {a.song}：{len(sys_idx)} 行谱子，模型 {cfg.model}，精细度 {cfg.effort}，"
        f"双重校对 {a.double_check}，切半 {recognize.SPLIT_ROWS if a.split is None else a.split}"
    )
    print(f"将调用模型 {lo}–{hi} 次（有问题的行会返修、重读），预估费用 {est}")
    if a.refine:
        n = refine_bars_to_check(truth, sys_idx)
        print(f"精修：再核对约 {n} 个小节，每个一次调用，另加约 ${n * refine.COST_PER_BAR:.2f}")
    if not a.yes:
        print("这只是计划。确认要运行请加 --yes。")
        return

    rep = run(
        a.song, cfg, systems, a.concurrency, a.price_as or "", a.double_check, a.split, a.refine, a.relayout
    )
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
