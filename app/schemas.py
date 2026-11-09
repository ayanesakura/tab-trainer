"""接口的请求体：类型、长度和数值范围只在这里检查（格式不对时返回 400 和中文说明，不会在深处变成 500）。
内容层面的校验（音名、时值能不能解析等）在 music / songs 里做。小节事件的结构用 app/model.py 的 Event。"""

from __future__ import annotations

import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.model import DAY, MAX_SESSION_MINUTES, STUDY_SETTINGS, Event
from app.music import STRINGS, STUDY_ISSUES

MAX_TAGS = 12
MAX_TAG_LEN = 20


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")  # 前端多带的字段忽略，不报错


class _Strict(_In):
    """不认识的字段报 400：写错了键名（{"bar": [...]} 少了 s）不能被当成没写——精修 / 重新识别会变成整首（要花钱），
    改谱头、改小节会悄悄什么都不改。前端发的都是固定的几个键；练习记录（关页面时发出、出错没人看到）和
    GP 转换结果（字段跟着转换器走）仍然忽略多余的字段。"""

    model_config = ConfigDict(extra="forbid")


class SettingsIn(_Strict):
    api_key: str | None = Field(None, max_length=500)
    clear_api_key: bool = False
    base_url: str | None = Field(None, max_length=500)
    model: str | None = Field(None, max_length=200)
    effort: Literal["", "low", "medium", "high", "xhigh", "max"] | None = None
    double_check: Literal["off", "doubts", "all"] | None = None
    refine: Literal["manual", "auto"] | None = None


class DerivedInfo(_In):
    version: int | None = None
    alphatab: str = Field("", max_length=50)


class DerivedIn(_In):
    meta: dict = Field(default_factory=dict)
    bars: list[dict] = Field(default_factory=list, max_length=5000)
    track: int = Field(0, ge=0, le=200)
    derived: DerivedInfo | None = None


class ImportErrorIn(_Strict):
    error: str = Field("", max_length=5000)


class MetaIn(_Strict):
    title: str | None = Field(None, max_length=200)
    artist: str | None = Field(None, max_length=200)
    tuning: list[str] | None = Field(None, max_length=STRINGS)
    capo: int | None = Field(None, ge=0, le=12)
    time_signature: list[int] | None = Field(None, max_length=2)
    rev: int | None = None  # 曲子的 meta_rev：带上时和现在的不一致 → 409（别处改过谱头）


class BarIn(_Strict):
    events: list[Event] = Field(max_length=500)
    tip: str | None = Field(None, max_length=5000)
    rev: int | None = None  # 这个小节的 rev：带上时和现在的不一致 → 409（别处改过这个小节）


class AskIn(_Strict):
    question: str = Field(max_length=500)

    @field_validator("question")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("问题不能为空")
        return v


class PracticeIn(_In):
    bar_ids: list[str] = Field(min_length=1, max_length=5000)
    loops: int = Field(0, ge=0, le=10000)
    bpm: float = Field(gt=0, le=400)  # 前端的速度可能带小数，记录时取整
    seconds: float = Field(0, ge=0, le=6 * 3600)


class TagsIn(_Strict):
    tags: list[str] = Field(max_length=50)

    @field_validator("tags")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        """去掉首尾空格、空的和重复的（保持顺序）。"""
        clean = list(dict.fromkeys(t.strip() for t in v if t and t.strip()))
        if len(clean) > MAX_TAGS:
            raise ValueError(f"一首曲子最多 {MAX_TAGS} 个标签")
        if any(len(t) > MAX_TAG_LEN for t in clean):
            raise ValueError(f"标签最长 {MAX_TAG_LEN} 个字")
        return clean


class ReviewIn(_Strict):
    ok: bool = True
    rev: int | None = None


class RevIn(_Strict):
    rev: int | None = None  # 这个小节的 rev：带上时和现在的不一致 → 409


class ReparseIn(_Strict):
    hint: str = Field("", max_length=1000)


class RefineIn(_Strict):
    """精修的范围：都不给 = 整首；bars = 这几个小节（小节卡片上的“精修这个小节”）；system = 这一行（从 0 数）。
    hint：用户写的“哪里不对”，核对每个小节时告诉模型。"""

    bars: list[str] | None = Field(None, max_length=2000)
    system: int | None = Field(None, ge=0)
    hint: str = Field("", max_length=1000)


# ---------------------------------------------------------------------------
# 练琴计划（app/study.py）
# ---------------------------------------------------------------------------
class StudyIn(_Strict):
    """加入 / 移出练琴计划、暂停 / 继续：只改给了的。"""

    in_plan: bool | None = None
    paused: bool | None = None


