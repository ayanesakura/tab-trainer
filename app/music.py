"""谱子的数据模型：调弦、拍号、时值、小节事件和反复记号的整理与校验。纯函数，不读写文件、不调模型。

用户输入格式不对时抛 errors.Invalid（也是 ValueError）；模型输出走宽松路线，读不懂就退回默认值。
"""

from __future__ import annotations

import json
import math
import re
import uuid
from fractions import Fraction
from pathlib import Path

from app.errors import Invalid

# 前后端共用的词表（app/static/js/vocab.json）：乐器、技巧、反复记号、节奏网格、警告代码、练琴自评
VOCAB = json.loads((Path(__file__).parent / "static" / "js" / "vocab.json").read_text(encoding="utf-8"))
INSTRUMENT = VOCAB["instrument"]
STRINGS: int = INSTRUMENT["strings"]
MAX_FRET: int = INSTRUMENT["max_fret_written"]  # 数据里接受的最高品（泛音节点、加长指板）
# 点弦泛音（th）右手点的品：可以比指板还高（按 7 品、点 12 品之外的 19 品、26 品，点在音孔上方）
MAX_TAP_FRET: int = INSTRUMENT["max_tap_fret"]
# 演奏技巧代号（模型输出的 schema、前端和 alphaTex 转换都用这一套）
TECHS = [t["code"] for t in VOCAB["techs"]]
STANDARD = list(INSTRUMENT["standard_tuning"])
WARN_CODES: dict[str, str] = dict(VOCAB["warn_codes"])
# 直接导入的曲子（Guitar Pro / MusicXML）：source_type → 扩展名
SCORE_FILES: dict[str, tuple[str, ...]] = {
    k: tuple(v) for k, v in VOCAB["score_files"].items() if k in ("gp", "musicxml")
}
GBK_EXT = tuple(VOCAB["score_files"]["gbk"])  # 中文系统上按 GBK 存文字的老格式（GP7+ 和 MusicXML 是 UTF-8）
# 练琴计划（app/study.py）：练完一遍的自评 1–4（= FSRS 的 Again / Hard / Good / Easy）和可勾的问题
STUDY_RATINGS: dict[int, str] = {int(k): v for k, v in VOCAB["study_ratings"].items()}
STUDY_ISSUES: tuple[str, ...] = tuple(VOCAB["study_issues"])
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


def tuning_midi(tuning: list[str]) -> list[int]:
    """音名（带八度，normalize_meta 整理过的）→ MIDI 音高。读不懂的弦按标准调弦。"""
    out = []
    for i, name in enumerate(tuning):
        std = note_to_midi(STANDARD[i]) if i < len(STANDARD) else 40
        try:
            out.append(note_to_midi(str(name), near=std))
        except ValueError:
            out.append(std)
    return out


def normalize_meta(raw: dict, fallback_title: str = "", strict: bool = False) -> dict:
    """整理谱头信息。模型的输出宽松处理（读不懂就退回默认值）；strict=True 用于用户手工修改，格式不对直接报错。"""
    try:
        tuning = list(raw.get("tuning") or [])
    except TypeError:  # 不是列表（比如一个数字）
        tuning = []
    if len(tuning) != STRINGS:
        if strict:
            raise Invalid(f"调弦需要 {STRINGS} 根弦的音名")
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
    except (TypeError, ValueError, OverflowError):  # OverflowError：JSON 里的 1e400 解析成 inf
        if strict:
            raise Invalid("变调夹应为 0–12 的整数") from None
        capo = 0
    try:
        tempo = max(0, int(raw.get("tempo") or 0))
    except (TypeError, ValueError, OverflowError):
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


# 一个事件的时值范围（拍）：最短 1 tick（播放器的最小单位）；最长 256 拍，比最长的小节（32/1 拍 = 128 拍）还宽一倍，
# GP 里写得比拍号长的小节也够用。没有上限的话 "1e400" 能存进去，之后每次读取算时值合计都会溢出
MIN_DUR = Fraction(1, VOCAB["ticks_per_quarter"])
MAX_DUR = Fraction(256)


