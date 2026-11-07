"""识别流程：保存上传的页面 → 读谱头 → 版面检测 → 逐行调用 Claude 识别 → 校验/返修 → 练习建议。"""

from __future__ import annotations

import io
import logging
import math
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from fractions import Fraction

import numpy as np
from PIL import Image, ImageOps

from app import ocr, store
from app.layout import detect_page
from app.llm import TECHS, Claude, LLMConfig, LLMError

log = logging.getLogger("tab_trainer.pipeline")

CONCURRENCY = 4
LABEL_STRINGS = True  # 在裁剪图上标弦号（见 _label_strings）
OCR_CHECK = True  # 用本机数字识别校对 AI 读出的弦和品（见 app/ocr.py、ocr_disagreements）
PDF_DPI = 200
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
        raise ValueError(f"无法识别的音名：{name!r}")
    pc = _NOTE[m.group(1).upper()] + {"#": 1, "♯": 1, "b": -1, "♭": -1}.get(m.group(2), 0)
    if m.group(3) is not None:
        return (int(m.group(3)) + 1) * 12 + pc
    if near is None:
        raise ValueError(f"音名缺少八度：{name!r}")
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
            raise ValueError("调弦需要 6 根弦的音名")
        tuning = STANDARD
    midis = []
    for i, n in enumerate(tuning):
        std = note_to_midi(STANDARD[i])
        try:
            m = note_to_midi(str(n), near=std)
        except ValueError:
            if strict:
                raise ValueError(f"{i + 1} 弦的音名无法识别：{n!r}") from None
            m = std
        midis.append(m)
    ts = list(raw.get("time_signature") or [4, 4])
    ok_ts = len(ts) == 2 and all(isinstance(x, int) and not isinstance(x, bool) for x in ts)
    if not ok_ts or ts[0] <= 0 or ts[1] not in (1, 2, 4, 8, 16):
        if strict:
            raise ValueError("拍号格式不对，应写成 4/4、3/4、6/8 这样")
        ts = [4, 4]
    try:
        capo = int(raw.get("capo") or 0)
    except (TypeError, ValueError):
        if strict:
            raise ValueError("变调夹应为 0–12 的整数") from None
        capo = 0
    if strict and not 0 <= capo <= 12:
        raise ValueError("变调夹应为 0–12 的整数")
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


def bar_length(meta: dict) -> Fraction:
    n, d = meta["time_signature"]
    return Fraction(n * 4, d)


def parse_dur(s) -> Fraction:
    return Fraction(str(s).strip())


def clean_events(events) -> list[dict]:
    """把模型或用户提交的事件整理成统一结构。格式不对一律抛 ValueError（带中文说明）。"""
    if not isinstance(events, list):
        raise ValueError("events 必须是列表")  # noqa: TRY004 - 约定：用户输入的格式问题统一用 ValueError，接口返回 400
    out = []
    for i, e in enumerate(events, 1):
        if not isinstance(e, dict) or "dur" not in e:
            raise ValueError(f"第 {i} 个事件缺少 dur")
        try:
            dur = parse_dur(e["dur"])
        except (ValueError, ZeroDivisionError, TypeError):
            raise ValueError(f"第 {i} 个事件的时值无法识别：{e['dur']!r}") from None
        if dur <= 0:
            raise ValueError(f"第 {i} 个事件的时值必须大于 0：{e['dur']!r}")
        notes = []
        for n in e.get("notes") or []:
            try:
                st, fr = int(n["string"]), int(n["fret"])
                grace = int(n.get("grace", -1))
                bend = float(n.get("bend", 0) or 0)
                tech = [t for t in (n.get("tech") or []) if t in TECHS]
            except (KeyError, TypeError, ValueError, AttributeError):
                raise ValueError(f"第 {i} 个事件里有音符格式不对（需要 string、fret）") from None
            if not 1 <= st <= 6:
                raise ValueError(f"第 {i} 个事件：弦号 {st} 超出 1–6")
            if not 0 <= fr <= 30:
                raise ValueError(f"第 {i} 个事件：品数 {fr} 超出 0–30")
            if not math.isfinite(bend) or not 0 <= bend <= 4:
                raise ValueError(f"第 {i} 个事件：推弦幅度 {bend} 不合理（0–4 个半音）")
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


def rest_bar(meta: dict) -> list[dict]:
    """整小节休止：识别失败或被清空的小节用它占位，页面才能正常显示和播放。"""
    return [{"dur": str(bar_length(meta)), "notes": []}]


def new_bar_id() -> str:
    return uuid.uuid4().hex[:10]


GRID = Fraction(1, 48)  # 播放器能精确写出的最小时值单位（四分音符的 1/48：64 分音符和 64 分三连音的公约数）


