"""精修：普通识别之后，逐小节放大核对（慢、贵，但更准）。

每个小节裁出它自己（左右各多带 15% 的宽度，看得到连进来的连音线、符杠）、整行的高度，放大后连同当前读法和上一小节的读法
发给模型，让它逐拍对照；它说改了、而且改出来的音和原来不一样，就写回。用最高精细度（llm.VERIFY_EFFORT）：
评测里错的 25 个改对了 24 个、抽查对的 208 个一个没改坏，约 $0.2 / 小节（high 档：726 个小节里错的 25 个改对 16 个）。

- 核对哪些小节（pick）：只有图片 / PDF 曲子；跳过用户手改过的、确认过没问题的、识别失败按休止占位的、所在行正在识别的，
  以及整行当一个小节的（没检测到小节线，裁不出一个小节）。核对过、音没再变的（refine_key）不再核对；
  用户点“精修”时全都核对过了就整首重来（自动精修不重来）。
- 确认（CONFIRM_CHANGES）：模型提出改动时再独立核对一两遍，有两遍提出一模一样的改动才改，否则不改、标成待核对（见 verify）。
- 写回：每个小节核对完马上写回，但只在这个小节还在、rev 没变（期间用户没改过）、仍然该核对时才写，否则悄悄跳过。
  改了的小节：提示 / 问答 / 练习建议按“音变了”处理（staleness.refine_bar），改之前的内容记在 bar["refine"]，可以撤销（undo）。
- 任务：activity kind = refine，期间照常能练、能改；进度 {stage: 精修, done, total}；全局名额和识别共用（recognize.run_queued），
  能取消；费用记到曲子上（jobs.UsageMeter）。整首精修完成时记下 song["refined"] = {at, checked, changed}。
- 自动精修（设置里 refine = auto，见 config.refine_mode）：识别结束后在同一个任务里接着精修（refine_after）。
"""

from __future__ import annotations

import logging
import math
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed

from PIL import Image

from app import config, jobs, recognize, staleness, state, store, views
from app.config import settings
from app.errors import Busy, Invalid
from app.jobs import Cancelled
from app.llm import LLM, friendly, request_id_of
from app.model import is_score, notes_key, now
from app.music import clean_events, same_events
from app.songs import check_rev, find_bar

log = logging.getLogger("tab_trainer.refine")

PAD = 0.15  # 裁剪时小节左右各多带它宽度的 15%
COST_PER_BAR = 0.20  # 评测里的平均费用（美元 / 小节，Opus、最高精细度，见 llm.VERIFY_EFFORT）：确认框里的预估
SECONDS_PER_BAR = 75  # 一次核对大约多久（几个小节并行）：确认框里的“约 N 分钟”
# 模型提出改动时再独立核对一两遍，有两遍提出一模一样的改动才改（见 verify）
CONFIRM_CHANGES = True


# ---------------------------------------------------------------------------
# 核对哪些小节
# ---------------------------------------------------------------------------
def eligible(s: dict, bar: dict) -> bool:
    """这个小节现在能不能精修（见模块说明）。"""
    if is_score(s) or bar.get("edited") or bar.get("placeholder") or views.reviewed(bar):
        return False
    si, systems = bar.get("system"), s.get("systems") or []
    if not isinstance(si, int) or not 0 <= si < len(systems) or state.row_parsing(s, si):
        return False
    return bool(systems[si].get("spans")) and bool(bar.get("region")) and bar.get("page") is not None


def pick(s: dict, only: set[str] | None = None, recheck: bool = False) -> list[str]:
    """要核对的小节 id（按顺序）：能精修、还没核对过（或核对之后音又变了）的。only：只在这些小节里挑。
    recheck（用户点了“精修”）：全都核对过了的话就整首重新核对一遍；自动精修不重来。"""
    ok = [b for b in s.get("bars") or [] if eligible(s, b) and (only is None or b.get("id") in only)]
    fresh = [b for b in ok if b.get("refine_key") != notes_key(b)]
    return [b["id"] for b in (ok if recheck and not fresh else fresh)]


