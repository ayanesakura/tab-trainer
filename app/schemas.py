"""接口的请求体：类型、长度和数值范围只在这里检查（格式不对时返回 400 和中文说明，不会在深处变成 500）。
内容层面的校验（音名、时值能不能解析等）在 music / songs 里做。小节事件的结构用 app/model.py 的 Event。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.model import Event
from app.music import STRINGS

MAX_TAGS = 12
MAX_TAG_LEN = 20


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")  # 前端多带的字段忽略，不报错


class SettingsIn(_In):
    api_key: str | None = Field(None, max_length=500)
    clear_api_key: bool = False
    base_url: str | None = Field(None, max_length=500)
    model: str | None = Field(None, max_length=200)
    effort: Literal["", "low", "medium", "high", "xhigh", "max"] | None = None


class DerivedInfo(_In):
    version: int | None = None
    alphatab: str = Field("", max_length=50)


class DerivedIn(_In):
    meta: dict = Field(default_factory=dict)
    bars: list[dict] = Field(default_factory=list, max_length=5000)
    track: int = Field(0, ge=0, le=200)
    derived: DerivedInfo | None = None


class ImportErrorIn(_In):
    error: str = Field("", max_length=5000)


class MetaIn(_In):
    title: str | None = Field(None, max_length=200)
    artist: str | None = Field(None, max_length=200)
    tuning: list[str] | None = Field(None, max_length=STRINGS)
    capo: int | None = Field(None, ge=0, le=12)
    time_signature: list[int] | None = Field(None, max_length=2)
    rev: int | None = None  # 带上时和曲子现在的 rev 不一致 → 409（别处改过）


class BarIn(_In):
    events: list[Event] = Field(max_length=500)
    tip: str | None = Field(None, max_length=5000)
    rev: int | None = None


class AskIn(_In):
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


class TagsIn(_In):
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


class ReparseIn(_In):
    hint: str = Field("", max_length=1000)


FIELD_NAMES = {
    "api_key": "API key",
    "base_url": "Base URL",
    "model": "模型",
    "effort": "识别精细度",
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
    "track": "音轨",
}


def describe(errors: list[dict]) -> str:
    """把 Pydantic 的校验错误变成一句中文。"""
    if not errors:
        return "请求格式不对"
    e = errors[0]
    loc = [str(x) for x in e.get("loc", []) if x not in ("body",)]
    field = FIELD_NAMES.get(loc[0], loc[0]) if loc else ""
    if len(loc) > 1 and loc[0] in ("events", "bars") and loc[1].isdigit():
        field += f"第 {int(loc[1]) + 1} 个"
    kind = e.get("type", "")
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
    elif kind == "value_error":
        why = str((e.get("ctx") or {}).get("error") or e.get("msg", "格式不对"))
    else:
        why = e.get("msg", "格式不对")
    return f"请求格式不对：{field}{'：' if field else ''}{why}"
