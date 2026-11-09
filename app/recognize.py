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
from app.model import is_score, notes_key
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
# 太宽的行切成两半识别（见 _should_split / _transcribe_halves）。评测：难的指弹行两次平均 94.7%（不切三次平均 90.6%），
# 合成弹唱谱 99%（不切约 98%）；费用多约 23%，耗时不变
SPLIT_ROWS = True


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
        except Swapped as e:
            log.exception("song=%s parse failed after the new result was saved", sid)
            _end_parse(sid, "error", ("error", str(e)))
        except Exception as e:
            rid = request_id_of(e)
            log.exception("request_id=%s song=%s parse failed", rid, sid)
            if not _end_restage(sid, "重新识别失败：" + friendly(e)):
                _end_parse(sid, "error", ("error", friendly(e)))
        else:
            if (
                store.load(sid).get("status") == "ready"
            ):  # 识别成功（个别行失败也算）：设置了自动精修就接着精修整首
                _auto_refine(sid, cancel, None)

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
    def apply(s):
        _fail_unfinished_rows(s, "已取消" if status == "cancelled" else "识别中断，没有识别")
        systems = s.get("systems") or []
        if status == "error" and notice and s.get("bars") and all(x.get("status") == "done" for x in systems):
            # 每一行都识别完、写好了，出错在最后（生成练习建议、收尾存盘；重新识别时新结果已经换上）：不算识别失败
            state.finish(
                s,
                status="ready",
                notice=("warn", "识别结果已保存，但收尾时出错：" + notice[1]),
                source="parse",
            )
            return
        state.finish(s, status=status, notice=notice, source="parse")

    store.update(sid, apply)


def _auto_refine(sid: str, cancel: threading.Event, before: set | None) -> None:
    """设置了自动精修（config.refine_mode）时，识别结束后在同一个任务里接着精修。
    before：识别开始前各小节的 (id, 音符指纹)（_bar_keys），只精修这之后新识别出的小节；None = 整首。"""
    if cancel.is_set() or config.refine_mode() != "auto":
        return
    from app import refine  # refine 依赖本模块（全局名额）

    try:
        only = None
        if before is not None:
            only = {b["id"] for b in store.load(sid)["bars"] if (b["id"], notes_key(b)) not in before}
            if not only:
                return
        refine.refine_after(sid, cancel, only)
    except Exception:  # 识别已经完成，精修出错不影响识别结果
        log.exception("song=%s auto refine failed", sid)


def _bar_keys(song: dict) -> set[tuple[str, str]]:
    return {(b["id"], notes_key(b)) for b in song.get("bars") or []}


def _fail_unfinished_rows(s: dict, why: str) -> None:
    """任务结束时还停在“等待 / 识别中”的行（没轮到、被取消、写回出错、锁着等重做的）标成失败：
    不然它们一直锁着，“重试失败的行”也找不到。"""
    for x in s.get("systems") or []:
        if x.get("status") in ("parsing", "pending"):
            x.update(status="error", error=why)


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