def estimate(s: dict) -> dict | None:
    """现在点“精修”的范围和预估：{bars, cost_usd, minutes}。GP / MusicXML 曲子是 None。
    费用 = 小节数 × COST_PER_BAR；时间 = 按 row_concurrency 个小节并行、每次约 SECONDS_PER_BAR 秒。"""
    if is_score(s):
        return None
    n = len(pick(s, recheck=True))
    rounds = math.ceil(n / max(1, settings.row_concurrency))
    return {
        "bars": n,
        "cost_usd": round(n * COST_PER_BAR, 2),
        "minutes": max(1, math.ceil(rounds * SECONDS_PER_BAR / 60)) if n else 0,
    }


# ---------------------------------------------------------------------------
# 核对一个小节
# ---------------------------------------------------------------------------
def crop(page: Image.Image, sysinfo: dict, region) -> tuple[Image.Image, tuple[float, float]]:
    """裁出一个小节：左右各多带 PAD 倍的宽度（不超出这一行的裁剪范围），整行的高度。
    返回 (图, 小节在图里的左右边界)。"""
    x0, x1 = region[0], region[1]
    w = x1 - x0
    cx0 = max(sysinfo["crop"][0], int(x0 - w * PAD))
    cx1 = min(sysinfo["crop"][1], int(x1 + w * PAD))
    return page.crop((cx0, sysinfo["crop"][2], cx1, sysinfo["crop"][3])), (x0 - cx0, x1 - cx0)


def verify(
    claude: LLM,
    page: Image.Image,
    sysinfo: dict,
    bar: dict,
    meta: dict,
    previous: list[dict] | None,
    time_signature: list[int] | None = None,
    cancel: threading.Event | None = None,
) -> dict:
    """核对一个小节。返回 {out: 模型的回答, events: 要改成的事件（不改是 None）, status, note}：
    - status = unchanged：模型说没问题（或者改出来的格式不对、和原来一样）；
    - accepted：模型提出了改动、不做确认（CONFIRM_CHANGES 关着）直接采用；
    - confirmed：再独立核对一遍（还是对着原来的读法），提出了一模一样的改动（第二遍不一样就核对第三遍，三遍里有两遍一样才算）；
    - rejected：几遍提出的改动对不上，不改；note 是要加到这个小节“不确定”说明里的话，提醒用户对照原谱。
    改动很少（约 1/20 的小节），确认多花的钱不多，能挡掉偶尔的改坏（评测：模糊的截图上把倚音当成了十六分音符）。
    cancel：确认的几遍之间点了取消就不再核对（Cancelled）。"""
    img, span = crop(page, sysinfo, bar["region"])

    def propose() -> tuple[dict, list[dict] | None]:
        out = claude.verify_bar(img, meta, span, bar["events"], previous, time_signature)
        if not out.get("changed"):
            return out, None
        try:
            events = clean_events(out.get("events") or [])
        except ValueError as e:
            log.info("refine: bar %s returned bad events: %s", bar.get("id"), e)
            return out, None
        if not events or same_events(events, bar["events"]):
            return out, None
        return out, events

    first = propose()
    if first[1] is None:
        return {"out": first[0], "events": None, "status": "unchanged", "note": ""}
    if not CONFIRM_CHANGES:
        return {"out": first[0], "events": first[1], "status": "accepted", "note": ""}
    runs = [first]
    for _ in range(2):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        runs.append(propose())
        agreed = _agreed(runs)
        if agreed:
            return {"out": agreed[0], "events": agreed[1], "status": "confirmed", "note": ""}
    diff = str(first[0].get("diff") or "").strip()
    note = "精修时发现这个小节可能读错了，但几次核对的结果不一致，没有改，请对照原谱核对" + (
        f"：{diff}" if diff else "。"
    )
    return {"out": first[0], "events": None, "status": "rejected", "note": note}


