"""谱面变了以后，AI 写的内容（小节提示、问答、练习建议）和练习记录的失效与恢复。

都是对内存里 song dict 的纯操作，由调用方放在 store.update 里执行。改音的第一次会把原内容记在 bar["_orig"]，
之后又改回原样（比如撤销）就能恢复。
"""

from __future__ import annotations

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
        b.pop("_orig", None)
        mark_qa_stale(b)
    invalidate_plan(s)


def mark_qa_stale(bar: dict) -> None:
    """问答整体过时，而且不会因为改回原样而恢复。"""
    for q in bar.get("qa") or []:
        q["stale"] = True
        q.pop("stale_by_edit", None)
