"""曲子数据（song.json）的结构：一处定义，存储、业务、接口和模型 schema 都从这里来。

- Song / Bar / Event / Note …：song.json 的形状（Pydantic）。导入外来的曲子包时用它检查；新建曲子用 new_song。
  业务代码照常操作 dict（在 store.update 里读-改-写），字段名和这里一致。
- 派生字段“读时算、写时去”：小节编号 n、按内容算的警告（时值、网格、弦品范围、占位）、meta.tuning_midi、
  meta.title（= 曲子的 title）、plan.stale 都不存盘。store.load / update 之后调用 derive 补上，store.save 之前用 strip 去掉，
  所以文件里每样东西只有一份，内存里的 dict 仍然什么都有（识别、讲解、提示词照常用）。
- llm_schema：从 Note / Event 的字段生成给模型的 JSON Schema（llm.NOTE_SCHEMA / EVENT_SCHEMA），
  tests/test_model.py 断言它和原来手写的逐字一致。
- SongView / LibraryItem：接口返回的形状（见 app/views.py）。
"""

from __future__ import annotations

import copy
import time
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field

from app.music import DERIVED_WARNS, MAX_FRET, STRINGS, TECHS, WARN_CODES, derive_warns, tuning_midi

SourceType = Literal["image", "pdf", "gp", "musicxml"]
Status = Literal["queued", "parsing", "ready", "error", "cancelled", "imported"]
ActivityKind = Literal["parse", "reparse_row", "explain"]
WarnCode = Literal[tuple(WARN_CODES)]  # type: ignore[valid-type]
SCORE_SOURCES = ("gp", "musicxml")
USAGE_KEYS = ("input_tokens", "output_tokens", "cost_usd", "unpriced_tokens")


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# ---------------------------------------------------------------------------
# 音符和事件（同时生成模型输出的 schema，见 llm_schema）
# ---------------------------------------------------------------------------
class Note(_M):
    string: int = Field(
        ge=1,
        le=STRINGS,
        description="1 = highest (top tab line) ... 6 = lowest",
        json_schema_extra={"llm_enum": True},
    )
    fret: int = Field(ge=0, le=MAX_FRET, description="fret number exactly as written in the tab")
    tech: list[str] = Field(default_factory=list, json_schema_extra={"llm_items_enum": TECHS})
    grace: int = Field(-1, description="fret of a grace note leading into this note, -1 if none")
    bend: float = Field(0, description="bend amount in semitones, 0 if no bend")


class Event(_M):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    dur: str = Field(
        description="time from this onset to the next onset (or bar end), in quarter notes, "
        'as a fraction string: "1/4" = 16th, "1/2" = 8th, "1/3" = 8th-note triplet, '
        '"1/6" = 16th sextuplet, "3/4" = dotted 8th, "1", "3/2", "2", "4" ...'
    )
    notes: list[Note] = Field(default_factory=list, description="empty for a rest")


_JSON_TYPES = {int: "integer", float: "number", str: "string", bool: "boolean"}


def llm_schema(cls: type[BaseModel]) -> dict:
    """给模型的 structured outputs schema：所有字段必填、不允许多余字段；描述、枚举取自字段定义。"""
    props = {}
    for name, f in cls.model_fields.items():
        extra = f.json_schema_extra if isinstance(f.json_schema_extra, dict) else {}
        ann = f.annotation
        if get_origin(ann) is list:
            item = get_args(ann)[0]
            if isinstance(item, type) and issubclass(item, BaseModel):
                items: dict = llm_schema(item)
            else:
                items = {"type": _JSON_TYPES[item]}
                if "llm_items_enum" in extra:
                    items["enum"] = list(extra["llm_items_enum"])
            prop: dict = {"type": "array", "items": items}
        else:
            prop = {"type": _JSON_TYPES[ann]}
            if extra.get("llm_enum"):
                lo = next(m.ge for m in f.metadata if getattr(m, "ge", None) is not None)
                hi = next(m.le for m in f.metadata if getattr(m, "le", None) is not None)
                prop["enum"] = list(range(lo, hi + 1))
        if f.description:
            prop["description"] = f.description
        props[name] = prop
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


# ---------------------------------------------------------------------------
# song.json
# ---------------------------------------------------------------------------
class Warn(_M):
    code: WarnCode
    msg: str


class QA(_M):
    q: str
    a: str
    at: str = ""
    stale: bool = False
    stale_by_edit: bool = False


class Orig(_M):
    """第一次手改之前的内容（撤销回识别结果用）。"""

    events: list[Event]
    tip: str = ""
    uncertain: str = ""


