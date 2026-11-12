"""接口背后的用例：main.py 只做 HTTP（路由、参数、文件、响应），每个路由调这里的一个函数，再由这里分给 songs / recognize /
pages / coach / chat / bundles / study / practice / config。业务规则（上传的上限和读文件、新建后要不要识别、页面 / 原文件能不能给、
返回什么形状的视图）都在这一层或更下层，main 不直接调下层模块。不少函数只是一行转发：留着让 main 只依赖这一个模块。

错误一律抛 app.errors 里的 AppError（main 统一转成 {"detail": 中文说明} 和对应的状态码）。
"""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

from app import bundles, chat, coach, config, pages, practice, recognize, refine, songs, store, study, views
from app.errors import Busy, Conflict, Invalid, NotFound
from app.jobs import jobs
from app.model import is_score

Upload = tuple[str, int | None, BinaryIO]  # 接口收到的文件：(文件名, 声明的大小, 文件对象)


def startup() -> None:
    songs.recover_interrupted()


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------
def settings_view() -> dict:
    """设置页显示的内容（key 打码；has_key：现在有没有能用的 key，练习页据此把 AI 按钮置灰）。"""
    return config.public_settings()


def save_settings(api_key: str | None, clear_api_key: bool, fields: dict) -> dict:
    return config.update_settings(api_key, clear_api_key, fields)


def test_settings() -> dict:
    return config.test_connection()


# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------
def upload(files: list[Upload], title: str) -> str:
    """新建曲子（上限见 songs.read_uploads）；图片 / PDF 曲子马上开始识别。"""
    sid = songs.create_song(songs.read_uploads(files), title)
    if not is_score(store.read(sid)):
        recognize.start_parse(sid)
    return sid


def arrange_pages(sid: str, order: list[str], files: list[Upload]) -> dict:
    """补页、删页、调整页序（order：已有页的文件名或 "new:k"）。新页上的行在后台识别。"""
    store.read(sid)  # 曲子不存在：404（先于读上传的文件）
    pages.arrange_pages(sid, order, songs.read_uploads(files))
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
    """删除曲子（数据坏了的也能删）。有任务在跑时不删（423）。删除期间占着任务登记：
    “查有没有任务”和“删”之间不会插进一个补页之类的任务（它会往删掉的目录里写）。"""
    if not store.valid_id(sid):
        raise NotFound()  # 不为乱七八糟的 id 登记任务
    if jobs.claim(sid) is None:
        raise Busy("正在识别中，完成后再删除")
    try:
        store.delete(sid)
    finally:
        jobs.release(sid)


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


def set_archived(sid: str, archived: bool) -> dict:
    return {"archived": songs.set_archived(sid, archived)}


def ask_bar(sid: str, bar_id: str, question: str) -> dict:
    return coach.ask_bar(sid, bar_id, question)


def prepare_ask(sid: str, bar_id: str, question: str):
    """流式问答：先检查（404 / 400 在这里抛），返回 run(on_delta) → {"answer", "qa"}（见 coach.prepare_ask）。"""
    return coach.prepare_ask(sid, bar_id, question)


def record_practice(
    sid: str,
    bar_ids: list[str],
    loops: int,
    bpm: int,
    seconds: float,
    kind: str = "play",
    clean: bool | None = None,
) -> dict:
    return practice.record(sid, bar_ids, loops, bpm, seconds, kind, clean)


# ---------------------------------------------------------------------------
# 首页的 AI 对话（app/chat.py）
# ---------------------------------------------------------------------------
def chat_view() -> dict:
    return chat.conversation()


def chat_new() -> dict:
    return chat.new_conversation()


def prepare_chat(question: str):
    """流式提问：先检查（没配 key 400 在这里抛），返回 run(on_delta) → {"answer", "id", "turns", "cost"}（见 chat.prepare_ask）。"""
    return chat.prepare_ask(question)


# ---------------------------------------------------------------------------
# 练琴计划（app/study.py）
# ---------------------------------------------------------------------------
def set_study(sid: str, in_plan: bool | None, paused: bool | None) -> dict:
    return study.set_plan(sid, in_plan, paused)


def study_order(ids: list[str]) -> None:
    study.reorder(ids)


def rate_song(
    sid: str, rating: int, issues: list[str], note: str, bpm: int | None, stuck_bars: list[str] | None = None
) -> dict:
    return study.rate(sid, rating, issues, note, bpm or 0, stuck_bars=stuck_bars)


def study_today() -> dict:
    return study.today()


def study_timer() -> dict:
    return study.timer()


def study_timer_action(action: str, minutes: float | None) -> dict:
    return study.timer_action(action, minutes)


def add_study_session(day: str, minutes: float, note: str) -> dict:
    return study.add_session(day, minutes, note)


def study_page_beat(session_id: str, song: str | None, end: bool) -> dict:
    return study.page_beat(session_id, song, end)


def study_page_minutes(session_id: str, minutes: float) -> dict:
    return study.set_page_minutes(session_id, minutes)


def delete_study_session(session_id: str) -> None:
    study.delete_session(session_id)


def study_settings(fields: dict) -> dict:
    return study.set_settings(fields)


def study_calendar(days: int) -> dict:
    return study.calendar(days)


def study_day(day: str) -> dict:
    return study.day_detail(day)
