"""识别图片 / PDF 谱子：读谱头 → 版面检测 → 逐行调用 Claude 识别（本机 OCR 校对、有问题返修一次）→ 练习建议。"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from fractions import Fraction

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app import config, jobs, ocr, staleness, state, store
from app.config import settings
from app.errors import Busy, Invalid
from app.jobs import Cancelled
from app.layout import detect_page
from app.llm import LLM, LLMError, friendly, request_id_of
from app.model import is_score
from app.music import (
    PLACEHOLDER_MSG,
    apply_time_signatures,
    bar_length,
    check_bar,
    clean_events,
    clean_repeats,
    clean_shapes,
    clean_time_signature,
    new_bar_id,
    normalize_meta,
    parse_dur,
    rest_bar,
    warn,
)

log = logging.getLogger("tab_trainer.recognize")

LABEL_STRINGS = True  # 在裁剪图上标弦号（见 label_strings）
OCR_CHECK = True  # 用本机数字识别校对 AI 读出的弦和品（见 app/ocr.py、ocr_disagreements）


# ---------------------------------------------------------------------------
# 任务入口
# ---------------------------------------------------------------------------
def start_parse(sid: str, cancel: threading.Event | None = None) -> bool:
    """后台识别整首；已经有任务在跑就返回 False。cancel：调用方已经登记好的任务（调整页面时一直占着，
    中间不会被别的任务插进来），这时不再登记。"""

    def run(cancel):
        try:
            run_parse(sid, cancel)
        except Cancelled:
            if not _end_restage(sid, "重新识别已取消"):
                _end_parse(sid, "cancelled", ("warn", "识别已取消，已完成的行会保留。"))
        except RestageFailed as e:
            _end_restage(sid, f"重新识别没有全部成功（{e}）")
        except Exception as e:
            rid = request_id_of(e)
            log.exception("request_id=%s song=%s parse failed", rid, sid)
            if not _end_restage(sid, "重新识别失败：" + friendly(e)):
                _end_parse(sid, "error", ("error", friendly(e)))

    try:
        jobs.start(sid, "", _begin_parse, run, cancel)
    except Busy:
        return False
    return True


def _begin_parse(s: dict, stage: str = "排队中") -> None:
    """整首识别开始：已有小节时是重新识别（restaging：原来的结果在新结果全部成功前不动，status 也不变）。"""
    restage = bool(s.get("bars"))
    state.begin(
        s,
        "parse",
        restaging=restage,
        status=None if restage else ("queued" if stage == "排队中" else "parsing"),
        progress={"stage": stage, "done": 0, "total": 0},
    )


def _end_parse(sid: str, status: str, notice: tuple[str, str] | None) -> None:
    store.update(sid, lambda s: state.finish(s, status=status, notice=notice, source="parse"))


# ---------------------------------------------------------------------------
# 版面
# ---------------------------------------------------------------------------
def plan_systems(sid: str, song: dict) -> list[dict]:
    return [s for pi, page in enumerate(song["pages"]) for s in plan_page_systems(sid, page, pi)]


def plan_page_systems(sid: str, page: dict, pi: int) -> list[dict]:
    """一页上的谱行（裁剪范围、小节位置、弦线）。检测不到六线谱时整页算一行。"""
    lay = detect_page(store.page_image(sid, page))
    if not lay["systems"]:
        return [
            {"page": pi, "crop": [0, page["width"], 0, page["height"]], "spans": None, "status": "pending"}
        ]
    systems = []
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
    - 整行一致率不到一半，或 OCR 只读对了 AI 写的音的不到 60%：多半是 OCR 在这种排版上读不准，整行都不判断。
    """
    pairs = []
    ai_total = 0  # 整行 AI 写的音（不含闷音）
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
                ai_total += 1
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
    # OCR 在整行里只读对了 AI 结果的一小部分（斜体 / 手写风格的数字、很糊的截图）：它在这种排版上靠不住，
    # 这时的“不一致”多半是 OCR 读错，提示给模型反而会把对的改错。GP 等软件导出的谱子每行都在 78% 以上
    if agree < 0.6 * ai_total:
        return {}
    return {i: (got - ai, ai - got) for i, ai, got in pairs if got != ai}