def ocr_disagreements(
    bars: list[dict],
    ocr_bars: list[list[tuple[int, int]]],
    chord_sheet: bool = False,
    tied_in: dict[int, int] | None = None,
) -> dict[int, tuple]:
    """逐小节比较 AI 结果和本机数字识别读到的（弦, 品）。返回 {小节序号: (只有 OCR 读到的, 只有 AI 写了的)}。

    - OCR 在这个小节什么都没读到：不判断（可能是没读出来，不代表没有音）；
    - 死音（谱上写 x）OCR 不读，不比较；倚音的小数字 OCR 会读到，算进去；
    - 人工泛音旁边标的“品位 + 12”（如 2<14>）、点弦泛音点的品（如 5<22> 的 22）OCR 会多读一个，允许；
    - 整行一致率不到一半，或 OCR 只读对了 AI 写的音的不到 60%：多半是 OCR 在这种排版上读不准，整行都不判断。
    tied_in：图上第一个小节之前每根弦最后一个音的品位（右半行接左半的音、一行接上一行的音；{} = 前面没有音）。
    None = 不知道（各行并行识别，上一行还没结果）：第一个小节 AI 这边没有对不上的音时，OCR 多读的音每根弦
    放过一个（可能是接上一行的延续音），和后面小节的规则一样；但 AI 在小节开头这根弦上已经写了新音的不放过：
    延续音占着小节开头的这根弦，那里不会同时起一个新音，多出来的只能是小节中间漏掉的音。
    """
    pairs = []
    ai_total = 0  # 整行 AI 写的音（不含闷音）
    last_fret: dict[int, int] = dict(
        tied_in or {}
    )  # 每根弦上最后一个音的品位（判断跨小节线、写成括号的延续音）
    for i, (bar, seen) in enumerate(zip(bars, ocr_bars, strict=False)):
        tied_ok = set(last_fret.items())
        unknown_tie = i == 0 and tied_in is None
        ai: Counter = Counter()
        harmonic_marks: Counter = Counter()
        first = bar["events"][0]["notes"] if bar["events"] else []
        struck_at_start = {n["string"] for n in first if "x" not in n["tech"]}
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
                if n.get("harm"):  # 点弦泛音：谱上 <19> 这样的点弦位置 OCR 也会读到，不是另一个音
                    harmonic_marks[(n["string"], n["harm"])] += 1
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
            loose: set[int] = set()  # 不知道上一行时，已经放过一个的弦
            for key, k in list((got - ai).items()):
                if key in tied_ok:
                    got[key] -= k
                elif unknown_tie and key[0] not in loose and key[0] not in struck_at_start:
                    got[key] -= 1
                    loose.add(key[0])
            got += Counter()  # 去掉计数为 0 的
        if got:
            pairs.append((i, ai, got))
    agree = sum(sum((ai & got).values()) for _, ai, got in pairs)
    total = sum(max(sum(ai.values()), sum(got.values())) for _, ai, got in pairs)
    if not total or (not chord_sheet and agree / total < 0.5):
        return {}
    # OCR 在整行里只读对了 AI 结果的一小部分（斜体 / 手写风格的数字、很糊的截图）：它在这种排版上靠不住，
    # 这时的“不一致”多半是 OCR 读错，提示给模型反而会把对的改错。GP 等软件导出的谱子每行都在 78% 以上
    # 弹唱谱的 × 是按和弦图换算的，OCR 本来就看不到：改看 OCR 读到的数字里有多少和 AI 对得上
    ocr_total = sum(sum(got.values()) for _, _, got in pairs)
    if (agree < 0.7 * ocr_total) if chord_sheet else (agree < 0.6 * ai_total):
        return {}
    return {i: (got - ai, ai - got) for i, ai, got in pairs if got != ai}


# ---------------------------------------------------------------------------
# 识别一行
# ---------------------------------------------------------------------------
# 所有曲子加起来同时识别几行：信号量在导入时按 settings.max_rows_global 建好，之后改这个设置不生效（测试直接换掉 _system_slots）
_system_slots = threading.BoundedSemaphore(settings.max_rows_global)


def run_queued(fn, cancel: threading.Event | None = None):
    """排队等全局名额再执行 fn()（识别一行、精修一个小节）；排队期间点了取消就不再执行。"""
    while not _system_slots.acquire(timeout=0.2):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
    try:
        return fn()
    finally:
        _system_slots.release()


def transcribe_queued(*args, cancel: threading.Event | None = None, **kw) -> list[dict]:
    """排队等全局名额再识别一行；排队期间点了取消就不再识别。"""
    return run_queued(lambda: transcribe_system(*args, cancel=cancel, **kw), cancel)