def dur_problem(v) -> str | None:
    """时值有什么问题（没问题返回 None）。"""
    try:
        dur = parse_dur(v)
    except (ValueError, ZeroDivisionError, TypeError, OverflowError):
        return f"时值无法识别：{str(v)[:40]!r}"
    if dur <= 0:
        return f"时值必须是大于 0 的分数：{str(v)[:40]!r}"
    if not MIN_DUR <= dur <= MAX_DUR:
        return f"时值 {str(v)[:40]!r} 超出范围（1/{MIN_DUR.denominator}–{MAX_DUR} 拍）"
    return None


MAX_VERSES = 4


def clean_lyrics(lyrics) -> list[str]:
    """一个拍上的歌词：每段一个字符串（第 1 段在前），去掉首尾空白；后面空的段去掉，全空返回 []。"""
    if not isinstance(lyrics, list):
        return []
    out = [str(x).strip()[:40] if isinstance(x, (str, int, float)) else "" for x in lyrics[:MAX_VERSES]]
    while out and not out[-1]:
        out.pop()
    return out


def clean_shapes(shapes) -> dict[str, list[int]]:
    """弹唱谱的和弦指法：{和弦名: [1–6 弦的品位，-1 = 不弹]}。格式不对的条目丢掉（只用来画和弦图）。"""
    out: dict[str, list[int]] = {}
    for name, frets in (shapes or {}).items() if isinstance(shapes, dict) else ():
        name = str(name).strip()[:24]
        try:
            fr = [int(f) for f in frets]
        except (TypeError, ValueError):
            continue
        if name and len(fr) == STRINGS and all(-1 <= f <= MAX_FRET for f in fr):
            out[name] = fr
    return out


def clean_events(events) -> list[dict]:
    """把模型或用户提交的事件整理成统一结构。格式不对一律抛 Invalid（带中文说明）。"""
    if not isinstance(events, list):
        raise Invalid("events 必须是列表")
    out = []
    for i, e in enumerate(events, 1):
        if not isinstance(e, dict) or "dur" not in e:
            raise Invalid(f"第 {i} 个事件缺少 dur")
        if why := dur_problem(e["dur"]):
            raise Invalid(f"第 {i} 个事件的{why}")
        dur = parse_dur(e["dur"])
        notes = []
        if not isinstance(e.get("notes") or [], list):
            raise Invalid(f"第 {i} 个事件的 notes 必须是列表")
        for n in e.get("notes") or []:
            try:
                st, fr = int(n["string"]), int(n["fret"])
                grace = int(n.get("grace", -1))
                bend = float(n.get("bend", 0) or 0)
                tech = [t for t in (n.get("tech") or []) if t in TECHS]
                harm = int(n.get("harm") or 0) if "th" in tech else 0
            except (KeyError, TypeError, ValueError, AttributeError, OverflowError):  # 1e400 → inf
                raise Invalid(f"第 {i} 个事件里有音符格式不对（需要 string、fret）") from None
            if not 1 <= st <= STRINGS:
                raise Invalid(f"第 {i} 个事件：弦号 {st} 超出 1–{STRINGS}")
            if not 0 <= fr <= MAX_FRET:
                raise Invalid(f"第 {i} 个事件：品数 {fr} 超出 0–{MAX_FRET}")
            if not math.isfinite(bend) or not 0 <= bend <= 4:
                raise Invalid(f"第 {i} 个事件：推弦幅度 {bend} 不合理（0–4 个半音）")
            if "th" in tech:
                # 点弦泛音：fret = 左手按的品（谱上的第一个数），harm = 右手点的品（<> 里的数）。
                # 没写（模型给 0）按最常见的点法：高 12 品
                harm = harm or min(fr + 12, MAX_TAP_FRET)
                if not fr < harm <= MAX_TAP_FRET:
                    raise Invalid(
                        f"第 {i} 个事件：点弦泛音点在 {harm} 品不合理（要比按的品高，最多 {MAX_TAP_FRET} 品）"
                    )
            note = {
                "string": st,
                "fret": fr,
                "tech": tech,
                "grace": grace if 0 <= grace <= MAX_FRET else -1,
                "bend": bend,
            }
            if harm:  # 只有点弦泛音才有这个键：别的音一个字节都不变（不然每个小节的 rev 都会变）
                note["harm"] = harm
            notes.append(note)
        ev = {"dur": str(dur), "notes": notes}
        chord = str(e.get("chord") or "").strip()[:24]
        if chord:
            ev["chord"] = chord
        if e.get("strum") in ("down", "up"):
            ev["strum"] = e["strum"]
        lyrics = clean_lyrics(e.get("lyrics"))
        if lyrics:
            ev["lyrics"] = lyrics
        out.append(ev)
    return out


