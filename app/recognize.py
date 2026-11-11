"""识别图片 / PDF 谱子的任务：读谱头 → 版面（app/systems.py）→ 逐行识别（app/transcribe.py：本机 OCR 校对、有问题返修）
→ 复核变拍号和和弦图（app/review.py）→ 练习建议；整首重新识别、单行重新识别、补页的新行、重试失败的行。

这里只管编排：排队等全局名额（jobs.run_queued）、几行并行、每行结束写回、任务的开始和结束。
识别的选项（双重校对、切半、OCR 校对、标弦号）在任务开始时定好一次（options），之后各行都按它。
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed

from app import config, jobs, ocr, refine, review, staleness, state, store
from app.config import settings
from app.errors import Busy, Invalid, NotFound
from app.jobs import Cancelled
from app.llm import LLM, LLMError, friendly, request_id_of
from app.model import is_score, notes_key
from app.music import apply_time_signatures, normalize_meta
from app.ocrcheck import ocr_disagreements, tied_in
from app.systems import WHOLE_PAGE_BARS, label_strings, plan_page_systems, plan_systems, tab_lines
from app.transcribe import RowOptions, transcribe_system

# 别的模块、脚本和测试按 recognize.xxx 用的名字
__all__ = [
    "RowOptions",
    "label_strings",
    "ocr_disagreements",
    "plan_page_systems",
    "plan_systems",
    "tab_lines",
    "transcribe_system",
]

log = logging.getLogger("tab_trainer.recognize")

# 识别的开关（任务开始时读，见 options；测试可以直接改）
LABEL_STRINGS = True  # 在裁剪图上标弦号（见 systems.label_strings）
OCR_CHECK = True  # 用本机数字识别校对 AI 读出的弦和品（见 app/ocr.py、app/ocrcheck.py）
# 太宽的行切成两半识别（见 systems.should_split / transcribe._transcribe_halves）。评测：难的指弹行两次平均 94.7%
# （不切三次平均 90.6%），合成弹唱谱 99%（不切约 98%）；费用多约 23%，耗时不变
SPLIT_ROWS = True


def options() -> RowOptions:
    """这次识别任务的选项：上面的开关、ocr.MARKS_IN_BLOCK 和设置里的双重校对，读一次，之后各行都按它
    （任务进行中改设置不会让同一首曲子前后几行按不同的方式识别）。"""
    return RowOptions(
        double_check=config.double_check(),
        split=SPLIT_ROWS,
        ocr_check=OCR_CHECK,
        label_strings=LABEL_STRINGS,
        ocr_marks=ocr.MARKS_IN_BLOCK,
    )


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
# 逐行识别（整首、补页的新行、重试失败的行共用；单行重新识别只有一行，直接调 transcribe_queued）
# ---------------------------------------------------------------------------
def transcribe_queued(*args, cancel: threading.Event | None = None, **kw) -> list[dict]:
    """排队等全局名额再识别一行（参数同 transcribe.transcribe_system）；排队期间点了取消就不再识别。"""
    return jobs.run_queued(lambda: transcribe_system(*args, cancel=cancel, **kw), cancel)


def first_bar_of(song: dict, si: int) -> int:
    """第 si 行的起始小节号：前面各行已有的小节数；前面还没识别出小节的行按检测到的小节数估
    （识别失败的行、识别完了但没有小节的整页（没有六线谱的页）算 0，还没识别的整页一行按 WHOLE_PAGE_BARS 估）。
    只用来告诉模型小节号，写回时统一重新编号。"""
    first = 1
    for k, info in enumerate(song["systems"][:si]):
        n = sum(1 for b in song["bars"] if b["system"] == k)
        if n or info.get("status") in ("error", "done"):
            first += n
        else:
            first += len(info["spans"]) if info.get("spans") else WHOLE_PAGE_BARS
    return first


class EmptiedPage(LLMError):
    """重新识别整页一行（没检测到六线谱的页）时模型说这一页没有六线谱，但这一页原来有小节：按失败处理，原来的小节不动。"""


def _bars_per_page(song: dict) -> Counter:
    """每一页现在有几个小节（小节没记页号的按所在行的页）。"""
    systems = song.get("systems") or []
    return Counter(
        b["page"] if b.get("page") is not None else systems[b["system"]]["page"]
        for b in song.get("bars") or []
    )


def check_page_not_emptied(system: dict, bars: list[dict], had: Counter) -> None:
    """整页一行读出 0 个小节：第一次识别时是“这一页没有六线谱”（封面、歌词页），这一行照常算完成（见 transcribe_system）。
    重新识别（单行、重试失败的行、整首）时这一页原来有小节（had：_bars_per_page）：多半是模型这次没看出来，
    不能悄悄把原来的小节、手改、问答都删掉（练习记录也对不上），按失败处理（EmptiedPage）。"""
    if not bars and had.get(system["page"]):
        raise EmptiedPage(
            f"模型这次说第 {system['page'] + 1} 页没有六线谱，但这一页原来有 {had[system['page']]} 个小节，没有采用这次的结果"
        )


def _recognize_rows(
    claude: LLM,
    sid: str,
    song: dict,
    rows: list[tuple],
    cancel: threading.Event,
    on_row,
    opts: RowOptions | None = None,
    had: Counter | None = None,
) -> tuple[dict[int, list[int]], list[str]]:
    """并行识别 rows：(行号, 这一行开头的拍号；None = 按 song 里前面各行写的变拍号推算[, 程序给模型的补充说明（context）])。
    起始小节号和拍号在提交前按 song 算好。每行结束（成功、失败、取消）都在调用线程里调用
    on_row(行号, 小节 | None, 错误说明 | None)。opts：这次任务的选项（None = 现在的 options()）。
    had：识别前每一页有几个小节（见 check_page_not_emptied；None = 按 song 算）。
    返回 (每行识别时用的开头拍号, 失败的说明)。"""
    opts = opts or options()
    had = _bars_per_page(song) if had is None else had
    meta_ts = song["meta"]["time_signature"]
    plan = [
        (
            r[0],
            first_bar_of(song, r[0]),
            r[1] or review.row_start_ts(song["bars"], song["meta"], r[0]),
            tied_in(song, r[0]),
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
                opts=opts,
                context=note[0] if note else "",  # 程序给的说明（和弦图读法），不是用户写的提示
                tied_in=tied,
            ): si
            for si, first, ts, tied, *note in plan
        }
        for fut in as_completed(futs):
            si = futs[fut]
            try:
                bars, err = fut.result(), None
                check_page_not_emptied(song["systems"][si], bars, had)
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


def _write_row(s: dict, si: int, bars: list[dict] | None, err: str | None) -> None:
    """一行结束，写进曲子：失败 / 取消的行只标状态、不动小节（按变拍号重做的行失败时，第一遍识别出的小节还留着）。"""
    if bars is not None:
        _replace_system_bars(s, si, bars)
        apply_time_signatures(s["bars"], s["meta"])  # 这一行新写的变拍号会影响后面的行
    s["systems"][si].update(status="error" if err else "done", error=err)


def _row_writer(sid: str, meter: jobs.UsageMeter, write=_write_row, work: dict | None = None):
    """每行结束就写回（_recognize_rows 的 on_row）：write(曲子, 行号, 小节, 错误) 写进存盘的曲子（None = 不写，
    只记进度和用量）；work：同时把结果攒进这个内存里的曲子（整首识别）。"""

    def on_row(si, bars, err):
        if work is not None:
            if bars is not None:
                _replace_system_bars(work, si, [dict(b) for b in bars])
            work["systems"][si].update(status="error" if err else "done", error=err)

        def apply(s):
            if write is not None:
                write(s, si, bars, err)
            if s.get("progress"):
                s["progress"]["done"] = min(s["progress"]["total"], s["progress"]["done"] + 1)
            meter.charge(s)

        meter.update(sid, apply)

    return on_row


# ---------------------------------------------------------------------------
# 整首
# ---------------------------------------------------------------------------
class RestageFailed(Exception):
    """重新识别没有全部成功：不替换原来的结果。"""


class Swapped(Exception):
    """重新识别的新结果已经换上之后（生成练习建议、收尾时）出的错：原来的结果已经不在了，不能说“保留不变”。"""


class _FirstParse:
    """第一次识别（还没有小节）：读出的谱头、版面和每一行一出来就写进曲子，练习页能边识别边看。"""

    restaging = False

    def __init__(self, sid: str):
        self.sid = sid

    def header(self, meta: dict) -> None:
        store.patch(self.sid, meta=meta)

    def layout(self, systems: list[dict]) -> None:
        store.patch(self.sid, systems=systems, bars=[])

    def write_row(self, s: dict, si: int, bars: list[dict] | None, err: str | None) -> None:
        _write_row(s, si, bars, err)

    def header_fixed(self, meta_user: dict) -> None:
        store.update(self.sid, lambda s: review.fix_header_time_signature(s, meta_user))

    def pick_redo(self, work: dict, used: dict, errors: list[str]) -> list:
        """要按开头实际拍号 / 多数的和弦图重做的行：在存盘的曲子上挑并锁上（期间用户改过的行不重做）。"""
        redo: list = []
        if review.rows_to_redo(
            work["bars"], work["meta"], used, skip_edited=False
        ):  # 先看一眼：没有要重做的就不写

            def begin_redo(s):
                review.lock_redo(s, used, redo)

            store.update(self.sid, begin_redo)
        return redo

    def check(self, errors: list[str]) -> None:
        """有行失败也照常完成（失败的行可以单独重试）。"""

    def no_bars(self, errors: list[str]) -> None:
        _end_parse(self.sid, "error", ("error", "；".join(errors) or "没有识别出任何小节（可能不是六线谱）"))

    def commit(self, work: dict) -> str | None:
        """结果已经逐行写进曲子了。"""
        return None

    def finish(self, fn) -> None:
        fn()


class _Restage:
    """重新识别已有结果的曲子：新结果先攒在内存里（work），原来的小节、调弦、手改、提示和练习记录都不动；
    每一行都成功才一次性替换（commit）。有行失败、被取消或服务中断，原来的结果原样保留（RestageFailed）。"""

    restaging = True
    write_row = None  # 存盘的曲子上只记进度和用量

    def __init__(self, sid: str):
        self.sid = sid

    def header(self, meta: dict) -> None:
        pass

    def layout(self, systems: list[dict]) -> None:
        pass

    def header_fixed(self, meta_user: dict) -> None:
        pass

    def pick_redo(self, work: dict, used: dict, errors: list[str]) -> list:
        if errors:  # 已经有行失败：结果注定不会采用，不再为重做花钱
            return []
        redo = review.rows_to_redo(
            work["bars"], work["meta"], used, skip_edited=False
        )  # 原来的结果没动，没有要锁的行
        if redo:
            store.patch(self.sid, progress=review.redo_progress(redo))
        return redo

    def check(self, errors: list[str]) -> None:
        if errors:
            raise RestageFailed("；".join(errors))

    def no_bars(self, errors: list[str]) -> None:
        raise RestageFailed("没有识别出任何小节")

    def commit(self, work: dict) -> str | None:
        """一次性换成新结果，返回要告诉用户的话（手改 / 精修的改动没能保留时）。"""

        def swap(s):
            # 曲名只存在 song.title，不受谱头影响。新旧小节按内容对上，对上的保留 id、问答、练习记录和手改
            # 识别期间用户改的谱头（只可能是艺人：调弦等识别时不能改）同样以用户的为准
            work["meta"] = normalize_meta(
                {**work["meta"], **(s.get("meta_user") or {})}, work["meta"]["title"]
            )
            changed = staleness.setting_changed(s.get("meta") or {}, work["meta"])
            metas = (s.get("meta") or {}, work["meta"])
            # 精修改过的小节：这个小节识别得和精修之前一样就保留精修的改正，否则不保留（见 reconcile_bars）
            dropped = staleness.reconcile_bars(s, s["bars"], work["bars"], staleness.RESTAGE, changed, metas)
            s.update(meta=work["meta"], systems=work["systems"], bars=work["bars"])
            if dropped.refines:  # 上次精修整首的结果（改了几个）不再是现在的样子
                s.pop("refined", None)
            return dropped

        dropped: list[staleness.Dropped] = []
        store.update(self.sid, lambda s: dropped.append(swap(s)))
        lost = [
            f"{n} 个{what}的小节和新结果对不上，{why}"
            for n, what, why in (
                (dropped[0].edits, "手改过", "改动没有保留"),
                (dropped[0].refines, "精修改过", "精修的改动没有保留，可以再精修"),
            )
            if n
        ]
        return "重新识别完成；有 " + "；".join(lost) + "。" if lost else None

    def finish(self, fn) -> None:
        try:
            fn()
        except Exception as e:  # 新结果已经换上了，原来的结果不在了
            raise Swapped(friendly(e)) from e


def run_parse(sid: str, cancel: threading.Event | None = None) -> None:
    """识别整首。第一次识别（还没有小节）见 _FirstParse，重新识别已有结果的曲子见 _Restage。"""
    cancel = cancel or threading.Event()
    claude = config.new_llm()
    meter = jobs.UsageMeter(claude)  # 每行完成就把新增的用量记上（中途失败也不漏记）
    try:
        _run_parse(sid, cancel, claude, meter, options())
    finally:  # 失败 / 取消时还没记上的（读谱头、没跑完的行）：已经发出去的请求也花了钱
        meter.flush(sid)


def _run_parse(
    sid: str, cancel: threading.Event, claude: LLM, meter: jobs.UsageMeter, opts: RowOptions
) -> None:
    song = store.load(sid)
    out = _Restage(sid) if song.get("bars") else _FirstParse(sid)
    meta_user = song.get("meta_user") or {}
    store.update(sid, lambda s: _begin_parse(s, "读取谱头"))
    head = claude.read_meta(store.page_image(sid, song["pages"][0]))
    # 用户手改过的艺人、调弦、变调夹、拍号以用户的为准
    meta = normalize_meta({**head, **meta_user}, song.get("title", ""))
    out.header(meta)
    store.patch(sid, progress={"stage": "分析版面", "done": 0, "total": 0})

    if cancel.is_set():
        raise Cancelled()
    systems = plan_systems(sid, song)
    work = {**song, "meta": meta, "systems": systems, "bars": []}  # 新结果攒在这里（重新识别时先不写进曲子）
    out.layout(systems)
    store.patch(sid, progress={"stage": "逐行识别", "done": 0, "total": len(systems)})

    on_row = _row_writer(sid, meter, out.write_row, work)
    had = _bars_per_page(song)  # 重新识别时原来的结果（work 里还没有小节）；第一次识别是空的
    used, errors = _recognize_rows(
        claude, sid, work, [(i, None) for i in range(len(systems))], cancel, on_row, opts, had
    )
    if review.fix_header_time_signature(work, meta_user):
        # 谱头的拍号读错了：改成第 1 小节上写的。后面按旧拍号识别的行，下面按“开头实际拍号”重做；
        # 第 1 行自己是按图上的拍号读的，不用重做
        used[0] = work["meta"]["time_signature"]
        out.header_fixed(meta_user)

    # 各行是并行识别的，前面某行写的变拍号后面的行看不到，模型会把小节硬凑成全曲拍号（每小节一样长，查不出来）。
    # 按第一遍的结果算出每行开头实际的拍号，和识别时用的不同的行带上它再识别一次（没有变拍号的曲子不多花钱）
    redo = [] if cancel.is_set() else out.pick_redo(work, used, errors)
    if redo:
        log.info("song=%s rerun rows (time signature / chord diagrams): %s", sid, redo)
        # 重做的整页一行读出 0 个小节：和第一遍的结果（work）比，不悄悄删掉第一遍读出的小节
        errors += _recognize_rows(claude, sid, work, redo, cancel, on_row, opts)[1]

    if cancel.is_set():
        raise Cancelled()
    out.check(errors)
    if not work["bars"]:
        out.no_bars(errors)
        return
    apply_time_signatures(work["bars"], work["meta"])
    note = out.commit(work)
    out.finish(
        lambda: _finish_parse(sid, claude, meter, len(systems), out.restaging, "；".join(errors) or note)
    )


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
        systems = x.get("systems") or []
        if not 0 <= si < len(systems):  # 锁里再查一次：调用方查过之后可能刚删了一页（行少了）
            raise NotFound("这一行不存在")
        systems[si].update(status="parsing", error=None)
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
            # 模型说整页没有六线谱、原来的小节保留着：这一行显示失败的同时说清楚小节还在（不然看着像丢了）
            kept = (
                f"第 {si + 1} 行重新识别失败：{msg}，原来的小节保留不变。"
                if isinstance(e, EmptiedPage)
                else None
            )

            def failed(x):
                _fail_unfinished_rows(x, msg)  # 这一行，以及按新拍号锁着等重做的行
                meter.charge(x)  # 失败 / 取消的这次也花了钱
                state.finish(x, notice=("warn", kept) if kept else None, source="reparse_row")

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
    opts = options()
    song = store.load(sid)
    start_ts = review.row_start_ts(song["bars"], song["meta"], si)  # 前面各行写的变拍号，这一行的图上看不到
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
        opts=opts,
        tied_in=tied_in(song, si),
    )
    check_page_not_emptied(song["systems"][si], bars, _bars_per_page(song))

    def apply(s):
        others = {b["id"]: b.get("time_signature") for b in s["bars"] if b["system"] != si}
        _write_row(s, si, bars, None)  # 这一行变了的小节在这里标进练习建议的 changed_bars
        if any(b.get("time_signature") != others[b["id"]] for b in s["bars"] if b["id"] in others):
            staleness.invalidate_plan(s)  # 后面各行的拍号跟着变了
        meter.charge(s)

    meter.update(sid, apply)
    # 这一行是用户要求重新识别的（可能带着提示）：只按拍号复核，不按别的行的和弦图读法再（不带提示）重做一遍
    failed = _redo_after(
        sid, song, {si: start_ts}, [si], cancel, meter, _row_writer(sid, meter), opts, shapes=False
    )

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


def _redo_after(
    sid: str,
    before: dict,
    used: dict[int, list[int]],
    rows: list[int],
    cancel,
    meter: jobs.UsageMeter,
    on_row,
    opts: RowOptions,
    shapes: bool = True,
) -> list[str]:
    """已有曲子里的 rows 识别完之后（used：它们识别时开头的拍号；before：识别前的曲子）：
    - 识别的是第一行：它第 1 小节写的拍号可能说明谱头的拍号读错了（同整首识别，见 review.fix_header_time_signature）；
    - 其他行开头实际的拍号（按现在各行写的变拍号推算）和它们当初识别时的（按 before 推算）不一样了
      （这几行写了 / 去掉了变拍号、改了全曲拍号，补页插进来的新行写了变拍号），按新拍号再识别一次；
    - shapes：rows 自己读出的和弦图和别的行的多数读法不一样时，带上多数的读法重做（补页、重试失败的行）。
      用户要求重新识别的一行不这样做（False）：重做时不带用户的提示，会把用户要的结果盖掉，还多花一次钱。
    返回失败的说明。"""
    if 0 in rows and not cancel.is_set():
        fixed = []
        store.update(
            sid, lambda s: fixed.append(review.fix_header_time_signature(s, s.get("meta_user") or {}))
        )
        if fixed[0]:  # 第一行自己是按图上的拍号读的，不用重做
            used = {**used, 0: store.load(sid)["meta"]["time_signature"]}
    others = {k for k in range(len(before["systems"])) if k not in used}
    used = {**{k: review.row_start_ts(before["bars"], before["meta"], k) for k in others}, **used}
    # 别的行只看拍号（和弦图的多数读法整首识别时已经处理过）
    ts_only = others if shapes else set(used)
    song = store.load(sid)
    redo: list[tuple[int, list[int], str]] = []
    # 先看一眼：没有要重做的就不写
    if not cancel.is_set() and review.rows_to_redo(song["bars"], song["meta"], used, ts_only=ts_only):
        store.update(sid, lambda s: review.lock_redo(s, used, redo, ts_only=ts_only))
    if not redo:
        return []
    log.info("song=%s rerun rows after %s (time signature): %s", sid, rows, redo)
    return _recognize_rows(meter.claude, sid, store.load(sid), redo, cancel, on_row, opts)[1]


def recognize_rows_job(sid: str, rows: list[int], cancel: threading.Event, what: str = "新加的页") -> None:
    """后台识别已有曲子里的某几行（补页的新行、重试失败的行），每行完成就写回（其他行照常能练、能改）。
    调用方已经登记任务、把这几行标成识别中（state.begin "reparse_row"）。what：结束提示里怎么称呼这几行。"""
    meter = jobs.UsageMeter(None)
    failed: list[str] = []
    keys = _bar_keys(store.load(sid))

    try:
        meter = jobs.UsageMeter(config.new_llm())
        opts = options()
        before = store.load(sid)
        on_row = _row_writer(sid, meter)
        used, errs = _recognize_rows(
            meter.claude, sid, before, [(si, None) for si in rows], cancel, on_row, opts
        )
        failed += errs
        failed += _redo_after(sid, before, used, rows, cancel, meter, on_row, opts)
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