def check_bar(events: list[dict], meta: dict) -> list[str]:
    problems = []
    total = sum((parse_dur(e["dur"]) for e in events), Fraction(0))
    want = bar_length(meta)
    if total != want:
        problems.append(f"时值合计 {float(total):g} 拍，应为 {float(want):g} 拍")
    odd = sorted({e["dur"] for e in events if (parse_dur(e["dur"]) / GRID).denominator != 1}, key=parse_dur)
    if odd:
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


# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------
def _save_pages(uploads: list[tuple[str, bytes]], pages_dir) -> list[dict]:
    pages_dir.mkdir(parents=True, exist_ok=True)
    images: list[Image.Image] = []
    for name, data in uploads:
        if name.lower().endswith(".pdf") or data[:5] == b"%PDF-":
            import fitz  # pymupdf

            with fitz.open(stream=data, filetype="pdf") as doc:
                for page in doc:
                    pix = page.get_pixmap(dpi=PDF_DPI)
                    images.append(Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB"))
        else:
            img = Image.open(io.BytesIO(data))
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", img.size, "white")
                bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
                img = bg
            images.append(img.convert("RGB"))
    pages = []
    for i, img in enumerate(images):
        fn = f"p{i + 1}.png"
        img.save(pages_dir / fn, optimize=True)
        pages.append({"file": fn, "width": img.width, "height": img.height})
    return pages


SCORE_EXT = (".gp", ".gpx", ".gp3", ".gp4", ".gp5", ".musicxml", ".xml", ".mxl")


def _score_ext(name: str) -> str | None:
    low = name.lower()
    return next((e for e in SCORE_EXT if low.endswith(e)), None)


def create_song(uploads: list[tuple[str, bytes]], title: str = "") -> str:
    if not uploads:
        raise ValueError("没有上传文件")
    if any(_score_ext(n) for n, _ in uploads):
        return _create_score_song(uploads, title)
    sid = store.new_id()
    d = store.song_dir(sid)
    try:
        pages = _save_pages(uploads, d / "pages")
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise
    if not pages:
        shutil.rmtree(d, ignore_errors=True)
        raise ValueError("文件里没有可用的页面")
    store.save(
        {
            "id": sid,
            "title": title.strip(),
            "created": time.strftime("%Y-%m-%d %H:%M"),
            "status": "queued",
            "progress": {"stage": "排队中", "done": 0, "total": 0},
            "error": None,
            "pages": pages,
            "meta": None,
            "systems": [],
            "bars": [],
            "plan": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    )
    start_parse(sid)
    return sid


def _looks_like_score(ext: str, data: bytes) -> bool:
    """按文件头粗查一下，挡住损坏的文件和改了扩展名的其他文件（真正的解析在浏览器里用 alphaTab 做）。"""
    head = data[:4096]
    zipped = head.startswith(b"PK\x03\x04")
    if ext in (".gp3", ".gp4", ".gp5"):
        return b"FICHIER GUITAR PRO" in head[:64]
    if ext == ".gpx":
        return head[:4] in (b"BCFS", b"BCFZ")
    if ext == ".gp":  # Guitar Pro 7/8 是 zip；也有人把 gp5 改成 .gp
        return zipped or b"FICHIER GUITAR PRO" in head[:64]
    if ext == ".mxl":
        return zipped
    text = head.decode("utf-8", "ignore").lower()
    return "<score-partwise" in text or "<score-timewise" in text


def import_failed(sid: str, error: str) -> dict:
    """浏览器解析 GP / MusicXML 失败：记到曲库里，不再显示成“可以练习”。"""

    def apply(s):
        if s.get("kind") != "score":
            raise ValueError("只有 Guitar Pro / MusicXML 曲子会这样报错")
        s.update(status="error", error=(str(error or "").strip() or "曲谱文件无法读取")[:300])

    return store.update(sid, apply)


def _create_score_song(uploads: list[tuple[str, bytes]], title: str) -> str:
    """Guitar Pro / MusicXML：本来就是精确数据，不用 AI 识别。原文件保存下来，
    练习页用 alphaTab 打开后把小节数据回传（set_derived）。"""
    if len(uploads) != 1:
        raise ValueError("Guitar Pro / MusicXML 文件一次只能上传一个，也不能和图片混在一起")
    name, data = uploads[0]
    ext = _score_ext(name)
    if not _looks_like_score(ext, data):
        raise ValueError(
            f"“{name.rsplit('/', 1)[-1]}”不是有效的 Guitar Pro / MusicXML 文件（文件内容和扩展名对不上），可能已损坏或只是改了扩展名"
        )
    sid = store.new_id()
    d = store.song_dir(sid)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"source{ext}").write_bytes(data)
    stem = name.rsplit("/", 1)[-1][: -len(ext)]
    store.save(
        {
            "id": sid,
            "kind": "score",
            "source": f"source{ext}",
            "title": title.strip() or stem,
            "created": time.strftime("%Y-%m-%d %H:%M"),
            "status": "imported",  # 文件在浏览器里第一次成功解析后才变成 ready（见 set_derived / import_failed）
            "progress": None,
            "error": None,
            "pages": [],
            "meta": None,
            "systems": [],
            "bars": [],
            "plan": None,
            "track": 0,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    )
    return sid


def set_derived(sid: str, meta_raw: dict, bars_raw: list, track: int = 0) -> dict:
    """保存练习页从 GTP / MusicXML 解析出的调弦和小节（换音轨时也走这里，会清掉旧的讲解）。"""

    def apply(s):
        if s.get("kind") != "score":
            raise ValueError("只有 Guitar Pro / MusicXML 曲子可以这样保存")
        meta = normalize_meta(meta_raw or {}, s.get("title", ""))
        bars = []
        for i, b in enumerate(bars_raw or []):
            try:
                events = clean_events((b or {}).get("events", []))
                warn = check_bar(events, meta) if events else []
            except ValueError as e:
                events, warn = [], [f"导入时这一小节的数据无法使用：{e}"]
            bars.append(
                {
                    "id": new_bar_id(),
                    "n": i + 1,
                    "system": i // 4,  # 没有原谱图片，按每 4 小节一段
                    "page": None,
                    "region": None,
                    "events": events or rest_bar(meta),
                    "tip": "",
                    "uncertain": "",
                    "warn": warn,
                    **clean_repeats(b or {}),
                }
            )
        s.update(meta=meta, bars=bars, track=int(track or 0), plan=None, status="ready", error=None)
        _prune_practice(s)

    return store.update(sid, apply)


# ---------------------------------------------------------------------------
# 谱面变了以后，AI 写的内容和练习记录的失效处理
# ---------------------------------------------------------------------------
def _invalidate_bar(bar: dict) -> None:
    """这个小节的音变了：提示和不确定说明是按旧音写的，清掉；问答保留但标成“改谱之前的”。"""
    bar["tip"] = ""
    bar["uncertain"] = ""
    for q in bar.get("qa") or []:
        q["stale"] = True


def _invalidate_plan(s: dict) -> None:
    if s.get("plan"):
        s["plan"]["stale"] = True


def _prune_practice(s: dict) -> None:
    """小节被重新识别 / 换了音轨以后 id 都变了：丢掉对不上的练习记录（累计总时长保留）。"""
    p = s.get("practice")
    if not p:
        return
    ids = {b.get("id") for b in s.get("bars", [])}
    p["bars"] = {k: v for k, v in (p.get("bars") or {}).items() if k in ids}
    last = p.get("last")
    if last and (last.get("a") not in ids or last.get("b") not in ids):
        p.pop("last", None)


# ---------------------------------------------------------------------------
# 识别
# ---------------------------------------------------------------------------
class Cancelled(Exception):
    pass


class Jobs:
    """每首曲子同一时间只允许一个识别任务（整首或单行）。登记、取消、查询都在一把锁里。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._active: dict[str, threading.Event] = {}

    def claim(self, sid: str) -> threading.Event | None:
        with self._lock:
            if sid in self._active:
                return None
            ev = self._active[sid] = threading.Event()
            return ev

    def release(self, sid: str) -> None:
        with self._lock:
            self._active.pop(sid, None)

    def running(self, sid: str) -> bool:
        with self._lock:
            return sid in self._active

    def cancel(self, sid: str) -> bool:
        with self._lock:
            ev = self._active.get(sid)
        if ev:
            ev.set()
        return ev is not None


jobs = Jobs()


def llm_config() -> LLMConfig:
    c = store.load_config()
    return LLMConfig(
        api_key=c.get("api_key", ""),
        base_url=c.get("base_url", ""),
        model=c.get("model") or LLMConfig.model,
        effort=c.get("effort") or LLMConfig.effort,
    )


def check_llm_ready() -> None:
    """发请求前先确认 key 配好了，免得任务先显示“进行中”过一会儿才失败。"""
    try:
        Claude(llm_config())
    except LLMError as e:
        raise ValueError(str(e)) from None


def start_parse(sid: str) -> bool:
    cancel = jobs.claim(sid)
    if cancel is None:
        return False
    store.update(
        sid,
        lambda s: s.update(status="queued", error=None, progress={"stage": "排队中", "done": 0, "total": 0}),
    )
    threading.Thread(target=_run_parse_safe, args=(sid, cancel), daemon=True).start()
    return True


def is_running(sid: str) -> bool:
    return jobs.running(sid)


def cancel_parse(sid: str) -> bool:
    return jobs.cancel(sid)


def ensure_bar_ids(song: dict) -> None:
    for b in song.get("bars", []):
        if not b.get("id"):
            b["id"] = new_bar_id()


def recover_interrupted() -> None:
    """服务启动时调用：上次没跑完的识别（服务被关掉）标成中断，否则曲子会一直卡在“识别中”。
    同时给老数据里没有 id 的小节补上 id。"""
    if not store.SONGS.exists():
        return
    for d in store.SONGS.iterdir():
        try:
            song = store.load(d.name)
        except (KeyError, ValueError):
            continue
        changed = False
        if song.get("status") in ("parsing", "queued"):
            if song.get("kind") == "score":
                song["status"], song["error"], song["progress"] = (
                    "ready",
                    "服务重启，讲解生成中断，可以重新生成。",
                    None,
                )
            else:
                song["status"] = "error"
                song["error"] = "服务重启，识别中断。可以在曲库的“…”菜单里重新识别。"
            changed = True
        for s in song.get("systems", []):
            if s.get("status") in ("parsing", "pending"):
                s.update(status="error", error="服务重启，识别中断")
                changed = True
        if any(not b.get("id") for b in song.get("bars", [])):
            ensure_bar_ids(song)
            changed = True
        if changed:
            store.save(song)


def _page_image(sid: str, page: dict) -> Image.Image:
    return Image.open(store.song_dir(sid) / "pages" / page["file"]).convert("RGB")


def _plan_systems(sid: str, song: dict) -> list[dict]:
    systems = []
    for pi, page in enumerate(song["pages"]):
        lay = detect_page(_page_image(sid, page))
        if not lay["systems"]:
            systems.append(
                {
                    "page": pi,
                    "crop": [0, page["width"], 0, page["height"]],
                    "spans": None,
                    "status": "pending",
                }
            )
            continue
        for s in lay["systems"]:
            pad = 12
            x0 = max(0, s["x0"] - 70)  # 带上行首的谱号 / 小节号
            crop = [x0, min(page["width"], s["x1"] + pad), s["top"], s["bottom"]]
            systems.append(
                {"page": pi, "crop": crop, "spans": s["bars"], "lines": s["lines"], "status": "pending"}
            )
    return systems


def _tab_lines(sid: str, song: dict, sysinfo: dict) -> list[float] | None:
    """这一行六线谱 6 根弦线在裁剪图里的纵坐标。旧曲子没存，就对整页重新检测，取落在裁剪范围里的那一行。"""
    x0, x1, y0, y1 = sysinfo["crop"]
    lines = sysinfo.get("lines")
    if not lines:
        page = song["pages"][sysinfo["page"]]
        for s in detect_page(_page_image(sid, page))["systems"]:
            if y0 <= s["lines"][0] and s["lines"][-1] <= y1 and s["x0"] < x1 and x0 < s["x1"]:
                lines = s["lines"]
                break
    return [y - y0 for y in lines] if lines else None


def _label_strings(img: Image.Image, lines: list[float], x_start: float) -> Image.Image:
    """在六线谱左边每根弦线旁标上红色的弦号 1–6。模型数线时容易把低音弦的数字往下错一根，
    直接看图对照弦号比数线可靠（只告诉它坐标没有用，评测里错误照旧）。"""
    from PIL import ImageDraw, ImageFont

    img = img.convert("RGB").copy()
    sp = (lines[-1] - lines[0]) / 5
    size = max(9, round(sp * 0.85))
    try:
        font = ImageFont.load_default(size=size)
    except TypeError:  # 老版本 Pillow
        font = ImageFont.load_default()
    d = ImageDraw.Draw(img)
    x = max(1, x_start - size * 1.6)
    for i, y in enumerate(lines):
        d.text((x, y), str(i + 1), fill=(220, 30, 30), font=font, anchor="lm")
    return img


def _note_names(bag) -> str:
    return "、".join(f"{s}弦{f}品" + (f"×{n}" if n > 1 else "") for (s, f), n in sorted(bag.items()))


def ocr_disagreements(bars: list[dict], ocr_bars: list[list[tuple[int, int]]]) -> dict[int, tuple]:
    """逐小节比较 AI 结果和本机数字识别读到的（弦, 品）。返回 {小节序号: (只有 OCR 读到的, 只有 AI 写了的)}。

    - OCR 在这个小节什么都没读到：不判断（可能是没读出来，不代表没有音）；
    - 死音（谱上写 x）OCR 不读，不比较；倚音的小数字 OCR 会读到，算进去；
    - 人工泛音旁边标的“品位 + 12”（如 2<14>）OCR 会多读一个 14，允许；
    - 整行一致率不到一半：多半是 OCR 在这种排版上读不准，整行都不判断。
    """
    from collections import Counter

    pairs = []
    for i, (bar, seen) in enumerate(zip(bars, ocr_bars, strict=False)):
        ai: Counter = Counter()
        harmonic_marks: Counter = Counter()
        for e in bar["events"]:
            for n in e["notes"]:
                if "x" in n["tech"]:
                    continue
                ai[(n["string"], n["fret"])] += 1
                if n.get("grace", -1) >= 0:
                    ai[(n["string"], n["grace"])] += 1
                if "ah" in n["tech"]:
                    harmonic_marks[(n["string"], n["fret"] + 12)] += 1
        got = Counter(seen) - harmonic_marks
        # 两位数的两个数字隔得稍远时 OCR 会读成两个音（11 → 1、1）：AI 写的两位数能由这几位拼出来就算一致
        for (st, f), k in list((ai - got).items()):
            parts = Counter((st, int(d)) for d in str(f)) if f >= 10 else None
            for _ in range(k):
                if parts and not (parts - (got - ai)):
                    got -= parts
                    got[(st, f)] += 1
        if got:
            pairs.append((i, ai, got))
    agree = sum(sum((ai & got).values()) for _, ai, got in pairs)
    total = sum(max(sum(ai.values()), sum(got.values())) for _, ai, got in pairs)
    if not total or agree / total < 0.5:
        return {}
    return {i: (got - ai, ai - got) for i, ai, got in pairs if got != ai}


def _transcribe_system(
    claude: Claude,
    sid: str,
    song: dict,
    si: int,
    first_bar: int,
    hint: str = "",
    cancel: threading.Event | None = None,
) -> list[dict]:
    sysinfo = song["systems"][si]
    page = song["pages"][sysinfo["page"]]
    x0, x1, y0, y1 = sysinfo["crop"]
    img = _page_image(sid, page).crop((x0, y0, x1, y1))
    spans = [(a - x0, b - x0) for a, b in sysinfo["spans"]] if sysinfo["spans"] else None
    lines = _tab_lines(sid, song, sysinfo) if spans else None
    ocr_bars = None
    if spans and lines and OCR_CHECK:
        try:
            ocr_bars = ocr.read_system(np.asarray(img.convert("L")), lines, spans)
        except Exception as e:  # noqa: BLE001 - 校对只是锦上添花，出错就跳过
            log.warning("request_id=none song=%s system=%d ocr failed: %s", sid, si, e)
    labeled = bool(lines and LABEL_STRINGS)
    if labeled:
        img = _label_strings(img, lines, spans[0][0])
    meta = song["meta"]

    def run(previous=None, problems=None):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        raw = claude.transcribe(
            img,
            meta,
            first_bar,
            spans,
            hint=hint,
            previous=previous,
            problems=problems,
            labeled_strings=labeled,
        )
        bars, issues = [], []
        for i, b in enumerate(raw.get("bars", [])):
            try:
                events = clean_events(b.get("events", []))
            except ValueError as e:
                events = []
                issues.append(f"第 {first_bar + i} 小节数据格式错误：{e}")
            probs = check_bar(events, meta)
            issues += [f"第 {first_bar + i} 小节：{p}" for p in probs]
            if not events:
                events = rest_bar(meta)
                probs = ["没有识别出可用的音符，暂按整小节休止处理"]
            bars.append(
                {
                    "events": events,
                    "tip": b.get("tip", ""),
                    "uncertain": b.get("uncertain", ""),
                    "warn": probs,
                    **clean_repeats(b),
                }
            )
        if spans and len(bars) != len(spans):
            issues.append(f"应有 {len(spans)} 个小节，返回了 {len(bars)} 个")
        elif ocr_bars:
            for i, (only_ocr, only_ai) in ocr_disagreements(bars, ocr_bars).items():
                parts = []
                if only_ocr:
                    parts.append(f"本地数字识别在图上读到 {_note_names(only_ocr)}，你的结果里没有")
                if only_ai:
                    parts.append(f"你写的 {_note_names(only_ai)} 它没读到")
                issues.append(
                    f"第 {first_bar + i} 小节："
                    + "；".join(parts)
                    + "（它只读品位数字，也可能读错：请对照图片核对这几个位置，图上确实如此再改）"
                )
                bars[i]["_ocr"] = (only_ocr, only_ai)
        return raw, bars, issues

    raw, bars, issues = run()
    if issues:
        log.info("request_id=unknown song=%s system=%d retry: %s", sid, si, issues)
        _raw2, bars2, issues2 = run(previous=raw, problems=issues)
        if len(issues2) <= len(issues):
            bars, issues = bars2, issues2

    out = []
    for b in bars:
        only = b.pop("_ocr", None)
        if only:
            only_ocr, only_ai = only
            msg = "本地数字识别和 AI 结果不一致"
            if only_ocr:
                msg += f"：谱上读到 {_note_names(only_ocr)}"
            if only_ai:
                msg += f"{'，' if only_ocr else '：'}AI 写的是 {_note_names(only_ai)}"
            b["warn"] = [*b["warn"], msg + "。请对照原谱核对"]
    for i, b in enumerate(bars):
        if spans and i < len(spans):
            a, c = sysinfo["spans"][i]
            region = [max(0, a - 8), min(page["width"], c + 8), y0, y1]
        else:
            region = [x0, x1, y0, y1]
        out.append({"id": new_bar_id(), "system": si, "page": sysinfo["page"], "region": region, **b})
    return out


def _renumber(song: dict) -> None:
    song["bars"].sort(key=lambda b: (b["system"], b.get("_order", 0)))
    for i, b in enumerate(song["bars"]):
        b["n"] = i + 1
        b.pop("_order", None)


def _set(sid: str, **kw) -> None:
    store.update(sid, lambda s: s.update(kw))


def _run_parse_safe(sid: str, cancel: threading.Event) -> None:
    try:
        _run_parse(sid, cancel)
    except Cancelled:
        _set(sid, status="cancelled", error="识别已取消，已完成的行会保留。")
    except Exception as e:
        log.exception("request_id=unknown song=%s parse failed", sid)
        _set(sid, status="error", error=_friendly(e))
    finally:
        jobs.release(sid)


def _friendly(e: Exception) -> str:
    import anthropic

    if isinstance(e, anthropic.AuthenticationError):
        return "API key 无效，请在首页“模型设置”里检查。"
    if isinstance(e, anthropic.PermissionDeniedError):
        return "API key 没有调用该模型的权限。"
    if isinstance(e, anthropic.NotFoundError):
        return "模型不存在或无权访问，请检查“模型设置”里的模型名。"
    if isinstance(e, anthropic.RateLimitError):
        return "调用太频繁或额度不足（429），稍后再试。"
    if isinstance(e, anthropic.APIConnectionError):
        return "连不上模型服务，请检查网络或 Base URL。"
    if isinstance(e, anthropic.APIStatusError):
        return f"模型服务返回错误 {e.status_code}：{e.message}"
    if isinstance(e, LLMError):
        return str(e)
    return f"{type(e).__name__}: {e}"


def _run_parse(sid: str, cancel: threading.Event | None = None) -> None:
    cancel = cancel or threading.Event()
    claude = Claude(llm_config())
    song = store.load(sid)
    _set(sid, status="parsing", error=None, progress={"stage": "读取谱头", "done": 0, "total": 0})
    meta = normalize_meta(claude.read_meta(_page_image(sid, song["pages"][0])), song.get("title", ""))
    _set(sid, meta=meta, progress={"stage": "分析版面", "done": 0, "total": 0})

    if cancel.is_set():
        raise Cancelled()
    song = store.load(sid)
    systems = _plan_systems(sid, song)
    _set(sid, systems=systems, bars=[], progress={"stage": "逐行识别", "done": 0, "total": len(systems)})
    song = store.load(sid)

    # 先按检测到的小节数预估每行的起始小节号；整页识别的页面数量未知，最后统一重新编号
    starts, n = [], 1
    for s in systems:
        starts.append(n)
        n += len(s["spans"]) if s["spans"] else 8

    errors = []
    with ThreadPoolExecutor(CONCURRENCY) as pool:
        futs = {
            pool.submit(_transcribe_system, claude, sid, song, i, starts[i], "", cancel): i
            for i in range(len(systems))
        }
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                bars = fut.result()
                status, err = "done", None
            except (Cancelled, CancelledError):
                bars, status, err = [], "error", "已取消"
            except Exception as e:
                log.exception("request_id=unknown song=%s system=%d failed", sid, i)
                bars, status, err = [], "error", _friendly(e)
                errors.append(f"第 {i + 1} 行：{err}")

            def apply(s, i=i, bars=bars, status=status, err=err):
                for k, b in enumerate(bars):
                    b["_order"] = k
                s["bars"] = [b for b in s["bars"] if b["system"] != i] + bars
                _renumber(s)
                s["systems"][i].update(status=status, error=err)
                s["progress"]["done"] += 1
                s["usage"] = dict(claude.usage)
                _prune_practice(s)

            store.update(sid, apply)
            if cancel.is_set():
                for f in futs:
                    f.cancel()  # 还没开始的行不再发请求；正在进行的请求会跑完

    if cancel.is_set():
        raise Cancelled()
    song = store.load(sid)
    if not song["bars"]:
        _set(sid, status="error", error="；".join(errors) or "没有识别出任何小节（可能不是六线谱）")
        return

    _set(sid, progress={"stage": "生成练习建议", "done": len(systems), "total": len(systems)})
    try:
        plan = claude.practice_plan(song["meta"], song["bars"])
    except Exception as e:  # noqa: BLE001 - 练习建议失败不影响使用
        log.warning("request_id=unknown song=%s practice plan failed: %s", sid, e)
        plan = None
    _set(
        sid,
        plan=plan,
        status="ready",
        error="；".join(errors) or None,
        usage=dict(claude.usage),
        progress={"stage": "完成", "done": len(systems), "total": len(systems)},
    )


# ---------------------------------------------------------------------------
# 重新识别某一行 / 手工修改
# ---------------------------------------------------------------------------
EXPLAIN_CHUNK = 16


def start_explain(sid: str) -> None:
    """补写缺失的小节提示，再重新生成整曲练习建议（只发文字，费用很低）。
    GP / MusicXML 导入的曲子一开始都没有提示；识别出来的曲子改过音、改过调弦之后，对应的提示会被清掉。"""
    song = store.load(sid)
    if not song.get("bars"):
        raise ValueError("曲子还没有小节数据，请先打开练习页让它加载一次")
    todo = [b["id"] for b in song["bars"] if not (b.get("tip") or "").strip()]
    plan = song.get("plan")
    if not todo and plan and not plan.get("stale"):
        raise ValueError("每个小节都已经有提示，练习建议也是最新的")
    check_llm_ready()
    cancel = jobs.claim(sid)
    if cancel is None:
        raise RuntimeError("这首曲子正在处理中")
    total = (len(todo) + EXPLAIN_CHUNK - 1) // EXPLAIN_CHUNK
    _set(sid, status="parsing", error=None, progress={"stage": "生成讲解", "done": 0, "total": total})

    def add_usage(s, claude, before):
        s["usage"] = {
            k: s.get("usage", {}).get(k, 0) + claude.usage[k] - before[k]
            for k in ("input_tokens", "output_tokens")
        }

    def run():
        try:
            claude = Claude(llm_config())
            for k in range(total):
                if cancel.is_set():
                    raise Cancelled()
                cur = store.load(sid)
                ids = set(todo[k * EXPLAIN_CHUNK : (k + 1) * EXPLAIN_CHUNK])
                chunk = [b for b in cur["bars"] if b.get("id") in ids]
                if not chunk:
                    continue
                before = dict(claude.usage)
                tips = {
                    t["number"]: t["tip"] for t in claude.explain_bars(cur["meta"], chunk).get("bars", [])
                }

                def apply(s, tips=tips, k=k, ids=ids, before=before):
                    for b in s["bars"]:
                        if b.get("id") in ids and b["n"] in tips and not (b.get("tip") or "").strip():
                            b["tip"] = tips[b["n"]]
                    s["progress"] = {"stage": "生成讲解", "done": k + 1, "total": total}
                    add_usage(s, claude, before)

                store.update(sid, apply)
            cur = store.load(sid)
            before = dict(claude.usage)
            new_plan = claude.practice_plan(cur["meta"], cur["bars"])

            def finish(s):
                s.update(plan=new_plan, status="ready", progress=None)
                add_usage(s, claude, before)

            store.update(sid, finish)
        except Cancelled:
            _set(sid, status="ready", progress=None, error="讲解生成已取消，已完成的部分会保留。")
        except Exception as e:
            log.exception("request_id=unknown song=%s explain failed", sid)
            _set(sid, status="ready", progress=None, error="生成讲解失败：" + _friendly(e))
        finally:
            jobs.release(sid)

    threading.Thread(target=run, daemon=True).start()


def start_reparse(sid: str, si: int, hint: str = "") -> None:
    """在请求里就登记任务（而不是等后台线程），连点两次或和整首识别撞上时直接拒绝。"""
    check_llm_ready()
    cancel = jobs.claim(sid)
    if cancel is None:
        raise RuntimeError("这首曲子正在识别中")
    try:
        store.update(sid, lambda x: x["systems"][si].update(status="parsing", error=None))
    except Exception:
        jobs.release(sid)
        raise

    def run():
        try:
            reparse_system(sid, si, hint, cancel)
        except Exception as e:
            log.exception("request_id=unknown song=%s system=%d reparse failed", sid, si)
            msg = "已取消" if isinstance(e, Cancelled) else _friendly(e)
            store.update(sid, lambda x: x["systems"][si].update(status="error", error=msg))
        finally:
            jobs.release(sid)

    threading.Thread(target=run, daemon=True).start()


def reparse_system(sid: str, si: int, hint: str = "", cancel: threading.Event | None = None) -> None:
    claude = Claude(llm_config())
    song = store.load(sid)
    # 起始小节号：前面各行最后一个小节 + 1（这一行之前识别失败、没有小节时也对）
    first = max((b["n"] for b in song["bars"] if b["system"] < si), default=0) + 1
    bars = _transcribe_system(claude, sid, song, si, first, hint=hint, cancel=cancel)

    def apply(s):
        for k, b in enumerate(bars):
            b["_order"] = k
        s["bars"] = [b for b in s["bars"] if b["system"] != si] + bars
        _renumber(s)
        s["systems"][si].update(status="done", error=None)
        _invalidate_plan(s)
        _prune_practice(s)
        s["usage"] = {
            k: s.get("usage", {}).get(k, 0) + claude.usage[k] for k in ("input_tokens", "output_tokens")
        }

    store.update(sid, apply)


class BarNotFound(LookupError):
    pass


def edit_bar(sid: str, bar_id: str, events, tip: str | None) -> dict:
    """按小节的稳定 id 修改（编号在识别过程中会变，按编号改可能改到别的小节）。"""
    cleaned = clean_events(events)

    def apply(s):
        bar = next((b for b in s["bars"] if b.get("id") == bar_id), None)
        if bar is None:
            raise BarNotFound(bar_id)
        new = cleaned or rest_bar(s["meta"])
        if new != bar["events"]:
            _invalidate_bar(bar)
            _invalidate_plan(s)
        bar["events"] = new
        bar["warn"] = check_bar(bar["events"], s["meta"])
        bar["uncertain"] = ""
        if tip is not None:
            bar["tip"] = tip

    return store.update(sid, apply)


def edit_meta(sid: str, raw: dict) -> dict:
    def apply(s):
        old = s.get("meta") or {}
        merged = {**old, **raw}
        s["meta"] = normalize_meta(merged, strict=True)
        if any(s["meta"][k] != old.get(k) for k in ("tuning_midi", "capo", "time_signature")):
            for b in s["bars"]:  # 调弦 / 变调夹 / 拍号变了：所有提示都按旧设定写的
                _invalidate_bar(b)
            _invalidate_plan(s)
        if "title" in raw:
            s["title"] = str(raw["title"]).strip()
        for b in s["bars"]:
            b["warn"] = check_bar(b["events"], s["meta"])

    return store.update(sid, apply)


# ---------------------------------------------------------------------------
# 小节 AI 教练
# ---------------------------------------------------------------------------
QA_KEEP = 20  # 每个小节保留最近几条问答


class CoachError(RuntimeError):
    pass


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
        raise ValueError("问题不能为空")
    if len(question) > 500:
        raise ValueError("问题太长了（最多 500 字）")
    song = store.load(sid)
    idx = next((i for i, b in enumerate(song["bars"]) if b.get("id") == bar_id), None)
    if idx is None:
        raise BarNotFound(bar_id)
    bar = song["bars"][idx]
    neighbours = [song["bars"][j] for j in (idx - 1, idx + 1) if 0 <= j < len(song["bars"])]
    image = None
    if bar.get("region") and bar.get("page") is not None:
        x0, x1, y0, y1 = bar["region"]
        image = _page_image(sid, song["pages"][bar["page"]]).crop((x0, y0, x1, y1))
    try:
        claude = Claude(llm_config())
    except LLMError as e:
        raise ValueError(str(e)) from None  # 没配 key：用户能自己解决的问题，返回 400
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
    except LLMError as e:
        raise CoachError(str(e)) from None
    except Exception as e:  # noqa: BLE001 - 网络 / API 错误统一转成中文说明
        raise CoachError(_friendly(e)) from None
    if not answer.strip():
        raise CoachError("模型没有给出回答，换个问法再试一次")
    entry = {"q": question, "a": answer.strip(), "at": time.strftime("%Y-%m-%d %H:%M")}

    def apply(s):
        b = next((x for x in s["bars"] if x.get("id") == bar_id), None)
        if b is not None:  # 等回答期间小节被重新识别掉了，就不存
            b["qa"] = [*(b.get("qa") or []), entry][-QA_KEEP:]
        s["usage"] = {
            k: s.get("usage", {}).get(k, 0) + claude.usage[k] for k in ("input_tokens", "output_tokens")
        }

    s = store.update(sid, apply)
    b = next((x for x in s["bars"] if x.get("id") == bar_id), None)
    return {"answer": entry["a"], "qa": (b or {}).get("qa", [entry])}


# ---------------------------------------------------------------------------
# 练习记录
# ---------------------------------------------------------------------------
def record_practice(sid: str, body: dict) -> dict:
    """记一段练习：循环了哪几个小节（bar_ids，按顺序）、几遍、多少 BPM、多少秒。
    按小节累计到 practice.bars，最近一次记在 practice.last（下次打开时提示接着练）。"""
    try:
        ids = [str(x) for x in body.get("bar_ids") or []]
        loops = int(body.get("loops") or 0)
        bpm = int(body.get("bpm") or 0)
        seconds = float(body.get("seconds") or 0)
    except (TypeError, ValueError):
        raise ValueError("练习记录格式不对") from None
    if not ids or not 0 <= loops <= 10000 or not 0 < bpm <= 400 or not 0 <= seconds <= 6 * 3600:
        raise ValueError("练习记录格式不对")
    now = time.strftime("%Y-%m-%d %H:%M")

    def apply(s):
        known = {b.get("id") for b in s["bars"]}
        valid = list(dict.fromkeys(i for i in ids if i in known))  # 保持顺序去重
        if not valid:
            return
        p = s.setdefault("practice", {})
        bars = p.setdefault("bars", {})
        for i in valid:
            r = bars.setdefault(i, {"loops": 0, "seconds": 0, "best_bpm": 0, "last": ""})
            r["loops"] += loops
            r["seconds"] = round(r["seconds"] + seconds / len(valid), 1)  # 时间平摊到每个小节
            if loops:
                r["best_bpm"] = max(r["best_bpm"], bpm)
            r["last"] = now
        p["last"] = {"a": valid[0], "b": valid[-1], "bpm": bpm, "at": now}
        p["total_seconds"] = round(p.get("total_seconds", 0) + seconds, 1)

    return store.update(sid, apply).get("practice") or {}