def transcribe_system(
    claude: LLM,
    sid: str,
    song: dict,
    si: int,
    first_bar: int,
    hint: str = "",
    cancel: threading.Event | None = None,
    start_ts: list[int] | None = None,
    double_check: str | None = None,
    split: bool | None = None,
    context: str = "",
    tied_in: dict[int, int] | None = None,
) -> list[dict]:
    """识别一行。double_check：off / doubts（第一遍有警告或“不确定”才再读）/ all，见 config.double_check_mode；
    None = 按设置。split：一行太宽时切成左右两半分别识别（None = 按 SPLIT_ROWS，见 _should_split）。
    context：程序给模型的补充说明（不是用户的话）；tied_in：见 ocr_disagreements（见 _tied_in）。"""
    if double_check is None:
        double_check = config.double_check()
    sysinfo = song["systems"][si]
    if (SPLIT_ROWS if split is None else split) and _should_split(sysinfo):
        return _transcribe_halves(
            claude, sid, song, si, first_bar, hint, cancel, start_ts, double_check, context, tied_in
        )
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
            **({"context": context} if context else {}),
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
            and len(bars)
            >= 2  # 只有 1 个小节（短的末行、1 小节的半行）看不出“整行一样长”，按普通的时值问题返修
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
            found = ocr_disagreements(bars, ocr_bars, chord_sheet=bool(shapes), tied_in=tied_in)
            for i, (only_ocr, only_ai) in found.items():
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

    def rank(bars, issues) -> tuple[bool, int]:
        # 小节数对的优先（小节数不对时 OCR 不比，问题数显得少），再看问题少的
        return (bool(spans) and len(bars) != len(spans), len(issues))

    def read_once() -> tuple[list[dict], list]:
        """读一遍这一行：有问题返修一次；返修后小节数还不对就从头再读一次。"""
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
                if rank(bars2, issues2) <= rank(bars, issues):
                    bars, issues = bars2, issues2
        if spans and len(bars) != len(spans):
            # 返修后小节数还是不对（模型漏了 / 合并了整小节，比读错一个音严重得多）：不带上次的结果重新读一遍，取问题少的
            log.info(
                "request_id=%s song=%s system=%d bar count still wrong, rereading",
                claude.last_request_id,
                sid,
                si,
            )
            try:
                _raw3, bars3, issues3 = run()
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001 - 重读出错：用前面的结果
                log.warning("request_id=%s song=%s system=%d reread failed: %s", request_id_of(e), sid, si, e)
            else:
                if rank(bars3, issues3) < rank(bars, issues):
                    bars, issues = bars3, issues3
        return bars, issues

    bars, _ = read_once()
    doubtful = any(
        any(w.get("code") not in _INFO_ONLY for w in b.get("warn") or [])
        or b.get("_ocr")
        or str(b.get("uncertain") or "").strip()
        for b in bars
    )
    if (
        spans
        and len(bars) == len(spans)
        and (double_check == "all" or (double_check == "doubts" and doubtful))
    ):
        bars = _double_check(bars, read_once, len(spans), sid, si)
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
    # 小节数和检测到的小节线对得上，才按位置给每个小节它自己的区域；对不上（多了 / 漏了小节）时第 k 个小节不一定是
    # 第 k 个区间，按位置分会让精修拿别的小节的图去核对（refine.refinable 也会挡掉这样的行），都给整行的区域
    exact = bool(spans) and len(bars) == len(spans)
    for i, b in enumerate(bars):
        if exact:
            a, c = sysinfo["spans"][i]
            region = [max(0, a - 8), min(page["width"], c + 8), y0, y1]
        else:
            region = [x0, x1, y0, y1]
        out.append({"id": new_bar_id(), "system": si, "page": sysinfo["page"], "region": region, **b})
    return out


def _should_split(sysinfo: dict) -> bool:
    """这一行切成两半能不能明显看得更清楚：整行发给模型时受最长边限制放不大（宽的行、高分辨率截图），
    切开以后每半能多放大 1.4 倍以上；至少 2 个小节。"""
    spans = sysinfo.get("spans") or []
    if len(spans) < 2:
        return False
    x0, x1, y0, y1 = sysinfo["crop"]
    w, h = x1 - x0, y1 - y0

    def scale(width: float) -> float:
        return min(3.0, 2400 / width, 2576 / max(width, h))

    return scale(w / 2 + 40) >= 1.4 * scale(w)