# ---------------------------------------------------------------------------
# 识别一行
# ---------------------------------------------------------------------------
# 所有曲子加起来同时识别几行：信号量在导入时按 settings.max_rows_global 建好，之后改这个设置不生效（测试直接换掉 _system_slots）
_system_slots = threading.BoundedSemaphore(settings.max_rows_global)


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
    claude: LLM,
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
        # 弹唱谱：这一行印出来的和弦图（和弦名 → 1–6 弦品位）。有和弦图时谱上的 x 按和弦换算成了品位，
        # 本机 OCR 只读数字、看不到这些音，“AI 写了、OCR 没读到”不再算不一致
        shapes = clean_shapes({str(c.get("name")): c.get("frets") for c in raw.get("chords") or []})
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
                issues.append(("other", f"第 {first_bar + i} 小节数据格式错误：{e}"))
            probs = check_bar(events, meta, bar=ctx)
            # issues：(警告 code, 发给模型返修的文字)
            issues += [
                (p["code"], f"第 {first_bar + i} 小节：{p['msg']}")
                for p in check_bar(events, meta, info=False, bar=ctx)
            ]
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
            used = {e["chord"]: shapes[e["chord"]] for e in events if e.get("chord") in shapes}
            if used:
                bar["shapes"] = used
            if not events:
                bar.update(events=rest_bar(meta, ctx), placeholder=True)
                bar["warn"] = [warn("placeholder", PLACEHOLDER_MSG)]
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
            issues = [x for x in issues if x[0] != "length"] + [("other", msg)]
        if spans and len(bars) != len(spans):
            issues.append(("other", f"应有 {len(spans)} 个小节，返回了 {len(bars)} 个"))
        elif ocr_bars:
            for i, (only_ocr, only_ai) in ocr_disagreements(bars, ocr_bars).items():
                if shapes:
                    only_ai = []
                    if not only_ocr:
                        continue
                parts = []
                if only_ocr:
                    parts.append(f"本地数字识别在图上读到 {_note_names(only_ocr)}，你的结果里没有")
                if only_ai:
                    parts.append(f"你写的 {_note_names(only_ai)} 它没读到")
                issues.append(
                    (
                        "ocr_mismatch",
                        f"第 {first_bar + i} 小节："
                        + "；".join(parts)
                        + "（它只读品位数字，也可能读错：请对照图片核对这几个位置，图上确实如此再改）",
                    )
                )
                bars[i]["_ocr"] = (only_ocr, only_ai)
        return raw, bars, issues

    raw, bars, issues = run()
    if issues:
        texts = [t for _, t in issues]
        log.info("request_id=%s song=%s system=%d retry: %s", claude.last_request_id, sid, si, texts)
        try:
            _raw2, bars2, issues2 = run(previous=raw, problems=texts)
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001 - 返修出错（过载、断线）：用第一次的结果，警告照常标在小节上
            log.warning("request_id=%s song=%s system=%d retry failed: %s", request_id_of(e), sid, si, e)
        else:
            if len(issues2) <= len(issues):
                bars, issues = bars2, issues2
    if not bars:  # 返修后还是一个小节都没有：算这一行失败（不能标成完成；重新识别时会整行删掉原来的小节）
        raise LLMError("模型没有读出这一行的小节，请重新识别这一行", getattr(claude, "last_request_id", None))

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
            b["warn"] = [*b["warn"], warn("ocr_mismatch", msg + "。请对照原谱核对")]
    for i, b in enumerate(bars):
        if spans and i < len(spans):
            a, c = sysinfo["spans"][i]
            region = [max(0, a - 8), min(page["width"], c + 8), y0, y1]
        else:
            region = [x0, x1, y0, y1]
        out.append({"id": new_bar_id(), "system": si, "page": sysinfo["page"], "region": region, **b})
    return out


def _replace_system_bars(s: dict, si: int, bars: list[dict]) -> None:
    """用新识别的小节替换第 si 行原来的小节，再按行和行内顺序重新编号。
    和原来这一行的小节按内容 + 位置对上的，沿用 id、问答（练习记录按 id 自然保留），见 staleness.reconcile_bars。"""
    staleness.reconcile_bars(s, [b for b in s["bars"] if b["system"] == si], bars, staleness.ROW)
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
    claude = config.new_llm()
    meter = jobs.UsageMeter(claude)  # 每行完成就把新增的用量记上（中途失败也不漏记）
    try:
        _run_parse(sid, cancel, claude, meter.charge)
    finally:  # 失败 / 取消时还没记上的（读谱头、没跑完的行）：已经发出去的请求也花了钱
        meter.flush(sid)


