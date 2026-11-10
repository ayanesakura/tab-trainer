"""AI 讲解：补写小节提示并重新生成练习建议（后台任务），以及针对某个小节的问答。只发文字（问答带小节图），费用很低。"""

from __future__ import annotations

import logging
from collections.abc import Callable

from app import config, jobs, practice, staleness, state, store
from app.config import settings
from app.errors import AppError, Invalid, Upstream
from app.jobs import Cancelled
from app.llm import friendly, request_id_of
from app.model import now
from app.music import same_events
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
    size = settings.explain_chunk
    chunks = [todo[k : k + size] for k in range(0, len(todo), size)]

    # 进度记在 activity / progress 里，不动 status：讲解期间曲子照常能练，曲库也不会显示成“识别中”
    def begin(s):
        state.begin(s, "explain", progress={"stage": "生成讲解", "done": 0, "total": len(chunks)})

    def end(notice=None):
        store.update(sid, lambda s: state.finish(s, notice=notice, source="explain"))

    def run(cancel):
        try:
            _explain(sid, chunks, cancel)
            end()
        except Cancelled:
            end(("warn", "讲解生成已取消，已完成的部分会保留。"))
        except Exception as e:
            rid = request_id_of(e)
            log.exception("request_id=%s song=%s explain failed", rid, sid)
            end(("error", "生成讲解失败：" + friendly(e)))

    jobs.start(sid, "这首曲子正在处理中", begin, run)


def _explain(sid: str, chunks: list[list[str]], cancel) -> None:
    claude = config.new_llm()
    meter = jobs.UsageMeter(claude)  # 每批写回时把新增的用量记上
    try:
        _explain_chunks(sid, chunks, cancel, claude, meter.charge)
    finally:  # 某一批或练习建议失败 / 取消：已经发出去的请求也花了钱
        meter.flush(sid)


def _explain_chunks(sid: str, chunks: list[list[str]], cancel, claude, charge) -> None:
    for k, ids in enumerate(chunks):
        if cancel.is_set():
            raise Cancelled()
        cur = store.load(sid)
        chunk = [b for b in cur["bars"] if b.get("id") in set(ids)]
        if not chunk:
            continue
        seen = {b["id"]: b["events"] for b in chunk}  # 写回时小节已经被改过的，就不写旧内容的提示
        tips = {t["number"]: t["tip"] for t in claude.explain_bars(cur["meta"], chunk).get("bars", [])}

        def apply(s, tips=tips, k=k, seen=seen, meta=cur["meta"]):
            same_meta = not staleness.setting_changed(meta or {}, s["meta"] or {})  # 改曲名、艺人不影响提示
            for b in s["bars"]:
                if (
                    same_meta
                    and same_events(seen.get(b.get("id")), b["events"])
                    and b["n"] in tips
                    and not (b.get("tip") or "").strip()
                ):
                    b["tip"] = tips[b["n"]]  # 按现在的音新写的（改过的小节撤销时仍恢复识别时的提示）
            s["progress"] = {"stage": "生成讲解", "done": k + 1, "total": len(chunks)}
            charge(s)

        store.update(sid, apply)

    cur = store.load(sid)
    seen = {b["id"]: b["events"] for b in cur["bars"]}
    plan = claude.practice_plan(cur["meta"], cur["bars"])

    def finish(s, meta=cur["meta"]):
        staleness.store_plan(s, plan, seen, meta)
        charge(s)

    store.update(sid, finish)


# ---------------------------------------------------------------------------
# 小节问答
# ---------------------------------------------------------------------------
def _bar_image(sid: str, song: dict, bar: dict):
    """问答带上的小节原图；没有区域、页号对不上（外来 / 手改坏的数据）、图片读不出来时不带图，照样能问。"""
    pi, region, pages = bar.get("page"), bar.get("region"), song.get("pages") or []
    if not (isinstance(pi, int) and 0 <= pi < len(pages) and isinstance(region, list) and len(region) == 4):
        return None
    try:
        x0, x1, y0, y1 = region
        return store.page_image(sid, pages[pi]).crop((x0, y0, x1, y1))
    except (OSError, ValueError, LookupError, TypeError):
        log.warning("song=%s bar=%s 的原图读不出来，问答不带图", sid, bar.get("id"))
        return None


def ask_bar(sid: str, bar_id: str, question: str) -> dict:
    """问一个小节的练习问题，等回答写完再返回 {"answer", "qa"}（练习页用的是流式的，见 prepare_ask）。"""
    return prepare_ask(sid, bar_id, question)()


def prepare_ask(sid: str, bar_id: str, question: str) -> Callable[..., dict]:
    """准备问一个小节的练习问题（schemas.AskIn 已经去掉首尾空白、检查过长度）：找小节、取上下文、建模型客户端。
    曲子 / 小节不存在（404）、没配 key（400）都在这里抛，还没发请求。

    返回真正提问的 run(on_delta=None)：回答边生成边交给 on_delta（文字片段；None 表示前面的作废、从头再来，
    见 llm.join_text），写完后存进这个小节的 qa，返回 {"answer", "qa"}。失败抛 AppError（模型和网络错误转成
    中文说明的 Upstream）。费用只记一次：成功时随问答一起写进曲子，失败时补记。只存完整的回答。"""
    song = store.load(sid)
    bar = find_bar(song, bar_id)
    idx = song["bars"].index(bar)
    neighbours = [song["bars"][j] for j in (idx - 1, idx + 1) if 0 <= j < len(song["bars"])]
    image = _bar_image(sid, song, bar)
    history = [q for q in bar.get("qa") or [] if not q.get("stale")]  # 改谱之前的问答说的是旧音，不再当上下文
    log_text = practice.coach_text(sid, song, bar_id)
    claude = config.new_llm()  # 没配 key：MissingKey，400

    def run(on_delta: Callable[[str | None], None] | None = None) -> dict:
        meter = jobs.UsageMeter(claude)
        parts: list[str] = []
        try:
            for t in claude.coach_stream(song["meta"], bar, neighbours, question, history, log_text, image):
                if t is None:
                    parts.clear()
                else:
                    parts.append(t)
                if on_delta is not None:
                    on_delta(t)
        except AppError:
            meter.flush(sid)  # 失败了这次请求也花了钱
            raise
        except Exception as e:  # noqa: BLE001 - 网络 / API 错误统一转成中文说明
            log.warning("request_id=%s song=%s ask failed: %s", request_id_of(e), sid, e)
            meter.flush(sid)  # 失败了这次请求也花了钱
            raise Upstream(friendly(e)) from None
        answer = "".join(parts).strip()
        if not answer:
            meter.flush(sid)  # 失败了这次请求也花了钱
            raise Upstream("模型没有给出回答，换个问法再试一次")
        entry = {"q": question, "a": answer, "at": now()}

        def apply(s):
            b = next((x for x in s["bars"] if x.get("id") == bar_id), None)
            if b is not None and not same_events(b["events"], bar["events"]):
                entry["stale"] = True  # 等回答期间这个小节改过音：回答说的是改之前的谱
            if b is not None:  # 等回答期间小节被重新识别掉了，就不存
                b["qa"] = [*(b.get("qa") or []), entry][-settings.qa_keep :]
            meter.charge(s)

        s = store.update(sid, apply)
        b = next((x for x in s["bars"] if x.get("id") == bar_id), None)
        return {"answer": entry["a"], "qa": (b or {}).get("qa", [entry])}

    return run