class Bar(_M):
    id: str
    system: int | None = None
    page: int | None = None
    region: list[float] | None = None
    events: list[Event]
    tip: str = ""
    uncertain: str = ""
    warn: list[Warn] = Field(default_factory=list)  # 存盘的只有识别 / 导入时留下的（OCR 不一致等）
    edited: bool = False  # 用户手改过（撤销回原样后变回 False）
    orig: Orig | None = Field(None, alias="_orig")
    placeholder: bool = False  # 识别失败、按整小节休止占位
    qa: list[QA] = Field(default_factory=list)
    time_signature: list[int] | None = None  # 和全曲拍号不同时才有
    ts_change: list[int] | None = None  # 图片谱子：谱上这一小节写了变拍号
    repeat_start: bool | None = None
    repeat_end: int | None = None
    endings: list[int] | None = None
    directions: list[str] | None = None


class Page(_M):
    file: str
    width: int
    height: int


class System(_M):
    page: int
    crop: list[float]
    spans: list[list[float]] | None = None
    lines: list[float] | None = None
    status: Literal["pending", "parsing", "done", "error"] = "pending"
    error: str | None = None


class Meta(_M):
    artist: str = ""
    arranger: str = ""
    tuning: list[str]
    capo: int = 0
    time_signature: list[int]
    tempo: int = 0
    notes: str = ""


class PlanStep(_M):
    bars: str = ""
    title: str = ""
    advice: str = ""


class Plan(_M):
    model_config = ConfigDict(extra="allow")
    overview: str = ""
    steps: list[PlanStep] = Field(default_factory=list)
    changed_bars: list[str] = Field(default_factory=list)  # 改过音的小节（改回原样还能撤销）
    stale_all: bool = False  # 调弦 / 拍号变了、小节号整体移动了


class Activity(_M):
    kind: ActivityKind
    restaging: bool = False  # 整首重新识别（原来的结果在新结果全部成功前不动）
    systems: list[int] = Field(default_factory=list)  # 正在识别的行


class Notice(_M):
    kind: Literal["warn", "error"]
    msg: str
    source: str = ""  # 哪类任务留下的（同类任务再开始时清掉）


class Progress(_M):
    stage: str = ""
    done: int = 0
    total: int = 0


class Derived(_M):
    version: int | None = None
    alphatab: str = ""


class Song(_M):
    schema_version: int
    id: str
    title: str = ""
    created: str = ""
    imported: str | None = None
    source_type: SourceType
    source: str | None = None  # GP / MusicXML 原文件名
    status: Status
    activity: Activity | None = None
    notice: Notice | None = None
    progress: Progress | None = None
    rev: int = 0
    pages: list[Page] = Field(default_factory=list)
    meta: Meta | None = None
    meta_user: dict = Field(default_factory=dict)  # 用户亲手改的调弦 / 变调夹 / 拍号（重新识别时以它为准）
    systems: list[System] = Field(default_factory=list)
    bars: list[Bar] = Field(default_factory=list)
    plan: Plan | None = None
    usage: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    track: int = 0
    derived: Derived | None = None


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def new_song(sid: str, source_type: str, **fields) -> dict:
    """新曲子的 song.json 内容。"""
    return {
        "id": sid,
        "title": "",
        "created": now(),
        "source_type": source_type,
        "status": "queued",
        "activity": None,
        "notice": None,
        "progress": None,
        "rev": 0,
        "pages": [],
        "meta": None,
        "systems": [],
        "bars": [],
        "plan": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        **fields,
    }


def is_score(song: dict) -> bool:
    """Guitar Pro / MusicXML 导入的曲子：小节由浏览器从原文件转换（只读的缓存），不需要 AI 识别。"""
    return song.get("source_type") in SCORE_SOURCES


def add_usage(s: dict, usage: dict, before: dict | None = None) -> None:
    """把这次任务用掉的 token / 费用（usage 减去开始时的 before）累加到曲子上。"""
    old = s.get("usage") or {}
    s["usage"] = {k: old.get(k, 0) + usage.get(k, 0) - (before or {}).get(k, 0) for k in USAGE_KEYS}


# ---------------------------------------------------------------------------
# 派生字段
# ---------------------------------------------------------------------------
def derive(song: dict) -> dict:
    """补上不存盘的派生字段（原地修改并返回）。见模块说明。"""
    meta = song.get("meta")
    if isinstance(meta, dict) and meta.get("tuning"):
        meta["tuning_midi"] = tuning_midi(meta["tuning"])
        meta["title"] = song.get("title", "")
    for i, b in enumerate(song.get("bars") or []):
        b["n"] = i + 1
        if isinstance(meta, dict) and meta.get("time_signature"):
            derive_warns(b, meta)
    plan = song.get("plan")
    if isinstance(plan, dict):
        plan["stale"] = bool(plan.get("stale_all") or plan.get("changed_bars"))
    return song