# 事件上可以省略的字段：空的时候 clean_events 不写
OPTIONAL_EVENT_KEYS = ("chord", "lyrics", "strum")


def canonical_events(events):
    """比较“音变没变”用的统一形式（和 clean_events 的结果一样：空的 chord / lyrics / strum 不写、时值约分、
    bend 是小数、不是点弦泛音的音没有 harm）。外来曲子包补过默认值的、旧数据里写法不规范的，内容一样就算一样。整理不了的原样返回。"""
    try:
        return clean_events(events)
    except Invalid:
        return events


def same_events(a, b) -> bool:
    return canonical_events(a) == canonical_events(b)


def events_key(events) -> str:
    """按内容的比较键（JSON 文本）。"""
    return json.dumps(canonical_events(events), sort_keys=True, default=str)


def drop_empty_event_keys(events) -> bool:
    """去掉事件上空的可省略字段（原地修改），返回有没有改动。"""
    changed = False
    for e in events if isinstance(events, list) else ():
        for k in OPTIONAL_EVENT_KEYS if isinstance(e, dict) else ():
            if k in e and not e[k]:
                del e[k]
                changed = True
    return changed


# 反复和跳转记号（名字和 alphaTex 的 \jump 一致，顺序 = alphaTab 的 Direction 枚举）。播放时 alphaTab 会按它们跳转。
DIRECTIONS = tuple(d["code"] for d in VOCAB["directions"])
# 模型提示词里允许的子集（llm.py 的 schema 用），按 prompt_order 排（和以前写死的顺序一致，schema 逐字不变）
PROMPT_DIRECTIONS = [
    d["code"]
    for d in sorted((d for d in VOCAB["directions"] if d["prompt"]), key=lambda d: d["prompt_order"])
]


def clean_repeats(b: dict) -> dict:
    """小节上的反复 / 跳房子 / 跳转记号，只保留有的字段。repeat_end = 这段一共弹几遍（普通反复号 = 2）。"""
    out: dict = {}
    if b.get("repeat_start") is True:
        out["repeat_start"] = True
    try:
        n = int(b.get("repeat_end") or 0)
    except (TypeError, ValueError, OverflowError):  # OverflowError：JSON 里的 1e400 解析成 inf
        n = 0
    if n >= 1:
        out["repeat_end"] = max(2, min(n, 8))
    try:
        endings = sorted({int(x) for x in b.get("endings") or [] if 1 <= int(x) <= 8})
    except (TypeError, ValueError, OverflowError):
        endings = []
    if endings:
        out["endings"] = endings
    try:
        dirs = [d for d in dict.fromkeys(b.get("directions") or []) if d in DIRECTIONS]
    except TypeError:  # 不是列表，或者里面有列表之类
        dirs = []
    if dirs:
        out["directions"] = dirs
    return out


def rest_bar(meta: dict, bar: dict | None = None) -> list[dict]:
    """整小节休止：识别失败或被清空的小节用它占位，页面才能正常显示和播放。"""
    return [{"dur": str(bar_length(meta, bar)), "notes": []}]


def new_bar_id() -> str:
    return uuid.uuid4().hex[:10]


# 播放器能精确写出的最小时值单位（四分音符的 1/48：64 分音符和 64 分三连音的公约数）
GRID = Fraction(VOCAB["grid_ticks"], VOCAB["ticks_per_quarter"])

# 小节警告：{"code", "msg"}，code 取自 vocab.warn_codes。这些是按小节内容算出来的（读取时重算，不存盘）；
# 其他（OCR 不一致、导入失败说明）是识别 / 导入时留下的，存盘。
DERIVED_WARNS = frozenset({"length", "off_grid", "string_range", "empty", "placeholder"})
PLACEHOLDER_MSG = "没有识别出可用的音符，暂按整小节休止处理"


