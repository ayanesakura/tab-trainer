"""接口背后的编排：main.py 只做 HTTP（参数、文件、响应），这里把 songs / recognize / coach / bundles 串起来。

错误一律抛 app.errors 里的 AppError（main 统一转成 {"detail": 中文说明} 和对应的状态码）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import BinaryIO

from app import config, practice, recognize, songs, store, views
from app.config import settings
from app.errors import Busy, Conflict, Invalid, NotFound
from app.jobs import jobs
from app.model import is_score


# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------
def read_uploads(files: list[tuple[str, int | None, BinaryIO]]) -> list[tuple[str, bytes]]:
    """(文件名, 声明的大小, 文件对象) → (文件名, 内容)。超过上限的文件不整个读进内存。"""
    if len(files) > settings.max_files:
        raise Invalid(f"一次最多上传 {settings.max_files} 个文件")
    out = []
    for name, size, f in files:
        if size is not None and size > settings.max_file_bytes:
            raise Invalid(f"“{name}”太大了（单个文件最大 {settings.max_file_bytes // 1024 // 1024} MB）")
        out.append((name, f.read(settings.max_file_bytes + 1)))
    return out


def upload(uploads: list[tuple[str, bytes]], title: str) -> str:
    """新建曲子；图片 / PDF 曲子马上开始识别。"""
    sid = songs.create_song(uploads, title)
    if not is_score(store.load(sid)):
        recognize.start_parse(sid)
    return sid


def arrange_pages(sid: str, order: str, uploads: list[tuple[str, bytes]]) -> dict:
    """补页、删页、调整页序（order 是 JSON 数组：已有页的文件名或 "new:k"）。新页上的行在后台识别。"""
    store.load(sid)
    try:
        order_list = json.loads(order)
    except ValueError:
        raise Invalid("页序格式不对") from None
    if not isinstance(order_list, list) or not all(isinstance(x, str) for x in order_list):
        raise Invalid("页序格式不对")
    recognize.arrange_pages(sid, order_list, uploads)
    return song(sid)


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------
def song(sid: str) -> dict:
    return views.song_view(store.load(sid))


def library() -> list[dict]:
    return views.list_library()


def practice_history(sid: str) -> dict:
    return {"sessions": practice.history(sid, store.load(sid))}


def source_file(sid: str) -> Path:
    s = store.load(sid)
    if not s.get("source"):
        raise NotFound("这首曲子没有原文件")
    return store.song_dir(sid) / s["source"]


def page_file(sid: str, name: str) -> Path:
    store.load(sid)
    p = store.song_dir(sid) / "pages" / name
    if "/" in name or "\\" in name or name.startswith(".") or not p.is_file():
        raise NotFound("页面不存在")
    return p


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------
def reparse_all(sid: str) -> None:
    if is_score(store.load(sid)):
        raise Invalid("Guitar Pro / MusicXML 曲子不需要识别")
    config.check_llm_ready()
    if not recognize.start_parse(sid):
        raise Busy("已经在识别中")


def reparse_row(sid: str, si: int, hint: str) -> None:
    s = store.load(sid)
    if not 0 <= si < len(s.get("systems") or []):
        raise NotFound("这一行不存在")
    recognize.start_reparse(sid, si, hint.strip())


def cancel(sid: str) -> None:
    store.load(sid)
    if not jobs.cancel(sid):
        raise Conflict("没有正在进行的识别")


def delete(sid: str) -> None:
    """删除曲子（数据坏了的也能删）。有任务在跑时不删。"""
    if jobs.running(sid):
        raise Busy("正在识别中，完成后再删除")
    store.delete(sid)


# ---------------------------------------------------------------------------
# 写（返回改完的曲子视图）
# ---------------------------------------------------------------------------
def edit_bar(sid: str, bar_id: str, events: list[dict], tip: str | None, rev: int | None) -> dict:
    return views.song_view(songs.edit_bar(sid, bar_id, events, tip, rev))


def edit_meta(sid: str, fields: dict, rev: int | None) -> dict:
    return views.song_view(songs.edit_meta(sid, fields, rev))


def set_derived(sid: str, meta: dict, bars: list[dict], track: int, derived: dict | None) -> dict:
    return views.song_view(songs.set_derived(sid, meta, bars, track, derived))


def import_failed(sid: str, error: str) -> dict:
    return views.song_view(songs.import_failed(sid, error))


def record_practice(sid: str, bar_ids: list[str], loops: int, bpm: float, seconds: float) -> dict:
    return practice.record(sid, bar_ids, loops, bpm, seconds)