def _agreed(runs: list[tuple[dict, list[dict] | None]]) -> tuple[dict, list[dict]] | None:
    """几遍核对里有两遍提出了一模一样的改动：返回 (先提出它的那遍的回答, 改动)。"""
    props = [(out, ev) for out, ev in runs if ev is not None]
    for i, (out, ev) in enumerate(props):
        if any(same_events(ev, other) for _, other in props[i + 1 :]):
            return out, ev
    return None


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------
def start_refine(sid: str) -> None:
    """用户点了“精修”：在请求里就登记任务（已有任务在跑：423），在后台核对。"""
    s = store.load(sid)
    if is_score(s):
        raise Invalid("Guitar Pro / MusicXML 曲子的小节来自原文件，不需要精修")
    if not s.get("bars"):
        raise Invalid("还没有识别出的小节，识别完成后再精修")
    config.check_llm_ready()
    ids: list[str] = []

    def begin(x):
        if state.recognizing(x):
            raise Busy("这首曲子正在识别中，完成后再精修")
        ids[:] = pick(x, recheck=True)
        if not ids:
            raise Invalid("没有可以精修的小节（手改过、确认过没问题的小节不再核对）")
        _begin(x, len(ids))

    jobs.start(sid, "这首曲子正在处理中，完成后再精修", begin, lambda cancel: _run(sid, ids, cancel, True))


def refine_after(sid: str, cancel: threading.Event, only: set[str] | None = None) -> None:
    """自动精修：识别任务结束后在同一个任务（同一个取消信号）里接着做。only：只核对这些小节（刚识别出的）；
    None = 整首（核对过、音没变的不再核对，全都核对过也不重来）。没有要核对的就什么都不做。"""
    if cancel.is_set() or not pick(store.load(sid), only):
        return
    ids: list[str] = []

    def begin(x):
        ids[:] = pick(x, only)
        if ids:
            _begin(x, len(ids))

    store.update(sid, begin)
    if ids:
        _run(sid, ids, cancel, only is None)


def _begin(s: dict, n: int) -> None:
    state.begin(s, "refine", progress={"stage": "精修", "done": 0, "total": n})


def _run(sid: str, ids: list[str], cancel: threading.Event, whole: bool) -> None:
    """跑完一次精修并收尾（结束提示、整首的结果、费用）。错误都在这里处理，不往外抛。"""
    meter = jobs.UsageMeter(None)
    tally = {"checked": 0, "changed": 0, "doubtful": 0, "failed": 0, "error": ""}
    done = False
    try:
        meter = jobs.UsageMeter(config.new_llm())
        refine_bars(sid, ids, cancel, meter, tally)
        done = True
        msg = f"精修完成：检查了 {tally['checked']} 个小节，改了 {tally['changed']} 个"
        if tally["changed"]:
            msg += "（小节卡片上能看到改了什么，也能撤销）"
        if tally["doubtful"]:
            msg += f"；{tally['doubtful']} 个小节几次核对的结果不一致，没有改，标成了待核对"
        if tally["failed"]:
            msg += f"；{tally['failed']} 个小节没核对成（{tally['error']}），可以再点“精修”重试"
        notice = ("warn" if tally["failed"] else "info", msg + "。")
    except Cancelled:
        notice = (
            "warn",
            f"精修已取消：检查了 {tally['checked']} 个小节，改了 {tally['changed']} 个，改好的会保留。",
        )
    except Exception as e:
        rid = request_id_of(e)
        log.exception("request_id=%s song=%s refine failed", rid, sid)
        notice = ("error", "精修失败：" + friendly(e))

    def end(s):
        if done and whole:
            s["refined"] = {"at": now(), "checked": tally["checked"], "changed": tally["changed"]}
        other = s.get("notice") and s["notice"].get("source") != "refine"
        # 识别留下的提示（有行失败等）更要紧：不盖掉它（精修的结果在小节卡片和曲库里也看得到）
        state.finish(s, notice=None if other else notice, source="refine")
        meter.charge(s)

    try:
        meter.update(sid, end)
    except Exception as e:  # noqa: BLE001 - 曲子已经删掉之类
        log.warning("song=%s refine not finished: %s", sid, e)


