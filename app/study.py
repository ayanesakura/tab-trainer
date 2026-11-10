"""练琴计划：像背单词一样按曲子安排每天练什么，外加每天的练琴时长。

- 曲子加进计划后 song.json 里有 study = {order, added, paused}（model.Study）；移出计划只删这个字段，自评记录留着。
- 练完一遍自评 1 卡住 / 2 磕绊 / 3 顺 / 4 轻松，追加到 data/songs/<id>/study.jsonl（model.StudyRecord）。
  进度（新 / 学习中 / 复习、下次哪天）不存盘：每次读的时候按自评记录重放 FSRS（app/fsrs.py）。同一个练琴日只算
  最后一次自评（今天重评就替换今天的）。
- data/study.json：设置（每天目标分钟数、同时学几首、每天最多复习几首）、秒表、练琴记录（秒表停下的、手动补记的）。
- 练琴日：凌晨 4 点换日（DAY_START_HOUR），所有按天算的东西（自评、今天的队列、时长、连续天数）都用它。
- 一天的练琴时长 = 秒表的各段 + 练习页里的练习（每首曲子的 practice.jsonl）合起来去掉重叠，裁到这一天里，
  再加上手动补记的分钟数。

和时钟有关的函数都带 now 参数；不给时取 app.clock（测试可以用 clock.set_now 固定）。纯函数（replay / view / build_queue / merge …）只算数据，
读写文件的在后面几节。
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from pydantic import ValidationError

from app import clock, fsrs, practice, store
from app.errors import Conflict, CorruptSong, Invalid, NotFound
from app.model import MAX_SESSION_MINUTES, STUDY_SETTINGS, StudyRecord, StudySession, StudyTimer, is_score

log = logging.getLogger("tab_trainer.study")

DAY_START_HOUR = 4  # 练琴日从凌晨 4 点开始：半夜 1 点练的还算前一天
TIME_FMT = clock.STAMP_FMT
NOT_IN_PLAN = "这首曲子不在练琴计划里"
MIN_TIMER_SECONDS = 60  # 秒表停下时不到 1 分钟的不记（点错了），除非手填了分钟数

Interval = tuple[dt.datetime, dt.datetime]


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------
def current(now: dt.datetime | None = None) -> dt.datetime:
    """现在：给了 now 用它，否则取 app.clock（和练习记录同一个时钟）。"""
    return (now or clock.now()).replace(microsecond=0)


def practice_day(t: dt.datetime) -> str:
    """本地时间 t 所在的练琴日 "YYYY-MM-DD"（凌晨 4 点前算前一天）。"""
    return (t - dt.timedelta(hours=DAY_START_HOUR)).date().isoformat()


def day_start(day: str) -> dt.datetime:
    """练琴日开始的时刻（当天 04:00）。"""
    return dt.datetime.fromisoformat(day) + dt.timedelta(hours=DAY_START_HOUR)


def day_bounds(day: str) -> Interval:
    start = day_start(day)
    return start, start + dt.timedelta(days=1)


def add_days(day: str, n: int) -> str:
    return (dt.date.fromisoformat(day) + dt.timedelta(days=n)).isoformat()


def days_between(a: str, b: str) -> int:
    """从练琴日 a 到 b 过了几天（b 在前是负数）。"""
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def fmt(t: dt.datetime) -> str:
    return t.strftime(TIME_FMT)


def parse_time(s: object) -> dt.datetime | None:
    """ "YYYY-MM-DD HH:MM[:SS]" → datetime；格式不对是 None。"""
    if not isinstance(s, str) or len(s) < 16:
        return None
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def valid_day(day: object) -> bool:
    if not isinstance(day, str) or len(day) != 10:
        return False
    try:
        dt.date.fromisoformat(day)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# 自评记录 study.jsonl
# ---------------------------------------------------------------------------
def clean_rating(rec: object) -> dict | None:
    """一行自评记录按 model.StudyRecord 检查并整理；格式不对返回 None（这一行不要）。"""
    if not isinstance(rec, dict):
        return None
    try:
        return StudyRecord.model_validate(rec).model_dump()
    except ValidationError:
        return None


def ratings(song_id: str) -> list[dict]:
    """这首曲子的全部自评（按写入的先后）。store.read_records 按文件签名缓存，只读。"""
    return store.read_records(song_id, store.STUDY_LOG_FILE, clean_rating)


def last_per_day(log_: list[dict]) -> dict[str, dict]:
    """每个练琴日只留最后一次自评（今天重评替换今天的），按日期排好。"""
    by_day: dict[str, dict] = {}
    for r in log_:
        by_day[r["day"]] = r
    return dict(sorted(by_day.items()))


# ---------------------------------------------------------------------------
# 重放 FSRS（纯函数）
# ---------------------------------------------------------------------------
def replay(rated: list[tuple[str, int]]) -> dict:
    """按练琴日先后的 [(day, 自评)] 重放，得到 {state, due, reps, lapses, s, d}。
    第一天：S = S0、D = D0，顺（≥3）直接进复习、按间隔排下次，否则进学习、明天再练。
    之后：隔 t 天更新 D 和 S；复习中卡住退回学习（lapses +1）、明天再练，否则按间隔排；
    学习中顺（≥3）毕业进复习、按间隔排，否则明天再练。"""
    st: dict = {"state": "new", "due": None, "reps": 0, "lapses": 0, "s": None, "d": None}
    prev = None
    for day, g in rated:
        if prev is None:
            s, d = fsrs.init_stability(g), fsrs.init_difficulty(g)
            graduate = g >= 3
        else:
            d, s = fsrs.review(st["d"], st["s"], days_between(prev, day), g)
            if st["state"] == "review":
                graduate = g >= 2
                if g == 1:
                    st["lapses"] += 1
            else:
                graduate = g >= 3
        st.update(
            state="review" if graduate else "learning",
            due=add_days(day, fsrs.interval(s) if graduate else 1),
            s=s,
            d=d,
            reps=st["reps"] + 1,
        )
        prev = day
    return st


def view(study: dict, log_: list[dict], today: str) -> dict:
    """接口里的 study（见 model.StudyView）：study 是 song.json 里的字段，log_ 是自评记录。"""
    days = last_per_day(log_)
    rated = [(d, r["rating"]) for d, r in days.items()]
    st = replay(rated)
    last = days[next(reversed(days))] if days else None
    others = [x for x in rated if x[0] != today]
    preview = {}
    for g in range(1, 5):
        p = replay(sorted([*others, (today, g)]))
        preview[str(g)] = {"state": p["state"], "due_in": _due_in(p["due"], today)}
    return {
        "state": st["state"],
        "paused": bool(study.get("paused")),
        "order": int(study.get("order") or 0),
        "added": str(study.get("added") or ""),
        "due": st["due"],
        "due_in": _due_in(st["due"], today),
        "last": {k: last[k] for k in ("day", "rating", "issues", "note", "bpm")} if last else None,
        "rated_today": days[today]["rating"] if today in days else None,
        "reps": st["reps"],
        "lapses": st["lapses"],
        "stability": round(st["s"], 3) if st["s"] is not None else None,
        "difficulty": round(st["d"], 3) if st["d"] is not None else None,
        "preview": preview,
    }


def _due_in(due: str | None, today: str) -> int | None:
    return days_between(today, due) if due else None


# ---------------------------------------------------------------------------
# 今天的队列（纯函数）
# ---------------------------------------------------------------------------
def build_queue(items: list[dict], settings: dict, today: str, rated_at: dict[str, str]) -> dict:
    """items：计划里的曲子（{id, title, artist, study: view, today_seconds, playable}）；rated_at：今天评过的曲子 →
    今天最后一次自评的时间。返回 {queue, done, counts}。暂停的不排进队列。
    队列：学习中、今天还没评的（按计划顺序）→ 到期的复习（最过期的在前，同样过期按计划顺序，最多 max_reviews 首，
    0 = 不限）→ 新曲子（按计划顺序），学习中的（今天已经练过的也算）加上新排进来的不超过 max_learning 首。
    done：今天评过的（按评的时间）。"""
    items = sorted(items, key=lambda x: (x["study"]["order"], x["id"]))
    active = [x for x in items if not x["study"]["paused"]]
    todo = [x for x in active if x["id"] not in rated_at]
    learning = [x for x in todo if x["study"]["state"] == "learning"]
    due = [x for x in todo if x["study"]["state"] == "review" and x["study"]["due_in"] <= 0]
    due.sort(key=lambda x: x["study"]["due_in"])  # 稳定排序：同样过期的保持计划顺序
    limit = settings["max_reviews"]
    waiting = [x for x in active if x["study"]["state"] == "new"]
    # 同时在学的（今天已经练过的也算）加上新排进来的不超过 max_learning
    learning_all = sum(1 for x in active if x["study"]["state"] == "learning")
    room = max(0, settings["max_learning"] - learning_all)
    queue = (
        [{**x, "kind": "learning"} for x in learning]
        + [{**x, "kind": "review"} for x in (due[:limit] if limit else due)]
        + [{**x, "kind": "new"} for x in waiting[:room]]
    )
    done = sorted((x for x in items if x["id"] in rated_at), key=lambda x: (rated_at[x["id"]], x["id"]))
    return {
        "queue": queue,
        "done": [{**x, "kind": x["study"]["state"]} for x in done],
        "counts": {
            "learning": learning_all,
            "review_due": len(due),
            "new": len(waiting),
            "in_plan": len(items),
            "paused": len(items) - len(active),
        },
    }


def streak(active: set[str], today: str) -> int:
    """连续练琴的天数：到今天为止；今天还没练的话到昨天为止。"""
    day = today if today in active else add_days(today, -1)
    n = 0
    while day in active:
        n += 1
        day = add_days(day, -1)
    return n


# ---------------------------------------------------------------------------
# 时间段：合并、裁剪、按天分（纯函数）
# ---------------------------------------------------------------------------
def merge(intervals: list[Interval]) -> list[Interval]:
    """去掉重叠（首尾相接的也连成一段），按开始时间排好。长度为 0 的不要。"""
    out: list[list[dt.datetime]] = []
    for a, b in sorted(x for x in intervals if x[1] > x[0]):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def clip(intervals: list[Interval], lo: dt.datetime, hi: dt.datetime) -> list[Interval]:
    return [(max(a, lo), min(b, hi)) for a, b in intervals if b > lo and a < hi]


def seconds_of(intervals: list[Interval]) -> float:
    return sum((b - a).total_seconds() for a, b in intervals)


def seconds_by_day(merged: list[Interval]) -> dict[str, float]:
    """合并好的时间段按练琴日切开，每天多少秒。"""
    out: dict[str, float] = {}
    for a, b in merged:
        while a < b:
            day = practice_day(a)
            end = min(b, day_bounds(day)[1])
            out[day] = out.get(day, 0.0) + (end - a).total_seconds()
            a = end
    return out


def _segments(segs: list, now: dt.datetime) -> list[Interval]:
    """存盘的 [[开始, 结束 | None], ...] → 时间段；没结束的那段到 now 为止（时钟往回拨了的按 0 算）。"""
    out = []
    for a, b in segs:
        start = parse_time(a)
        end = parse_time(b) if b is not None else now
        if start and end:
            out.append((start, max(start, end)))
    return out


def session_intervals(session: dict) -> list[Interval]:
    """秒表记下的一次练琴实际占的时间段：总长按记下的 minutes——停的时候改小了（忘了停）就从头截到这么长，
    改大了就把多出来的算在第一段之前。"""
    segs = _segments(session.get("segments") or [], dt.datetime.min)
    left = float(session.get("minutes") or 0) * 60
    out = []
    for a, b in segs:
        take = min(left, (b - a).total_seconds())
        if take > 0:
            out.append((a, a + dt.timedelta(seconds=take)))
            left -= take
    if left > 0.5 and segs:  # 记下的比秒表走的长（取整误差不算）：算作开表之前就在练，不往以后延
        first = segs[0][0]
        out.insert(0, (first - dt.timedelta(seconds=left), first))
    return out


def app_intervals(records: list[dict]) -> list[Interval]:
    """练习页的练习记录 → 时间段 [at − seconds, at]（at 是练完的时间，到秒；早先的记录只到分钟）。
    种子记录（旧版本的聚合）和没记时间的没有时间段，不算（见 practice.undated_seconds）。"""
    out = []
    for r in records:
        end = parse_time(r.get("at"))
        secs = float(r.get("seconds") or 0)
        if r.get("seed") or end is None or secs <= 0:
            continue
        out.append((end - dt.timedelta(seconds=secs), end))
    return out


# ---------------------------------------------------------------------------
# data/study.json
# ---------------------------------------------------------------------------
def clean_settings(raw: object) -> dict:
    """设置：每一项不对（没有、类型不对、超出范围）就用默认值。"""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for k, (lo, hi, default) in STUDY_SETTINGS.items():
        v = raw.get(k)
        ok = isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi
        out[k] = v if ok else default
    return out


def clean_data(raw: object) -> dict:
    """study.json 的内容整理成 {settings, timer, sessions, next_order}：格式不对的秒表当没有，格式不对的练琴记录跳过。
    next_order：下一首加进计划的曲子的序号（见 _next_order）。"""
    raw = raw if isinstance(raw, dict) else {}
    timer = None
    if raw.get("timer") is not None:
        try:
            timer = StudyTimer.model_validate(raw["timer"]).model_dump()
        except ValidationError:
            log.warning("study.json 里的秒表格式不对，当作没有")
    sessions = []
    for s in raw.get("sessions") if isinstance(raw.get("sessions"), list) else []:
        try:
            rec = StudySession.model_validate(s).model_dump()
        except ValidationError:
            log.warning("study.json 里有一条练琴记录格式不对，跳过：%r", s)
            continue
        if any(b is None for _, b in rec["segments"]):
            continue
        sessions.append(rec)
    nxt = raw.get("next_order")
    nxt = nxt if isinstance(nxt, int) and not isinstance(nxt, bool) and nxt >= 0 else 0
    return {
        "settings": clean_settings(raw.get("settings")),
        "timer": timer,
        "sessions": sessions,
        "next_order": nxt,
    }


def load() -> dict:
    return clean_data(store.load_study())


def _update(fn) -> dict:
    """加锁读-改-写 study.json；fn 拿到的是整理过的内容。fn 抛错时不写。"""

    def apply(raw: dict) -> None:
        data = clean_data(raw)
        fn(data)
        raw.clear()
        raw.update(data)

    return store.update_study(apply)


def timer_view(timer: dict | None, now: dt.datetime) -> dict | None:
    """接口里的秒表：{running, seconds（累计，含正在走的这段）, since（正在走的这段从几点开始）, started（第一段开始）}。"""
    if not timer:
        return None
    segs = timer["segments"]
    running = segs[-1][1] is None
    return {
        "running": running,
        "seconds": round(seconds_of(_segments(segs, now)), 1),
        "since": segs[-1][0] if running else None,
        "started": segs[0][0],
    }


def timer(now: dt.datetime | None = None) -> dict:
    """GET /api/study/timer：只读 study.json（练习页轮询用）。"""
    return {"timer": timer_view(load()["timer"], current(now))}


def timer_action(action: str, minutes: float | None = None, now: dt.datetime | None = None) -> dict:
    """秒表：start / pause / resume / stop / discard。返回 {timer, session}（stop 记下的练琴记录，别的是 None）。
    已经有秒表时 start、没有秒表时其他操作都是 409。stop 可以带 minutes：忘了停的时候改成实际练的分钟数。
    不到 1 分钟就 stop（没填分钟数）：当作没开始过，不记。暂停中再暂停、走着再继续什么都不做。"""
    t = current(now)
    stamp = fmt(t)
    if minutes is not None and action != "stop":
        raise Invalid("只有结束计时时才能填分钟数")
    out: dict = {"session": None}

    def fn(data: dict) -> None:
        tm = data["timer"]
        if action == "start":
            if tm:
                raise Conflict("已经在计时")
            data["timer"] = {"segments": [[stamp, None]]}
            return
        if not tm:
            raise Conflict("没有在计时")
        segs = tm["segments"]
        running = segs[-1][1] is None
        if running and action in ("pause", "stop"):
            start = parse_time(segs[-1][0])
            segs[-1][1] = fmt(max(t, start)) if start else stamp
        if action == "resume" and not running:
            segs.append([stamp, None])
        if action in ("stop", "discard"):
            data["timer"] = None
        if action != "stop":
            return
        total = seconds_of(_segments(segs, t))
        if minutes is None and total < MIN_TIMER_SECONDS:
            return
        start = parse_time(segs[0][0]) or t
        session = {
            "id": _new_session_id(data["sessions"]),
            "kind": "timer",
            "day": practice_day(start),
            "segments": segs,
            # 忘了停的秒表最多记 MAX_SESSION_MINUTES（和手填的上限一样）
            "minutes": round(min(minutes if minutes is not None else total / 60, MAX_SESSION_MINUTES), 2),
            "note": "",
        }
        data["sessions"].append(session)
        out["session"] = session

    data = _update(fn)
    return {"timer": timer_view(data["timer"], t), "session": out["session"]}


def _new_session_id(sessions: list[dict]) -> str:
    used = {s["id"] for s in sessions}
    while (sid := uuid.uuid4().hex[:8]) in used:
        pass
    return sid


def add_session(day: str, minutes: float, note: str = "", now: dt.datetime | None = None) -> dict:
    """手动补记一次练琴（格式和范围在 schemas.StudySessionIn 里检查过）。不能记今天以后的日子。"""
    if day > practice_day(current(now)):
        raise Invalid("不能补记今天以后的练琴")
    out: dict = {}

    def fn(data: dict) -> None:
        out.update(
            id=_new_session_id(data["sessions"]),
            kind="manual",
            day=day,
            segments=[],
            minutes=round(float(minutes), 2),
            note=note.strip(),
        )
        data["sessions"].append(dict(out))

    _update(fn)
    return out


def delete_session(session_id: str) -> None:
    def fn(data: dict) -> None:
        keep = [s for s in data["sessions"] if s["id"] != session_id]
        if len(keep) == len(data["sessions"]):
            raise NotFound("这条练琴记录不存在")
        data["sessions"] = keep

    _update(fn)


def set_settings(fields: dict) -> dict:
    """改设置（只改给了的几项，范围在 schemas.StudySettingsIn 里检查过），返回全部设置。"""

    def fn(data: dict) -> None:
        data["settings"].update({k: int(v) for k, v in fields.items() if k in STUDY_SETTINGS})

    return _update(fn)["settings"]


# ---------------------------------------------------------------------------
# 读曲库：每首曲子的计划状态、自评、练习页的练习时间
# ---------------------------------------------------------------------------
class _Library:
    """一次读完整个曲库要用的东西（每个接口调用读一次）。"""

    def __init__(self) -> None:
        self.songs: dict[str, dict] = {}  # 读得出来的曲子
        self.app: dict[str, list[Interval]] = {}  # 每首曲子练习页里的练习时间段（读不出来的曲子也算）
        self.logs: dict[str, list[dict]] = {}  # 每首曲子的自评（移出计划的也在）
        for sid in store.song_ids():
            try:
                self.app[sid] = app_intervals(practice.records(sid))
                self.logs[sid] = ratings(sid)
            except (NotFound, OSError, ValueError):  # 刚删掉的；记录文件不是 UTF-8（手改坏了）的这首不算
                continue
            try:
                self.songs[sid] = store.read(sid)  # 有缓存，只读
            except NotFound:
                continue
            except CorruptSong as e:
                log.warning("song=%s 数据损坏，练琴计划里跳过：%s", sid, e)
            except Exception:  # 内容不对（手改坏的文件之类）：跳过这一首，别的照常
                log.exception("song=%s 无法读取，练琴计划里跳过", sid)

    def plan(self) -> dict[str, dict]:
        return {sid: s for sid, s in self.songs.items() if isinstance(s.get("study"), dict)}

    def all_intervals(self, data: dict, now: dt.datetime) -> list[Interval]:
        """所有的练琴时间段（没合并）：秒表记下的、正在走的秒表、练习页的。"""
        out = [x for v in self.app.values() for x in v]
        for s in data["sessions"]:
            if s["kind"] == "timer":
                out += session_intervals(s)
        if data["timer"]:
            out += _segments(data["timer"]["segments"], now)
        return out

    def activity(self, data: dict, now: dt.datetime) -> dict[str, dict]:
        """每个有动静的练琴日：{minutes（秒表 + 练习页去重后，加上手动补记）, manual, ratings（评了几首）}。"""
        days: dict[str, dict] = {}

        def day(d: str) -> dict:
            return days.setdefault(d, {"seconds": 0.0, "manual": 0.0, "ratings": 0})

        for d, secs in seconds_by_day(merge(self.all_intervals(data, now))).items():
            day(d)["seconds"] += secs
        for s in data["sessions"]:
            if s["kind"] == "manual":
                day(s["day"])["manual"] += s["minutes"]
        for log_ in self.logs.values():
            for d in last_per_day(log_):
                day(d)["ratings"] += 1
        for v in days.values():
            v["minutes"] = round(v["seconds"] / 60 + v["manual"], 1)
            v["manual"] = round(v["manual"], 1)
        return days


def playable(song: dict) -> bool:
    """能打开练习（和曲库页的 canPlay 一样）：有小节，或者是 GP / MusicXML（小节由浏览器转换）。"""
    return song.get("status") != "corrupt" and (bool(song.get("bars")) or is_score(song))


def song_study(song: dict, now: dt.datetime | None = None) -> dict | None:
    """曲库列表项 / 曲子详情里的 study；不在计划里是 None。"""
    st = song.get("study")
    if not isinstance(st, dict):
        return None
    return view(st, ratings(song["id"]), practice_day(current(now)))


def _minutes(act: dict, day: str, goal: int) -> dict:
    a = act.get(day) or {}
    return {"total": a.get("minutes", 0.0), "manual": a.get("manual", 0.0), "goal": goal}


def today(now: dt.datetime | None = None) -> dict:
    """GET /api/study/today：今天的队列、做完的、时长、连续天数、秒表。"""
    t = current(now)
    day = practice_day(t)
    data = load()
    lib = _Library()
    act = lib.activity(data, t)
    lo, hi = day_bounds(day)
    items, rated_at = [], {}
    for sid, s in lib.plan().items():
        log_ = lib.logs.get(sid, [])
        meta = s.get("meta") if isinstance(s.get("meta"), dict) else {}
        items.append(
            {
                "id": sid,
                "title": s.get("title") or "未命名",
                "artist": meta.get("artist", ""),
                "study": view(s["study"], log_, day),
                "today_seconds": round(seconds_of(clip(merge(lib.app.get(sid, [])), lo, hi)), 1),
                "playable": playable(s),
            }
        )
        if today_rec := last_per_day(log_).get(day):
            rated_at[sid] = today_rec["at"]
    active = {d for d, v in act.items() if v["minutes"] > 0 or v["ratings"]}
    week = [add_days(day, -i) for i in range(7)]
    return {
        "day": day,
        "settings": data["settings"],
        "minutes": _minutes(act, day, data["settings"]["goal_minutes"]),
        "streak": streak(active, day),
        "week_minutes": round(sum((act.get(d) or {}).get("minutes", 0.0) for d in week), 1),
        "timer": timer_view(data["timer"], t),
        **build_queue(items, data["settings"], day, rated_at),
    }


def calendar(days: int = 371, now: dt.datetime | None = None) -> dict:
    """GET /api/study/calendar：最近 days 个练琴日（含今天）里有练琴或自评的日子，早的在前。"""
    t = current(now)
    day = practice_day(t)
    data = load()
    act = _Library().activity(data, t)
    first = add_days(day, -(days - 1))
    out = [
        {"day": d, "minutes": v["minutes"], "ratings": v["ratings"]}
        for d, v in sorted(act.items())
        if first <= d <= day and (v["minutes"] > 0 or v["ratings"])
    ]
    return {"days": out, "goal": data["settings"]["goal_minutes"], "today": day}


def day_detail(day: str, now: dt.datetime | None = None) -> dict:
    """GET /api/study/day/{day}：这一天的时长、练琴记录、练过 / 评过的曲子、合并后的时间段。"""
    if not valid_day(day):
        raise Invalid("日期格式不对（要是 YYYY-MM-DD）")
    t = current(now)
    data = load()
    lib = _Library()
    act = lib.activity(data, t)
    lo, hi = day_bounds(day)
    songs = []
    for sid in lib.app.keys() | lib.logs.keys():
        secs = seconds_of(clip(merge(lib.app.get(sid, [])), lo, hi))
        rec = last_per_day(lib.logs.get(sid, [])).get(day)
        if secs <= 0 and not rec:
            continue
        s = lib.songs.get(sid) or {}
        songs.append(
            {
                "id": sid,
                "title": s.get("title") or ("未命名" if s else sid),
                "seconds": round(secs, 1),
                "rating": {k: rec[k] for k in ("rating", "issues", "note", "bpm")} if rec else None,
            }
        )
    songs.sort(key=lambda x: (-x["seconds"], x["title"], x["id"]))
    merged = clip(merge(lib.all_intervals(data, t)), lo, hi)
    return {
        "day": day,
        "minutes": _minutes(act, day, data["settings"]["goal_minutes"]),
        "sessions": [s for s in data["sessions"] if s["day"] == day],
        "songs": songs,
        "intervals": [[fmt(a), fmt(b)] for a, b in merged],
    }


# ---------------------------------------------------------------------------
# 写：加入 / 移出 / 暂停、排顺序、自评
# ---------------------------------------------------------------------------
def _plan_songs() -> dict[str, dict]:
    """计划里、读得出来的曲子（排顺序、加入时找最大序号用；store.read 的缓存，只读）。不拿任何锁。"""
    out = {}
    for sid in store.song_ids():
        try:
            s = store.read(sid)
        except (NotFound, CorruptSong):  # 刚删掉的、坏了的在这里不管
            continue
        except Exception:
            log.exception("song=%s 无法读取，练琴计划里跳过", sid)
            continue
        if isinstance(s.get("study"), dict):
            out[sid] = s
    return out


def _next_order() -> int:
    """新加进计划的曲子排在最后：比现在计划里最大的序号大。扫曲库不拿锁；取号在 study.json 的锁里，
    同时加两首也不会拿到同一个序号（计数器 next_order 存在 study.json）。"""
    biggest = max((int(x["study"].get("order") or 0) for x in _plan_songs().values()), default=-1)
    out = {}

    def fn(data: dict) -> None:
        out["n"] = max(data["next_order"], biggest + 1)
        data["next_order"] = out["n"] + 1

    _update(fn)
    return out["n"]


def set_plan(
    sid: str, in_plan: bool | None = None, paused: bool | None = None, now: dt.datetime | None = None
) -> dict:
    """PUT /api/songs/{sid}/study：加入计划（排在最后）/ 移出（自评记录留着）/ 暂停 / 继续。返回 {study}。"""
    t = current(now)
    s = store.read(sid)
    st = s.get("study") if isinstance(s.get("study"), dict) else None
    if in_plan is False:
        if st is not None:
            store.update(sid, lambda x: x.pop("study", None))
        return {"study": None}
    if st is None and not in_plan:
        if paused is not None:
            raise Invalid(NOT_IN_PLAN)
        return {"study": None}
    order = _next_order() if st is None else None  # 在曲子的锁外面取号（要扫曲库）

    def apply(x: dict) -> None:
        cur = x.get("study") if isinstance(x.get("study"), dict) else None
        if cur is None:
            if not in_plan:  # 刚被别处移出了计划
                raise Invalid(NOT_IN_PLAN)
            # 读的时候还在计划里、改的时候已经被移出：重新取号（已经拿着曲子的锁，只看计数器、不扫曲库）
            n = order if order is not None else _next_order_counter()
            cur = {"order": n, "added": fmt(t), "paused": False}
        x["study"] = {**cur, **({"paused": paused} if paused is not None else {})}

    if st is None or (paused is not None and bool(st.get("paused")) != paused):
        s = store.update(sid, apply)
    return {"study": song_study(s, t)}


def _next_order_counter() -> int:
    """只按 study.json 里的计数器取号（已经拿着曲子的锁时用，不扫曲库）。"""
    out = {}

    def fn(data: dict) -> None:
        out["n"] = data["next_order"]
        data["next_order"] += 1

    _update(fn)
    return out["n"]


def reorder(ids: list[str]) -> None:
    """PUT /api/study/order：ids 里（计划中的）曲子按给的顺序排在最前，没列出的按原来的先后排在后面。
    扫曲库不拿锁；每首只在改自己的序号时锁一下（期间被移出计划、删掉的跳过）。"""
    plan = _plan_songs()
    listed = [x for x in dict.fromkeys(ids) if x in plan]
    rest = sorted(
        (x for x in plan if x not in listed), key=lambda x: (int(plan[x]["study"].get("order") or 0), x)
    )
    for i, sid in enumerate(listed + rest):
        if plan[sid]["study"].get("order") == i:
            continue

        def apply(s: dict, i: int = i) -> None:
            if isinstance(s.get("study"), dict):
                s["study"]["order"] = i

        try:
            store.update(sid, apply)
        except NotFound:
            continue


def rate(
    sid: str, rating: int, issues: list[str], note: str = "", bpm: int = 0, now: dt.datetime | None = None
) -> dict:
    """POST /api/songs/{sid}/study/rate：记下这一遍的自评（同一天再评替换今天的）。返回 {study}。"""
    t = current(now)
    s = store.read(sid)
    if not isinstance(s.get("study"), dict):
        raise Invalid(NOT_IN_PLAN)
    rec = clean_rating(
        {
            "at": fmt(t),
            "day": practice_day(t),
            "rating": rating,
            "issues": issues,
            "note": note.strip(),
            "bpm": bpm,
        }
    )
    if rec is None:  # schemas.StudyRateIn 检查过，不会到这里
        raise Invalid("自评格式不对")
    store.append_study_log(sid, rec)
    return {"study": song_study(s, t)}