def _transcribe_halves(
    claude: LLM,
    sid: str,
    song: dict,
    si: int,
    first_bar: int,
    hint: str,
    cancel: threading.Event | None,
    start_ts: list[int] | None,
    double_check: str,
    context: str = "",
    tied_in: dict[int, int] | None = None,
) -> list[dict]:
    """把一行切成左右两半，先识别左半，再带上左半结束时的拍号和和弦识别右半（右半开头延续的和弦，
    图上可能看不到它的和弦图）。图小了一半，能放大得更多，密集的符杠、小数字更容易看清。"""
    sysinfo = song["systems"][si]
    spans = sysinfo["spans"]
    k = (len(spans) + 1) // 2
    x0, x1, y0, y1 = sysinfo["crop"]
    lines = sysinfo.get("lines") or []
    sp = (lines[-1] - lines[0]) / 5 if len(lines) == 6 else 12
    cut = spans[k][0]
    halves = [
        {**sysinfo, "crop": [x0, min(x1, int(cut + sp)), y0, y1], "spans": spans[:k]},
        {**sysinfo, "crop": [max(x0, int(cut - sp * 2.5)), x1, y0, y1], "spans": spans[k:]},
    ]

    def sub(half: dict) -> dict:
        systems = list(song["systems"])
        systems[si] = half
        return {**song, "systems": systems}

    left = transcribe_system(
        claude,
        sid,
        sub(halves[0]),
        si,
        first_bar,
        hint,
        cancel,
        start_ts,
        double_check,
        split=False,
        context=context,
        tied_in=tied_in,
    )
    meta = song["meta"]
    ts = start_ts or meta["time_signature"]
    for b in left:
        ts = b.get("ts_change") or ts
    notes = [context] if context else []
    chord = next((e["chord"] for b in reversed(left) for e in reversed(b["events"]) if e.get("chord")), None)
    if chord:
        shapes = {k_: v for b in left for k_, v in (b.get("shapes") or {}).items()}
        frets = f" (diagram frets for strings 1-6: {shapes[chord]})" if chord in shapes else ""
        notes.append(
            f"This image is the right half of a line. The chord still sounding at its left edge is {chord}{frets}: "
            "x marks before the first chord name in this image belong to it. Return only the bars listed above."
        )
    else:
        notes.append(
            "This image is the right half of a line; the left half was read separately. "
            "Return only the bars listed above."
        )
    right = transcribe_system(
        claude,
        sid,
        sub(halves[1]),
        si,
        first_bar + len(left),
        hint,
        cancel,
        ts if ts != meta["time_signature"] else None,
        double_check,
        split=False,
        context=" ".join(notes),
        tied_in=_last_frets(left),
    )
    return left + right


def _tied_in(song: dict, si: int) -> dict[int, int] | None:
    """第 si 行开头之前每根弦最后一个音的品位（给 OCR 校对认行首的延续音）：第一行前面没有音；
    上一行还没有结果（各行并行识别）时不知道，返回 None。"""
    if si == 0:
        return {}
    prev = [b for b in song.get("bars") or [] if b.get("system") == si - 1]
    return _last_frets(prev) if prev else None


def _last_frets(bars: list[dict]) -> dict[int, int]:
    """每根弦上最后一个音的品位（闷音不算）。"""
    out: dict[int, int] = {}
    for b in bars:
        for e in b["events"]:
            for n in e["notes"]:
                if "x" not in n["tech"]:
                    out[n["string"]] = n["fret"]
    return out


def _bar_key(bar: dict) -> tuple:
    """比较两遍识别是否一致：每个起拍的时间、弦、品和技巧（点弦泛音连同点的品），以及小节总长
    （最后一个音的时值不同也算不一样）。"""
    t, out = Fraction(0), []
    for e in bar["events"]:
        if e["notes"]:
            out.append(
                (
                    t,
                    tuple(
                        sorted(
                            (n["string"], n["fret"], tuple(sorted(n["tech"])), n.get("harm", 0))
                            for n in e["notes"]
                        )
                    ),
                )
            )
        t += parse_dur(e["dur"])
    return (*out, t)


