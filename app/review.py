"""第一遍逐行识别之后的复核：各行是并行、分开识别的，一行看不到别的行。
- 谱头的拍号读错了（第 1 小节就写着“变拍号”）：改全曲拍号（fix_header_time_signature）；
- 前面的行变了拍号，后面的行识别时不知道（模型会把小节硬凑成全曲拍号）：按前面各行写的变拍号算出每行开头实际的
  拍号（row_start_ts），和识别时用的不一样的行带上它再识别一次；
- 弹唱谱：一行读出的和弦图和多数行不一样（多半是“3fr.”这类位置标记算错了）：带上多数的读法再识别一次。
挑出要重做的行（rows_to_redo）是纯函数；lock_redo 在 store.update 里挑并标上“识别中”。"""

from __future__ import annotations

from collections import Counter

from app import prompts
from app.music import apply_time_signatures


def fix_header_time_signature(s: dict, meta_user: dict) -> bool:
    """第 1 小节就写着“变拍号”：其实是读谱头时把拍号读错了（例如 3/4 读成 4/4）。全曲拍号改成它，
    第 1 小节不再算变拍号。用户手动设过拍号的不动。返回有没有改。"""
    if "time_signature" in meta_user or not s.get("meta"):
        return False
    first = next((b for b in s.get("bars") or [] if b.get("system") == 0), None)
    ts = (first or {}).get("ts_change")
    if not ts or ts == s["meta"]["time_signature"]:
        return False
    s["meta"] = {**s["meta"], "time_signature": ts}
    first.pop("ts_change", None)
    first.pop("time_signature", None)
    apply_time_signatures(s["bars"], s["meta"])
    return True


def row_start_ts(bars: list[dict], meta: dict, si: int) -> list[int]:
    """第 si 行开头实际的拍号：按顺序走过前面各行写的变拍号（ts_change）。"""
    cur = meta["time_signature"]
    for b in bars:
        if b["system"] >= si:
            break
        cur = b.get("ts_change") or cur
    return cur


def rows_to_redo(
    bars: list[dict],
    meta: dict,
    used: dict[int, list[int]],
    skip_edited: bool = True,
    ts_only: set[int] | frozenset = frozenset(),
) -> list[tuple[int, list[int], str]]:
    """第一遍之后要再识别一次的行：(行号, 开头的拍号, 给模型的提示)。两种情况：
    - 开头实际的拍号（按前面各行写的变拍号推算）和识别时用的不一样（行首自己写了变拍号的不算，模型看得到）；
    - 弹唱谱：这一行读出的和弦图和别的行对同一个和弦的读法不一样（多数行一致时以多数为准，
      常见原因是“3fr.”这类位置标记算错了和弦图），带上多数的读法再识别，谱上的 x 才能换算对。
    skip_edited：用户改过的行不重做（不覆盖用户的改动）；ts_only：这几行只看拍号。"""
    consensus = shape_consensus(bars)
    out = []
    for si in sorted(used):
        row = [b for b in bars if b["system"] == si]
        if not row or (skip_edited and any(b.get("edited") for b in row)):
            continue
        ts = row_start_ts(bars, meta, si)
        ts_wrong = ts != used[si] and not row[0].get("ts_change")
        mine = {k: v for b in row for k, v in (b.get("shapes") or {}).items()}
        odd = {k: consensus[k] for k, v in mine.items() if k in consensus and v != consensus[k]}
        if si in ts_only:
            odd = {}
        if not ts_wrong and not odd:
            continue
        hint = prompts.shape_consensus_context(odd) if odd else ""
        out.append((si, ts if ts_wrong else used[si], hint))
    return out


def shape_consensus(bars: list[dict]) -> dict[str, list[int]]:
    """各行对同一个和弦图的读法：超过一半的行（至少两行）读成一样的，就以它为准。"""
    per_row: dict[tuple[int, str], tuple[int, ...]] = {}
    for b in bars:
        for name, frets in (b.get("shapes") or {}).items():
            per_row[(b.get("system", -1), name)] = tuple(frets)
    votes: dict[str, Counter] = {}
    for (_, name), frets in per_row.items():
        votes.setdefault(name, Counter())[frets] += 1
    out = {}
    for name, c in votes.items():
        frets, n = c.most_common(1)[0]
        if n >= 2 and n * 2 > sum(c.values()):
            out[name] = list(frets)
    return out


def redo_progress(redo: list) -> dict:
    return {"stage": "复核个别行（变拍号、和弦图）", "done": 0, "total": len(redo)}


def lock_redo(
    s: dict,
    used: dict[int, list[int]],
    redo: list,
    ts_only: set[int] | frozenset = frozenset(),
    skip_edited: bool = True,
) -> None:
    """挑出要重做的行（填进 redo）并把它们标成识别中，在同一次读-改-写里：中间用户改了的行不会被重做覆盖
    （skip_edited=False：有手改的行也重做，由写回时保留手改的小节，见 recognize._redo_after）。"""
    redo[:] = rows_to_redo(s["bars"], s["meta"], used, skip_edited, ts_only)
    for si, *_ in redo:
        s["systems"][si].update(status="parsing", error=None)
    if redo:
        s["progress"] = redo_progress(redo)
