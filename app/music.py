"""谱子的数据模型：调弦、拍号、时值、小节事件和反复记号的整理与校验。纯函数，不读写文件、不调模型。

用户输入格式不对时抛 errors.Invalid（也是 ValueError）；模型输出走宽松路线，读不懂就退回默认值。
"""

from __future__ import annotations

import math
import re
import uuid
from fractions import Fraction

from app.errors import Invalid

# 演奏技巧代号（模型输出的 schema、前端和 alphaTex 转换都用这一套）
TECHS = ["h", "p", "sl", "si", "pm", "nh", "ah", "roll", "x", "vib", "bend"]
STANDARD = ["E4", "B3", "G3", "D3", "A2", "E2"]
_NOTE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_NOTE_RE = re.compile(r"^\s*([A-Ga-g])\s*([#♯b♭]?)\s*(-?\d)?\s*$")
_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


# ---------------------------------------------------------------------------
# 调弦 / 拍号 / 时值
# ---------------------------------------------------------------------------
def note_to_midi(name: str, near: int | None = None) -> int:
    m = _NOTE_RE.match(name or "")
    if not m:
        raise Invalid(f"无法识别的音名：{name!r}")
    pc = _NOTE[m.group(1).upper()] + {"#": 1, "♯": 1, "b": -1, "♭": -1}.get(m.group(2), 0)
    if m.group(3) is not None:
        return (int(m.group(3)) + 1) * 12 + pc
    if near is None:
        raise Invalid(f"音名缺少八度：{name!r}")
    # 没写八度：取离标准调弦这根弦最近的那个八度（最多降 6 / 升 5 个半音）
    base = near - ((near - pc) % 12)
    return base if near - base <= 6 else base + 12


def midi_to_name(m: int) -> str:
    return f"{_NAMES[m % 12]}{m // 12 - 1}"


def normalize_meta(raw: dict, fallback_title: str = "", strict: bool = False) -> dict:
    """整理谱头信息。模型的输出宽松处理（读不懂就退回默认值）；strict=True 用于用户手工修改，格式不对直接报错。"""
    tuning = list(raw.get("tuning") or [])
    if len(tuning) != 6:
        if strict:
            raise Invalid("调弦需要 6 根弦的音名")
        tuning = STANDARD
    midis = []
    for i, n in enumerate(tuning):
        std = note_to_midi(STANDARD[i])
        try:
            m = note_to_midi(str(n), near=std)
        except ValueError:
            if strict:
                raise Invalid(f"{i + 1} 弦的音名无法识别：{n!r}") from None
            m = std
        midis.append(m)
    ts = clean_time_signature(raw.get("time_signature") or [4, 4])
    if ts is None:
        if strict:
            raise Invalid("拍号格式不对，应写成 4/4、3/4、6/8 这样")
        ts = [4, 4]
    try:
        capo = int(raw.get("capo") or 0)
    except (TypeError, ValueError):
        if strict:
            raise Invalid("变调夹应为 0–12 的整数") from None
        capo = 0
    if strict and not 0 <= capo <= 12:
        raise Invalid("变调夹应为 0–12 的整数")
    try:
        tempo = max(0, int(raw.get("tempo") or 0))
    except (TypeError, ValueError):
        tempo = 0
    return {
        "title": str(raw.get("title") or fallback_title or "").strip(),
        "artist": str(raw.get("artist") or "").strip(),
        "arranger": str(raw.get("arranger") or "").strip(),
        "tuning": [midi_to_name(m) for m in midis],
        "tuning_midi": midis,
        "capo": max(0, min(12, capo)),
        "time_signature": ts,
        "tempo": tempo,
        "notes": str(raw.get("notes") or "").strip(),
    }


def clean_time_signature(raw) -> list[int] | None:
    """[分子, 分母]，不合法返回 None。"""
    try:
        ts = list(raw)
    except TypeError:
        return None
    if len(ts) != 2 or not all(isinstance(x, int) and not isinstance(x, bool) for x in ts):
        return None
    if not 1 <= ts[0] <= 32 or ts[1] not in (1, 2, 4, 8, 16, 32):
        return None
    return ts


def bar_time_signature(meta: dict, bar: dict | None = None) -> list[int]:
    """小节自己的拍号（GP / MusicXML 中途变拍号时有），没有就用曲子的拍号。"""
    return (bar or {}).get("time_signature") or meta["time_signature"]


def bar_length(meta: dict, bar: dict | None = None) -> Fraction:
    n, d = bar_time_signature(meta, bar)
    return Fraction(n * 4, d)


def parse_dur(s) -> Fraction:
    return Fraction(str(s).strip())


def clean_events(events) -> list[dict]:
    """把模型或用户提交的事件整理成统一结构。格式不对一律抛 Invalid（带中文说明）。"""
    if not isinstance(events, list):
        raise Invalid("events 必须是列表")
    out = []
    for i, e in enumerate(events, 1):
        if not isinstance(e, dict) or "dur" not in e:
            raise Invalid(f"第 {i} 个事件缺少 dur")
        try:
            dur = parse_dur(e["dur"])
        except (ValueError, ZeroDivisionError, TypeError):
            raise Invalid(f"第 {i} 个事件的时值无法识别：{e['dur']!r}") from None
        if dur <= 0:
            raise Invalid(f"第 {i} 个事件的时值必须大于 0：{e['dur']!r}")
        notes = []
        for n in e.get("notes") or []:
            try:
                st, fr = int(n["string"]), int(n["fret"])
                grace = int(n.get("grace", -1))
                bend = float(n.get("bend", 0) or 0)
                tech = [t for t in (n.get("tech") or []) if t in TECHS]
            except (KeyError, TypeError, ValueError, AttributeError):
                raise Invalid(f"第 {i} 个事件里有音符格式不对（需要 string、fret）") from None
            if not 1 <= st <= 6:
                raise Invalid(f"第 {i} 个事件：弦号 {st} 超出 1–6")
            if not 0 <= fr <= 30:
                raise Invalid(f"第 {i} 个事件：品数 {fr} 超出 0–30")
            if not math.isfinite(bend) or not 0 <= bend <= 4:
                raise Invalid(f"第 {i} 个事件：推弦幅度 {bend} 不合理（0–4 个半音）")
            notes.append(
                {
                    "string": st,
                    "fret": fr,
                    "tech": tech,
                    "grace": grace if 0 <= grace <= 30 else -1,
                    "bend": bend,
                }
            )
        out.append({"dur": str(dur), "notes": notes})
    return out