class StudyOrderIn(_Strict):
    ids: list[str] = Field(max_length=5000)


class StudyRateIn(_Strict):
    rating: int = Field(ge=1, le=4)
    issues: list[str] = Field(default_factory=list, max_length=len(STUDY_ISSUES))
    note: str = Field("", max_length=500)
    bpm: int | None = Field(None, ge=0, le=1000)

    @field_validator("issues")
    @classmethod
    def _known(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("有重复的项")
        if bad := [x for x in v if x not in STUDY_ISSUES]:
            raise ValueError(f"不认识的问题：{bad[0][:20]}")
        return v


class StudyTimerIn(_Strict):
    action: Literal["start", "pause", "resume", "stop", "discard"]
    minutes: float | None = Field(
        None, gt=0, le=MAX_SESSION_MINUTES
    )  # 只有 stop 用：忘了停时改成实际练的分钟数


class StudySessionIn(_Strict):
    day: str = Field(pattern=DAY)
    minutes: float = Field(ge=1, le=MAX_SESSION_MINUTES)
    note: str = Field("", max_length=500)

    @field_validator("day")
    @classmethod
    def _real_day(cls, v: str) -> str:
        try:
            d = datetime.date.fromisoformat(v)
        except ValueError:
            raise ValueError("日期不存在") from None
        if d.year < 2000:
            raise ValueError("日期太早了")
        return v


def _setting(name: str):
    lo, hi, _ = STUDY_SETTINGS[name]
    return Field(None, ge=lo, le=hi)


class StudySettingsIn(_Strict):
    goal_minutes: int | None = _setting("goal_minutes")
    max_learning: int | None = _setting("max_learning")
    max_reviews: int | None = _setting("max_reviews")


def page_order(raw: str) -> list[str]:
    """补页 / 调页序的表单字段 order：JSON 数组，每项是已有页的文件名或 "new:k"（multipart 里没法直接用 JSON 请求体）。"""
    import json

    from app.errors import Invalid

    try:
        order = json.loads(raw)
    except ValueError:
        raise Invalid("页序格式不对") from None
    if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
        raise Invalid("页序格式不对")
    return order


FIELD_NAMES = {
    "api_key": "API key",
    "base_url": "Base URL",
    "model": "模型",
    "effort": "识别精细度",
    "double_check": "双重校对",
    "refine": "精修",
    "events": "小节内容（events）",
    "tip": "提示",
    "question": "问题",
    "bar_ids": "小节",
    "loops": "遍数",
    "bpm": "速度",
    "seconds": "时长",
    "hint": "说明",
    "title": "曲名",
    "tags": "标签",
    "tuning": "调弦",
    "capo": "变调夹",
    "time_signature": "拍号",
    "bars": "小节",
    "system": "行",
    "track": "音轨",
    "in_plan": "加入计划",
    "paused": "暂停",
    "ids": "曲子",
    "rating": "自评",
    "issues": "问题",
    "note": "备注",
    "action": "操作",
    "minutes": "分钟数",
    "day": "日期",
    "days": "天数",
    "goal_minutes": "每天目标",
    "max_learning": "同时学几首",
    "max_reviews": "每天复习几首",
}


def describe(errors: list[dict]) -> str:
    """把 Pydantic 的校验错误变成一句中文。"""
    if not errors:
        return "请求格式不对"
    e = errors[0]
    loc = [str(x) for x in e.get("loc", []) if x not in ("body", "query", "path")]
    field = FIELD_NAMES.get(loc[0], loc[0]) if loc else ""
    if len(loc) > 1 and loc[0] in ("events", "bars") and loc[1].isdigit():
        field += f"第 {int(loc[1]) + 1} 个"
    kind = e.get("type", "")
    if kind == "extra_forbidden":
        return f"请求格式不对：不认识“{'.'.join(loc)}”这一项（可能写错了）"
    if kind == "missing":
        why = "缺少这一项"
    elif kind in ("string_too_long", "too_long"):
        why = "太长了"
    elif kind == "literal_error":
        why = "取值不在允许的范围里"
    elif kind.endswith(("_type", "_parsing")):
        why = "类型不对"
    elif kind in ("greater_than", "greater_than_equal", "less_than", "less_than_equal"):
        why = "超出范围"
    elif kind == "too_short":
        why = "不能为空"
    elif kind == "string_pattern_mismatch":
        why = "格式不对"
    elif kind == "value_error":
        why = str((e.get("ctx") or {}).get("error") or e.get("msg", "格式不对"))
    else:
        why = e.get("msg", "格式不对")
    return f"请求格式不对：{field}{'：' if field else ''}{why}"