# 只是提醒、读对了也会有的警告（五连音之类不在播放网格上）：doubts 模式不为它再读一遍
_INFO_ONLY = frozenset({"off_grid"})
# 读错的明显迹象：两遍读法不一样又没有多数时，选没有这些警告的那遍
_BAD_READ = frozenset({"length", "placeholder", "empty", "string_range", "ocr_mismatch"})


def _bad_signs(bar: dict) -> int:
    return sum(1 for w in bar.get("warn") or [] if w.get("code") in _BAD_READ) + bool(bar.get("_ocr"))


def _double_check(first: list[dict], read_once, n: int, sid: str, si: int) -> list[dict]:
    """双重校对：再独立读一遍；两遍不一样的小节读第三遍当裁判，和哪一遍一样就用哪一遍。
    三遍都不一样的小节用第一遍的结果，标上“不确定”，提醒对照原谱。（评测：同一批难的小节，单遍三次 85%–93%，
    双重校对一次 91%：能减少偶尔的读错，提升不稳定；费用约 2 倍）"""
    try:
        second, _ = read_once()
    except Cancelled:
        raise
    except Exception as e:  # noqa: BLE001 - 第二遍失败：用第一遍
        log.warning("song=%s system=%d double check failed: %s", sid, si, e)
        return first
    if len(second) != n:
        return first
    diff = [i for i in range(n) if _bar_key(first[i]) != _bar_key(second[i])]
    if not diff:
        return first
    log.info("song=%s system=%d double check: bars %s differ, reading a third time", sid, si, diff)
    try:
        third, _ = read_once()
    except Cancelled:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("song=%s system=%d third read failed: %s", sid, si, e)
        third = []
    out = list(first)
    for i in diff:
        k3 = _bar_key(third[i]) if len(third) == n else None
        if k3 is not None and k3 == _bar_key(second[i]):
            out[i] = second[i]
        elif k3 is None or k3 != _bar_key(first[i]):
            # 没有多数：用明显问题少的那遍（一样多时用第一遍），标上“不确定”
            pick = second[i] if _bad_signs(second[i]) < _bad_signs(first[i]) else first[i]
            if k3 is None:
                note = "两次识别这个小节结果不一样（第三次没读出来），请对照原谱核对。"
            else:
                note = "三次识别这个小节结果都不一样，请对照原谱核对。"
            out[i] = {**pick, "uncertain": " ".join(x for x in (pick.get("uncertain", ""), note) if x)}
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
        _run_parse(sid, cancel, claude, meter)
    finally:  # 失败 / 取消时还没记上的（读谱头、没跑完的行）：已经发出去的请求也花了钱
        meter.flush(sid)


def _run_parse(sid: str, cancel: threading.Event, claude: LLM, meter: jobs.UsageMeter) -> None:
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
            meter.charge(s)

        meter.update(sid, apply)

    used, errors = _recognize_rows(
        claude, sid, work, [(i, None) for i in range(len(systems))], cancel, on_row
    )
    if _fix_header_time_signature(work, song.get("meta_user") or {}):
        # 谱头的拍号读错了：改成第 1 小节上写的。后面按旧拍号识别的行，下面按“开头实际拍号”重做；
        # 第 1 行自己是按图上的拍号读的，不用重做
        meta = work["meta"]
        used[0] = meta["time_signature"]
        if not restage:
            store.update(sid, lambda s: _fix_header_time_signature(s, song.get("meta_user") or {}))

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

    try:
        _finish_parse(sid, claude, meter, len(systems), restage, "；".join(errors) or note)
    except Exception as e:
        if restage:  # 新结果已经换上了，原来的结果不在了
            raise Swapped(friendly(e)) from e
        raise


