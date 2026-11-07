"""接口的请求体：类型和长度在这里先挡一道，格式不对时返回 400（中文说明），不会在深处变成 500。
内容层面的校验（音名、时值、弦品范围等）仍在 music / songs 里做。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
    tuning: list[str] | None = Field(None, max_length=6)
    capo: int | None = None
    time_signature: list[int] | None = Field(None, max_length=2)


class BarIn(_In):
    events: list[dict] = Field(max_length=500)
    tip: str | None = Field(None, max_length=5000)


class AskIn(_In):
    question: str = Field("", max_length=1000)


class PracticeIn(_In):
    bar_ids: list[str] = Field(max_length=5000)
    loops: int = 0
    bpm: float = 0  # 前端的速度可能带小数，record_practice 里取整
    seconds: float = 0


class TagsIn(_In):
    tags: list[str] = Field(max_length=50)


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
    kind = e.get("type", "")
    if kind == "missing":
        why = "缺少这一项"
    elif kind in ("string_too_long", "too_long"):
        why = "太长了"
    elif kind == "literal_error":
        why = "取值不在允许的范围里"
    elif kind.endswith(("_type", "_parsing")):
        why = "类型不对"
    else:
        why = e.get("msg", "格式不对")
    return f"请求格式不对：{field}{'：' if field else ''}{why}"
