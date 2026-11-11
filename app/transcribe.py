"""识别一行：裁图（标弦号、本机 OCR 读数字）→ 调模型 → 检查（时值、小节数、和 OCR 对不对得上）→ 有问题返修一次
→（按设置）再读一两遍投票 → 整理成要写进曲子的小节。太宽的行切成左右两半分别识别。

只读曲子、不写回：写回、排队等全局名额、并行识别几行由 app/recognize.py 负责。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace
from fractions import Fraction

import numpy as np
from PIL import Image

from app import ocr, ocrcheck, prompts, store
from app.jobs import Cancelled
from app.llm import LLM, LLMError, request_id_of
from app.music import (
    PLACEHOLDER_MSG,
    bar_length,
    check_bar,
    clean_events,
    clean_repeats,
    clean_shapes,
    clean_time_signature,
    new_bar_id,
    parse_dur,
    rest_bar,
    warn,
)
from app.systems import bar_regions, label_strings, should_split, split_halves, tab_lines

log = logging.getLogger("tab_trainer.recognize")


@dataclass(frozen=True)
class RowOptions:
    """识别的选项。应用里由 recognize.options() 在一个识别任务开始时定好（读 recognize.LABEL_STRINGS 等开关和
    双重校对的设置），之后各行都按它，任务进行中改设置不影响它；测试、评测直接构造。"""

    double_check: str = (
        "off"  # off / doubts（第一遍有警告或“不确定”才再读）/ all，见 config.double_check_mode
    )
    split: bool = True  # 太宽的行切成两半识别（见 systems.should_split）
    ocr_check: bool = True  # 用本机数字识别校对 AI 读出的弦和品（见 app/ocrcheck.py）
    label_strings: bool = True  # 在裁剪图上标弦号（见 systems.label_strings）
    ocr_marks: bool | None = None  # OCR 数字块连同括号一起读（None = ocr.MARKS_IN_BLOCK）


@dataclass(frozen=True)
class RowTask:
    """要识别的一行。first_bar：起始小节号（只用来告诉模型，写回时统一重新编号）；start_ts：这一行开头实际的拍号
    （前面的行变过拍号时；None = 全曲拍号）；hint：用户写的提示；context：程序给模型的补充说明（不是用户的话）；
    tied_in：见 ocrcheck.ocr_disagreements。"""

    si: int
    first_bar: int
    hint: str = ""
    start_ts: list[int] | None = None
    context: str = ""
    tied_in: dict[int, int] | None = None


@dataclass
class RowImage:
    """发给模型的这一行：裁剪图（可能标了弦号）、小节在图里的区间（整页一行时为 None）、OCR 读到的每个小节的（弦, 品）。"""

    img: Image.Image
    spans: list[tuple[float, float]] | None
    labeled: bool = False
    ocr_bars: list[list[tuple[int, int]]] | None = None

    @classmethod
    def load(cls, sid: str, song: dict, si: int, opts: RowOptions) -> RowImage:
        sysinfo = song["systems"][si]
        x0, x1, y0, y1 = sysinfo["crop"]
        img = store.page_image(sid, song["pages"][sysinfo["page"]]).crop((x0, y0, x1, y1))
        spans = [(a - x0, b - x0) for a, b in sysinfo["spans"]] if sysinfo["spans"] else None
        lines = tab_lines(sid, song, sysinfo) if spans else None
        ocr_bars = None
        if spans and lines and opts.ocr_check:
            try:
                ocr_bars = ocr.read_system(
                    np.asarray(img.convert("L")), lines, spans, marks_in_block=opts.ocr_marks
                )
            except Exception as e:  # noqa: BLE001 - 校对只是锦上添花，出错就跳过
                log.warning("song=%s system=%d ocr failed: %s", sid, si, e)
        labeled = bool(lines and opts.label_strings)
        if labeled:
            img = label_strings(img, lines, spans[0][0])
        return cls(img, spans, labeled, ocr_bars)


@dataclass
class Reading:
    """模型读一次的结果：原始回答、整理好的小节、要返修的问题 [(警告 code, 发给模型的文字)]。"""

    raw: dict
    bars: list[dict]
    issues: list[tuple[str, str]]

    def rank(self, spans) -> tuple[bool, int]:
        """越小越好：小节数对的优先（小节数不对时 OCR 不比，问题数显得少），再看问题少的。"""
        return (bool(spans) and len(self.bars) != len(spans), len(self.issues))


# ---------------------------------------------------------------------------
# 检查模型的回答（纯函数）
# ---------------------------------------------------------------------------
def parse_bars(
    raw: dict, meta: dict, first_bar: int, start_ts: list[int] | None = None
) -> tuple[list[dict], list[tuple[str, str]], dict]:
    """模型的回答 → (小节, 要返修的问题, 这一行的和弦图)。小节上带着按内容算的警告；没读出音符的小节按整小节休止占位。"""
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
        # 只有 1 个小节（短的末行、1 小节的半行）看不出“整行一样长”，按普通的时值问题返修
        and len(bars) >= 2
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
    return bars, issues, shapes


def check_reading(raw: dict, meta: dict, task: RowTask, row: RowImage) -> Reading:
    """整理一次回答，再查小节数、和本机 OCR 对不对得上（对不上的小节记在 bar["_ocr"]，最后变成警告）。"""
    bars, issues, shapes = parse_bars(raw, meta, task.first_bar, task.start_ts)
    if row.spans and len(bars) != len(row.spans):
        issues.append(("other", f"应有 {len(row.spans)} 个小节，返回了 {len(bars)} 个"))
    elif row.ocr_bars:
        found = ocrcheck.ocr_disagreements(bars, row.ocr_bars, chord_sheet=bool(shapes), tied_in=task.tied_in)
        for i, (only_ocr, only_ai) in found.items():
            if shapes:
                only_ai = []
                if not only_ocr:
                    continue
            issues.append(("ocr_mismatch", ocrcheck.mismatch_issue(task.first_bar + i, only_ocr, only_ai)))
            bars[i]["_ocr"] = (only_ocr, only_ai)
    return Reading(raw, bars, issues)


# 只是提醒、读对了也会有的警告（五连音之类不在播放网格上）：doubts 模式不为它再读一遍
INFO_ONLY = frozenset({"off_grid"})
# 读错的明显迹象：两遍读法不一样又没有多数时，选没有这些警告的那遍
BAD_READ = frozenset({"length", "placeholder", "empty", "string_range", "ocr_mismatch"})


def doubtful(bars: list[dict]) -> bool:
    """这一遍有没有值得再读一遍的疑点：要紧的警告、和 OCR 对不上、模型自己说“不确定”。"""
    return any(
        any(w.get("code") not in INFO_ONLY for w in b.get("warn") or [])
        or b.get("_ocr")
        or str(b.get("uncertain") or "").strip()
        for b in bars
    )


def bad_signs(bar: dict) -> int:
    return sum(1 for w in bar.get("warn") or [] if w.get("code") in BAD_READ) + bool(bar.get("_ocr"))


def bar_key(bar: dict) -> tuple:
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


# ---------------------------------------------------------------------------
# 读一行
# ---------------------------------------------------------------------------
class RowReader:
    """对着一行的图问模型：call 一次调用；read_once 读一遍（有问题返修一次，返修后小节数还不对就从头再读一次）。"""

    def __init__(
        self,
        claude: LLM,
        sid: str,
        meta: dict,
        task: RowTask,
        row: RowImage,
        cancel: threading.Event | None = None,
    ):
        self.claude, self.sid, self.meta, self.task, self.row, self.cancel = (
            claude,
            sid,
            meta,
            task,
            row,
            cancel,
        )

    def call(self, previous: dict | None = None, problems: list[str] | None = None) -> Reading:
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()
        t = self.task
        raw = self.claude.transcribe(
            self.row.img,
            self.meta,
            t.first_bar,
            self.row.spans,
            hint=t.hint,
            previous=previous,
            problems=problems,
            labeled_strings=self.row.labeled,
            start_ts=t.start_ts,
            **({"context": t.context} if t.context else {}),
        )
        return check_reading(raw, self.meta, t, self.row)

    def _try(self, what: str, **kw) -> Reading | None:
        """返修 / 重读：出错（过载、断线）时返回 None，用前面的结果。"""
        try:
            return self.call(**kw)
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001 - 用前面的结果，警告照常标在小节上
            log.warning(
                "request_id=%s song=%s system=%d %s failed: %s",
                request_id_of(e),
                self.sid,
                self.task.si,
                what,
                e,
            )
            return None

    def read_once(self) -> tuple[list[dict], list]:
        spans, si = self.row.spans, self.task.si
        best = self.call()
        if best.issues:
            texts = [t for _, t in best.issues]
            log.info(
                "request_id=%s song=%s system=%d retry: %s", self.claude.last_request_id, self.sid, si, texts
            )
            fixed = self._try("retry", previous=best.raw, problems=texts)
            if fixed is not None and fixed.rank(spans) <= best.rank(spans):
                best = fixed
        if spans and len(best.bars) != len(spans):
            # 返修后小节数还是不对（模型漏了 / 合并了整小节，比读错一个音严重得多）：不带上次的结果重新读一遍，取问题少的
            log.info(
                "request_id=%s song=%s system=%d bar count still wrong, rereading",
                self.claude.last_request_id,
                self.sid,
                si,
            )
            again = self._try("reread")
            if again is not None and again.rank(spans) < best.rank(spans):
                best = again
        return best.bars, best.issues


def vote(first: list[dict], read_once, n: int, sid: str, si: int) -> list[dict]:
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
    diff = [i for i in range(n) if bar_key(first[i]) != bar_key(second[i])]
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
        k3 = bar_key(third[i]) if len(third) == n else None
        if k3 is not None and k3 == bar_key(second[i]):
            out[i] = second[i]
        elif k3 is None or k3 != bar_key(first[i]):
            # 没有多数：用明显问题少的那遍（一样多时用第一遍），标上“不确定”
            pick = second[i] if bad_signs(second[i]) < bad_signs(first[i]) else first[i]
            if k3 is None:
                note = "两次识别这个小节结果不一样（第三次没读出来），请对照原谱核对。"
            else:
                note = "三次识别这个小节结果都不一样，请对照原谱核对。"
            out[i] = {**pick, "uncertain": " ".join(x for x in (pick.get("uncertain", ""), note) if x)}
    return out


def finish_bars(bars: list[dict], song: dict, si: int) -> list[dict]:
    """整理成要写进曲子的小节：和 OCR 还对不上的留警告，加上 id、所在行 / 页和在页面上的区域。"""
    sysinfo = song["systems"][si]
    page = song["pages"][sysinfo["page"]]
    for b in bars:
        only = b.pop("_ocr", None)
        if only:
            b["warn"] = [*b["warn"], ocrcheck.mismatch_warning(*only)]
    regions = bar_regions(sysinfo, page["width"], len(bars))
    return [
        {"id": new_bar_id(), "system": si, "page": sysinfo["page"], "region": region, **b}
        for b, region in zip(bars, regions, strict=True)
    ]


def transcribe_system(
    claude: LLM,
    sid: str,
    song: dict,
    si: int,
    first_bar: int,
    hint: str = "",
    cancel: threading.Event | None = None,
    start_ts: list[int] | None = None,
    opts: RowOptions | None = None,
    context: str = "",
    tied_in: dict[int, int] | None = None,
) -> list[dict]:
    """识别第 si 行，返回要写进曲子的小节（参数见 RowTask、RowOptions；opts 为 None 时用 RowOptions 的默认值）。
    检测到了小节线的行一个小节都没读出来：LLMError（这一行算失败）。整页一行（没检测到六线谱）读出 0 个小节：
    这一页没有六线谱（封面、歌词页），返回 []（重新识别时这一页原来有小节的，调用方按失败处理，见
    recognize.check_page_not_emptied）。"""
    opts = opts or RowOptions()
    task = RowTask(si, first_bar, hint, start_ts, context, tied_in)
    if opts.split and should_split(song["systems"][si]):
        return _transcribe_halves(claude, sid, song, task, opts, cancel)
    row = RowImage.load(sid, song, si, opts)
    reader = RowReader(claude, sid, song["meta"], task, row, cancel)
    bars, _ = reader.read_once()
    spans = row.spans
    if (
        spans
        and len(bars) == len(spans)
        and (opts.double_check == "all" or (opts.double_check == "doubts" and doubtful(bars)))
    ):
        bars = vote(bars, reader.read_once, len(spans), sid, si)
    if (
        not bars and spans
    ):  # 返修后还是一个小节都没有：算这一行失败（不能标成完成；重新识别时会整行删掉原来的小节）
        raise LLMError("模型没有读出这一行的小节，请重新识别这一行", getattr(claude, "last_request_id", None))
    # 检测不到六线谱、整页当一行的（spans 为 None）：模型说这一页没有六线谱（封面、歌词页）就是 0 个小节，第一次识别时
    # 这一行照常算完成；重新识别时原来这一页有小节的不算（见 recognize.check_page_not_emptied）
    return finish_bars(bars, song, si)


def _transcribe_halves(
    claude: LLM, sid: str, song: dict, task: RowTask, opts: RowOptions, cancel: threading.Event | None
) -> list[dict]:
    """把一行切成左右两半，先识别左半，再带上左半结束时的拍号和和弦识别右半（右半开头延续的和弦，
    图上可能看不到它的和弦图）。图小了一半，能放大得更多，密集的符杠、小数字更容易看清。"""
    si = task.si
    left_half, right_half = split_halves(song["systems"][si])
    whole = replace(opts, split=False)

    def sub(half: dict) -> dict:
        systems = list(song["systems"])
        systems[si] = half
        return {**song, "systems": systems}

    left = transcribe_system(
        claude,
        sid,
        sub(left_half),
        si,
        task.first_bar,
        task.hint,
        cancel,
        task.start_ts,
        whole,
        context=task.context,
        tied_in=task.tied_in,
    )
    meta = song["meta"]
    ts = task.start_ts or meta["time_signature"]
    for b in left:
        ts = b.get("ts_change") or ts
    chord = next((e["chord"] for b in reversed(left) for e in reversed(b["events"]) if e.get("chord")), None)
    shapes = {k: v for b in left for k, v in (b.get("shapes") or {}).items()}
    right = transcribe_system(
        claude,
        sid,
        sub(right_half),
        si,
        task.first_bar + len(left),
        task.hint,
        cancel,
        ts if ts != meta["time_signature"] else None,
        whole,
        context=prompts.right_half_context(task.context, chord, shapes.get(chord) if chord else None),
        tied_in=ocrcheck.last_frets(left),
    )
    return left + right
