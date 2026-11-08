"""谱面变了以后，AI 写的内容（小节提示、问答、练习建议）的失效与恢复，以及新旧两批小节的对应（reconcile_bars）。

都是对内存里 song dict 的纯操作，由调用方放在 store.update 里执行。
- 用户改了音：bar["edited"] = True，第一次改之前的内容记在 bar["_orig"]（撤销基线）；之后又改回原样就恢复、edited 变回 False。
- 练习记录按小节 id 记在 practice.jsonl 里（见 app/practice.py）：小节换了一批时，对上的沿用原来的 id，
  记录自然保留；对不上的小节的记录在聚合时忽略，不用删。
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass

from app.model import touch_bar
from app.music import bar_length

# 影响音的设定：这些变了，所有小节的提示都按旧设定写的。曲名、艺人、备注之类不算（派生的 meta.title 跟着曲名变）
SETTING_KEYS = ("tuning_midi", "capo", "time_signature")


def invalidate_bar(bar: dict) -> None:
    """这个小节的音变了：提示和不确定说明是按旧音写的，清掉；问答保留但标成“改谱之前的”。
    第一次改动前的内容记在 _orig 里：之后又改回原样（比如撤销），就把它们恢复。"""
    first = "_orig" not in bar
    bar["edited"] = True
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
    bar["edited"] = False
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
    plan["stale"] = bool(plan.get("stale_all") or plan.get("changed_bars"))  # 派生字段，读取时也会重算


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
        meta_changed = setting_changed(meta or {}, s["meta"] or {})  # 只看影响音的设定，改曲名不算
        if changed or meta_changed:
            plan.update(changed_bars=changed, stale_all=meta_changed, stale=True)
    s["plan"] = plan


def setting_changed(old_meta: dict, new_meta: dict) -> bool:
    return any(new_meta.get(k) != old_meta.get(k) for k in SETTING_KEYS)


def invalidate_tips_for_setting(s: dict) -> None:
    """调弦 / 变调夹 / 拍号变了：所有提示和问答都按旧设定写的（AI 读谱的不确定说明和设定无关，保留）。"""
    for b in s["bars"]:
        if b.get("tip"):
            touch_bar(b)  # 提示没了也是这个小节的内容变了
        b["tip"] = ""
        if "_orig" in b:  # 改回原样时也不该恢复旧设定下写的提示
            b["_orig"]["tip"] = ""
        mark_qa_stale(b)
    invalidate_plan(s)


def mark_qa_stale(bar: dict) -> None:
    """问答整体过时，而且不会因为改回原样而恢复。"""
    for q in bar.get("qa") or []:
        q["stale"] = True
        q.pop("stale_by_edit", None)


@dataclass(frozen=True)
class Policy:
    """新旧小节对应时怎么处理用户手改过的小节。"""

    keep_edits: bool = False  # True：手改过的小节保留用户的音，新识别的结果当成它的撤销基线
    # 比对时和音符一起看的拍号字段。单行识别出来的新小节还没按前面各行的变拍号算过 time_signature
    # （apply_time_signatures 在替换之后才跑），所以按谱上写的 ts_change 比，不按派生的 time_signature 比
    ts_field: str = "time_signature"


DERIVE = Policy()  # GP / MusicXML 重新转换（小节不能手改）
RESTAGE = Policy(keep_edits=True)  # 整首重新识别图片谱子（新结果替换前已经算好拍号）
ROW = Policy(ts_field="ts_change")  # 重新识别某一行（用户要的就是新结果）、补页、两遍携带拍号


def reconcile_bars(
    s: dict,
    old: list[dict],
    bars: list[dict],
    policy: Policy = DERIVE,
    setting_changed_: bool = False,
    metas: tuple[dict, dict] | None = None,
) -> int:
    """一批新小节（bars）取代原来的小节（old）：把原来小节的 id、提示、问答搬到新小节上（练习记录按 id 自然保留）。
    按内容做序列比对（小节数变了也能对上）：内容一样的原样保留；对不上但位置相当的（比对里的“替换”段）
    保留 id，提示用新小节自己的、问答标成过时；新多出来的是新小节，没了的就没了。

    policy.keep_edits（重新识别图片谱子）：用户手改过的小节（edited）按改之前的识别结果（_orig）去比对，
    一一对应上的保留用户改的音，新识别的结果记成它的 _orig（撤销回识别结果用）。
    setting_changed_：调弦 / 变调夹 / 拍号也变了（所有提示都按旧设定写的）。metas =（原来的谱头, 新的谱头）给出时，
    设定变了也保留手改的音：品位和调弦、变调夹无关；拍号变了的话只保留小节长度没变的（见 _edit_fits）。
    返回因为对不上而没能保留的手改小节数。调用方之后自己把 bars 放进 s。"""
    keep_edits = policy.keep_edits

    def key(events, b):
        return json.dumps([events, b.get(policy.ts_field)], sort_keys=True)

    def old_key(b):
        orig = b.get("_orig") if keep_edits and b.get("edited") else None
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
    dropped = sum(1 for b in old if keep_edits and b.get("edited") and id(b) not in paired)
    for prev, new, same, one_to_one in pairs:
        new["id"] = prev["id"]
        if prev.get("qa"):
            new["qa"] = prev["qa"]
        # 用户的确认：内容没变（bump_revs 沿用原来的 rev）就还有效，变了自然失效
        for k in ("reviewed_rev", "reviewed_warn"):
            if k in prev:
                new[k] = prev[k]
        edited = keep_edits and bool(prev.get("edited"))
        if edited and (same or one_to_one) and (not setting_changed_ or _edit_fits(prev, new, metas)):
            _keep_edit(prev, new, setting_changed_)
            if setting_changed_:
                mark_qa_stale(new)
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


def _edit_fits(prev: dict, new: dict, metas: tuple[dict, dict] | None) -> bool:
    """设定变了时手改的音还能不能用：调弦、变调夹不影响品位；拍号变了，这个小节的长度不变才行。"""
    if metas is None:
        return False
    old_meta, new_meta = metas
    if old_meta.get("time_signature") == new_meta.get("time_signature"):
        return True
    return bar_length(old_meta, prev) == bar_length(new_meta, new)


def _keep_edit(prev: dict, new: dict, setting_changed_: bool = False) -> None:
    """重新识别后保留用户手改的音：识别结果放进 _orig，改回去时还能恢复。
    setting_changed_：原来的提示是按旧设定写的，不再沿用（新识别结果自己的提示是按新设定写的，照常留着）。"""
    if prev["events"] == new["events"]:  # 新识别的正好就是用户改成的样子
        if not setting_changed_:
            new["tip"] = prev.get("tip") or new.get("tip", "")
        return
    new["_orig"] = {"events": new["events"], "tip": new.get("tip", ""), "uncertain": new.get("uncertain", "")}
    new["edited"] = True
    new["events"] = prev["events"]
    new.pop("placeholder", None)  # 这次没识别出来、按休止占位的：保留的是用户的音，不能再被当成占位休止换掉
    new["tip"], new["uncertain"] = "" if setting_changed_ else prev.get("tip", ""), ""
    new["warn"] = [w for w in new.get("warn") or [] if w.get("code") != "ocr_mismatch"]  # OCR 比的是识别结果
