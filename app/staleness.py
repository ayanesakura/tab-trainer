"""谱面变了以后，AI 写的内容（小节提示、问答、练习建议）和练习记录的失效与恢复。

都是对内存里 song dict 的纯操作，由调用方放在 store.update 里执行。改音的第一次会把原内容记在 bar["_orig"]，
之后又改回原样（比如撤销）就能恢复。
"""

from __future__ import annotations

import difflib
import json

from app.music import check_bar

SETTING_KEYS = ("tuning_midi", "capo", "time_signature")  # 这些变了，所有小节的提示都按旧设定写的


def invalidate_bar(bar: dict) -> None:
    """这个小节的音变了：提示和不确定说明是按旧音写的，清掉；问答保留但标成“改谱之前的”。
    第一次改动前的内容记在 _orig 里：之后又改回原样（比如撤销），就把它们恢复。"""
    first = "_orig" not in bar
    if first:
        bar["_orig"] = {
            "events": bar["events"],
            "tip": bar.get("tip", ""),
            "uncertain": bar.get("uncertain", ""),
        }
    bar["tip"] = ""
    bar["uncertain"] = ""
    for q in bar.get("qa") or []:
        if not q.get("stale"):
            q["stale"] = True
            if first:  # 只有原样时问的问答，改回原样后才恢复；改动期间问的说的是改过的谱
                q["stale_by_edit"] = True


def restore_bar(bar: dict) -> None:
    """改回了第一次改动前的音：恢复当时的提示和不确定说明；改动期间问的问题反过来变成过时的。"""
    orig = bar.pop("_orig")
    bar["tip"], bar["uncertain"] = orig["tip"], orig["uncertain"]
    for q in bar.get("qa") or []:
        if q.pop("stale_by_edit", False):
            q.pop("stale", None)
        else:
            q["stale"] = True


def invalidate_plan(s: dict, bar_id: str | None = None) -> None:
    """练习建议过时：bar_id 给出时只记下“这个小节改过”（改回原样还能撤销），否则是整体过时（调弦、重新识别）。"""
    plan = s.get("plan")
    if not plan:
        return
    if bar_id:
        plan["changed_bars"] = sorted({*plan.get("changed_bars", []), bar_id})
    else:
        plan["stale_all"] = True
    plan["stale"] = bool(plan.get("stale_all") or plan.get("changed_bars"))


def restore_plan_bar(s: dict, bar_id: str) -> None:
    plan = s.get("plan")
    if plan and bar_id in plan.get("changed_bars", []):
        plan["changed_bars"] = [b for b in plan["changed_bars"] if b != bar_id]
        plan["stale"] = bool(plan.get("stale_all") or plan["changed_bars"])


def store_plan(s: dict, plan: dict | None, seen: dict, meta: dict) -> None:
    """写回后台生成的练习建议。seen / meta 是生成时用的小节音符（按 id）和曲子设定：
    生成期间谱子被改过的话，新计划一出来就是过时的，照样保存但标成过时（改回原样还能撤销）。"""
    if plan:
        changed = sorted(b["id"] for b in s["bars"] if seen.get(b["id"]) != b["events"])
        meta_changed = s["meta"] != meta
        if changed or meta_changed:
            plan.update(changed_bars=changed, stale_all=meta_changed, stale=True)
    s["plan"] = plan


def prune_practice(s: dict) -> None:
    """小节被重新识别 / 换了音轨以后 id 都变了：丢掉对不上的练习记录（累计总时长保留）。"""
    p = s.get("practice")
    if not p:
        return
    ids = {b.get("id") for b in s.get("bars", [])}
    p["bars"] = {k: v for k, v in (p.get("bars") or {}).items() if k in ids}
    last = p.get("last")
    if last and (last.get("a") not in ids or last.get("b") not in ids):
        p.pop("last", None)


def setting_changed(old_meta: dict, new_meta: dict) -> bool:
    return any(new_meta.get(k) != old_meta.get(k) for k in SETTING_KEYS)