def _run_parse(sid: str, cancel: threading.Event, claude: LLM, charge) -> None:
    song = store.load(sid)
    restage = bool(song.get("bars"))
    store.update(sid, lambda s: _begin_parse(s, "读取谱头"))
    head = claude.read_meta(store.page_image(sid, song["pages"][0]))
    # 用户手改过的艺人、调弦、变调夹、拍号以用户的为准
    meta = normalize_meta({**head, **(song.get("meta_user") or {})}, song.get("title", ""))
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

    def on_row(si, bars, err):
        # 失败 / 取消的行只标状态、不动小节：按变拍号重做的行失败时，第一遍识别出的小节还留着
        status = "error" if err else "done"
        if bars is not None:
            _replace_system_bars(work, si, [dict(b) for b in bars])
        work["systems"][si].update(status=status, error=err)

        def apply(s):
            if not restage:
                if bars is not None:
                    _replace_system_bars(s, si, bars)
                    apply_time_signatures(s["bars"], s["meta"])
                s["systems"][si].update(status=status, error=err)
            s["progress"]["done"] += 1
            charge(s)

        store.update(sid, apply)

    used, errors = _recognize_rows(
        claude, sid, work, [(i, None) for i in range(len(systems))], cancel, on_row
    )

    # 各行是并行识别的，前面某行写的变拍号后面的行看不到，模型会把小节硬凑成全曲拍号（每小节一样长，查不出来）。
    # 按第一遍的结果算出每行开头实际的拍号，和识别时用的不同的行带上它再识别一次（没有变拍号的曲子不多花钱）。
    # 重新识别已经有行失败：结果注定不会采用，不再为重做花钱
    redo: list[tuple[int, list[int], str]] = []
    if not cancel.is_set() and not (restage and errors):
        if restage:  # 原来的结果没动，没有要锁的行
            redo = _rows_to_redo(work["bars"], meta, used, skip_edited=False)
            if redo:
                store.patch(sid, progress=_redo_progress(redo))
        elif _rows_to_redo(work["bars"], meta, used, skip_edited=False):  # 先看一眼：没有要重做的就不写

            def begin_redo(s):
                _lock_redo(s, used, redo)

            store.update(sid, begin_redo)
    if redo:
        log.info("song=%s rerun rows (time signature / chord diagrams): %s", sid, redo)
        errors += _recognize_rows(claude, sid, work, redo, cancel, on_row)[1]

    if cancel.is_set():
        raise Cancelled()
    if restage and errors:
        raise RestageFailed("；".join(errors))
    if not work["bars"]:
        if restage:
            raise RestageFailed("没有识别出任何小节")
        _end_parse(sid, "error", ("error", "；".join(errors) or "没有识别出任何小节（可能不是六线谱）"))
        return
    apply_time_signatures(work["bars"], work["meta"])
    note = None
    if restage:

        def swap(s):
            # 一次性换成新结果（曲名只存在 song.title，不受谱头影响）。新旧小节按内容对上，对上的保留 id、问答、练习记录和手改
            # 识别期间用户改的谱头（只可能是艺人：调弦等识别时不能改）同样以用户的为准
            work["meta"] = normalize_meta(
                {**work["meta"], **(s.get("meta_user") or {})}, work["meta"]["title"]
            )
            changed = staleness.setting_changed(s.get("meta") or {}, work["meta"])
            metas = (s.get("meta") or {}, work["meta"])
            dropped = staleness.reconcile_bars(s, s["bars"], work["bars"], staleness.RESTAGE, changed, metas)
            s.update(meta=work["meta"], systems=work["systems"], bars=work["bars"])
            return dropped

        dropped = []
        store.update(sid, lambda s: dropped.append(swap(s)))
        if dropped and dropped[0]:
            note = f"重新识别完成；有 {dropped[0]} 个手改过的小节和新结果对不上，改动没有保留。"

    song = store.load(sid)
    store.patch(sid, progress={"stage": "生成练习建议", "done": len(systems), "total": len(systems)})
    seen = {b["id"]: b["events"] for b in song["bars"]}
    try:
        plan = claude.practice_plan(song["meta"], song["bars"])
    except Exception as e:  # noqa: BLE001 - 练习建议失败不影响使用
        log.warning("request_id=%s song=%s practice plan failed: %s", request_id_of(e), sid, e)
        plan = None

    def finish(s, meta=song["meta"]):
        if plan or not restage:  # 重新识别后生成建议失败：留着原来的建议（已标成过时）
            staleness.store_plan(s, plan, seen, meta)
        charge(s)
        msg = "；".join(errors) or note
        state.finish(s, status="ready", notice=("warn", msg) if msg else None, source="parse")

    store.update(sid, finish)