def refine_bars(
    sid: str, ids: list[str], cancel: threading.Event, meter: jobs.UsageMeter, tally: dict
) -> None:
    """并行核对 ids 里的小节（每首最多 row_concurrency 个，所有曲子共用识别的全局名额），每个核对完就写回。
    tally 累计 checked / changed / doubtful（几遍核对对不上、标成待核对的）/ failed / error（取消时调用方照样能拿到已完成的部分）。取消：Cancelled。"""
    song = store.load(sid)
    bars = song["bars"]
    index = {b["id"]: i for i, b in enumerate(bars)}
    work = [(bars[index[i]], bars[index[i] - 1] if index[i] > 0 else None) for i in ids if i in index]
    pages: dict[int, Image.Image] = {}
    lock = threading.Lock()

    def page(pi: int) -> Image.Image:
        with lock:
            if pi not in pages:
                pages[pi] = store.page_image(sid, song["pages"][pi])
            return pages[pi]

    def one(bar: dict, prev: dict | None):
        def go():
            if cancel.is_set():
                raise Cancelled()
            return verify(
                meter.claude,
                page(bar["page"]),
                song["systems"][bar["system"]],
                bar,
                song["meta"],
                prev["events"] if prev else None,
                bar.get("time_signature"),
                cancel,
            )

        return recognize.run_queued(go, cancel)

    def write(bar: dict, result):
        def apply(s):
            if s.get("progress"):
                s["progress"]["done"] = min(s["progress"]["total"], s["progress"]["done"] + 1)
            meter.charge(s)
            if result is None:
                return
            out, events = result["out"], result["events"]
            cur = next((b for b in s["bars"] if b.get("id") == bar["id"]), None)
            # 核对期间这个小节被改过（rev 变了）、被重新识别掉了、或者已经不该核对（确认过、正在识别）：不写
            if cur is None or int(cur.get("rev") or 0) != int(bar.get("rev") or 0) or not eligible(s, cur):
                return
            tally["checked"] += 1
            if events is not None:
                diff = str(out.get("diff") or "").strip()
                staleness.refine_bar(s, cur, events, str(out.get("uncertain") or "").strip(), diff)
                tally["changed"] += 1
            elif result["note"] and result["note"] not in (cur.get("uncertain") or ""):
                # 几遍核对对不上：不改音，标成待核对（不确定说明里写上第一遍说的问题）
                cur["uncertain"] = " ".join(
                    x for x in (str(cur.get("uncertain") or "").strip(), result["note"]) if x
                )
                tally["doubtful"] += 1
            cur["refine_key"] = notes_key(cur)

        meter.update(sid, apply)

    pool = ThreadPoolExecutor(settings.row_concurrency)
    try:
        futs = {pool.submit(one, b, p): b for b, p in work}
        for fut in as_completed(futs):
            bar = futs[fut]
            try:
                result = fut.result()
            except (Cancelled, CancelledError):
                result = None
            except Exception as e:  # noqa: BLE001 - 一个小节失败不影响别的
                log.warning(
                    "request_id=%s song=%s bar=%s refine failed: %s", request_id_of(e), sid, bar["id"], e
                )
                tally["failed"] += 1
                tally["error"] = friendly(e)
                result = None
            write(bar, result)
            if cancel.is_set():
                for f in futs:
                    f.cancel()  # 还没开始的不再发请求；正在进行的请求会跑完
    except BaseException:
        cancel.set()  # 写回出错（曲子被删之类）：还没开始的不再发请求
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    if cancel.is_set():
        raise Cancelled()


# ---------------------------------------------------------------------------
# 撤销精修
# ---------------------------------------------------------------------------
def undo(sid: str, bar_id: str, rev: int | None = None) -> dict:
    """撤销一个小节的精修改动（rev = 这个小节的 rev，见 songs.check_rev）。撤销后不再自动核对它（除非音又变了）。"""

    def apply(s):
        if not state.can_edit_bars(s):
            raise Invalid(state.SCORE_BARS_LOCKED)
        bar = find_bar(s, bar_id)
        check_rev(bar.get("rev"), rev)
        if why := state.bar_locked(s, bar):
            raise Busy(why)
        if not bar.get("refine"):
            raise Invalid("这个小节没有精修改动可以撤销")
        staleness.undo_refine(s, bar)
        bar["refine_key"] = notes_key(bar)  # 用户不要这次改动：之后的精修不再改它

    return store.update(sid, apply)
