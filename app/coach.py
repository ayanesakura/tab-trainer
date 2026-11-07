"""AI 讲解：补写小节提示并重新生成练习建议（后台任务），以及针对某个小节的问答。只发文字（问答带小节图），费用很低。"""

from __future__ import annotations

import logging

from app import config, jobs, practice, staleness, state, store
from app.config import settings
from app.errors import AppError, Invalid, Upstream
from app.jobs import Cancelled
from app.llm import friendly, request_id_of
from app.model import add_usage, now
from app.songs import find_bar

log = logging.getLogger("tab_trainer.coach")


# ---------------------------------------------------------------------------
# 补写讲解
# ---------------------------------------------------------------------------
def start_explain(sid: str) -> None:
    """补写缺失的小节提示，再重新生成整曲练习建议。
    GP / MusicXML 导入的曲子一开始都没有提示；识别出来的曲子改过音、改过调弦之后，对应的提示会被清掉。"""
    song = store.load(sid)
    if not song.get("bars"):
        raise Invalid("曲子还没有小节数据，请先打开练习页让它加载一次")
    todo = [b["id"] for b in song["bars"] if not (b.get("tip") or "").strip()]
    plan = song.get("plan")
    if not todo and plan and not plan.get("stale"):
        raise Invalid("每个小节都已经有提示，练习建议也是最新的")
    config.check_llm_ready()
    cancel = jobs.claim_or_busy(sid, "这首曲子正在处理中")
    size = settings.explain_chunk
    chunks = [todo[k : k + size] for k in range(0, len(todo), size)]

    # 进度记在 activity / progress 里，不动 status：讲解期间曲子照常能练，曲库也不会显示成“识别中”
    def begin(s):
        state.begin(s, "explain", progress={"stage": "生成讲解", "done": 0, "total": len(chunks)})

    try:
        store.update(sid, begin)
    except Exception:
        jobs.jobs.release(sid)
        raise

    def end(notice=None):
        store.update(sid, lambda s: state.finish(s, notice=notice, source="explain"))

    def run():
        try:
            _explain(sid, chunks, cancel)
            end()
        except Cancelled:
            end(("warn", "讲解生成已取消，已完成的部分会保留。"))
        except Exception as e:
            rid = request_id_of(e)
            log.exception("request_id=%s song=%s explain failed", rid, sid)
            end(("error", "生成讲解失败：" + friendly(e)))

    jobs.spawn(sid, run)


def _explain(sid: str, chunks: list[list[str]], cancel) -> None:
    claude = config.new_llm()
    for k, ids in enumerate(chunks):
        if cancel.is_set():
            raise Cancelled()
        cur = store.load(sid)
        chunk = [b for b in cur["bars"] if b.get("id") in set(ids)]
        if not chunk:
            continue
        seen = {b["id"]: b["events"] for b in chunk}  # 写回时小节已经被改过的，就不写旧内容的提示
        before = dict(claude.usage)
        tips = {t["number"]: t["tip"] for t in claude.explain_bars(cur["meta"], chunk).get("bars", [])}

        def apply(s, tips=tips, k=k, seen=seen, before=before, meta=cur["meta"]):
            same_meta = s["meta"] == meta
            for b in s["bars"]:
                if (
                    same_meta
                    and seen.get(b.get("id")) == b["events"]
                    and b["n"] in tips
                    and not (b.get("tip") or "").strip()
                ):
                    b["tip"] = tips[b["n"]]  # 按现在的音新写的（改过的小节撤销时仍恢复识别时的提示）
            s["progress"] = {"stage": "生成讲解", "done": k + 1, "total": len(chunks)}
            add_usage(s, claude.usage, before)

        store.update(sid, apply)

    cur = store.load(sid)
    seen = {b["id"]: b["events"] for b in cur["bars"]}
    before = dict(claude.usage)
    plan = claude.practice_plan(cur["meta"], cur["bars"])

    def finish(s, meta=cur["meta"]):
        staleness.store_plan(s, plan, seen, meta)
        add_usage(s, claude.usage, before)

    store.update(sid, finish)


# ---------------------------------------------------------------------------
# 小节问答
# ---------------------------------------------------------------------------
def ask_bar(sid: str, bar_id: str, question: str) -> dict:
    """问一个小节的练习问题（schemas.AskIn 已经去掉首尾空白、检查过长度），回答存进这个小节的 qa 里。
    返回 {"answer", "qa"}。"""
    song = store.load(sid)
    bar = find_bar(song, bar_id)
    idx = song["bars"].index(bar)
    neighbours = [song["bars"][j] for j in (idx - 1, idx + 1) if 0 <= j < len(song["bars"])]
    image = None
    if bar.get("region") and bar.get("page") is not None:
        x0, x1, y0, y1 = bar["region"]
        image = store.page_image(sid, song["pages"][bar["page"]]).crop((x0, y0, x1, y1))
    claude = config.new_llm()  # 没配 key：MissingKey，400
    try:
        answer = claude.coach(
            song["meta"],
            bar,
            neighbours,
            question,
            [q for q in bar.get("qa") or [] if not q.get("stale")],  # 改谱之前的问答说的是旧音，不再当上下文
            practice.coach_text(sid, song, bar_id),
            image,
        )
    except AppError:
        raise
    except Exception as e:  # noqa: BLE001 - 网络 / API 错误统一转成中文说明
        raise Upstream(friendly(e)) from None
    if not answer.strip():
        raise Upstream("模型没有给出回答，换个问法再试一次")
    entry = {"q": question, "a": answer.strip(), "at": now()}

    def apply(s):
        b = next((x for x in s["bars"] if x.get("id") == bar_id), None)
        if b is not None and b["events"] != bar["events"]:
            entry["stale"] = True  # 等回答期间这个小节改过音：回答说的是改之前的谱
        if b is not None:  # 等回答期间小节被重新识别掉了，就不存
            b["qa"] = [*(b.get("qa") or []), entry][-settings.qa_keep :]
        add_usage(s, claude.usage)

    s = store.update(sid, apply)
    b = next((x for x in s["bars"] if x.get("id") == bar_id), None)
    return {"answer": entry["a"], "qa": (b or {}).get("qa", [entry])}
