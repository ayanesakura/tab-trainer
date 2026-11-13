"""精修：普通识别之后，逐小节放大核对（慢、贵，但更准）。

每个小节裁出它自己（左右各多带 15% 的宽度，看得到连进来的连音线、符杠）、整行的高度，放大后连同当前读法和上一小节的读法
发给模型，让它逐拍对照；它说改了、而且改出来的音和原来不一样，就写回。用最高精细度（llm.VERIFY_EFFORT）：
评测里错的 25 个改对了 24 个、抽查对的 208 个一个没改坏，约 $0.2 / 小节（high 档：726 个小节里错的 25 个改对 16 个）。

- 范围：整首（曲库“…”菜单和练习页的“精修整首”、自动精修），或者用户在小节卡片上点的某个小节 / 某一行
  （POST /api/songs/{id}/refine 带 {"bars": [...]} 或 {"system": 行号}，可以带 hint：用户写的“哪里不对”，告诉模型）。
- 核对哪些小节：只有图片 / PDF 曲子；跳过识别失败按休止占位的、所在行正在识别的，以及整行当一个小节的
  （没检测到小节线，裁不出一个小节）、识别出的小节数和小节线对不上的行（不知道哪个小节在哪，见 refinable）。整首（pick）：再跳过用户手改过的、确认过没问题的；核对过、
  音没再变的（refine_key）不再核对，用户点“精修整首”时全都核对过了就整首重来（自动精修不重来）。
  点了某个小节 / 某一行（pick_scope）：范围里的都核对，手改过、确认过、核对过的也核对（用户明确要求的，改了能撤销）。
- 疑似切错的行不核对（suspect_rows）：小节线检测把扫弦箭头当成小节线时，“小节”和图上的区间对不上（行首只有谱号的
  一段被读成一个休止小节），模型对着一张没有音符的图想几分钟也核对不出结果。点了这样的小节 / 行：不发请求，400 提示
  重新识别这一行；整首、自动精修：跳过这些行，结束提示里说明。
- 确认（CONFIRM_CHANGES）：模型提出改动时再独立核对，最多 MAX_READS 遍里有两遍提出一模一样的改动才改，否则不改、标成待核对（见 verify）。
- 写回：每个小节核对完马上写回，但只在这个小节还在、rev 没变（期间用户没改过）、仍然该核对时才写，否则悄悄跳过。
  改了的小节：提示 / 问答 / 练习建议按“音变了”处理（staleness.refine_bar），改之前的内容记在 bar["refine"]，可以撤销（undo）。
- 任务：activity kind = refine（scope = song / bars / row，row = 行号，bars = 范围里的小节 id，pending = 还没核对完的），
  每首曲子同一时间只有一个任务（再点：423，说清楚正在做什么，见 busy_text）；期间照常能练、能改小节和曲名，
  调弦 / 变调夹 / 拍号不能改（核对是按它们做的，见 state.settings_lock_reason）；进度 {stage: 精修, done, total}；
  全局名额和识别共用（jobs.run_queued，每一遍核对单独排队，不会一个小节连着几遍占住名额），能取消
  （正在进行的那一遍请求马上断开，见 llm.interruptible；已经花掉的照样记费用）；
  费用记到曲子上（jobs.UsageMeter）。
  整首精修完成时记下 song["refined"] = {at, checked, changed}（只精修了一部分不动它）；结束提示见 _end_notice。
- 自动精修（设置里 refine = auto，见 config.refine_mode）：识别结束后在同一个任务里接着精修（refine_after）。
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from PIL import Image

from app import config, jobs, staleness, state, store, systems
from app.config import settings
from app.confirmed import reviewed
from app.errors import Busy, Invalid
from app.jobs import Cancelled
from app.llm import LLM, friendly, interruptible, request_id_of
from app.model import is_score, notes_key, now
from app.music import clean_events, same_events
from app.songs import check_rev, find_bar

log = logging.getLogger("tab_trainer.refine")

PAD = 0.15  # 裁剪时小节左右各多带它宽度的 15%
COST_PER_BAR = 0.20  # 评测里的平均费用（美元 / 小节，Opus、最高精细度，见 llm.VERIFY_EFFORT）：确认框里的预估
SECONDS_PER_BAR = 75  # 一次核对大约多久（几个小节并行）：确认框里的“约 N 分钟”
# 模型提出改动时再独立核对几遍，有两遍提出一模一样的改动才改（见 verify）
CONFIRM_CHANGES = True
# 一个小节最多核对几遍（含第一遍）。探查（每个小节核对 5 遍）：模型提出的改动每次都一模一样、而且都对，
# 分歧只在“提出改动”和“说没问题”之间（如 改 / 没问题 / 没问题 / 改 / 改）：只核对三遍会把对的改动放弃掉
MAX_READS = 5
# 行首第一个“小节”不到这么多个线距宽、读出来又一个音都没有：多半是谱号到第一拍扫弦箭头之间的那一段（见 miscut_rows）。
# 真的窄小节（一个全音符和弦约 5 个线距）里有音；曲库里行首的全休止小节没有比它窄的
CLEF_BAR_WIDTH = 7


@dataclass(frozen=True)
class VerifyOptions:
    """核对一个小节的选项：一次精修任务开始时按上面两个开关定好（options），之后每个小节都按它。"""

    confirm: bool = True  # CONFIRM_CHANGES
    max_reads: int = 5  # MAX_READS


def options() -> VerifyOptions:
    return VerifyOptions(confirm=CONFIRM_CHANGES, max_reads=MAX_READS)


# ---------------------------------------------------------------------------
# 核对哪些小节
# ---------------------------------------------------------------------------
def row_slots(s: dict) -> dict[str, tuple[int, int]]:
    """小节 id → (它在所在行里是第几个（从 0 数）, 这一行有几个小节)。给 refinable 对小节线区间用。"""
    rows: dict = {}
    for b in s.get("bars") or []:
        rows.setdefault(b.get("system"), []).append(b.get("id"))
    return {bid: (k, len(ids)) for ids in rows.values() for k, bid in enumerate(ids)}


def refinable(s: dict, bar: dict, slots: dict[str, tuple[int, int]] | None = None) -> bool:
    """这个小节能不能单独裁出来核对（不管手改过、确认过没有）：图片 / PDF 曲子、不是识别失败按休止占位的、
    所在行检测到了小节线（整行一个小节时裁不出来），而且这一行识别出的小节数和小节线区间数一样、
    这个小节的区域就是它那个区间（小节数对不上时第 k 个小节不一定在第 k 个区间里：早先的数据按位置分了区域，
    精修会拿相邻小节的图去核对；之后识别的给的是整行的区域）。小节卡片上的“精修这个小节 / 这一行”按它显示。
    slots：row_slots(s) 的结果（挑一批小节时只算一次）。"""
    if is_score(s) or bar.get("placeholder"):
        return False
    si, systems = bar.get("system"), s.get("systems") or []
    if not isinstance(si, int) or not 0 <= si < len(systems):
        return False
    spans, region = systems[si].get("spans"), bar.get("region")
    if not spans or not region or bar.get("page") is None:
        return False
    k, n = (slots if slots is not None else row_slots(s)).get(bar.get("id"), (-1, 0))
    if n != len(spans) or not 0 <= k < n:
        return False
    a, c = spans[k][0], spans[k][1]
    # 识别时给的是区间左右各多 8 像素（不超出页面）：区域要盖住这个区间、又不比它宽出太多（整行的区域）
    return region[0] <= a and region[1] >= c and region[1] - region[0] <= c - a + 17


def eligible(s: dict, bar: dict, explicit: bool = False, slots: dict | None = None) -> bool:
    """这个小节现在能不能精修（见模块说明）。explicit：用户点了这个小节 / 这一行，手改过、确认过的也核对。"""
    if not refinable(s, bar, slots) or state.row_parsing(s, bar.get("system")):
        return False
    return explicit or not (bar.get("edited") or reviewed(bar))


def miscut_rows(s: dict) -> set[int]:
    """只看已有数据就能认出的切错的行（行号）：行首第一个小节很窄（不到 CLEF_BAR_WIDTH 个线距）、读出来一个音都没有。
    那一段是 TAB 谱号到第一拍扫弦箭头之间的空白（箭头被当成了小节线），这一行后面的小节也都跟着错位。"""
    out: set[int] = set()
    seen: set[int] = set()
    rows = s.get("systems") or []
    for b in s.get("bars") or []:
        si = b.get("system")
        if not isinstance(si, int) or si in seen or not 0 <= si < len(rows):
            continue
        seen.add(si)  # 只看每行的第一个小节
        spans, lines = rows[si].get("spans"), rows[si].get("lines")
        if not spans or len(spans) < 2 or not lines or len(lines) < 2 or b.get("placeholder"):
            continue
        sp = (lines[-1] - lines[0]) / (len(lines) - 1)
        empty = not any(e.get("notes") for e in b.get("events") or [])
        if empty and spans[0][1] - spans[0][0] < sp * CLEF_BAR_WIDTH:
            out.add(si)
    return out


def stale_rows(sid: str, s: dict, rows: set[int] | None = None) -> set[int]:
    """识别时存下的小节区间数和现在的版面检测对不上的行（行号）：小节线检测改进之前识别的曲子，那一行当时多切 / 少切了
    小节，要重新识别才对得上。对这些行所在的页重新检测一遍（只看图，每页几十毫秒），按弦线的位置找到同一行。
    rows：只查这些行。原图读不出来、现在找不到这一行的不算（宁可照常核对）。"""
    out: set[int] = set()
    by_page: dict[int, list[int]] = {}
    for si, x in enumerate(s.get("systems") or []):
        if (rows is None or si in rows) and x.get("spans") and x.get("lines"):
            by_page.setdefault(x["page"], []).append(si)
    for pi, sis in by_page.items():
        try:
            now_rows = systems.plan_page_systems(sid, s["pages"][pi], pi)
        except Exception as e:  # noqa: BLE001 - 原图丢了、坏了：查不了，不拦
            log.warning("song=%s page %s: could not re-detect bar lines: %s", sid, pi, e)
            continue
        for si in sis:
            x = s["systems"][si]
            same = [
                r
                for r in now_rows
                if r.get("spans")
                and abs(r["lines"][0] - x["lines"][0]) <= 3
                and r["crop"][0] < x["crop"][1]
                and x["crop"][0] < r["crop"][1]
            ]
            if same and len(same[0]["spans"]) != len(x["spans"]):
                out.add(si)
    return out


def suspect_rows(sid: str, s: dict, rows: set[int] | None = None) -> set[int]:
    """疑似切错、不该拿去核对的行（见模块说明）：miscut_rows 和 stale_rows 合起来。rows：只查这些行。"""
    found = miscut_rows(s) | stale_rows(sid, s, rows)
    return found if rows is None else found & rows


def rows_text(rows: set[int]) -> str:
    """“第 4、5 行”。"""
    return "第 " + "、".join(str(si + 1) for si in sorted(rows)) + " 行"


def pick(
    s: dict, only: set[str] | None = None, recheck: bool = False, skip_rows: set[int] | None = None
) -> list[str]:
    """整首要核对的小节 id（按顺序）：能精修、还没核对过（或核对之后音又变了）的。only：只在这些小节里挑。
    recheck（用户点了“精修整首”）：全都核对过了的话就整首重新核对一遍；自动精修不重来。
    skip_rows：这些行不核对（疑似切错的行，见 suspect_rows）。"""
    slots = row_slots(s)
    ok = [
        b
        for b in s.get("bars") or []
        if (only is None or b.get("id") in only)
        and b.get("system") not in (skip_rows or ())
        and eligible(s, b, slots=slots)
    ]
    fresh = [b for b in ok if b.get("refine_key") != notes_key(b)]
    return [b["id"] for b in (ok if recheck and not fresh else fresh)]


def scope_ids(s: dict, bars: list[str] | None = None, system: int | None = None) -> list[str] | None:
    """请求里的范围 → 范围里的小节 id（按曲子里的顺序）；都没给 = 整首（None）。小节 / 行不存在：Invalid。"""
    if bars is not None and system is not None:
        raise Invalid("小节和行只能指定一个")
    if bars is not None:
        if not bars:
            raise Invalid("没有指定要精修的小节")
        known = {b.get("id") for b in s.get("bars") or []}
        if unknown := [x for x in bars if x not in known]:
            raise Invalid(f"小节不存在：{unknown[0]}（可能已经重新识别过，刷新后再试）")
        want = set(bars)
        return [b["id"] for b in s["bars"] if b["id"] in want]
    if system is not None:
        if not 0 <= system < len(s.get("systems") or []):
            raise Invalid(f"第 {system + 1} 行不存在")
        return [b["id"] for b in s.get("bars") or [] if b.get("system") == system]
    return None


def pick_scope(s: dict, ids: list[str]) -> list[str]:
    """用户点的小节 / 行里要核对的：能裁出来核对的都核对（手改过、确认过、核对过的也核对）。"""
    want, slots = set(ids), row_slots(s)
    return [
        b["id"]
        for b in s.get("bars") or []
        if b.get("id") in want and eligible(s, b, explicit=True, slots=slots)
    ]


def _estimate(n: int) -> dict:
    """核对 n 个小节的预估：费用 = n × COST_PER_BAR；时间 = 按 row_concurrency 个小节并行、每次约 SECONDS_PER_BAR 秒
    （seconds；minutes 向上取整，至少 1 分钟）。"""
    rounds = math.ceil(n / max(1, settings.row_concurrency))
    seconds = rounds * SECONDS_PER_BAR if n else 0
    return {
        "bars": n,
        "cost_usd": round(n * COST_PER_BAR, 2),
        "minutes": max(1, math.ceil(seconds / 60)) if n else 0,
        "seconds": seconds,
    }


def estimate(s: dict) -> dict | None:
    """现在点“精修整首”的范围和预估：{bars, cost_usd, minutes, seconds}。GP / MusicXML 曲子是 None。"""
    if is_score(s):
        return None
    return _estimate(len(pick(s, recheck=True)))


def scope_estimates(s: dict) -> dict | None:
    """小节卡片上“精修这个小节 / 这一行”的预估（和整首的算法一样）：{bar: 一个小节的, rows: [每一行的；裁不出小节的行是 None]}。
    GP / MusicXML 曲子是 None。"""
    if is_score(s):
        return None
    counts, slots = [0] * len(s.get("systems") or []), row_slots(s)
    for b in s.get("bars") or []:
        if refinable(s, b, slots):
            counts[b["system"]] += 1
    return {"bar": _estimate(1), "rows": [_estimate(n) if n else None for n in counts]}


def scope_label(s: dict, a: dict | None) -> str:
    """精修任务（activity）的范围怎么说：整首 / 第 12 小节 / 第 3 行（4 个小节） / 5 个小节。"""
    a = a or {}
    ids = a.get("bars") or []
    if a.get("scope") == "row" and isinstance(a.get("row"), int):
        return f"第 {a['row'] + 1} 行（{len(ids)} 个小节）"
    if a.get("scope") == "bars" and len(ids) == 1:
        n = next((i for i, b in enumerate(s.get("bars") or []) if b.get("id") == ids[0]), None)
        return f"第 {n + 1} 小节" if n is not None else "1 个小节"
    if a.get("scope") == "bars":
        return f"{len(ids)} 个小节"
    return "整首"


def busy_text(s: dict) -> str:
    """已经有任务在跑时再点精修（423）的说明。"""
    a = s.get("activity") or {}
    if a.get("kind") == "refine":
        return f"正在精修{scope_label(s, a)}，完成后再试"
    if state.recognizing(s):
        return "这首曲子正在识别中，完成后再精修"
    if a.get("kind") == "explain":
        return "正在生成讲解，完成后再精修"
    return "这首曲子正在处理中，完成后再精修"


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
    hint: str = "",
    shapes: dict[str, list[int]] | None = None,
    queued: Callable[[Callable[[], dict]], dict] | None = None,
    opts: VerifyOptions | None = None,
) -> dict:
    """核对一个小节。返回 {out: 模型的回答, events: 要改成的事件（不改是 None）, status, note}：
    - status = unchanged：模型说没问题（或者改出来的和原来一样）；
    - invalid：模型说要改，但给出的事件格式不对或是空的：不知道它到底要怎么改，算没核对成（不记 refine_key，可以再试）；
    - accepted：模型提出了改动、不做确认（CONFIRM_CHANGES 关着）直接采用；
    - confirmed：再独立核对（还是对着原来的读法），有两遍提出了一模一样的改动（最多核对 MAX_READS 遍，够了就停）；
    - rejected：核对 MAX_READS 遍都没有两遍一样的改动，不改；note 是要加到这个小节“不确定”说明里的话，提醒用户对照原谱。
    改动很少（约 1/20 的小节），确认多花的钱不多，能挡掉偶尔的改坏（评测：模糊的截图上把倚音当成了十六分音符）。
    cancel：确认的几遍之间点了取消就不再核对（Cancelled）。hint：用户写的“哪里不对”（每一遍都告诉模型）。
    shapes：告诉模型的和弦图（None = 这个小节识别时读出的，见 context_shapes）。
    queued：每一遍调用模型怎么执行（refine_bars 给的是排队等全局名额：每一遍单独占名额，几遍之间让出来，
    一个小节最多 MAX_READS 遍、几分钟，不会一直占着让别的曲子的识别等着）；None = 直接调用。
    opts：确认几遍（None = 现在的 options()）。"""
    opts = opts or options()
    if shapes is None:
        shapes = context_shapes(bar, None)
    img, span = crop(page, sysinfo, bar["region"])

    bad: list[bool] = []  # 每一遍：模型说要改、给出的事件却用不了

    def ask() -> dict:
        return claude.verify_bar(
            img, meta, span, bar["events"], previous, time_signature, hint=hint, shapes=shapes or None
        )

    def propose() -> tuple[dict, list[dict] | None]:
        out = queued(ask) if queued else ask()
        bad.append(False)
        if not out.get("changed"):
            return out, None
        try:
            events = clean_events(out.get("events") or [])
        except ValueError as e:
            log.info("refine: bar %s returned bad events: %s", bar.get("id"), e)
            events = []
        if not events:
            bad[-1] = True
            return out, None
        if same_events(events, bar["events"]):
            return out, None
        return out, events

    first = propose()
    if bad[0]:
        return {"out": first[0], "events": None, "status": "invalid", "note": ""}
    if first[1] is None:
        return {"out": first[0], "events": None, "status": "unchanged", "note": ""}
    if not opts.confirm:
        return {"out": first[0], "events": first[1], "status": "accepted", "note": ""}
    runs = [first]
    while len(runs) < opts.max_reads:
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


def context_shapes(bar: dict, prev: dict | None) -> dict[str, list[int]]:
    """核对一个小节时告诉模型的和弦图（弹唱谱）：上一小节和这个小节识别时读出的（小节开头的 x 可能属于上一小节
    延续过来的和弦；裁出的小节图上一般看不到和弦图）。"""
    return {**((prev or {}).get("shapes") or {}), **(bar.get("shapes") or {})}


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
def start_refine(sid: str, bars: list[str] | None = None, system: int | None = None, hint: str = "") -> None:
    """用户点了“精修整首”（bars、system 都没给），或小节卡片上的“精修这个小节”（bars）/“精修这一行”（system）：
    在请求里就登记任务（已有任务在跑：423，说清楚在做什么），在后台核对。hint：用户写的“哪里不对”，核对时告诉模型。"""
    s = store.load(sid)
    if is_score(s):
        raise Invalid("Guitar Pro / MusicXML 曲子的小节来自原文件，不需要精修")
    if not s.get("bars"):
        raise Invalid("还没有识别出的小节，识别完成后再精修")
    scope = scope_ids(s, bars, system)
    config.check_llm_ready()
    kind = "song" if scope is None else "row" if system is not None else "bars"
    # 疑似切错的行：点的小节 / 行在里面就不发请求，说清楚要重新识别；整首时跳过它们
    wanted = None if scope is None else {b["system"] for b in s["bars"] if b["id"] in set(scope)}
    suspect = suspect_rows(sid, s, wanted)
    if scope is not None and suspect:
        raise Invalid(f"{rows_text(suspect)}的小节线可能切错了，核对不了：请重新识别这一行")
    ids: list[str] = []

    def begin(x):
        if x.get("activity"):
            raise Busy(busy_text(x))
        if scope is None:
            ids[:] = pick(x, recheck=True, skip_rows=suspect)
            if not ids and suspect:
                raise Invalid(f"{rows_text(suspect)}的小节线可能切错了，核对不了：请重新识别这几行")
            if not ids:
                raise Invalid("没有可以精修的小节（手改过、确认过没问题的小节不再核对）")
        else:
            ids[:] = pick_scope(x, scope)
            if not ids:
                raise Invalid(
                    "没有可以精修的小节（识别失败按休止占位的小节、没检测到小节线的行裁不出单个小节）"
                )
        _begin(x, ids, kind, system)

    jobs.start(
        sid,
        busy_text(s),
        begin,
        lambda cancel: _run(
            sid,
            ids,
            cancel,
            whole=scope is None,
            explicit=scope is not None,
            hint=hint.strip(),
            skipped=suspect,
        ),
    )


def refine_after(sid: str, cancel: threading.Event, only: set[str] | None = None) -> None:
    """自动精修：识别任务结束后在同一个任务（同一个取消信号）里接着做。only：只核对这些小节（刚识别出的）；
    None = 整首（核对过、音没变的不再核对，全都核对过也不重来）。没有要核对的就什么都不做。"""
    # 刚识别完：存的区间就是现在检测的，只用看读出来的结果像不像切错（miscut_rows）
    s = store.load(sid)
    skip = miscut_rows(s)
    if cancel.is_set() or not pick(s, only, skip_rows=skip):
        return
    ids: list[str] = []

    def begin(x):
        ids[:] = pick(x, only, skip_rows=skip)
        if ids:
            _begin(x, ids, "song" if only is None else "bars")

    store.update(sid, begin)
    if ids:
        _run(sid, ids, cancel, whole=only is None, skipped=skip)


def _begin(s: dict, ids: list[str], scope: str, row: int | None = None) -> None:
    state.begin(s, "refine", progress={"stage": "精修", "done": 0, "total": len(ids)})
    s["activity"].update(scope=scope, row=row, bars=list(ids), pending=list(ids))


def _run(
    sid: str,
    ids: list[str],
    cancel: threading.Event,
    whole: bool,
    explicit: bool = False,
    hint: str = "",
    skipped: set[int] | None = None,
) -> None:
    """跑完一次精修并收尾（结束提示、整首的结果、费用）。错误都在这里处理，不往外抛。
    whole：整首（做完记下 song["refined"]；某个小节、某一行、自动精修刚识别出的几行不动它）；
    explicit：用户点的小节 / 行（手改过、确认过的也写回）；hint：用户写的“哪里不对”；
    skipped：疑似切错、这次没核对的行（结束提示里说明）。"""
    meter = jobs.UsageMeter(None)
    tally = {"checked": 0, "changed": 0, "doubtful": 0, "failed": 0, "error": ""}
    done = cancelled = False
    error = ""
    try:
        meter = jobs.UsageMeter(config.new_llm())
        refine_bars(sid, ids, cancel, meter, tally, explicit=explicit, hint=hint)
        done = True
    except Cancelled:
        cancelled = True
    except Exception as e:
        rid = request_id_of(e)
        log.exception("request_id=%s song=%s refine failed", rid, sid)
        error = friendly(e)

    def end(s):
        if done and whole:
            s["refined"] = {"at": now(), "checked": tally["checked"], "changed": tally["changed"]}
        # 总要告诉用户精修的结果；识别留下的提示（有行失败等）也不盖掉：叠在它上面（见 state.set_notice）
        kind, msg = _end_notice(s, tally, cancelled, error)
        if skipped and not error and not cancelled:
            kind = "warn"
            msg += f"{rows_text(skipped)}的小节线可能切错了，没有核对：请重新识别。"
        state.finish(s, notice=(kind, msg), source="refine")
        meter.charge(s)

    try:
        meter.update(sid, end)
    except Exception as e:  # noqa: BLE001 - 曲子已经删掉之类
        log.warning("song=%s refine not finished: %s", sid, e)


def _end_notice(s: dict, tally: dict, cancelled: bool, error: str) -> tuple[str, str]:
    """结束提示（在 state.finish 之前调用，要用 activity 里的范围）。整首：检查了几个、改了几个……；
    一个小节 / 一行：短一点（“第 12 小节精修完：改了”“第 3 行精修完：4 个小节都没问题”）。"""
    if error:
        return "error", "精修失败：" + error
    checked, changed, doubtful, failed = (tally[k] for k in ("checked", "changed", "doubtful", "failed"))
    if cancelled:
        return "warn", f"精修已取消：检查了 {checked} 个小节，改了 {changed} 个，改好的会保留。"
    a = s.get("activity") or {}
    scope, label = a.get("scope"), scope_label(s, a)
    why = f"（{tally['error']}）" if tally["error"] else ""
    if scope == "bars" and len(a.get("bars") or []) == 1:
        if failed:
            return "warn", f"{label}没核对成{why}，可以再试一次。"
        if changed:
            return "info", f"{label}精修完：改了（小节卡片上能看到改了什么，也能撤销）。"
        if doubtful:
            return "info", f"{label}精修完：几次核对的结果不一致，没有改，标成了待核对。"
        if checked:
            return "info", f"{label}精修完：没问题。"
        return "info", f"{label}在精修期间改过，精修的结果没有写回。"
    if scope in ("bars", "row"):
        name = f"第 {a['row'] + 1} 行" if scope == "row" else label
        ok = checked and not changed and not doubtful
        msg = f"{name}精修完：" + (
            f"{checked} 个小节都没问题" if ok else f"检查了 {checked} 个小节，改了 {changed} 个"
        )
        retry = "可以再试一次"
    else:
        msg = f"精修完成：检查了 {checked} 个小节，改了 {changed} 个"
        retry = "可以再点“精修整首”重试"
    if changed:
        msg += "（小节卡片上能看到改了什么，也能撤销）"
    if doubtful:
        msg += f"；{doubtful} 个小节几次核对的结果不一致，没有改，标成了待核对"
    if failed:
        msg += f"；{failed} 个小节没核对成{why}，{retry}"
    return ("warn" if failed else "info"), msg + "。"


def refine_bars(
    sid: str,
    ids: list[str],
    cancel: threading.Event,
    meter: jobs.UsageMeter,
    tally: dict,
    explicit: bool = False,
    hint: str = "",
) -> None:
    """并行核对 ids 里的小节（每首最多 row_concurrency 个，所有曲子共用识别的全局名额），每个核对完就写回
    （并从 activity.pending 里去掉）。tally 累计 checked / changed / doubtful（几遍核对对不上、标成待核对的）/ failed / error
    （取消时调用方照样能拿到已完成的部分）。explicit、hint 见 _run。取消：Cancelled（正在进行的请求马上断开）。"""
    song = store.load(sid)
    opts = options()  # 这次精修的选项：定好一次，每个小节都按它
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

    def queued(ask):
        def go():
            if cancel.is_set():
                raise Cancelled()
            try:
                with interruptible(cancel):  # 点了取消：正在进行的这一遍马上断开，不等它跑完
                    return ask()
            except Exception:
                if cancel.is_set():  # 被取消打断的请求抛的是连接中断：算取消，不算这个小节失败
                    raise Cancelled() from None
                raise

        return jobs.run_queued(go, cancel)

    def one(bar: dict, prev: dict | None):
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
            hint,
            context_shapes(bar, prev),
            queued,
            opts,
        )

    def write(bar: dict, result):
        def apply(s):
            if s.get("progress"):
                s["progress"]["done"] = min(s["progress"]["total"], s["progress"]["done"] + 1)
            a = s.get("activity") or {}
            if a.get("kind") == "refine" and bar["id"] in (a.get("pending") or []):
                a["pending"] = [x for x in a["pending"] if x != bar["id"]]
            meter.charge(s)
            if result is None:
                return
            if result["status"] == "invalid":  # 说要改、却没给出能用的改法：算没核对成，下次照样核对
                tally["failed"] += 1
                tally["error"] = tally["error"] or "AI 给出的改动格式不对"
                return
            out, events = result["out"], result["events"]
            cur = next((b for b in s["bars"] if b.get("id") == bar["id"]), None)
            # 核对期间这个小节被改过（rev 变了）、被重新识别掉了、或者已经不该核对（确认过、正在识别），
            # 或者调弦 / 变调夹 / 拍号变了（核对时按旧设定；精修期间本来就不让改，见 state.settings_lock_reason）：不写
            if (
                cur is None
                or int(cur.get("rev") or 0) != int(bar.get("rev") or 0)
                or staleness.setting_changed(song["meta"], s["meta"])
                or not eligible(s, cur, explicit)
            ):
                return
            tally["checked"] += 1
            if events is not None:
                diff = str(out.get("diff") or "").strip()
                staleness.refine_bar(s, cur, events, str(out.get("uncertain") or "").strip(), diff)
                tally["changed"] += 1
            elif result["status"] == "rejected":
                # 几遍核对对不上：不改音，标成待核对（不确定说明里写上第一遍说的问题）
                if result["note"] not in (cur.get("uncertain") or ""):
                    cur["uncertain"] = " ".join(
                        x for x in (str(cur.get("uncertain") or "").strip(), result["note"]) if x
                    )
                # 用户确认过没问题的（点了“精修这个小节”）：精修觉得可能读错了，确认不再算数，重新待核对
                for k in ("reviewed_key", "reviewed_rev", "reviewed_warn"):
                    cur.pop(k, None)
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
                    f.cancel()  # 还没开始的不再发请求；正在进行的请求已经被打断（见 queued）
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
