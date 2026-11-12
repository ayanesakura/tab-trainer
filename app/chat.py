"""首页的 AI 对话（练琴 / 曲库页右侧的“问 AI”）：不针对某首曲子，问练习建议、让 AI 安排先练哪首。

- 给模型的上下文由这里按现有的聚合组装成纯文字（home_context）：每首曲子的概况用曲库列表的 views.library_item
  （练习时间、最近一次练习、练琴计划的 study.view），最快速度用 practice.aggregate，今日队列 / 时长 / 连续天数用
  study.today，最近的练琴日历用 study.calendar——口径和页面上显示的一样，不另算。曲子多时按优先级截断（见 _songs_text）。
- 对话存在 data/coach.json（store.load_coach / update_coach，原子写；坏了改名留着、从空对话开始）：
  {"id": 这一段对话的 id, "turns": [{q, a, at}], "usage": 累计用量}。只留最近 settings.chat_keep 轮；“新对话”换一个 id、
  清空 turns。用量（token / 费用）记在这个文件上（没有曲子可记），新对话不清零。
- 第一版 AI 只给建议：这里只读曲库和练琴数据，不改任何东西。
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Callable

from app import config, practice, store, study, views
from app.config import settings
from app.errors import AppError, CorruptSong, NotFound, Upstream
from app.llm import friendly, request_id_of
from app.model import add_usage, now
from app.music import STANDARD, STUDY_RATINGS, VOCAB

log = logging.getLogger("tab_trainer.chat")

SONGS_CHARS = 12000  # 曲子的详细信息最多这么多字（约一万 token）；放不下的只列曲名
NAMES_CHARS = 3000  # 只列曲名的部分最多这么多字；再放不下的只说还有几首
CALENDAR_DAYS = 28  # 带上最近几个练琴日的时长
HISTORY_TURNS = 6  # 带上前面几轮问答当上下文
HISTORY_ANSWER_CHARS = 1500  # 前面的回答太长时截断

WEEKDAYS = "一二三四五六日"
KIND = {"learning": "学习中", "review": "到期复习", "new": "新曲"}
STATE = {"new": "新曲（还没开始）", "learning": "学习中", "review": "复习"}
NOT_PLAYABLE = {
    "queued": "排队识别中",
    "parsing": "识别中",
    "error": "识别失败",
    "cancelled": "识别已取消",
    "corrupt": "数据损坏",
}
ISSUES: dict[str, str] = VOCAB["study_issues"]


# ---------------------------------------------------------------------------
# 存储：data/coach.json
# ---------------------------------------------------------------------------
def _turns(raw: object) -> list[dict]:
    """存盘的问答整理成 [{q, a, at}]：格式不对的（手改坏的）跳过。"""
    out = []
    for t in raw if isinstance(raw, list) else []:
        if isinstance(t, dict) and isinstance(t.get("q"), str) and isinstance(t.get("a"), str):
            out.append({"q": t["q"], "a": t["a"], "at": t["at"] if isinstance(t.get("at"), str) else ""})
    return out


def _conv_id(raw: dict) -> str:
    cid = raw.get("id")
    return cid if isinstance(cid, str) and cid else ""


def _view(raw: dict) -> dict:
    """接口返回的对话：{id, turns, cost}（cost 和曲库里每首曲子的花费同一个形状，见 views.cost_summary）。"""
    return {
        "id": _conv_id(raw),
        "turns": _turns(raw.get("turns")),
        "cost": views.cost_summary(raw.get("usage")),
    }


def conversation() -> dict:
    """GET /api/coach：现在这段对话。"""
    return _view(store.load_coach())


def new_conversation() -> dict:
    """DELETE /api/coach：“新对话”——清空问答、换一个 id（还在回答的那个问题写完时不再并进来）。用量留着。"""

    def fn(d: dict) -> None:
        d["id"] = uuid.uuid4().hex[:12]
        d["turns"] = []

    return _view(store.update_coach(fn))


def _charge(claude) -> None:
    """把这个客户端用掉的 token / 费用记到 coach.json 上（失败时也要记：发出去的请求已经花了钱）。"""
    try:
        store.update_coach(lambda d: add_usage(d, claude.usage))
    except Exception as e:  # noqa: BLE001 - 不能盖住原来的错误
        log.warning("chat usage not recorded: %s", e)


# ---------------------------------------------------------------------------
# 给模型的上下文（纯文字）
# ---------------------------------------------------------------------------
def _clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _duration(seconds: float) -> str:
    m = round(float(seconds or 0) / 60)
    if m < 1:
        return "不到 1 分钟" if seconds else "0 分钟"
    return f"{m // 60} 小时 {m % 60} 分钟" if m >= 60 else f"{m} 分钟"


def ref(sid: str, title: str) -> str:
    """模型提到曲子时用的标记 [[id|曲名]]（前端换成练习页的链接）。曲名里的 | [ ] 换掉，免得标记断开。"""
    clean = title.replace("|", "/").replace("[", "(").replace("]", ")")
    return f"[[{sid}|{clean}]]"


def _days_text(n: int | None) -> str:
    if n is None:
        return ""
    if n == 0:
        return "今天"
    return f"{n} 天后" if n > 0 else f"已过期 {-n} 天"


def _study_text(st: dict, rank: int | None) -> str:
    parts = [f"计划第 {rank} 首" if rank else "在计划里", STATE.get(st["state"], st["state"])]
    if st.get("paused"):
        parts.append("已暂停")
    if st.get("due"):
        parts.append(f"下次该练 {st['due']}（{_days_text(st.get('due_in'))}）")
    last = st.get("last")
    if last:
        s = f"上次自评 {last['day']}：{last['rating']} {STUDY_RATINGS.get(last['rating'], '')}"
        issues = "、".join(ISSUES.get(x, x) for x in last.get("issues") or [])
        if issues:
            s += f"，问题：{issues}"
        if last.get("bpm"):
            s += f"，{last['bpm']} BPM"
        if last.get("note"):
            s += f"，备注“{_clip(last['note'], 60)}”"
        parts.append(s)
    if st.get("rated_today"):
        parts.append("今天已自评")
    if st.get("reps"):
        parts.append(f"评过 {st['reps']} 天")
    if st.get("lapses"):
        parts.append(f"复习时卡住退回 {st['lapses']} 次")
    stuck = [str(b["n"]) for b in st.get("stuck_bars") or []]
    if stuck:
        parts.append(f"上次卡在第 {'、'.join(stuck[:12])} 小节")
    return "；".join(parts)


def _song_text(s: dict, item: dict, rank: int | None) -> str:
    """一首曲子一行：标记、艺人、标签、谱子信息、能不能练、练习情况、计划状态。"""
    meta = s.get("meta") if isinstance(s.get("meta"), dict) else {}
    parts = [ref(item["id"], item["title"])]
    if item.get("artist"):
        parts.append(_clip(item["artist"], 40))
    if item.get("tags"):
        parts.append("标签 " + "、".join(_clip(t, 20) for t in item["tags"][:8]))
    # 不带拍号：GP 曲子的 meta.time_signature 是第一小节的，有弱起小节时是 1/8 这种，会误导模型
    score = [f"{item['bars']} 小节"] if item.get("bars") else []
    if isinstance(meta.get("tempo"), int | float) and meta["tempo"] > 0:
        score.append(f"原速 {round(meta['tempo'])} BPM")
    if isinstance(meta.get("capo"), int) and meta["capo"] > 0:
        score.append(f"变调夹 {meta['capo']}")
    tuning = meta.get("tuning")
    if isinstance(tuning, list) and tuning and tuning != STANDARD:
        score.append("调弦 " + " ".join(map(str, tuning)))
    score.append("Guitar Pro / MusicXML" if item.get("is_score") else "原谱识别")
    parts.append("，".join(score))
    if item.get("archived"):
        parts.append("已归档（不排进今日）")
    if not study.playable(s):
        parts.append("现在还不能练：" + NOT_PLAYABLE.get(item.get("status", ""), "没有小节"))
    elif item.get("needs_review_bars"):
        parts.append(f"{item['needs_review_bars']} 小节识别结果待核对")
    p = item.get("practice")
    if p and p.get("total_seconds"):
        best = max(
            (b.get("best_bpm", 0) for b in practice.aggregate(item["id"], s)["bars"].values()), default=0
        )
        txt = f"累计练了 {_duration(p['total_seconds'])}"
        if p.get("last_at"):
            txt += f"，最近一次 {p['last_at'][:16]}"
        if best:
            txt += f"，最快完整循环 {best} BPM"
        parts.append(txt)
    else:
        parts.append("还没练过")
    if item.get("study"):
        parts.append(_study_text(item["study"], rank))
    else:
        parts.append("不在计划里")
    return "- " + " | ".join(parts)


def _ordered(songs: list[tuple[dict, dict]]) -> list[tuple[dict, dict]]:
    """哪些曲子先列：计划里没归档的（按计划顺序）→ 没归档的 → 归档的；后两组里最近练过的（没练过的按添加时间）在前。"""

    def recent(x: tuple[dict, dict]) -> str:
        item = x[1]
        return (item.get("practice") or {}).get("last_at") or item.get("created") or ""

    def group(x: tuple[dict, dict]) -> tuple:
        item = x[1]
        st = item.get("study")
        if st and not item.get("archived"):
            return (0, st["order"])
        return (2 if item.get("archived") else 1, 0)

    return sorted(sorted(songs, key=recent, reverse=True), key=group)  # 稳定排序：同一组里保持新的在前


def _library() -> tuple[list[tuple[dict, dict]], int]:
    """读得出来的曲子 [(song, 曲库列表项)] 和读不出来的首数。"""
    out, bad = [], 0
    for sid in store.song_ids():
        try:
            s = store.read(sid)  # 有缓存，只读
            out.append((s, views.library_item(s)))
        except NotFound:
            continue
        except CorruptSong:
            bad += 1
        except Exception:  # 手改坏的文件之类：跳过这一首
            log.exception("song=%s 无法读取，AI 对话的上下文里跳过", sid)
            bad += 1
    return out, bad


def _songs_text(songs: list[tuple[dict, dict]], bad: int) -> str:
    """曲库：按 _ordered 的顺序一首一行，超过 SONGS_CHARS 的只列曲名，再超过 NAMES_CHARS 的只说还有几首。"""
    songs = _ordered(songs)
    plan = sorted((it for _, it in songs if it.get("study")), key=lambda it: (it["study"]["order"], it["id"]))
    rank = {it["id"]: i + 1 for i, it in enumerate(plan)}
    lines, names, used, left = [], [], 0, 0
    for s, item in songs:
        if not names:
            line = _song_text(s, item, rank.get(item["id"]))
            if used + len(line) <= SONGS_CHARS:
                lines.append(line)
                used += len(line) + 1
                continue
        name = ref(item["id"], item["title"]) + ("（已归档）" if item.get("archived") else "")
        if used + len(name) <= SONGS_CHARS + NAMES_CHARS:
            names.append(name)
            used += len(name) + 1
        else:
            left += 1
    head = f"曲库共 {len(songs)} 首" + (f"（另有 {bad} 首数据损坏、读不出来）" if bad else "")
    out = [head + "：" if songs else head + "，还没有曲子。", *lines]
    if names or left:
        out.append(
            f"（曲子太多，只有上面 {len(lines)} 首列了详细信息；下面 {len(names)} 首只有曲名"
            + (f"，另有 {left} 首连曲名也没列出" if left else "")
            + "）"
        )
        if names:
            out.append("、".join(names))
    return "\n".join(out)


def _today_text(t: dict) -> str:
    m, c, st = t["minutes"], t["counts"], t["settings"]
    lines = [
        f"今天已练 {round(m['total'])} 分钟，每日目标 {m['goal']} 分钟；连续练琴 {t['streak']} 天；"
        f"最近 7 天共 {round(t['week_minutes'])} 分钟。"
        + ("秒表正在计时。" if (t.get("timer") or {}).get("running") else ""),
        f"计划里 {c['in_plan']} 首（学习中 {c['learning']}，到期复习 {c['review_due']}，没开始的新曲 {c['new']}，"
        f"暂停 {c['paused']}，另有归档的 {c.get('archived', 0)} 首不排进今日）。"
        f"设置：同时在学最多 {st['max_learning']} 首，每天复习最多 "
        + (f"{st['max_reviews']} 首。" if st["max_reviews"] else "不限。"),
    ]
    if t["queue"]:
        lines.append("今日队列（还没练的，按顺序）：")
        for x in t["queue"]:
            due = x["study"].get("due_in")
            extra = f"，{_days_text(due)}" if x["kind"] == "review" and due else ""
            lines.append(f"- {ref(x['id'], x['title'])} {KIND.get(x['kind'], x['kind'])}{extra}")
    else:
        lines.append("今日队列：空的（没有该练的）。")
    if t["done"]:
        lines.append(
            "今天已练完并自评："
            + "、".join(
                f"{ref(x['id'], x['title'])}（{x['study']['rated_today']} {STUDY_RATINGS.get(x['study']['rated_today'], '')}）"
                for x in t["done"]
            )
        )
    return "\n".join(lines)


def _calendar_text(cal: dict) -> str:
    days = cal["days"]
    head = f"最近 {CALENDAR_DAYS} 个练琴日里练过 {sum(1 for d in days if d['minutes'] > 0)} 天"
    total = f"有记录以来共练 {round(cal['total_minutes'])} 分钟" + (
        f"（含早期没记日期的 {round(cal['undated_minutes'])} 分钟）" if cal.get("undated_minutes") else ""
    )
    if not days:
        return f"{head}；{total}。"
    rows = "、".join(
        f"{d['day']} {round(d['minutes'])} 分钟" + (f"（自评 {d['ratings']} 首）" if d["ratings"] else "")
        for d in days
    )
    return f"{head}（没列出的日子没有练琴记录）；{total}。\n{rows}"


def today_label(now_: dt.datetime | None = None) -> str:
    """今天的练琴日和星期，如 “2026-11-12（星期四）”。"""
    day = study.practice_day(study.current(now_))
    return f"{day}（星期{WEEKDAYS[dt.date.fromisoformat(day).weekday()]}）"


def home_context(now_: dt.datetime | None = None) -> str:
    """给模型的曲库 / 练琴数据（中文纯文字）：今天、曲库每首曲子、最近的练琴日历。"""
    songs, bad = _library()
    sections = [
        "【今天】\n" + _today_text(study.today(now_)),
        "【最近的练琴】\n" + _calendar_text(study.calendar(CALENDAR_DAYS, now_)),
        "【曲库】\n" + _songs_text(songs, bad),
    ]
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# 提问
# ---------------------------------------------------------------------------
def ask(question: str) -> dict:
    """问一个问题，等回答写完再返回（首页用的是流式的，见 prepare_ask）。"""
    return prepare_ask(question)()


def prepare_ask(question: str, now_: dt.datetime | None = None) -> Callable[..., dict]:
    """准备问一个问题（schemas.AskIn 已经去掉首尾空白、检查过长度）：读对话、组装上下文、建模型客户端。
    没配 key（400）在这里抛，还没发请求。

    返回真正提问的 run(on_delta=None)：回答边生成边交给 on_delta（文字片段；None 表示前面的作废、从头再来），
    写完后存进对话（等回答期间点了“新对话”的，不并进新对话），返回 {"answer", "id", "turns", "cost"}。
    失败抛 AppError（模型和网络错误转成中文说明的 Upstream）。用量成功、失败都记在 coach.json 上。只存完整的回答。"""
    raw = store.load_coach()
    conv = _conv_id(raw)
    history = _turns(raw.get("turns"))[-HISTORY_TURNS:]
    history = [{**h, "a": _clip_answer(h["a"])} for h in history]
    context = home_context(now_)
    today = today_label(now_)
    claude = config.new_llm()  # 没配 key：MissingKey，400

    def run(on_delta: Callable[[str | None], None] | None = None) -> dict:
        parts: list[str] = []
        try:
            for t in claude.chat_stream(question, context, history, today):
                if t is None:
                    parts.clear()
                else:
                    parts.append(t)
                if on_delta is not None:
                    on_delta(t)
        except AppError:
            _charge(claude)
            raise
        except Exception as e:  # noqa: BLE001 - 网络 / API 错误统一转成中文说明
            log.warning("request_id=%s chat failed: %s", request_id_of(e), e)
            _charge(claude)
            raise Upstream(friendly(e)) from None
        answer = "".join(parts).strip()
        if not answer:
            _charge(claude)
            raise Upstream("模型没有给出回答，换个问法再试一次")
        entry = {"q": question, "a": answer, "at": now()}

        def apply(d: dict) -> None:
            if _conv_id(d) == conv:  # 等回答期间点了“新对话”：不并进新对话
                d["id"] = conv or uuid.uuid4().hex[:12]
                d["turns"] = [*_turns(d.get("turns")), entry][-settings.chat_keep :]
            add_usage(d, claude.usage)

        try:
            d = store.update_coach(apply)
        except Exception:
            _charge(claude)
            raise
        return {"answer": answer, **_view(d)}

    return run


def _clip_answer(a: str) -> str:
    return a if len(a) <= HISTORY_ANSWER_CHARS else a[:HISTORY_ANSWER_CHARS] + "…（后面略）"