# ---------------------------------------------------------------------------
# 逐行识别（整首、补页的新行共用；单行重新识别只有一行，直接调 transcribe_queued）
# ---------------------------------------------------------------------------
def first_bar_of(song: dict, si: int) -> int:
    """第 si 行的起始小节号：前面各行已有的小节数；前面还没识别出小节的行按检测到的小节数估
    （识别失败的行算 0，整页一行的按 8 估）。只用来告诉模型小节号，写回时统一重新编号。"""
    first = 1
    for k, info in enumerate(song["systems"][:si]):
        n = sum(1 for b in song["bars"] if b["system"] == k)
        first += n or (len(info["spans"]) if info.get("spans") else 0 if info.get("status") == "error" else 8)
    return first


def _recognize_rows(
    claude: LLM,
    sid: str,
    song: dict,
    rows: list[tuple],
    cancel: threading.Event,
    on_row,
) -> tuple[dict[int, list[int]], list[str]]:
    """并行识别 rows：(行号, 这一行开头的拍号；None = 按 song 里前面各行写的变拍号推算[, 给模型的提示])。
    起始小节号和拍号在提交前按 song 算好。每行结束（成功、失败、取消）都在调用线程里调用
    on_row(行号, 小节 | None, 错误说明 | None)。返回 (每行识别时用的开头拍号, 失败的说明)。"""
    meta_ts = song["meta"]["time_signature"]
    plan = [
        (r[0], first_bar_of(song, r[0]), r[1] or _row_start_ts(song["bars"], song["meta"], r[0]), *r[2:3])
        for r in rows
    ]
    errors: list[str] = []
    with ThreadPoolExecutor(settings.row_concurrency) as pool:
        futs = {
            pool.submit(
                transcribe_queued,
                claude,
                sid,
                song,
                si,
                first,
                hint[0] if hint else "",
                cancel=cancel,
                start_ts=ts if ts != meta_ts else None,  # 和全曲拍号一样就不用特别告诉模型
            ): si
            for si, first, ts, *hint in plan
        }
        for fut in as_completed(futs):
            si = futs[fut]
            try:
                bars, err = fut.result(), None
            except (Cancelled, CancelledError):
                bars, err = None, "已取消"
            except Exception as e:
                rid = request_id_of(e)
                log.exception("request_id=%s song=%s system=%d failed", rid, sid, si)
                bars, err = None, friendly(e)
                errors.append(f"第 {si + 1} 行：{err}")
            on_row(si, bars, err)
            if cancel.is_set():
                for f in futs:
                    f.cancel()  # 还没开始的行不再发请求；正在进行的请求会跑完
    return {si: ts for si, _, ts, *_ in plan}, errors


def _row_start_ts(bars: list[dict], meta: dict, si: int) -> list[int]:
    """第 si 行开头实际的拍号：按顺序走过前面各行写的变拍号（ts_change）。"""
    cur = meta["time_signature"]
    for b in bars:
        if b["system"] >= si:
            break
        cur = b.get("ts_change") or cur
    return cur


def _rows_to_redo(
    bars: list[dict], meta: dict, used: dict[int, list[int]], skip_edited: bool = True
) -> list[tuple[int, list[int], str]]:
    """第一遍之后要再识别一次的行：(行号, 开头的拍号, 给模型的提示)。两种情况：
    - 开头实际的拍号（按前面各行写的变拍号推算）和识别时用的不一样（行首自己写了变拍号的不算，模型看得到）；
    - 弹唱谱：这一行读出的和弦图和别的行对同一个和弦的读法不一样（多数行一致时以多数为准，
      常见原因是“3fr.”这类位置标记算错了和弦图），带上多数的读法再识别，谱上的 x 才能换算对。
    skip_edited：用户改过的行不重做（不覆盖用户的改动）。"""
    consensus = _shape_consensus(bars)
    out = []
    for si in sorted(used):
        row = [b for b in bars if b["system"] == si]
        if not row or (skip_edited and any(b.get("edited") for b in row)):
            continue
        ts = _row_start_ts(bars, meta, si)
        ts_wrong = ts != used[si] and not row[0].get("ts_change")
        mine = {k: v for b in row for k, v in (b.get("shapes") or {}).items()}
        odd = {k: consensus[k] for k, v in mine.items() if k in consensus and v != consensus[k]}
        if not ts_wrong and not odd:
            continue
        hint = ""
        if odd:
            hint = (
                "The chord diagrams below were read the same way in the other lines of this score; use them "
                "(and resolve the x marks with them): "
                + "; ".join(f"{k} = frets for strings 1-6 {v}" for k, v in odd.items())
            )
        out.append((si, ts if ts_wrong else used[si], hint))
    return out