def invalidate_tips_for_setting(s: dict) -> None:
    """调弦 / 变调夹 / 拍号变了：所有提示和问答都按旧设定写的（AI 读谱的不确定说明和设定无关，保留）。"""
    for b in s["bars"]:
        b["tip"] = ""
        if "_orig" in b:  # 改回原样时也不该恢复旧设定下写的提示；_orig 留着，重新识别时靠它认出手改过的小节
            b["_orig"]["tip"] = ""
        mark_qa_stale(b)
    invalidate_plan(s)


def mark_qa_stale(bar: dict) -> None:
    """问答整体过时，而且不会因为改回原样而恢复。"""
    for q in bar.get("qa") or []:
        q["stale"] = True
        q.pop("stale_by_edit", None)


def carry_over(s: dict, meta: dict, bars: list[dict], keep_edits: bool = False) -> int:
    """同一首曲子换成一批新小节（GP 重新转换、整首重新识别）：把原来小节的 id、提示、问答和练习记录搬到新小节上。
    按内容做序列比对（小节数变了也能对上）：内容一样的原样保留；对不上但位置相当的（比对里的“替换”段）
    保留 id 和练习记录，提示用新小节自己的、问答标成过时；新多出来的是新小节，没了的连同练习记录一起去掉。

    keep_edits（重新识别图片谱子）：用户手改过的小节（有 _orig）按改之前的识别结果去比对，
    一一对应上的保留用户改的音，新识别的结果记成它的 _orig（撤销回识别结果用）。
    返回因为对不上而没能保留的手改小节数。调用方之后自己把 bars / meta 写进 s。"""
    old_meta = s.get("meta") or {}
    setting_changed_ = setting_changed(old_meta, meta)
    old = s.get("bars") or []

    def key(events, b):
        return json.dumps([events, b.get("time_signature")], sort_keys=True)

    def old_key(b):
        orig = b.get("_orig") if keep_edits else None
        return key(orig["events"] if orig else b["events"], b)

    pairs = []  # (原来的小节, 新小节, 内容是否一样, 是否一一对应)
    sm = difflib.SequenceMatcher(
        None, [old_key(b) for b in old], [key(b["events"], b) for b in bars], autojunk=False
    )
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op in ("equal", "replace"):
            one_to_one = i2 - i1 == j2 - j1
            pairs += [
                (old[i], bars[j], op == "equal", one_to_one) for i, j in zip(range(i1, i2), range(j1, j2))
            ]
    paired = {id(p[0]) for p in pairs}
    dropped = sum(1 for b in old if keep_edits and "_orig" in b and id(b) not in paired)
    for prev, new, same, one_to_one in pairs:
        new["id"] = prev["id"]
        if prev.get("qa"):
            new["qa"] = prev["qa"]
        edited = keep_edits and "_orig" in prev
        if edited and not setting_changed_ and (same or one_to_one):
            _keep_edit(prev, new, meta)
            continue
        dropped += edited
        if same and not setting_changed_:
            new["tip"] = prev.get("tip") or new.get("tip", "")  # 讲解补写的提示比识别时顺手写的更完整
            continue
        mark_qa_stale(new)
        if not setting_changed_:
            invalidate_plan(s, new["id"])
    if setting_changed_ or len(old) != len(bars):  # 小节编号整体移动了，练习建议里的小节号对不上
        invalidate_plan(s)
    return dropped


def _keep_edit(prev: dict, new: dict, meta: dict) -> None:
    """重新识别后保留用户手改的音：识别结果放进 _orig，改回去时还能恢复。"""
    if prev["events"] == new["events"]:  # 新识别的正好就是用户改成的样子
        new["tip"] = prev.get("tip") or new.get("tip", "")
        return
    new["_orig"] = {"events": new["events"], "tip": new.get("tip", ""), "uncertain": new.get("uncertain", "")}
    new["events"] = prev["events"]
    new["tip"], new["uncertain"] = prev.get("tip", ""), ""
    new["warn"] = check_bar(new["events"], meta, bar=new)