def strip(song: dict) -> dict:
    """要写进 song.json 的内容：去掉派生字段和以 _ 开头的临时字段（小节上的 _orig 是数据，保留）。
    曲名只存一份：song.title 为空而 meta 里读出了曲名（第一次识别读到谱头）时，用读出来的。"""
    meta = song.get("meta")
    if not song.get("title") and isinstance(meta, dict) and meta.get("title"):
        song["title"] = str(meta["title"]).strip()
    out: dict[str, Any] = {k: v for k, v in song.items() if not k.startswith("_")}
    if isinstance(meta, dict):
        out["meta"] = {k: v for k, v in meta.items() if k not in ("tuning_midi", "title")}
    bars = []
    for b in song.get("bars") or []:
        nb = {k: v for k, v in b.items() if k != "n" and not (k.startswith("_") and k != "_orig")}
        if "warn" in nb:
            nb["warn"] = [w for w in nb["warn"] if not (isinstance(w, dict) and w.get("code") in DERIVED_WARNS)]
        bars.append(nb)
    out["bars"] = bars
    if isinstance(song.get("plan"), dict):
        out["plan"] = {k: v for k, v in song["plan"].items() if k != "stale"}
    return copy.deepcopy(out)


# ---------------------------------------------------------------------------
# 接口返回的形状（app/views.py 组装）
# ---------------------------------------------------------------------------
class PracticeBar(_M):
    loops: int = 0
    seconds: float = 0
    best_bpm: int = 0
    last: str = ""


class PracticeLast(_M):
    a: str
    b: str
    bpm: int = 0
    at: str = ""
    a_n: int | None = None  # 现在的小节位置（1 起）；小节不存在了是 null
    b_n: int | None = None


class PracticeView(_M):
    bars: dict[str, PracticeBar] = Field(default_factory=dict)
    last: PracticeLast | None = None
    total_seconds: float = 0


class BarView(Bar):
    orig: Orig | None = Field(None, exclude=True)  # _orig 只是撤销基线，不给前端
    n: int
    needs_review: bool = False  # 有警告或模型标了不确定
    locked: str | None = None  # 现在不能改的原因
    warn: list[Warn] = Field(default_factory=list)


class SongView(_M):
    """GET /api/songs/{id} 的返回。"""

    id: str
    title: str
    created: str = ""
    imported: str | None = None
    source_type: SourceType
    source: str | None = None
    status: Status
    rev: int
    busy: bool  # 有任何后台任务（整首识别 / 单行重识别 / 讲解）
    recognizing: bool  # 整首识别或单行重识别在进行（讲解不算）
    activity: Activity | None = None
    can_edit_bars: bool
    editable_meta: list[str]
    notice: dict | None = None  # {kind: warn|error, msg}
    progress: Progress | None = None
    meta: dict | None = None  # 含派生的 title、tuning_midi
    pages: list[Page] = Field(default_factory=list)
    systems: list[System] = Field(default_factory=list)
    bars: list[BarView] = Field(default_factory=list)
    plan: dict | None = None  # 含派生的 stale
    practice: PracticeView
    usage: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    track: int = 0
    derived: Derived | None = None
    # 兼容旧前端，保留一版
    error: str | None = None  # = notice.msg
    running: bool = False  # = busy


class LibraryPractice(_M):
    last_at: str = ""
    total_seconds: float = 0
    last: PracticeLast | None = None


class LibraryItem(_M):
    """GET /api/songs 的一项。"""

    id: str
    title: str
    artist: str = ""
    status: Status | Literal["corrupt"]
    progress: Progress | None = None
    activity: Activity | None = None
    busy: bool = False
    recognizing: bool = False
    notice: dict | None = None
    bars: int = 0
    needs_review_bars: int = 0
    created: str | None = None
    imported: str | None = None
    source_type: SourceType | None = None
    tags: list[str] = Field(default_factory=list)
    practice: LibraryPractice | None = None
    cost_usd: float = 0
    cost_estimated: bool = False
    unpriced_tokens: int = 0
    tokens: int = 0
    # 兼容旧前端，保留一版
    error: str | None = None
    warn_bars: int = 0
    last_practiced: str = ""