def warn(code: str, msg: str) -> dict:
    assert code in WARN_CODES, code
    return {"code": code, "msg": msg}


def warn_code_of(w) -> str:
    """警告的 code。旧数据（和旧评测报告）里警告是一句话，按开头的文字认出来，认不出的算 other。"""
    if isinstance(w, dict):
        return w.get("code") if w.get("code") in WARN_CODES else "other"
    text = str(w)
    if text.startswith("时值合计"):
        return "length"
    if "网格" in text:
        return "off_grid"
    if text.startswith("本地数字识别"):
        return "ocr_mismatch"
    if text.startswith("没有识别出可用的音符"):
        return "placeholder"
    if text.startswith(("弦号", "品数")):
        return "string_range"
    if text.startswith("没有任何音符"):
        return "empty"
    return "other"


def as_warn(w) -> dict:
    """旧格式（字符串）或新格式的警告 → {"code", "msg"}。"""
    if isinstance(w, dict) and w.get("code") in WARN_CODES:
        return {"code": w["code"], "msg": str(w.get("msg") or WARN_CODES[w["code"]])}
    msg = w.get("msg", "") if isinstance(w, dict) else w
    return {"code": warn_code_of(w), "msg": str(msg)}


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
    keep = [w for w in bar.get("warn") or [] if warn_code_of(w) != "length"]
    length = [p for p in check_bar(bar["events"], meta, bar=bar) if p["code"] == "length"]
    bar["warn"] = length + keep


def warn_texts(bar: dict) -> list[str]:
    """小节上警告的文字（用户确认时记下来，之后多出新的警告要重新核对）。"""
    return [str(w.get("msg") or w.get("code")) for w in bar.get("warn") or [] if isinstance(w, dict)]


def derive_warns(bar: dict, meta: dict) -> None:
    """重算小节上按内容算出来的警告（时值、网格、弦品范围、占位），保留识别时留下的（OCR 不一致等）。"""
    kept = [as_warn(w) for w in bar.get("warn") or []]
    kept = [w for w in kept if w["code"] not in DERIVED_WARNS]
    derived = check_bar(bar["events"], meta, bar=bar) if bar.get("events") is not None else []
    if bar.get("placeholder"):
        derived.append(warn("placeholder", PLACEHOLDER_MSG))
    bar["warn"] = derived + kept


def _beats(f: Fraction) -> str:
    """给人看的拍数。万一是没有上限的旧数据，float 会溢出：直接写分数。"""
    try:
        return f"{float(f):g}"
    except OverflowError:
        return str(f)[:40]


def check_bar(events: list[dict], meta: dict, info: bool = True, bar: dict | None = None) -> list[dict]:
    """小节的问题（显示在练习页），[{"code", "msg"}]。info=False 时只返回需要模型返修的问题：
    时值不在播放网格上（五连音之类）只是提醒，读对了也会有，不能拿去让模型“改”。
    bar 给出时按它自己的拍号（如果有）算小节长度。"""
    problems = []
    total = sum((parse_dur(e["dur"]) for e in events), Fraction(0))
    want = bar_length(meta, bar)
    if total != want:
        problems.append(warn("length", f"时值合计 {_beats(total)} 拍，应为 {_beats(want)} 拍"))
    odd = sorted({e["dur"] for e in events if (parse_dur(e["dur"]) / GRID).denominator != 1}, key=parse_dur)
    if odd and info:
        problems.append(
            warn(
                "off_grid",
                f"时值 {'、'.join(odd)} 拍不在播放器支持的节奏网格上（支持到 64 分音符和三连音、六连音，五连音、七连音这类不支持），播放时按最接近的位置近似",
            )
        )
    for e in events:
        for n in e["notes"]:
            if not 1 <= n["string"] <= STRINGS:
                problems.append(warn("string_range", f"弦号 {n['string']} 超出 1–{STRINGS}"))
            if not 0 <= n["fret"] <= MAX_FRET:
                problems.append(warn("string_range", f"品数 {n['fret']} 不合理"))
    if not events:
        problems.append(warn("empty", "没有任何音符"))
    return problems
