"""接口背后的编排：main.py 只做 HTTP（参数、文件、响应），所有路由都经过这里，再分给 songs / recognize / pages /
coach / bundles / study。多数函数只是一行转发，留着是为了让 main 只依赖这一个模块、返回的视图形状统一在这里决定。

错误一律抛 app.errors 里的 AppError（main 统一转成 {"detail": 中文说明} 和对应的状态码）。
"""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

from app import bundles, coach, config, pages, practice, recognize, refine, songs, store, study, views
from app.config import settings
from app.errors import Busy, Conflict, Invalid, NotFound
from app.jobs import jobs
from app.model import is_score


def startup() -> None:
    songs.recover_interrupted()


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
    if not is_score(store.read(sid)):
        recognize.start_parse(sid)
    return sid


def arrange_pages(sid: str, order: list[str], uploads: list[tuple[str, bytes]]) -> dict:
    """补页、删页、调整页序（order：已有页的文件名或 "new:k"）。新页上的行在后台识别。"""
    store.read(sid)  # 曲子不存在：404
    pages.arrange_pages(sid, order, uploads)
    return song(sid)


def export_bundle(ids: list[str]) -> tuple[Path, str]:
    return bundles.export_bundle(ids)


def import_bundles(files: list[tuple[str, BinaryIO]]) -> dict:
    return bundles.import_bundles(files)


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------
def song(sid: str) -> dict:
    return views.song_view(store.read(sid))


def library() -> list[dict]:
    return views.list_library()


def practice_history(sid: str) -> dict:
    return {"sessions": practice.history(sid, store.read(sid))}


def source_file(sid: str) -> Path:
    """GP / MusicXML 原文件（练习页取来交给 alphaTab）。文件名按 model.SOURCE_FILE 检查（store.source_path）。"""
    p = store.source_path(sid, store.read(sid).get("source"))
    if not p.is_file():
        raise NotFound("这首曲子没有原文件")
    return p


def page_file(sid: str, name: str) -> Path:
    """曲子的一页原图。只认 pN.png（store.page_path），所以接口总是按 image/png 返回。"""
    p = store.page_path(sid, name)
    if not p.is_file():
        raise NotFound("页面不存在")
    return p


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------
def reparse_all(sid: str) -> None:
    if is_score(store.read(sid)):
        raise Invalid("Guitar Pro / MusicXML 曲子不需要识别")
    config.check_llm_ready()
    if not recognize.start_parse(sid):
        raise Busy("已经在识别中")


def retry_failed(sid: str) -> None:
    recognize.start_retry_failed(sid)


def explain(sid: str) -> None:
    coach.start_explain(sid)


def refine_song(sid: str, bars: list[str] | None = None, system: int | None = None, hint: str = "") -> None:
    refine.start_refine(sid, bars, system, hint)


def reparse_row(sid: str, si: int, hint: str) -> None:
    s = store.read(sid)
    if not 0 <= si < len(s.get("systems") or []):
        raise NotFound("这一行不存在")
    recognize.start_reparse(sid, si, hint.strip())


def cancel(sid: str) -> None:
    store.read(sid)  # 曲子不存在：404
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


def review_bar(sid: str, bar_id: str, ok: bool, rev: int | None) -> dict:
    return views.song_view(songs.review_bar(sid, bar_id, ok, rev))


def undo_refine(sid: str, bar_id: str, rev: int | None) -> dict:
    return views.song_view(refine.undo(sid, bar_id, rev))


def edit_meta(sid: str, fields: dict, rev: int | None) -> dict:
    return views.song_view(songs.edit_meta(sid, fields, rev))


def set_derived(sid: str, meta: dict, bars: list[dict], track: int, derived: dict | None) -> dict:
    return views.song_view(songs.set_derived(sid, meta, bars, track, derived))


def import_failed(sid: str, error: str) -> dict:
    return views.song_view(songs.import_failed(sid, error))


def set_tags(sid: str, tags: list[str]) -> dict:
    return {"tags": songs.set_tags(sid, tags)}


def ask_bar(sid: str, bar_id: str, question: str) -> dict:
    return coach.ask_bar(sid, bar_id, question)


def prepare_ask(sid: str, bar_id: str, question: str):
    """流式问答：先检查（404 / 400 在这里抛），返回 run(on_delta) → {"answer", "qa"}（见 coach.prepare_ask）。"""
    return coach.prepare_ask(sid, bar_id, question)


def record_practice(sid: str, bar_ids: list[str], loops: int, bpm: float, seconds: float) -> dict:
    return practice.record(sid, bar_ids, loops, bpm, seconds)


# ---------------------------------------------------------------------------
# 练琴计划（app/study.py）
# ---------------------------------------------------------------------------
def set_study(sid: str, in_plan: bool | None, paused: bool | None) -> dict:
    return study.set_plan(sid, in_plan, paused)


def study_order(ids: list[str]) -> None:
    study.reorder(ids)


def rate_song(sid: str, rating: int, issues: list[str], note: str, bpm: int | None) -> dict:
    return study.rate(sid, rating, issues, note, bpm or 0)


def study_today() -> dict:
    return study.today()


def study_timer() -> dict:
    return study.timer()


def study_timer_action(action: str, minutes: float | None) -> dict:
    return study.timer_action(action, minutes)


def add_study_session(day: str, minutes: float, note: str) -> dict:
    return study.add_session(day, minutes, note)


def delete_study_session(session_id: str) -> None:
    study.delete_session(session_id)


def study_settings(fields: dict) -> dict:
    return study.set_settings(fields)


def study_calendar(days: int) -> dict:
    return study.calendar(days)


def study_day(day: str) -> dict:
    return study.day_detail(day)
