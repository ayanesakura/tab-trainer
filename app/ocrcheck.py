"""本机 OCR 校对：比较 AI 读出的（弦, 品）和本机数字识别（app/ocr.py）在图上读到的，对不上的交给模型返修、
返修后还对不上的在小节上留警告。"""

from __future__ import annotations

from collections import Counter

from app.music import warn


def note_names(bag) -> str:
    """“1弦3品、2弦0品×2” 这样的音名列表（给模型的返修说明、小节警告用）。"""
    return "、".join(f"{s}弦{f}品" + (f"×{n}" if n > 1 else "") for (s, f), n in sorted(bag.items()))


def ocr_disagreements(
    bars: list[dict],
    ocr_bars: list[list[tuple[int, int]]],
    chord_sheet: bool = False,
    tied_in: dict[int, int] | None = None,
) -> dict[int, tuple]:
    """逐小节比较 AI 结果和本机数字识别读到的（弦, 品）。返回 {小节序号: (只有 OCR 读到的, 只有 AI 写了的)}。

    - OCR 在这个小节什么都没读到：不判断（可能是没读出来，不代表没有音）；
    - 死音（谱上写 x）OCR 不读，不比较；倚音的小数字 OCR 会读到，算进去；
    - 人工泛音旁边标的“品位 + 12”（如 2<14>）、点弦泛音点的品（如 5<22> 的 22）OCR 会多读一个，允许；
    - 整行一致率不到一半，或 OCR 只读对了 AI 写的音的不到 60%：多半是 OCR 在这种排版上读不准，整行都不判断。
    tied_in：图上第一个小节之前每根弦最后一个音的品位（右半行接左半的音、一行接上一行的音；{} = 前面没有音）。
    None = 不知道（各行并行识别，上一行还没结果）：第一个小节 AI 这边没有对不上的音时，OCR 多读的音每根弦
    放过一个（可能是接上一行的延续音），和后面小节的规则一样；但 AI 在小节开头这根弦上已经写了新音的不放过：
    延续音占着小节开头的这根弦，那里不会同时起一个新音，多出来的只能是小节中间漏掉的音。
    """
    pairs = []
    ai_total = 0  # 整行 AI 写的音（不含闷音）
    last_fret: dict[int, int] = dict(
        tied_in or {}
    )  # 每根弦上最后一个音的品位（判断跨小节线、写成括号的延续音）
    for i, (bar, seen) in enumerate(zip(bars, ocr_bars, strict=False)):
        tied_ok = set(last_fret.items())
        unknown_tie = i == 0 and tied_in is None
        ai: Counter = Counter()
        harmonic_marks: Counter = Counter()
        first = bar["events"][0]["notes"] if bar["events"] else []
        struck_at_start = {n["string"] for n in first if "x" not in n["tech"]}
        for e in bar["events"]:
            for n in e["notes"]:
                if "x" in n["tech"]:
                    continue
                ai[(n["string"], n["fret"])] += 1
                ai_total += 1
                last_fret[n["string"]] = n["fret"]
                if n.get("grace", -1) >= 0:
                    ai[(n["string"], n["grace"])] += 1
                if "ah" in n["tech"]:
                    harmonic_marks[(n["string"], n["fret"] + 12)] += 1
                if n.get("harm"):  # 点弦泛音：谱上 <19> 这样的点弦位置 OCR 也会读到，不是另一个音
                    harmonic_marks[(n["string"], n["harm"])] += 1
        got = Counter(seen) - harmonic_marks
        # 两位数的两个数字隔得稍远时 OCR 会读成两个音（11 → 1、1）：AI 写的两位数能由这几位拼出来就算一致
        for (st, f), k in list((ai - got).items()):
            parts = Counter((st, int(d)) for d in str(f)) if f >= 10 else None
            for _ in range(k):
                if parts and not (parts - (got - ai)):
                    got -= parts
                    got[(st, f)] += 1
        # 连音线跨过小节线延续的音，谱上常写成“(5)”，OCR 会读到，但它不是新起拍：上一小节这根弦的最后一个音
        # 就是这个品位、而且 AI 这边没有对不上的音（读错弦是两边各多一个，延续音只是 OCR 多一个），就不算多出来的
        if not (ai - got):
            loose: set[int] = set()  # 不知道上一行时，已经放过一个的弦
            for key, k in list((got - ai).items()):
                if key in tied_ok:
                    got[key] -= k
                elif unknown_tie and key[0] not in loose and key[0] not in struck_at_start:
                    got[key] -= 1
                    loose.add(key[0])
            got += Counter()  # 去掉计数为 0 的
        if got:
            pairs.append((i, ai, got))
    agree = sum(sum((ai & got).values()) for _, ai, got in pairs)
    total = sum(max(sum(ai.values()), sum(got.values())) for _, ai, got in pairs)
    if not total or (not chord_sheet and agree / total < 0.5):
        return {}
    # OCR 在整行里只读对了 AI 结果的一小部分（斜体 / 手写风格的数字、很糊的截图）：它在这种排版上靠不住，
    # 这时的“不一致”多半是 OCR 读错，提示给模型反而会把对的改错。GP 等软件导出的谱子每行都在 78% 以上
    # 弹唱谱的 × 是按和弦图换算的，OCR 本来就看不到：改看 OCR 读到的数字里有多少和 AI 对得上
    ocr_total = sum(sum(got.values()) for _, _, got in pairs)
    if (agree < 0.7 * ocr_total) if chord_sheet else (agree < 0.6 * ai_total):
        return {}
    return {i: (got - ai, ai - got) for i, ai, got in pairs if got != ai}


def mismatch_issue(n: int, only_ocr: Counter, only_ai: Counter) -> str:
    """第 n 小节 OCR 和 AI 对不上：发给模型返修的说明。"""
    parts = []
    if only_ocr:
        parts.append(f"本地数字识别在图上读到 {note_names(only_ocr)}，你的结果里没有")
    if only_ai:
        parts.append(f"你写的 {note_names(only_ai)} 它没读到")
    return (
        f"第 {n} 小节："
        + "；".join(parts)
        + "（它只读品位数字，也可能读错：请对照图片核对这几个位置，图上确实如此再改）"
    )


def mismatch_warning(only_ocr: Counter, only_ai: Counter) -> dict:
    """返修后还对不上：留在小节上的警告。"""
    msg = "本地数字识别和 AI 结果不一致"
    if only_ocr:
        msg += f"：谱上读到 {note_names(only_ocr)}"
    if only_ai:
        msg += f"{'，' if only_ocr else '：'}AI 写的是 {note_names(only_ai)}"
    return warn("ocr_mismatch", msg + "。请对照原谱核对")


def tied_in(song: dict, si: int) -> dict[int, int] | None:
    """第 si 行开头之前每根弦最后一个音的品位（给 OCR 校对认行首的延续音）：第一行前面没有音；
    上一行还没有结果（各行并行识别）时不知道，返回 None。"""
    if si == 0:
        return {}
    prev = [b for b in song.get("bars") or [] if b.get("system") == si - 1]
    return last_frets(prev) if prev else None


def last_frets(bars: list[dict]) -> dict[int, int]:
    """每根弦上最后一个音的品位（闷音不算）。"""
    out: dict[int, int] = {}
    for b in bars:
        for e in b["events"]:
            for n in e["notes"]:
                if "x" not in n["tech"]:
                    out[n["string"]] = n["fret"]
    return out
