"""识别图片 / PDF 谱子：读谱头 → 版面检测 → 逐行调用 Claude 识别（本机 OCR 校对、有问题返修一次）→ 练习建议。"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from fractions import Fraction

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app import jobs, ocr, staleness, store
from app.jobs import Cancelled
from app.layout import detect_page
from app.llm import Claude, request_id_of
from app.music import (
    LENGTH_WARN,
    apply_time_signatures,
    bar_length,
    check_bar,
    clean_events,
    clean_repeats,
    clean_time_signature,
    new_bar_id,
    normalize_meta,
    parse_dur,
    rest_bar,
)

log = logging.getLogger("tab_trainer.recognize")

CONCURRENCY = 4  # 一首曲子同时识别几行
MAX_SYSTEMS = 4  # 所有曲子加起来同时识别几行（连续上传多首时不会成倍地发请求、跑 OCR）
LABEL_STRINGS = True  # 在裁剪图上标弦号（见 label_strings）
OCR_CHECK = True  # 用本机数字识别校对 AI 读出的弦和品（见 app/ocr.py、ocr_disagreements）


# ---------------------------------------------------------------------------
# 任务入口
# ---------------------------------------------------------------------------
def start_parse(sid: str) -> bool:
    """后台识别整首；已经有任务在跑就返回 False。"""
    cancel = jobs.jobs.claim(sid)
    if cancel is None:
        return False
    try:
        store.patch(sid, status="queued", error=None, progress={"stage": "排队中", "done": 0, "total": 0})
    except Exception:
        jobs.jobs.release(sid)  # 登记了却没启动：不释放的话这首曲子会一直显示“忙”
        raise

    def run():
        try:
            run_parse(sid, cancel)
        except Cancelled:
            if not _end_restage(sid, "重新识别已取消"):
                store.patch(sid, status="cancelled", error="识别已取消，已完成的行会保留。")
        except RestageFailed as e:
            _end_restage(sid, f"重新识别没有全部成功（{e}）")
        except Exception as e:
            rid = request_id_of(e)
            log.exception("request_id=%s song=%s parse failed", rid, sid)
            if not _end_restage(sid, "重新识别失败：" + jobs.friendly(e)):
                store.patch(sid, status="error", error=jobs.friendly(e))

    jobs.spawn(sid, run)
    return True


# ---------------------------------------------------------------------------
# 版面
# ---------------------------------------------------------------------------
def plan_systems(sid: str, song: dict) -> list[dict]:
    systems = []
    for pi, page in enumerate(song["pages"]):
        lay = detect_page(store.page_image(sid, page))
        if not lay["systems"]:
            systems.append(
                {
                    "page": pi,
                    "crop": [0, page["width"], 0, page["height"]],
                    "spans": None,
                    "status": "pending",
                }
            )
            continue
        for s in lay["systems"]:
            pad = 12
            x0 = max(0, s["x0"] - 70)  # 带上行首的谱号 / 小节号
            crop = [x0, min(page["width"], s["x1"] + pad), s["top"], s["bottom"]]
            systems.append(
                {"page": pi, "crop": crop, "spans": s["bars"], "lines": s["lines"], "status": "pending"}
            )
    return systems


def tab_lines(sid: str, song: dict, sysinfo: dict) -> list[float] | None:
    """这一行六线谱 6 根弦线在裁剪图里的纵坐标。旧曲子没存，就对整页重新检测，取落在裁剪范围里的那一行。"""
    x0, x1, y0, y1 = sysinfo["crop"]
    lines = sysinfo.get("lines")
    if not lines:
        page = song["pages"][sysinfo["page"]]
        for s in detect_page(store.page_image(sid, page))["systems"]:
            if y0 <= s["lines"][0] and s["lines"][-1] <= y1 and s["x0"] < x1 and x0 < s["x1"]:
                lines = s["lines"]
                break
    return [y - y0 for y in lines] if lines else None


def label_strings(img: Image.Image, lines: list[float], x_start: float) -> Image.Image:
    """在六线谱左边每根弦线旁标上红色的弦号 1–6。模型数线时容易把低音弦的数字往下错一根，
    直接看图对照弦号比数线可靠（只告诉它坐标没有用，评测里错误照旧）。"""
    img = img.convert("RGB").copy()
    sp = (lines[-1] - lines[0]) / 5
    size = max(9, round(sp * 0.85))
    try:
        font = ImageFont.load_default(size=size)
    except TypeError:  # 老版本 Pillow
        font = ImageFont.load_default()
    d = ImageDraw.Draw(img)
    x = max(1, x_start - size * 1.6)
    for i, y in enumerate(lines):
        d.text((x, y), str(i + 1), fill=(220, 30, 30), font=font, anchor="lm")
    return img


# ---------------------------------------------------------------------------
# 本机 OCR 校对
# ---------------------------------------------------------------------------
def _note_names(bag) -> str:
    return "、".join(f"{s}弦{f}品" + (f"×{n}" if n > 1 else "") for (s, f), n in sorted(bag.items()))


def ocr_disagreements(bars: list[dict], ocr_bars: list[list[tuple[int, int]]]) -> dict[int, tuple]:
    """逐小节比较 AI 结果和本机数字识别读到的（弦, 品）。返回 {小节序号: (只有 OCR 读到的, 只有 AI 写了的)}。

    - OCR 在这个小节什么都没读到：不判断（可能是没读出来，不代表没有音）；
    - 死音（谱上写 x）OCR 不读，不比较；倚音的小数字 OCR 会读到，算进去；
    - 人工泛音旁边标的“品位 + 12”（如 2<14>）OCR 会多读一个 14，允许；
    - 整行一致率不到一半：多半是 OCR 在这种排版上读不准，整行都不判断。
    """
    pairs = []
    last_fret: dict[int, int] = {}  # 每根弦上最后一个音的品位（判断跨小节线、写成括号的延续音）
    for i, (bar, seen) in enumerate(zip(bars, ocr_bars, strict=False)):
        tied_ok = set(last_fret.items())
        ai: Counter = Counter()
        harmonic_marks: Counter = Counter()
        for e in bar["events"]:
            for n in e["notes"]:
                if "x" in n["tech"]:
                    continue
                ai[(n["string"], n["fret"])] += 1
                last_fret[n["string"]] = n["fret"]
                if n.get("grace", -1) >= 0:
                    ai[(n["string"], n["grace"])] += 1
                if "ah" in n["tech"]:
                    harmonic_marks[(n["string"], n["fret"] + 12)] += 1
        got = Counter(seen) - harmonic_marks
        # 两位数的两个数字隔得稍远时 OCR 会读成两个音（11 → 1、1）：AI 写的两位数能由这几位拼出来就算一致
        for (st, f), k in list((ai - got).items()):
            parts = Counter((st, int(d)) for d in str(f)) if f >= 10 else None
            for _ in range(k):
                if parts and not (parts - (got - ai)):
                    got -= parts
                    got[(st, f)] += 1
        # 连音线跨过小节线延续的音，谱上常写成“(5)”，OCR 会读到，但它不是新起拍：上一小节这根弦的最后一个音
        # 就是这个品位、而且 AI 这边没有对不上的音（读错弦是两边各多一个，延续音只是 OCR 多一个），就不算多出来的
        if not (ai - got):
            for key, k in list((got - ai).items()):
                if key in tied_ok:
                    got[key] -= k
            got += Counter()  # 去掉计数为 0 的
        if got:
            pairs.append((i, ai, got))
    agree = sum(sum((ai & got).values()) for _, ai, got in pairs)
    total = sum(max(sum(ai.values()), sum(got.values())) for _, ai, got in pairs)
    if not total or agree / total < 0.5:
        return {}
    return {i: (got - ai, ai - got) for i, ai, got in pairs if got != ai}


# ---------------------------------------------------------------------------
# 识别一行
# ---------------------------------------------------------------------------
_system_slots = threading.BoundedSemaphore(MAX_SYSTEMS)


def transcribe_queued(*args, cancel: threading.Event | None = None, **kw) -> list[dict]:
    """排队等全局名额再识别一行；排队期间点了取消就不再识别。"""
    while not _system_slots.acquire(timeout=0.2):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
    try:
        return transcribe_system(*args, cancel=cancel, **kw)
    finally:
        _system_slots.release()


def transcribe_system(
    claude: Claude,
    sid: str,
    song: dict,
    si: int,
    first_bar: int,
    hint: str = "",
    cancel: threading.Event | None = None,
    start_ts: list[int] | None = None,
) -> list[dict]:
    sysinfo = song["systems"][si]
    page = song["pages"][sysinfo["page"]]
    x0, x1, y0, y1 = sysinfo["crop"]
    img = store.page_image(sid, page).crop((x0, y0, x1, y1))
    spans = [(a - x0, b - x0) for a, b in sysinfo["spans"]] if sysinfo["spans"] else None
    lines = tab_lines(sid, song, sysinfo) if spans else None
    ocr_bars = None
    if spans and lines and OCR_CHECK:
        try:
            ocr_bars = ocr.read_system(np.asarray(img.convert("L")), lines, spans)
        except Exception as e:  # noqa: BLE001 - 校对只是锦上添花，出错就跳过
            log.warning("song=%s system=%d ocr failed: %s", sid, si, e)
    labeled = bool(lines and LABEL_STRINGS)
    if labeled:
        img = label_strings(img, lines, spans[0][0])
    meta = song["meta"]

    def run(previous=None, problems=None):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        raw = claude.transcribe(
            img,
            meta,
            first_bar,
            spans,
            hint=hint,
            previous=previous,
            problems=problems,
            labeled_strings=labeled,
            start_ts=start_ts,
        )
        bars, issues, lengths = [], [], []
        # 当前生效的拍号：这一行开头的（前面几行变过拍号时由调用方给出），遇到这一行里写的变拍号就换
        cur_ts = start_ts if start_ts and start_ts != meta["time_signature"] else None
        for i, b in enumerate(raw.get("bars", [])):
            change = clean_time_signature(b.get("time_signature") or [])
            cur_ts = change or cur_ts
            ctx = {"time_signature": cur_ts} if cur_ts and cur_ts != meta["time_signature"] else {}
            try:
                events = clean_events(b.get("events", []))
            except ValueError as e:
                events = []
                issues.append(f"第 {first_bar + i} 小节数据格式错误：{e}")
            probs = check_bar(events, meta, bar=ctx)
            issues += [f"第 {first_bar + i} 小节：{p}" for p in check_bar(events, meta, info=False, bar=ctx)]
            if events:
                lengths.append(sum((parse_dur(e["dur"]) for e in events), Fraction(0)))
            bar = {
                "events": events,
                "tip": b.get("tip", ""),
                "uncertain": b.get("uncertain", ""),
                "warn": probs,
                **clean_repeats(b),
            }
            if change:
                bar["ts_change"] = change
            if not events:
                bar.update(events=rest_bar(meta, ctx), placeholder=True)
                bar["warn"] = ["没有识别出可用的音符，暂按整小节休止处理"]
            bars.append(bar)
        if (
            not cur_ts
            and len(lengths) == len(bars)
            and len(set(lengths)) == 1
            and lengths[0] != bar_length(meta)
        ):
            # 整行每个小节一样长、只是和全曲拍号对不上，这一行又没写变拍号：可能是前面几行变了拍号（各行分开识别，
            # 模型看不到），也可能是整行时值读错了。不要逼模型凑拍数，让它自己判断
            n = lengths[0]
            msg = (
                f"这一行每个小节都是 {float(n):g} 拍，而全曲拍号每小节 {float(bar_length(meta)):g} 拍。"
                "如果是谱子在前面的行里变了拍号（这一行的节奏本来就是这样），时值保持不变，"
                "在这一行第一个小节的 time_signature 写上实际拍号；如果是时值读错了，就改正时值。"
            )
            issues = [x for x in issues if LENGTH_WARN not in x] + [msg]
        if spans and len(bars) != len(spans):
            issues.append(f"应有 {len(spans)} 个小节，返回了 {len(bars)} 个")
        elif ocr_bars:
            for i, (only_ocr, only_ai) in ocr_disagreements(bars, ocr_bars).items():
                parts = []
                if only_ocr:
                    parts.append(f"本地数字识别在图上读到 {_note_names(only_ocr)}，你的结果里没有")
                if only_ai:
                    parts.append(f"你写的 {_note_names(only_ai)} 它没读到")
                issues.append(
                    f"第 {first_bar + i} 小节："
                    + "；".join(parts)
                    + "（它只读品位数字，也可能读错：请对照图片核对这几个位置，图上确实如此再改）"
                )
                bars[i]["_ocr"] = (only_ocr, only_ai)
        return raw, bars, issues

    raw, bars, issues = run()
    if issues:
        log.info("request_id=%s song=%s system=%d retry: %s", claude.last_request_id, sid, si, issues)
        _raw2, bars2, issues2 = run(previous=raw, problems=issues)
        if len(issues2) <= len(issues):
            bars, issues = bars2, issues2

    out = []
    for b in bars:
        only = b.pop("_ocr", None)
        if only:
            only_ocr, only_ai = only
            msg = "本地数字识别和 AI 结果不一致"
            if only_ocr:
                msg += f"：谱上读到 {_note_names(only_ocr)}"
            if only_ai:
                msg += f"{'，' if only_ocr else '：'}AI 写的是 {_note_names(only_ai)}"
            b["warn"] = [*b["warn"], msg + "。请对照原谱核对"]
    for i, b in enumerate(bars):
        if spans and i < len(spans):
            a, c = sysinfo["spans"][i]
            region = [max(0, a - 8), min(page["width"], c + 8), y0, y1]
        else:
            region = [x0, x1, y0, y1]
        out.append({"id": new_bar_id(), "system": si, "page": sysinfo["page"], "region": region, **b})
    return out


def _replace_system_bars(s: dict, si: int, bars: list[dict]) -> None:
    """用新识别的小节替换第 si 行原来的小节，再按行和行内顺序重新编号。"""
    for k, bar in enumerate(bars):
        bar["_order"] = k
    s["bars"] = [b for b in s["bars"] if b["system"] != si] + bars
    s["bars"].sort(key=lambda b: (b["system"], b.get("_order", 0)))
    for i, bar in enumerate(s["bars"]):
        bar["n"] = i + 1
        bar.pop("_order", None)


# ---------------------------------------------------------------------------
# 整首
# ---------------------------------------------------------------------------
def run_parse(sid: str, cancel: threading.Event | None = None) -> None:
    """识别整首。

    第一次识别（还没有小节）：一行识别完就写进曲子，练习页能边识别边看。
    重新识别已有结果的曲子：新结果先放在内存里，原来的小节、调弦、手改、提示和练习记录都不动；
    每一行都成功才一次性替换。有行失败、被取消或服务中断，原来的结果原样保留。
    """
    cancel = cancel or threading.Event()
    claude = jobs.new_claude()
    song = store.load(sid)
    restage = bool(song.get("bars"))
    store.patch(
        sid,
        status="parsing",
        error=None,
        restaging=restage or None,
        progress={"stage": "读取谱头", "done": 0, "total": 0},
    )
    meta = normalize_meta(claude.read_meta(store.page_image(sid, song["pages"][0])), song.get("title", ""))
    if not restage:
        store.patch(sid, meta=meta)
    store.patch(sid, progress={"stage": "分析版面", "done": 0, "total": 0})

    if cancel.is_set():
        raise Cancelled()
    systems = plan_systems(sid, song)
    work = {**song, "meta": meta, "systems": systems, "bars": []}  # 重新识别时新结果攒在这里
    if not restage:
        store.patch(sid, systems=systems, bars=[])
    store.patch(sid, progress={"stage": "逐行识别", "done": 0, "total": len(systems)})

    # 先按检测到的小节数预估每行的起始小节号；整页识别的页面数量未知，最后统一重新编号
    starts, n = [], 1
    for s in systems:
        starts.append(n)
        n += len(s["spans"]) if s["spans"] else 8

    errors = []

    def run_rows(rows: list[tuple[int, list[int] | None]]) -> None:
        """并行识别这些行（行号，这一行开头的拍号），每行完成就写回（重新识别时写到 work 里）。"""
        with ThreadPoolExecutor(CONCURRENCY) as pool:
            futs = {
                pool.submit(
                    transcribe_queued, claude, sid, work, i, starts[i], "", cancel=cancel, start_ts=ts
                ): i
                for i, ts in rows
            }
            for fut in as_completed(futs):
                i = futs[fut]
                try:
                    bars = fut.result()
                    status, err = "done", None
                except (Cancelled, CancelledError):
                    bars, status, err = [], "error", "已取消"
                except Exception as e:
                    rid = request_id_of(e)
                    log.exception("request_id=%s song=%s system=%d failed", rid, sid, i)
                    bars, status, err = [], "error", jobs.friendly(e)
                    errors.append(f"第 {i + 1} 行：{err}")

                _replace_system_bars(work, i, [dict(b) for b in bars])
                work["systems"][i].update(status=status, error=err)

                def apply(s, i=i, bars=bars, status=status, err=err):
                    if not restage:
                        _replace_system_bars(s, i, bars)
                        apply_time_signatures(s["bars"], s["meta"])
                        s["systems"][i].update(status=status, error=err)
                        staleness.prune_practice(s)
                    s["progress"]["done"] += 1
                    s["usage"] = dict(claude.usage)

                store.update(sid, apply)
                if cancel.is_set():
                    for f in futs:
                        f.cancel()  # 还没开始的行不再发请求；正在进行的请求会跑完

    run_rows([(i, None) for i in range(len(systems))])

    # 各行是并行识别的，前面某行写的变拍号后面的行看不到，模型会把小节硬凑成全曲拍号（每小节一样长，查不出来）。
    # 按第一遍的结果算出每行开头实际的拍号，和全曲拍号不同的行带上它再识别一次（没有变拍号的曲子不多花钱）。
    redo = [] if cancel.is_set() else _rows_needing_carried_ts(work["bars"], meta, len(systems))
    if not restage:
        song_now = store.load(sid)
        edited = {b["system"] for b in song_now["bars"] if "_orig" in b}  # 用户已经改过的行不覆盖
        redo = [(i, ts) for i, ts in redo if i not in edited]
    if redo:
        log.info("song=%s rerun rows with carried time signature: %s", sid, redo)

        def begin_redo(s):
            s["progress"] = {"stage": "按前面的变拍号重新识别", "done": 0, "total": len(redo)}
            if not restage:
                for i, _ in redo:
                    s["systems"][i].update(status="parsing", error=None)  # 识别期间这几行不能改

        store.update(sid, begin_redo)
        run_rows(redo)

    if cancel.is_set():
        raise Cancelled()
    if restage and errors:
        raise RestageFailed("；".join(errors))
    if not work["bars"]:
        if restage:
            raise RestageFailed("没有识别出任何小节")
        store.patch(sid, status="error", error="；".join(errors) or "没有识别出任何小节（可能不是六线谱）")
        return
    apply_time_signatures(work["bars"], work["meta"])
    if restage:

        def swap(s):
            # 一次性换成新结果；曲名保留用户改过的。小节 id 全换了，旧提示、问答、练习记录随之作废
            work["meta"]["title"] = (s.get("meta") or {}).get("title") or work["meta"]["title"]
            s.update(meta=work["meta"], systems=work["systems"], bars=work["bars"], plan=None)
            s.pop("restaging", None)
            staleness.prune_practice(s)

        store.update(sid, swap)

    song = store.load(sid)
    store.patch(sid, progress={"stage": "生成练习建议", "done": len(systems), "total": len(systems)})
    seen = {b["id"]: b["events"] for b in song["bars"]}
    try:
        plan = claude.practice_plan(song["meta"], song["bars"])
    except Exception as e:  # noqa: BLE001 - 练习建议失败不影响使用
        log.warning("request_id=%s song=%s practice plan failed: %s", request_id_of(e), sid, e)
        plan = None

    def finish(s, meta=song["meta"]):
        staleness.store_plan(s, plan, seen, meta)
        s.update(
            status="ready",
            error="；".join(errors) or None,
            usage=dict(claude.usage),
            progress={"stage": "完成", "done": len(systems), "total": len(systems)},
        )

    store.update(sid, finish)


def _row_start_ts(bars: list[dict], meta: dict, si: int) -> list[int]:
    """第 si 行开头实际的拍号：按顺序走过前面各行写的变拍号（ts_change）。"""
    cur = meta["time_signature"]
    for b in bars:
        if b["system"] >= si:
            break
        cur = b.get("ts_change") or cur
    return cur


def _rows_needing_carried_ts(bars: list[dict], meta: dict, n_rows: int) -> list[tuple[int, list[int]]]:
    """第一遍按全曲拍号识别、但开头其实已经变了拍号的行（行首自己写了变拍号的不算）。"""
    out = []
    for si in range(n_rows):
        row = [b for b in bars if b["system"] == si]
        ts = _row_start_ts(bars, meta, si)
        if row and ts != meta["time_signature"] and not row[0].get("ts_change"):
            out.append((si, ts))
    return out


class RestageFailed(Exception):
    """重新识别没有全部成功：不替换原来的结果。"""


def _end_restage(sid: str, why: str) -> bool:
    """重新识别没完成（失败 / 取消）：原来的结果没动过，恢复成可练习的状态。不是重新识别时返回 False。"""
    done = []

    def apply(s):
        if s.pop("restaging", None):
            s.update(status="ready", error=f"{why}，原来的结果保留不变。", progress=None)
            done.append(True)

    store.update(sid, apply)
    return bool(done)


# ---------------------------------------------------------------------------
# 重新识别某一行
# ---------------------------------------------------------------------------
def start_reparse(sid: str, si: int, hint: str = "") -> None:
    """在请求里就登记任务（而不是等后台线程），连点两次或和整首识别撞上时直接拒绝。"""
    jobs.check_llm_ready()
    cancel = jobs.claim_or_busy(sid, "这首曲子正在识别中")
    try:
        store.update(sid, lambda x: x["systems"][si].update(status="parsing", error=None))
    except Exception:
        jobs.jobs.release(sid)
        raise

    def run():
        try:
            reparse_system(sid, si, hint, cancel)
        except Exception as e:
            rid = request_id_of(e)
            log.exception("request_id=%s song=%s system=%d reparse failed", rid, sid, si)
            msg = "已取消" if isinstance(e, Cancelled) else jobs.friendly(e)
            store.update(sid, lambda x: x["systems"][si].update(status="error", error=msg))

    jobs.spawn(sid, run)


def reparse_system(sid: str, si: int, hint: str = "", cancel: threading.Event | None = None) -> None:
    claude = jobs.new_claude()
    song = store.load(sid)
    # 起始小节号：前面各行最后一个小节 + 1（这一行之前识别失败、没有小节时也对）
    first = max((b["n"] for b in song["bars"] if b["system"] < si), default=0) + 1
    start_ts = _row_start_ts(song["bars"], song["meta"], si)  # 前面各行写的变拍号，这一行的图上看不到
    bars = transcribe_queued(claude, sid, song, si, first, hint=hint, cancel=cancel, start_ts=start_ts)

    def apply(s):
        _replace_system_bars(s, si, bars)
        apply_time_signatures(s["bars"], s["meta"])  # 这一行新写的变拍号会影响后面的行
        s["systems"][si].update(status="done", error=None)
        staleness.invalidate_plan(s)
        staleness.prune_practice(s)
        jobs.add_usage(s, claude.usage)

    store.update(sid, apply)