def _shape_consensus(bars: list[dict]) -> dict[str, list[int]]:
    """各行对同一个和弦图的读法：超过一半的行（至少两行）读成一样的，就以它为准。"""
    per_row: dict[tuple[int, str], tuple[int, ...]] = {}
    for b in bars:
        for name, frets in (b.get("shapes") or {}).items():
            per_row[(b.get("system", -1), name)] = tuple(frets)
    votes: dict[str, Counter] = {}
    for (_, name), frets in per_row.items():
        votes.setdefault(name, Counter())[frets] += 1
    out = {}
    for name, c in votes.items():
        frets, n = c.most_common(1)[0]
        if n >= 2 and n * 2 > sum(c.values()):
            out[name] = list(frets)
    return out


def _redo_progress(redo: list) -> dict:
    return {"stage": "复核个别行（变拍号、和弦图）", "done": 0, "total": len(redo)}


def _lock_redo(s: dict, used: dict[int, list[int]], redo: list) -> None:
    """挑出要重做的行（填进 redo）并把它们标成识别中，在同一次读-改-写里：中间用户改了的行不会被重做覆盖。"""
    redo[:] = _rows_to_redo(s["bars"], s["meta"], used)
    for si, *_ in redo:
        s["systems"][si].update(status="parsing", error=None)
    if redo:
        s["progress"] = _redo_progress(redo)


class RestageFailed(Exception):
    """重新识别没有全部成功：不替换原来的结果。"""


def _end_restage(sid: str, why: str) -> bool:
    """重新识别没完成（失败 / 取消）：原来的结果没动过，恢复成可练习的状态。不是重新识别时返回 False。"""
    done = []

    def apply(s):
        if state.restaging(s):
            state.finish(s, status="ready", notice=("warn", f"{why}，原来的结果保留不变。"), source="parse")
            done.append(True)

    store.update(sid, apply)
    return bool(done)


# ---------------------------------------------------------------------------
# 重新识别某一行
# ---------------------------------------------------------------------------
def start_reparse(sid: str, si: int, hint: str = "") -> None:
    """在请求里就登记任务（而不是等后台线程），连点两次或和整首识别撞上时直接拒绝。"""
    config.check_llm_ready()

    def begin(x):
        x["systems"][si].update(status="parsing", error=None)
        state.begin(x, "reparse_row", systems=[si])

    def run(cancel):
        meter = jobs.UsageMeter(None)
        try:
            meter = jobs.UsageMeter(config.new_llm())
            reparse_system(sid, si, hint, cancel, meter)
        except Exception as e:
            rid = request_id_of(e)
            log.exception("request_id=%s song=%s system=%d reparse failed", rid, sid, si)
            msg = "已取消" if isinstance(e, Cancelled) else friendly(e)

            def failed(x):
                x["systems"][si].update(status="error", error=msg)
                meter.charge(x)  # 失败 / 取消的这次也花了钱
                state.finish(x)

            store.update(sid, failed)

    jobs.start(sid, "这首曲子正在识别中", begin, run)


def reparse_system(
    sid: str,
    si: int,
    hint: str = "",
    cancel: threading.Event | None = None,
    meter: jobs.UsageMeter | None = None,
) -> None:
    meter = meter or jobs.UsageMeter(config.new_llm())
    song = store.load(sid)
    start_ts = _row_start_ts(song["bars"], song["meta"], si)  # 前面各行写的变拍号，这一行的图上看不到
    first = first_bar_of(song, si)
    bars = transcribe_queued(meter.claude, sid, song, si, first, hint=hint, cancel=cancel, start_ts=start_ts)

    def apply(s):
        others = {b["id"]: b.get("time_signature") for b in s["bars"] if b["system"] != si}
        _replace_system_bars(s, si, bars)  # 这一行变了的小节在这里标进练习建议的 changed_bars
        apply_time_signatures(s["bars"], s["meta"])  # 这一行新写的变拍号会影响后面的行
        s["systems"][si].update(status="done", error=None)
        if any(b.get("time_signature") != others[b["id"]] for b in s["bars"] if b["id"] in others):
            staleness.invalidate_plan(s)  # 后面各行的拍号跟着变了
        meter.charge(s)
        state.finish(s)

    store.update(sid, apply)