# 反复和跳转记号（名字和 alphaTex 的 \jump 一致）。播放时 alphaTab 会按它们跳转。
DIRECTIONS = (
    "fine", "segno", "segnosegno", "coda", "doublecoda", "dacapo", "dacapoalcoda", "dacapoaldoublecoda",
    "dacapoalfine", "dalsegno", "dalsegnoalcoda", "dalsegnoaldoublecoda", "dalsegnoalfine", "dalsegnosegno",
    "dalsegnosegnoalcoda", "dalsegnosegnoaldoublecoda", "dalsegnosegnoalfine", "dacoda", "dadoublecoda",
)  # fmt: skip


def clean_repeats(b: dict) -> dict:
    """小节上的反复 / 跳房子 / 跳转记号，只保留有的字段。repeat_end = 这段一共弹几遍（普通反复号 = 2）。"""
    out: dict = {}
    if b.get("repeat_start") is True:
        out["repeat_start"] = True
    try:
        n = int(b.get("repeat_end") or 0)
    except (TypeError, ValueError):
        n = 0
    if n >= 1:
        out["repeat_end"] = max(2, min(n, 8))
    try:
        endings = sorted({int(x) for x in b.get("endings") or [] if 1 <= int(x) <= 8})
    except (TypeError, ValueError):
        endings = []
    if endings:
        out["endings"] = endings
    dirs = [d for d in dict.fromkeys(b.get("directions") or []) if d in DIRECTIONS]
    if dirs:
        out["directions"] = dirs
    return out


def rest_bar(meta: dict, bar: dict | None = None) -> list[dict]:
    """整小节休止：识别失败或被清空的小节用它占位，页面才能正常显示和播放。"""
    return [{"dur": str(bar_length(meta, bar)), "notes": []}]


def new_bar_id() -> str:
    return uuid.uuid4().hex[:10]


GRID = Fraction(1, 48)  # 播放器能精确写出的最小时值单位（四分音符的 1/48：64 分音符和 64 分三连音的公约数）


LENGTH_WARN = "时值合计"  # check_bar 里时值合计不对的那条提示的开头


def apply_time_signatures(bars: list[dict], meta: dict) -> None:
    """图片识别的曲子：模型只标出谱上写了变拍号的小节（ts_change），这里按小节顺序往后传，
    给和全曲拍号不同的小节写上 time_signature（和 GP 导入的数据一样），并更新时值提示。
    各行是分开识别的，后面的行看不到前面行写的变拍号，所以要等整首（或改了一行）之后统一算。"""
    cur = meta["time_signature"]
    for b in bars:
        cur = b.get("ts_change") or cur
        if cur != meta["time_signature"]:
            b["time_signature"] = cur
        else:
            b.pop("time_signature", None)
        if b.get("placeholder"):  # 没识别出音符、按整小节休止占位的：休止长度跟着拍号
            b["events"] = rest_bar(meta, b)
        refresh_length_warn(b, meta)


def refresh_length_warn(bar: dict, meta: dict) -> None:
    """只重算“时值合计”这一条提示（OCR 不一致之类的其他提示保留）。"""
    keep = [w for w in bar.get("warn") or [] if not w.startswith(LENGTH_WARN)]
    length = [p for p in check_bar(bar["events"], meta, bar=bar) if p.startswith(LENGTH_WARN)]
    bar["warn"] = length + keep


def check_bar(events: list[dict], meta: dict, info: bool = True, bar: dict | None = None) -> list[str]:
    """小节的问题（显示在练习页）。info=False 时只返回需要模型返修的问题：
    时值不在播放网格上（五连音之类）只是提醒，读对了也会有，不能拿去让模型“改”。
    bar 给出时按它自己的拍号（如果有）算小节长度。"""
    problems = []
    total = sum((parse_dur(e["dur"]) for e in events), Fraction(0))
    want = bar_length(meta, bar)
    if total != want:
        problems.append(f"{LENGTH_WARN} {float(total):g} 拍，应为 {float(want):g} 拍")
    odd = sorted({e["dur"] for e in events if (parse_dur(e["dur"]) / GRID).denominator != 1}, key=parse_dur)
    if odd and info:
        problems.append(
            f"时值 {'、'.join(odd)} 拍不在播放器支持的节奏网格上（支持到 64 分音符和三连音、六连音，五连音、七连音这类不支持），播放时按最接近的位置近似"
        )
    for e in events:
        for n in e["notes"]:
            if not 1 <= n["string"] <= 6:
                problems.append(f"弦号 {n['string']} 超出 1–6")
            if not 0 <= n["fret"] <= 30:
                problems.append(f"品数 {n['fret']} 不合理")
    if not events:
        problems.append("没有任何音符")
    return problems
