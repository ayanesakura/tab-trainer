"""AI 讲解：补写小节提示并重新生成练习建议（后台任务），以及针对某个小节的问答。只发文字（问答带小节图），费用很低。"""

from __future__ import annotations

import logging
import time

from app import jobs, staleness, store
from app.errors import Invalid, Upstream
from app.jobs import Cancelled
from app.llm import LLMError, request_id_of
from app.songs import find_bar

log = logging.getLogger("tab_trainer.coach")

EXPLAIN_CHUNK = 16  # 一次请求讲解几个小节
QA_KEEP = 20  # 每个小节保留最近几条问答


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
    jobs.check_llm_ready()
    cancel = jobs.claim_or_busy(sid, "这首曲子正在处理中")
    chunks = [todo[k : k + EXPLAIN_CHUNK] for k in range(0, len(todo), EXPLAIN_CHUNK)]
    prev = {
        "status": song.get("status", "ready"),
        "error": song.get("error"),
    }  # 结束后恢复（比如“已取消”的识别）

    def begin(s):
        s.update(status="parsing", job="explain", job_prev=prev)
        s["progress"] = {"stage": "生成讲解", "done": 0, "total": len(chunks)}

    store.update(sid, begin)

    def end(error=None):
        def apply(s):
            s.pop("job", None)
            p = s.pop("job_prev", None) or prev
            s.update(status=p["status"], progress=None, error=error or p["error"])

        store.update(sid, apply)

    def run():
        try:
            _explain(sid, chunks, cancel)
            end()
        except Cancelled:
            end("讲解生成已取消，已完成的部分会保留。")
        except Exception as e:
            rid = request_id_of(e)
            log.exception("request_id=%s song=%s explain failed", rid, sid)
            end("生成讲解失败：" + jobs.friendly(e))

    jobs.spawn(sid, run)


def _explain(sid: str, chunks: list[list[str]], cancel) -> None:
    claude = jobs.new_claude()
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
                    b["tip"] = tips[b["n"]]
                    b.pop("_orig", None)  # 提示是按现在的音新写的
            s["progress"] = {"stage": "生成讲解", "done": k + 1, "total": len(chunks)}
            jobs.add_usage(s, claude.usage, before)

        store.update(sid, apply)

    cur = store.load(sid)
    seen = {b["id"]: b["events"] for b in cur["bars"]}
    before = dict(claude.usage)
    plan = claude.practice_plan(cur["meta"], cur["bars"])

    def finish(s, meta=cur["meta"]):
        staleness.store_plan(s, plan, seen, meta)
        jobs.add_usage(s, claude.usage, before)

    store.update(sid, finish)


# ---------------------------------------------------------------------------
# 小节问答
# ---------------------------------------------------------------------------
def practice_summary(song: dict, bar_id: str) -> str:
    """给 AI 看的练习记录（中文一句话）；没练过返回空串。"""
    p = ((song.get("practice") or {}).get("bars") or {}).get(bar_id)
    if not p:
        return ""
    mins = round(p.get("seconds", 0) / 60)
    return f"已循环 {p.get('loops', 0)} 遍，累计约 {mins} 分钟，最快练到 {p.get('best_bpm', 0)} BPM，上次练习 {p.get('last', '')}"


def ask_bar(sid: str, bar_id: str, question: str) -> dict:
    """问一个小节的练习问题，回答存进这个小节的 qa 里。返回 {"answer", "qa"}。"""
    question = (question or "").strip()
    if not question:
        raise Invalid("问题不能为空")
    if len(question) > 500:
        raise Invalid("问题太长了（最多 500 字）")
    song = store.load(sid)
    bar = find_bar(song, bar_id)
    idx = song["bars"].index(bar)
    neighbours = [song["bars"][j] for j in (idx - 1, idx + 1) if 0 <= j < len(song["bars"])]
    image = None
    if bar.get("region") and bar.get("page") is not None:
        x0, x1, y0, y1 = bar["region"]
        image = store.page_image(sid, song["pages"][bar["page"]]).crop((x0, y0, x1, y1))
    try:
        claude = jobs.new_claude()
    except LLMError as e:
        raise Invalid(str(e)) from None  # 没配 key：用户能自己解决的问题，返回 400
    try:
        answer = claude.coach(
            song["meta"],
            bar,
            neighbours,
            question,
            [q for q in bar.get("qa") or [] if not q.get("stale")],  # 改谱之前的问答说的是旧音，不再当上下文
            practice_summary(song, bar_id),
            image,
        )
    except Exception as e:  # noqa: BLE001 - 网络 / API 错误统一转成中文说明
        raise Upstream(jobs.friendly(e)) from None
    if not answer.strip():
        raise Upstream("模型没有给出回答，换个问法再试一次")
    entry = {"q": question, "a": answer.strip(), "at": time.strftime("%Y-%m-%d %H:%M")}

    def apply(s):
        b = next((x for x in s["bars"] if x.get("id") == bar_id), None)
        if b is not None and b["events"] != bar["events"]:
            entry["stale"] = True  # 等回答期间这个小节改过音：回答说的是改之前的谱
        if b is not None:  # 等回答期间小节被重新识别掉了，就不存
            b["qa"] = [*(b.get("qa") or []), entry][-QA_KEEP:]
        jobs.add_usage(s, claude.usage)

    s = store.update(sid, apply)
    b = next((x for x in s["bars"] if x.get("id") == bar_id), None)
    return {"answer": entry["a"], "qa": (b or {}).get("qa", [entry])}
