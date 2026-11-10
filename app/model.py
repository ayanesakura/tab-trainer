"""曲子数据（song.json）的结构：一处定义，存储、业务、接口和模型 schema 都从这里来。

- Song / Bar / Event / Note …：song.json 的形状（Pydantic）。导入外来的曲子包时用它检查；新建曲子用 new_song。
  业务代码照常操作 dict（在 store.update 里读-改-写），字段名和这里一致。
- 派生字段“读时算、写时去”：小节编号 n、按内容算的警告（时值、网格、弦品范围、占位）、meta.tuning_midi、
  meta.title（= 曲子的 title）、plan.stale 都不存盘。store.load / update 之后调用 derive 补上，store.save 之前用 strip 去掉，
  所以文件里每样东西只有一份，内存里的 dict 仍然什么都有（识别、讲解、提示词照常用）。
- llm_schema：从 Note / Event 的字段生成给模型的 JSON Schema（prompts.NOTE_SCHEMA / EVENT_SCHEMA），
  tests/test_model.py 断言它和原来手写的逐字一致。
- Study / StudyRecord / StudyTimer / StudySession：练琴计划（song.json 的 study 字段、study.jsonl 的一行、
  data/study.json 的秒表和练琴记录，见 app/study.py）。
- 接口返回的形状（SongView / LibraryItem …）在 app/viewmodels.py（这里按名字转过去，旧的 model.SongView 照样能用）。
- 乐观锁按对象：bar.rev 只在这个小节的内容（音符；用户改提示时也算）变了时 +1，song.meta_rev 只在可改的谱头字段
  （EDITABLE_META）变了时 +1，都由 store.update 比较改前改后自动维护（见 content_snapshot / bump_revs）。
  写问答、讲解补提示、进度、识别其他行都不会让别人手里的版本号过期。顶层 rev 是“任意保存计数”，不用于 409。
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
import re
from typing import Annotated, Any, ClassVar, Literal, get_args, get_origin

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from app import clock
from app.music import (
    DERIVED_WARNS,
    MAX_FRET,
    MAX_TAP_FRET,
    OPTIONAL_EVENT_KEYS,
    SCORE_FILES,
    STRINGS,
    STUDY_ISSUES,
    TECHS,
    WARN_CODES,
    clean_time_signature,
    derive_warns,
    dur_problem,
    events_key,
    tuning_midi,
)

SourceType = Literal["image", "pdf", "gp", "musicxml"]
Status = Literal["queued", "parsing", "ready", "error", "cancelled", "imported"]
ActivityKind = Literal["parse", "reparse_row", "explain", "refine"]
WarnCode = Literal[tuple(WARN_CODES)]  # type: ignore[valid-type]
SCORE_SOURCES = tuple(SCORE_FILES)  # ("gp", "musicxml")
USAGE_KEYS = ("input_tokens", "output_tokens", "cost_usd", "unpriced_tokens")
# 早期曲子只记了 token、没记钱：第一次再花钱时把这部分 token 记到这里，曲库按默认模型的价格估算
LEGACY_USAGE_KEYS = ("legacy_input_tokens", "legacy_output_tokens")
EDITABLE_META = (
    "title",
    "artist",
    "tuning",
    "capo",
    "time_signature",
)  # 用户能改的谱头字段（meta_rev 跟着它们）


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# ---------------------------------------------------------------------------
# 音符和事件（同时生成模型输出的 schema，见 llm_schema）
# ---------------------------------------------------------------------------
class Note(_M):
    # 空的时候不写的字段（和 music.clean_events 一致）：harm 只有点弦泛音才有，with_defaults 不给别的音补 0
    OMIT_EMPTY: ClassVar[tuple[str, ...]] = ("harm",)
    string: int = Field(
        ge=1,
        le=STRINGS,
        description="1 = highest (top tab line) ... 6 = lowest",
        json_schema_extra={"llm_enum": True},
    )
    fret: int = Field(
        ge=0,
        le=MAX_FRET,
        description="fret number exactly as written in the tab (a chord-sheet x mark: the fret of this string "
        "in the current chord diagram)",
    )
    tech: list[str] = Field(default_factory=list, json_schema_extra={"llm_items_enum": TECHS})
    grace: int = Field(-1, description="fret of a grace note leading into this note, -1 if none")
    bend: float = Field(0, description="bend amount in semitones, 0 if no bend")
    harm: int = Field(
        0,
        ge=0,
        le=MAX_TAP_FRET,
        description='for th: the tapped fret written in <> (e.g. 19 in "0 <19>"); 0 otherwise',
    )


class Event(_M):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    # 空的时候不写的字段（和 music.clean_events 一致）：with_defaults 不补，否则“音变没变”的比较会把它当成变了
    OMIT_EMPTY: ClassVar[tuple[str, ...]] = OPTIONAL_EVENT_KEYS
    dur: str = Field(
        description="time from this onset to the next onset (or bar end), in quarter notes, "
        'as a fraction string: "1/4" = 16th, "1/2" = 8th, "1/3" = 8th-note triplet, '
        '"1/6" = 16th sextuplet, "3/4" = dotted 8th, "1", "3/2", "2", "4" ...'
    )
    notes: list[Note] = Field(default_factory=list, description="empty for a rest")
    chord: str = Field(
        "",
        max_length=24,
        description='chord name printed above the staff where a new chord starts at this onset (e.g. "Em", '
        '"Dsus2/G"); "" if no new chord name starts here',
    )
    lyrics: list[str] = Field(
        default_factory=list,
        description="lyrics sung from this onset until the next onset, one string per printed verse line "
        "(verse 1 first); [] if no syllable starts here or the score has no lyrics",
    )
    strum: Literal["", "down", "up"] = Field(
        "",
        description='a strummed chord drawn as an arrow across the strings: "down" = downstroke from the low '
        'strings to the high ones (arrow pointing toward string 1, the top tab line); "up" = upstroke (arrow '
        'pointing toward string 6); "" if not strummed',
    )

    @field_validator("dur")
    @classmethod
    def _positive_fraction(cls, v: str) -> str:
        """时值必须是范围内（见 music.MIN_DUR / MAX_DUR）大于 0 的分数（"1/0"、"-1"、"abc"、"1e400" 之类
        会让读取时的计算出错）。"""
        if why := dur_problem(v):
            raise ValueError(why if "超出范围" in why else f"时值必须是大于 0 的分数：{str(v)[:40]!r}")
        return v


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
        elif get_origin(ann) is Literal:
            prop = {"type": "string", "enum": list(get_args(ann))}
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
# 曲子目录里的文件名（唯一一处规则：存储、接口、曲子包都按它检查，见 store.page_path / source_path）
# ---------------------------------------------------------------------------
# 页面图片一律是 PNG，名字 pN.png（songs.save_pages 存的）；GP / MusicXML 原文件是 source + 认的扩展名
PAGE_FILE = r"^p[0-9]{1,6}\.png$"
SCORE_EXTS = tuple(e for exts in SCORE_FILES.values() for e in exts)
SOURCE_FILE = r"^source(" + "|".join(re.escape(e) for e in SCORE_EXTS) + r")$"


def page_file_name(k: int) -> str:
    return f"p{k}.png"


def page_number(name: str) -> int | None:
    """页面文件名里的序号（p12.png → 12），不是页面文件名是 None。"""
    return int(name[1:-4]) if valid_page_file(name) else None


def valid_page_file(name: object) -> bool:
    return isinstance(name, str) and re.fullmatch(PAGE_FILE, name) is not None


def valid_source_file(name: object) -> bool:
    return isinstance(name, str) and re.fullmatch(SOURCE_FILE, name) is not None


# ---------------------------------------------------------------------------
# 时间格式
# ---------------------------------------------------------------------------
# 练习记录的时间：本地时间 "YYYY-MM-DD HH:MM:SS"（clock.stamp；早先的只到分钟，也认），空串 = 没记时间。
# 会显示在曲库里：外来曲子包、手改的文件里别的内容（比如 HTML）这一条记录就不要
LOCAL_TIME = r"^([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}(:[0-9]{2})?)?$"
# 练琴计划（app/study.py）的时间：本地时间 "YYYY-MM-DD HH:MM:SS"（也认不带秒的），必填；练琴日 "YYYY-MM-DD"
STAMP = r"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}(:[0-9]{2})?$"
DAY = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"


def _real_day(v: str) -> str:
    """格式对、而且是真的日期（2026-02-30 不行）。"""
    datetime.date.fromisoformat(v)
    return v


def _real_stamp(v: str) -> str:
    if not re.fullmatch(STAMP, v):
        raise ValueError(f"时间格式不对：{v[:30]!r}")
    datetime.datetime.fromisoformat(v)
    return v


Day = Annotated[str, Field(pattern=DAY), AfterValidator(_real_day)]
Stamp = Annotated[str, AfterValidator(_real_stamp)]


# ---------------------------------------------------------------------------
# song.json
# ---------------------------------------------------------------------------
def _check_ts(v: list[int]) -> list[int]:
    if clean_time_signature(v) is None:
        raise ValueError(f"拍号不对：{v!r}（分子 1–32，分母是 1–32 里 2 的幂）")
    return v


TimeSignature = Annotated[list[int], AfterValidator(_check_ts)]


def _check_shape(v: list[int]) -> list[int]:
    if len(v) != STRINGS or not all(-1 <= f <= MAX_FRET for f in v):
        raise ValueError(f"和弦指法要有 {STRINGS} 根弦、品位 -1–{MAX_FRET}：{v!r}")
    return v


ChordShape = Annotated[list[int], AfterValidator(_check_shape)]


class Warn(_M):
    code: WarnCode
    msg: str


class QA(_M):
    q: str
    a: str
    at: str = ""
    stale: bool = False
    stale_by_edit: bool = False
    stale_by_refine: bool = False  # 精修改了音才过时的：撤销精修时恢复


class Orig(_M):
    """第一次手改之前的内容（撤销回识别结果用）。"""

    events: list[Event]
    tip: str = ""
    uncertain: str = ""
    warn: list[Warn] | None = None  # 识别时留下的警告（OCR 不一致等），撤销时恢复；早先的没记


class RefinePrev(_M):
    """精修改动之前的内容（撤销精修时恢复）。"""

    events: list[Event]
    tip: str = ""
    uncertain: str = ""
    warn: list[Warn] = Field(default_factory=list)  # 识别时留下的警告（OCR 不一致等）


class Refine(_M):
    """精修改过这个小节：模型说的改动（diff）和改之前的内容。用户手改以后就没有了（撤销精修不再适用）。"""

    diff: str = ""
    prev: RefinePrev


class Bar(_M):
    id: str
    system: int | None = None
    page: int | None = None
    region: list[float] | None = None
    events: list[Event]
    tip: str = ""
    uncertain: str = ""
    warn: list[Warn] = Field(default_factory=list)  # 存盘的只有识别 / 导入时留下的（OCR 不一致等）
    rev: int = 0  # 这个小节内容的版本号（改小节的 PUT 用它做乐观锁）
    edited: bool = False  # 用户手改过（撤销回原样后变回 False）
    orig: Orig | None = Field(None, alias="_orig")
    placeholder: bool = False  # 识别失败、按整小节休止占位
    # 用户点了“确认没问题”时这个小节音符的指纹（notes_key）：音再变就要重新核对，提示变了不算
    reviewed_key: str | None = None
    reviewed_rev: int | None = None  # 早先的确认记的是 rev：rev 没变就还有效
    # 确认时看到的警告（文字）：之后冒出新的警告（比如改了拍号）要重新核对
    reviewed_warn: list[str] | None = None
    refine: Refine | None = None  # 精修改过音（见 app/refine.py）
    refine_key: str | None = None  # 精修核对过时音符的指纹（notes_key）：音没再变就不用再核对
    # 弹唱谱：这个小节里出现的和弦（事件的 chord）的指法，和弦名 → 1–6 弦的品位（-1 = 不弹），给谱面画和弦图
    shapes: dict[str, ChordShape] | None = None
    qa: list[QA] = Field(default_factory=list)
    time_signature: TimeSignature | None = None  # 和全曲拍号不同时才有
    ts_change: TimeSignature | None = None  # 图片谱子：谱上这一小节写了变拍号
    repeat_start: bool | None = None
    repeat_end: int | None = None
    endings: list[int] | None = None
    directions: list[str] | None = None


class Page(_M):
    file: str = Field(pattern=PAGE_FILE)  # pN.png（见 PAGE_FILE）
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
    time_signature: TimeSignature
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
    # 精修（kind = refine）的范围：song = 整首；bars = 这几个小节；row = 第 row 行（从 0 数）
    scope: Literal["song", "bars", "row"] | None = None
    row: int | None = None
    bars: list[str] = Field(default_factory=list)  # 要核对的小节 id
    pending: list[str] = Field(default_factory=list)  # 其中还没核对完的（练习页在这些小节上显示“精修中”）


class Notice(_M):
    kind: Literal["warn", "error", "info"]
    msg: str
    source: str = ""  # 哪类任务留下的（同类任务再开始时清掉）
    rest: Notice | None = None  # 被精修的结果叠在下面的、别的任务留下的提示（见 state.set_notice）


class Progress(_M):
    stage: str = ""
    done: int = 0
    total: int = 0


class Derived(_M):
    version: int | None = None
    alphatab: str = ""


class Refined(_M):
    """上一次精修整首的结果。"""

    at: str = ""  # YYYY-MM-DD HH:MM
    checked: int = 0  # 核对了几个小节
    changed: int = 0  # 改了几个


class Study(_M):
    """这首曲子在练琴计划里（见 app/study.py）：排第几、什么时候加的、是否暂停。没有这个字段 = 不在计划里。
    练习进度（新 / 学习中 / 复习）不存这里，每次按 study.jsonl 的自评记录重算。"""

    order: int = 0
    added: str = Field("", pattern=LOCAL_TIME)
    paused: bool = False


class Song(_M):
    schema_version: int
    id: str
    title: str = ""
    created: str = ""
    imported: str | None = None
    source_type: SourceType
    source: str | None = Field(None, pattern=SOURCE_FILE)  # GP / MusicXML 原文件名 source.<扩展名>
    status: Status
    activity: Activity | None = None
    notice: Notice | None = None
    progress: Progress | None = None
    rev: int = 0  # 保存次数（不用于 409）
    meta_rev: int = 0  # 可改谱头字段的版本号（改谱头的 PUT 用它做乐观锁）
    pages: list[Page] = Field(default_factory=list)
    meta: Meta | None = None
    meta_user: dict = Field(
        default_factory=dict
    )  # 用户亲手改的艺人 / 调弦 / 变调夹 / 拍号（重新识别时以它为准）
    systems: list[System] = Field(default_factory=list)
    bars: list[Bar] = Field(default_factory=list)
    plan: Plan | None = None
    usage: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    track: int = 0
    derived: Derived | None = None
    refined: Refined | None = None
    study: Study | None = None  # 在练琴计划里才有
    # 归档：曲库里收起来、不再排进今天的练琴清单，数据（练习记录、自评、计划位置）都留着。存在 song.json 里：
    # 跟着曲子走（曲子包导出 / 导入自然带上，删曲子一起删），曲库列表读一首曲子就知道，不用再查 study.json。
    # 没归档时不写这个键（所以默认是 None：导入曲子包补默认值时也不会写出 "archived": false）
    archived: bool | None = None


def problems(song: dict) -> list[str]:
    """song.json 的不变量（写盘前检查；导入曲子包时不满足就不导入）：形状符合 Song，以及字段之间的引用对得上——
    小节 id 不重复；图片曲子的小节在存在的页、存在的行上，页号和那一行一致；每一行都在存在的页上。"""
    try:
        Song.model_validate(song)
    except Exception as e:  # noqa: BLE001 - 只报告
        return [f"格式不对：{e}"[:500]]
    out = []
    pages, systems, bars = song.get("pages") or [], song.get("systems") or [], song.get("bars") or []
    ids = [b.get("id") for b in bars]
    if len(set(ids)) != len(ids):
        out.append("小节 id 重复")
    for si, x in enumerate(systems):
        if not 0 <= x["page"] < len(pages):
            out.append(f"第 {si} 行的页号 {x['page']} 不存在")
    for b in [] if is_score(song) else bars:  # GP / MusicXML 的 system 是谱面排版的行，没有 systems
        if b.get("page") is not None and not 0 <= b["page"] < len(pages):
            out.append(f"小节 {b.get('id')} 的页号 {b['page']} 不存在")
            continue
        si = b.get("system")
        if si is None:
            continue
        if not 0 <= si < len(systems):
            out.append(f"小节 {b.get('id')} 的行号 {si} 不存在")
        elif b.get("page") is not None and b["page"] != systems[si]["page"]:
            out.append(f"小节 {b.get('id')} 的页号和第 {si} 行不一致")
    return out


def _dump(v):
    if isinstance(v, BaseModel):
        return v.model_dump(by_alias=True, exclude_none=True)
    if isinstance(v, list):
        return [_dump(x) for x in v]
    return copy.deepcopy(v)


def _fill_defaults(raw, obj) -> None:
    if isinstance(obj, BaseModel) and isinstance(raw, dict):
        for name, f in type(obj).model_fields.items():
            key = f.alias or name
            val = getattr(obj, name)
            if key in raw:
                _fill_defaults(raw[key], val)
            elif name in raw:
                _fill_defaults(raw[name], val)
            elif val is not None and not (not val and name in getattr(obj, "OMIT_EMPTY", ())):
                raw[key] = _dump(val)  # 可选、默认 None 的字段（_orig、plan…）不写：代码按“有没有这个键”判断
    elif isinstance(obj, list) and isinstance(raw, list):
        for r, o in zip(raw, obj, strict=False):
            _fill_defaults(r, o)


def with_defaults(song: dict) -> dict:
    """外来的 song.json（曲子包）：按 Song 校验，缺的字段补上默认值（小节里缺 notes、tip 之类），
    其余原样保留（不在模型里的字段也不丢）。校验不过抛 pydantic.ValidationError。"""
    _fill_defaults(song, Song.model_validate(song))
    return song


# ---------------------------------------------------------------------------
# practice.jsonl 的一行（见 app/practice.py）
# ---------------------------------------------------------------------------
class _Rec(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)


class PracticeRecord(_Rec):
    """一段练习。"""

    at: str = Field("", pattern=LOCAL_TIME)
    ids: list[str] = Field(min_length=1)
    loops: int = Field(0, ge=0)
    bpm: int = Field(0, ge=0)
    seconds: float = Field(0, ge=0)
    kind: Literal["play", "rhythm"] = "play"  # rhythm = 敲节奏：算练习时间，不算遍数 / 最快速度 / 干净的遍数
    clean: bool | None = None  # 用户标了这一遍干净 / 不干净；没标是 None（不写）


class SeedBar(_Rec):
    loops: int = Field(0, ge=0)
    seconds: float = Field(0, ge=0)
    best_bpm: int = Field(0, ge=0)
    last: str = Field("", pattern=LOCAL_TIME)


class SeedLast(_Rec):
    a: str
    b: str | None = None
    bpm: int = Field(0, ge=0)
    at: str = Field("", pattern=LOCAL_TIME)


class PracticeSeed(_Rec):
    """旧版本 song.json 里的练习聚合结果（升级时变成 practice.jsonl 的第一行）。"""

    seed: Literal[True]
    bars: dict[str, SeedBar] = Field(default_factory=dict)
    last: SeedLast | None = None
    total_seconds: float = Field(0, ge=0)


# ---------------------------------------------------------------------------
# 练琴计划（见 app/study.py）：study.jsonl 的一行、data/study.json
# ---------------------------------------------------------------------------
MAX_STUCK_BARS = 500  # 一次自评最多记几个卡住的小节
MAX_BAR_ID = 64


class StudyRecord(_Rec):
    """练完一遍的自评（study.jsonl 一行）。day 是练琴日（凌晨 4 点换日）。"""

    at: Stamp
    day: Day
    rating: int = Field(ge=1, le=4)  # 1 卡住 / 2 磕绊 / 3 顺 / 4 轻松（vocab.study_ratings）
    issues: list[str] = Field(default_factory=list)  # vocab.study_issues 的代码
    note: str = Field("", max_length=500)
    bpm: int = Field(0, ge=0)
    # 这一遍卡在哪几个小节（小节 id，用户在打分时点的）；之后重新识别掉的 id 读的时候不算（见 study.view）
    stuck_bars: list[str] = Field(default_factory=list)

    @field_validator("stuck_bars", mode="before")
    @classmethod
    def _bar_ids(cls, v: object) -> list[str]:
        """只留像小节 id 的字符串（去重、保持顺序、最多 MAX_STUCK_BARS 个）：外来 / 手改的记录照样能读。"""
        if v is None:
            return []
        if not isinstance(v, list):
            raise ValueError("stuck_bars 要是列表")  # noqa: TRY004 - pydantic 只把 ValueError 当成校验错误
        ids = (x for x in v if isinstance(x, str) and 0 < len(x) <= MAX_BAR_ID)
        return list(dict.fromkeys(ids))[:MAX_STUCK_BARS]

    @field_validator("issues", mode="before")
    @classmethod
    def _known_issues(cls, v: object) -> list[str]:
        """只留认识的代码（去重、保持顺序）：以后词表删掉某一项时，旧记录照样能读。"""
        if not isinstance(v, list):
            raise ValueError("issues 要是列表")  # noqa: TRY004 - pydantic 只把 ValueError 当成校验错误
        return list(dict.fromkeys(x for x in v if isinstance(x, str) and x in STUDY_ISSUES))


# 练琴设置：字段 → (最小, 最大, 默认)。接口的范围（schemas.StudySettingsIn）和读文件时的检查都用这一份
STUDY_SETTINGS = {"goal_minutes": (5, 600, 45), "max_learning": (1, 10, 2), "max_reviews": (0, 50, 5)}
MAX_SESSION_MINUTES = 600


def _check_segments(v: list[list[str | None]]) -> list[list[str | None]]:
    """[[开始, 结束 | None], ...]：时间格式对；只有最后一段可以还没结束。"""
    for i, seg in enumerate(v):
        if len(seg) != 2 or seg[0] is None:
            raise ValueError("计时段要是 [开始, 结束]")
        for x in seg:
            if x is not None:
                _real_stamp(x)
        if seg[1] is None and i != len(v) - 1:
            raise ValueError("只有最后一段可以没结束")
    return v


Segments = Annotated[list[list[str | None]], AfterValidator(_check_segments)]


class StudyTimer(_Rec):
    """正在计时（或暂停中）的秒表：一段一段的 [开始, 结束]，最后一段结束是 None = 正在走。"""

    segments: Segments = Field(min_length=1)


class StudySession(_Rec):
    """记下的一次练琴：秒表停下时（timer）或手动补记（manual）。"""

    id: str = Field(pattern=r"^[0-9a-f]{8}$")
    kind: Literal["timer", "manual"]
    day: Day
    segments: Segments = Field(default_factory=list)  # 只有 timer 有，都已结束
    # 一次最多 MAX_SESSION_MINUTES（和接口的上限一样）：秒表忘了停记下的更长的旧记录读的时候按上限算，不丢
    minutes: float = Field(ge=0, le=MAX_SESSION_MINUTES)
    note: str = Field("", max_length=500)

    @field_validator("minutes", mode="before")
    @classmethod
    def _cap_minutes(cls, v: object) -> object:
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > MAX_SESSION_MINUTES:
            return MAX_SESSION_MINUTES
        return v


def now() -> str:
    """只给人看的时间（新建 / 导入、问答、上次精修）"YYYY-MM-DD HH:MM"，取自 app.clock（测试可以换）。"""
    return clock.minute_stamp()


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
        "meta_rev": 0,
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
    old = dict(s.get("usage") or {})
    if "cost_usd" not in old and (old.get("input_tokens") or old.get("output_tokens")):
        # 早期曲子：之前的 token 没有对应的钱数。不记下来的话，这次的 cost_usd 会被当成全部花费
        old["legacy_input_tokens"] = old.get("input_tokens", 0)
        old["legacy_output_tokens"] = old.get("output_tokens", 0)
    s["usage"] = {k: old.get(k, 0) + usage.get(k, 0) - (before or {}).get(k, 0) for k in USAGE_KEYS}
    s["usage"].update({k: old[k] for k in LEGACY_USAGE_KEYS if old.get(k)})


# ---------------------------------------------------------------------------
# 派生字段
# ---------------------------------------------------------------------------
def derive(song: dict) -> dict:
    """补上不存盘的派生字段（原地修改并返回）。见模块说明。"""
    meta = song.get("meta")
    if isinstance(meta, dict) and meta.get("tuning"):
        meta["tuning_midi"] = tuning_midi(meta["tuning"])
        # 没起名字时先用谱头读出来的（保存时采用，见 strip）
        meta["title"] = song.get("title") or str(meta.get("title") or "")
    for i, b in enumerate(song.get("bars") or []):
        b["n"] = i + 1
        if isinstance(meta, dict) and meta.get("time_signature"):
            derive_warns(b, meta)
    plan = song.get("plan")
    if isinstance(plan, dict):
        plan["stale"] = bool(plan.get("stale_all") or plan.get("changed_bars"))
    return song


# ---------------------------------------------------------------------------
# 按对象的版本号（乐观锁）
# ---------------------------------------------------------------------------
def _meta_key(song: dict) -> str:
    meta = song.get("meta") if isinstance(song.get("meta"), dict) else {}
    title = song.get("title") or meta.get("title") or ""
    return json.dumps([title, *(meta.get(k) for k in EDITABLE_META[1:])], sort_keys=True, default=str)


def _events_key(bar: dict) -> str:
    return events_key(bar.get("events"))  # 按内容比：空的 chord / lyrics 之类写没写不算变


def notes_key(bar: dict) -> str:
    """小节音符的指纹（用户确认时记下，见 Bar.reviewed_key）：按内容算，写法不同但音一样的算一样。"""
    return hashlib.sha1(_events_key(bar).encode()).hexdigest()[:16]


def content_snapshot(song: dict) -> tuple[str, dict[str, tuple[int, str]]]:
    """改之前的谱头和各小节内容（按 id），给 bump_revs 比较。"""
    bars = {
        b["id"]: (int(b.get("rev") or 0), _events_key(b))
        for b in song.get("bars") or []
        if isinstance(b, dict) and isinstance(b.get("id"), str)
    }
    return _meta_key(song), bars


def touch_bar(bar: dict) -> None:
    """小节内容变了但音符没变（用户改了提示、换调弦清掉了提示）：手动 +1。同一次保存里不会重复加。"""
    bar["rev"] = int(bar.get("rev") or 0) + 1


def bump_revs(song: dict, before: tuple[str, dict[str, tuple[int, str]]]) -> None:
    """和 content_snapshot 的结果比：音符变了的小节 rev +1（重新识别换上的新 dict 沿用原来的 rev 再看内容），
    谱头可改字段变了 meta_rev +1。"""
    meta_key, bars = before
    if _meta_key(song) != meta_key:
        song["meta_rev"] = int(song.get("meta_rev") or 0) + 1
    for b in song.get("bars") or []:
        prev = bars.get(b.get("id")) if isinstance(b, dict) else None
        if prev is None:
            continue
        old_rev, old_events = prev
        b["rev"] = max(int(b.get("rev") or 0), old_rev + (_events_key(b) != old_events))


# 只在接口返回里出现（app/views.py 算出来），不存盘
VIEW_ONLY = frozenset(
    {
        "practice",
        "busy",
        "recognizing",
        "can_edit_bars",
        "editable_meta",
        "is_score",
        "failed_rows",
        "refine_estimate",
        "refine_scopes",
    }
)  # 早期格式的 running / error / job / kind 由 migrate 在升级时去掉


def strip(song: dict) -> dict:
    """要写进 song.json 的内容：去掉派生字段和以 _ 开头的临时字段（小节上的 _orig 是数据，保留）。
    曲名只存一份：song.title 为空而 meta 里读出了曲名（第一次识别读到谱头）时，用读出来的。"""
    meta = song.get("meta")
    if not song.get("title") and isinstance(meta, dict) and meta.get("title"):
        song["title"] = str(meta["title"]).strip()
    out: dict[str, Any] = {k: v for k, v in song.items() if not k.startswith("_") and k not in VIEW_ONLY}
    if isinstance(meta, dict):
        out["meta"] = {k: v for k, v in meta.items() if k not in ("tuning_midi", "title")}
    bars = []
    for b in song.get("bars") or []:
        nb = {k: v for k, v in b.items() if k != "n" and not (k.startswith("_") and k != "_orig")}
        if "warn" in nb:
            nb["warn"] = [
                w for w in nb["warn"] if not (isinstance(w, dict) and w.get("code") in DERIVED_WARNS)
            ]
        bars.append(nb)
    out["bars"] = bars
    if isinstance(song.get("plan"), dict):
        out["plan"] = {k: v for k, v in song["plan"].items() if k != "stale"}
    return copy.deepcopy(out)


# 接口返回的形状搬到了 app/viewmodels.py；model.SongView 之类的旧写法照样能用（用到时才导入，不形成循环）
_VIEW_MODELS = frozenset(
    {
        "RefineEstimate",
        "RefineScopes",
        "PracticeBar",
        "PracticeLast",
        "PracticeView",
        "PracticeTime",
        "StudyState",
        "StudyPreview",
        "StudyLast",
        "StudyView",
        "StuckBar",
        "BarView",
        "SongView",
        "LibraryPractice",
        "LibraryItem",
    }
)


def __getattr__(name: str):
    if name in _VIEW_MODELS:
        from app import viewmodels

        return getattr(viewmodels, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