def _finish_parse(
    sid: str, claude: LLM, meter: jobs.UsageMeter, n: int, restage: bool, msg: str | None
) -> None:
    """所有行都识别完、写好之后：生成练习建议，标成完成。"""
    song = store.load(sid)
    store.patch(sid, progress={"stage": "生成练习建议", "done": n, "total": n})
    seen = {b["id"]: b["events"] for b in song["bars"]}
    try:
        plan = claude.practice_plan(song["meta"], song["bars"])
    except Exception as e:  # noqa: BLE001 - 练习建议失败不影响使用
        log.warning("request_id=%s song=%s practice plan failed: %s", request_id_of(e), sid, e)
        plan = None

    def finish(s, meta=song["meta"]):
        if plan or not restage:  # 重新识别后生成建议失败：留着原来的建议（已标成过时）
            staleness.store_plan(s, plan, seen, meta)
        meter.charge(s)
        state.finish(s, status="ready", notice=("warn", msg) if msg else None, source="parse")

    meter.update(sid, finish)


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
    """并行识别 rows：(行号, 这一行开头的拍号；None = 按 song 里前面各行写的变拍号推算[, 程序给模型的补充说明（context）])。
    起始小节号和拍号在提交前按 song 算好。每行结束（成功、失败、取消）都在调用线程里调用
    on_row(行号, 小节 | None, 错误说明 | None)。返回 (每行识别时用的开头拍号, 失败的说明)。"""
    meta_ts = song["meta"]["time_signature"]
    plan = [
        (
            r[0],
            first_bar_of(song, r[0]),
            r[1] or _row_start_ts(song["bars"], song["meta"], r[0]),
            _tied_in(song, r[0]),
            *r[2:3],
        )
        for r in rows
    ]
    errors: list[str] = []
    pool = ThreadPoolExecutor(settings.row_concurrency)
    try:
        futs = {
            pool.submit(
                transcribe_queued,
                claude,
                sid,
                song,
                si,
                first,
                cancel=cancel,
                start_ts=ts if ts != meta_ts else None,  # 和全曲拍号一样就不用特别告诉模型
                context=note[0] if note else "",  # 程序给的说明（和弦图读法），不是用户写的提示
                tied_in=tied,
            ): si
            for si, first, ts, tied, *note in plan
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
    except BaseException:
        cancel.set()  # 写回出错（存盘失败、曲子被删）：还没开始的行不再发请求，正在进行的读完这一次就停
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return {si: ts for si, _, ts, *_ in plan}, errors


def _fix_header_time_signature(s: dict, meta_user: dict) -> bool:
    """第 1 小节就写着“变拍号”：其实是读谱头时把拍号读错了（例如 3/4 读成 4/4）。全曲拍号改成它，
    第 1 小节不再算变拍号。用户手动设过拍号的不动。返回有没有改。"""
    if "time_signature" in meta_user or not s.get("meta"):
        return False
    first = next((b for b in s.get("bars") or [] if b.get("system") == 0), None)
    ts = (first or {}).get("ts_change")
    if not ts or ts == s["meta"]["time_signature"]:
        return False
    s["meta"] = {**s["meta"], "time_signature": ts}
    first.pop("ts_change", None)
    first.pop("time_signature", None)
    apply_time_signatures(s["bars"], s["meta"])
    return True


def _row_start_ts(bars: list[dict], meta: dict, si: int) -> list[int]:
    """第 si 行开头实际的拍号：按顺序走过前面各行写的变拍号（ts_change）。"""
    cur = meta["time_signature"]
    for b in bars:
        if b["system"] >= si:
            break
        cur = b.get("ts_change") or cur
    return cur


def _rows_to_redo(
    bars: list[dict],
    meta: dict,
    used: dict[int, list[int]],
    skip_edited: bool = True,
    ts_only: set[int] | frozenset = frozenset(),
) -> list[tuple[int, list[int], str]]:
    """第一遍之后要再识别一次的行：(行号, 开头的拍号, 给模型的提示)。两种情况：
    - 开头实际的拍号（按前面各行写的变拍号推算）和识别时用的不一样（行首自己写了变拍号的不算，模型看得到）；
    - 弹唱谱：这一行读出的和弦图和别的行对同一个和弦的读法不一样（多数行一致时以多数为准，
      常见原因是“3fr.”这类位置标记算错了和弦图），带上多数的读法再识别，谱上的 x 才能换算对。
    skip_edited：用户改过的行不重做（不覆盖用户的改动）；ts_only：这几行只看拍号。"""
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
        if si in ts_only:
            odd = {}
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


def _lock_redo(
    s: dict, used: dict[int, list[int]], redo: list, ts_only: set[int] | frozenset = frozenset()
) -> None:
    """挑出要重做的行（填进 redo）并把它们标成识别中，在同一次读-改-写里：中间用户改了的行不会被重做覆盖。"""
    redo[:] = _rows_to_redo(s["bars"], s["meta"], used, ts_only=ts_only)
    for si, *_ in redo:
        s["systems"][si].update(status="parsing", error=None)
    if redo:
        s["progress"] = _redo_progress(redo)


class RestageFailed(Exception):
    """重新识别没有全部成功：不替换原来的结果。"""


class Swapped(Exception):
    """重新识别的新结果已经换上之后（生成练习建议、收尾时）出的错：原来的结果已经不在了，不能说“保留不变”。"""


def _end_restage(sid: str, why: str) -> bool:
    """重新识别没完成（失败 / 取消）：原来的结果没动过，状态也照旧（原来有行失败的还是失败，
    全部行都成功的是可练习）。不是重新识别（或新结果已经换上）时返回 False。"""
    done = []

    def apply(s):
        if state.restaging(s):
            msg = f"{why}，原来的结果保留不变。"
            if failed_rows(s):
                msg += f"原来有 {len(failed_rows(s))} 行没识别出来，可以点“重试失败的行”再试。"
            state.finish(s, notice=("warn", msg), source="parse")  # 不给 status：照旧（见 state._settle）
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
        before = _bar_keys(store.load(sid))
        try:
            meter = jobs.UsageMeter(config.new_llm())
            reparse_system(sid, si, hint, cancel, meter)
        except Exception as e:
            rid = request_id_of(e)
            log.exception("request_id=%s song=%s system=%d reparse failed", rid, sid, si)
            msg = "已取消" if isinstance(e, Cancelled) else friendly(e)

            def failed(x):
                _fail_unfinished_rows(x, msg)  # 这一行，以及按新拍号锁着等重做的行
                meter.charge(x)  # 失败 / 取消的这次也花了钱
                state.finish(x)

            meter.update(sid, failed)
        else:
            _auto_refine(sid, cancel, before)

    jobs.start(sid, "这首曲子正在识别中", begin, run)


def reparse_system(
    sid: str,
    si: int,
    hint: str = "",
    cancel: threading.Event | None = None,
    meter: jobs.UsageMeter | None = None,
) -> None:
    meter = meter or jobs.UsageMeter(config.new_llm())
    cancel = cancel or threading.Event()
    song = store.load(sid)
    start_ts = _row_start_ts(song["bars"], song["meta"], si)  # 前面各行写的变拍号，这一行的图上看不到
    first = first_bar_of(song, si)
    bars = transcribe_queued(
        meter.claude,
        sid,
        song,
        si,
        first,
        hint=hint,
        cancel=cancel,
        start_ts=start_ts,
        tied_in=_tied_in(song, si),
    )

    def apply(s):
        others = {b["id"]: b.get("time_signature") for b in s["bars"] if b["system"] != si}
        _replace_system_bars(s, si, bars)  # 这一行变了的小节在这里标进练习建议的 changed_bars
        apply_time_signatures(s["bars"], s["meta"])  # 这一行新写的变拍号会影响后面的行
        s["systems"][si].update(status="done", error=None)
        if any(b.get("time_signature") != others[b["id"]] for b in s["bars"] if b["id"] in others):
            staleness.invalidate_plan(s)  # 后面各行的拍号跟着变了
        meter.charge(s)

    meter.update(sid, apply)
    failed = _redo_after(sid, song, {si: start_ts}, [si], cancel, meter, _row_writer(sid, meter))

    def finish(s):
        _fail_unfinished_rows(s, "已取消" if cancel.is_set() else "没有识别")
        msg = (
            "按新的拍号重做后面的行时有行没识别出来（"
            + "；".join(failed)[:300]
            + "），可以点“重试失败的行”再试"
        )
        state.finish(s, notice=("warn", msg) if failed else None, source="reparse_row")
        meter.charge(s)

    meter.update(sid, finish)


def _row_writer(sid: str, meter: jobs.UsageMeter):
    """识别已有曲子里的行时，每行结束就写回（_recognize_rows 的 on_row）。"""

    def on_row(si, bars, err):
        def apply(s):
            if bars is not None:
                _replace_system_bars(s, si, bars)
                apply_time_signatures(s["bars"], s["meta"])
            s["systems"][si].update(status="error" if err else "done", error=err)
            if s.get("progress"):
                s["progress"]["done"] = min(s["progress"]["total"], s["progress"]["done"] + 1)
            meter.charge(s)

        meter.update(sid, apply)

    return on_row


def _redo_after(
    sid: str,
    before: dict,
    used: dict[int, list[int]],
    rows: list[int],
    cancel,
    meter: jobs.UsageMeter,
    on_row,
) -> list[str]:
    """已有曲子里的 rows 识别完之后（used：它们识别时开头的拍号；before：识别前的曲子）：
    - 识别的是第一行：它第 1 小节写的拍号可能说明谱头的拍号读错了（同整首识别，见 _fix_header_time_signature）；
    - 其他行开头实际的拍号（按现在各行写的变拍号推算）和它们当初识别时的（按 before 推算）不一样了
      （这几行写了 / 去掉了变拍号、改了全曲拍号，补页插进来的新行写了变拍号），按新拍号再识别一次。
    返回失败的说明。"""
    if 0 in rows and not cancel.is_set():
        fixed = []
        store.update(sid, lambda s: fixed.append(_fix_header_time_signature(s, s.get("meta_user") or {})))
        if fixed[0]:  # 第一行自己是按图上的拍号读的，不用重做
            used = {**used, 0: store.load(sid)["meta"]["time_signature"]}
    others = {k for k in range(len(before["systems"])) if k not in used}
    used = {**{k: _row_start_ts(before["bars"], before["meta"], k) for k in others}, **used}
    song = store.load(sid)
    redo: list[tuple[int, list[int], str]] = []
    # 先看一眼：没有要重做的就不写。别的行只看拍号（和弦图的多数读法整首识别时已经处理过）
    if not cancel.is_set() and _rows_to_redo(song["bars"], song["meta"], used, ts_only=others):
        store.update(sid, lambda s: _lock_redo(s, used, redo, ts_only=others))
    if not redo:
        return []
    log.info("song=%s rerun rows after %s (time signature): %s", sid, rows, redo)
    return _recognize_rows(meter.claude, sid, store.load(sid), redo, cancel, on_row)[1]


def recognize_rows_job(sid: str, rows: list[int], cancel: threading.Event, what: str = "新加的页") -> None:
    """后台识别已有曲子里的某几行（补页的新行、重试失败的行），每行完成就写回（其他行照常能练、能改）。
    调用方已经登记任务、把这几行标成识别中（state.begin "reparse_row"）。what：结束提示里怎么称呼这几行。"""
    meter = jobs.UsageMeter(None)
    failed: list[str] = []
    keys = _bar_keys(store.load(sid))

    try:
        meter = jobs.UsageMeter(config.new_llm())
        before = store.load(sid)
        on_row = _row_writer(sid, meter)
        used, errs = _recognize_rows(meter.claude, sid, before, [(si, None) for si in rows], cancel, on_row)
        failed += errs
        failed += _redo_after(sid, before, used, rows, cancel, meter, on_row)
    except Exception as e:  # 建客户端失败之类：没开始的行都标成失败
        rid = request_id_of(e)
        log.exception("request_id=%s song=%s new pages failed", rid, sid)
        failed.append(friendly(e))
    finally:

        def end(s):
            # 这几行，以及按新拍号锁着等重做的行
            _fail_unfinished_rows(s, "已取消" if cancel.is_set() else "没有识别")
            msg = f"{what}有行没识别出来（" + "；".join(failed)[:300] + "），可以点“重试失败的行”再试"
            state.finish(s, notice=("warn", msg) if failed else None, source="reparse_row")
            meter.charge(s)

        meter.update(sid, end)
    _auto_refine(sid, cancel, keys)  # 只精修刚识别出的小节


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