def recognize_rows_job(sid: str, rows: list[int], cancel: threading.Event, what: str = "新加的页") -> None:
    """后台识别已有曲子里的某几行（补页的新行、重试失败的行），每行完成就写回（其他行照常能练、能改）。
    调用方已经登记任务、把这几行标成识别中（state.begin "reparse_row"）。what：结束提示里怎么称呼这几行。"""
    meter = jobs.UsageMeter(None)
    failed: list[str] = []

    def on_row(si, bars, err):
        def apply(s):
            if bars is not None:
                _replace_system_bars(s, si, bars)
                apply_time_signatures(s["bars"], s["meta"])
            s["systems"][si].update(status="error" if err else "done", error=err)
            if s.get("progress"):
                s["progress"]["done"] = min(s["progress"]["total"], s["progress"]["done"] + 1)
            meter.charge(s)

        store.update(sid, apply)

    try:
        meter = jobs.UsageMeter(config.new_llm())
        used, errs = _recognize_rows(
            meter.claude, sid, store.load(sid), [(si, None) for si in rows], cancel, on_row
        )
        failed += errs
        # 新页上前面的行写了变拍号、后面的行并行识别时看不到：按实际的起始拍号再识别一次
        song = store.load(sid)
        redo: list[tuple[int, list[int], str]] = []
        if not cancel.is_set() and _rows_to_redo(
            song["bars"], song["meta"], used
        ):  # 先看一眼：没有要重做的就不写

            def begin_redo(s):
                _lock_redo(s, used, redo)

            store.update(sid, begin_redo)
        if redo:
            failed += _recognize_rows(meter.claude, sid, store.load(sid), redo, cancel, on_row)[1]
    except Exception as e:  # 建客户端失败之类：没开始的行都标成失败
        rid = request_id_of(e)
        log.exception("request_id=%s song=%s new pages failed", rid, sid)
        failed.append(friendly(e))
    finally:

        def end(s):
            for si in rows:
                if si < len(s["systems"]) and s["systems"][si].get("status") in ("parsing", "pending"):
                    s["systems"][si].update(status="error", error="已取消" if cancel.is_set() else "没有识别")
            msg = f"{what}有行没识别出来（" + "；".join(failed)[:300] + "），可以点“重试失败的行”再试"
            state.finish(s, notice=("warn", msg) if failed else None, source="reparse_row")
            meter.charge(s)

        store.update(sid, end)


# ---------------------------------------------------------------------------
# 重试失败的行
# ---------------------------------------------------------------------------
def failed_rows(song: dict) -> list[int]:
    return [i for i, x in enumerate(song.get("systems") or []) if x.get("status") == "error"]


def start_retry_failed(sid: str) -> None:
    """只重新识别失败的行（成功的行不动、不花钱）。一个小节都还没有（或谱头都没读出来）时整首识别。"""
    song = store.load(sid)
    if is_score(song):
        raise Invalid("Guitar Pro / MusicXML 曲子不需要识别")
    whole = not song.get("bars") or not song.get("meta")
    if not whole and not failed_rows(song):
        raise Invalid("没有识别失败的行")
    config.check_llm_ready()
    if whole:
        if song.get("status") not in ("error", "cancelled") and not failed_rows(song):
            raise Invalid("没有识别失败的行")
        if not start_parse(sid):
            raise Busy("这首曲子正在处理中")
        return
    rows: list[int] = []

    def begin(s):
        if state.recognizing(s):
            raise Busy("这首曲子正在识别中")
        rows[:] = failed_rows(s)  # 在锁里重新挑：和整首识别、单行重识别不会撞上
        if not rows:
            raise Invalid("没有识别失败的行")
        for i in rows:
            s["systems"][i].update(status="parsing", error=None)
        state.begin(
            s, "reparse_row", systems=rows, progress={"stage": "重试失败的行", "done": 0, "total": len(rows)}
        )

    jobs.start(
        sid, "这首曲子正在处理中", begin, lambda cancel: recognize_rows_job(sid, rows, cancel, "重试的")
    )
