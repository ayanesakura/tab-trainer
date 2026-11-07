"""识别流程：保存上传的页面 → 读谱头 → 版面检测 → 逐行调用 Claude 识别 → 校验/返修 → 练习建议。"""

from __future__ import annotations

import io
import logging
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from fractions import Fraction

from PIL import Image, ImageOps

from app import store
from app.layout import detect_page
from app.llm import TECHS, Claude, LLMConfig, LLMError

log = logging.getLogger("tab_trainer.pipeline")

CONCURRENCY = 4
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


def normalize_meta(raw: dict, fallback_title: str = "") -> dict:
    tuning = list(raw.get("tuning") or [])
    if len(tuning) != 6:
        tuning = STANDARD
    midis = []
    for i, n in enumerate(tuning):
        std = note_to_midi(STANDARD[i])
        try:
            m = note_to_midi(n, near=std)
        except ValueError:
            m = std
        midis.append(m)
    ts = list(raw.get("time_signature") or [4, 4])
    if len(ts) != 2 or ts[0] <= 0 or ts[1] not in (1, 2, 4, 8, 16):
        ts = [4, 4]
    return {
        "title": (raw.get("title") or fallback_title or "").strip(),
        "artist": (raw.get("artist") or "").strip(),
        "arranger": (raw.get("arranger") or "").strip(),
        "tuning": [midi_to_name(m) for m in midis],
        "tuning_midi": midis,
        "capo": max(0, min(12, int(raw.get("capo") or 0))),
        "time_signature": ts,
        "tempo": max(0, int(raw.get("tempo") or 0)),
        "notes": (raw.get("notes") or "").strip(),
    }


def bar_length(meta: dict) -> Fraction:
    n, d = meta["time_signature"]
    return Fraction(n * 4, d)


def parse_dur(s) -> Fraction:
    return Fraction(str(s).strip())


def clean_events(events: list) -> list[dict]:
    """把模型或用户提交的事件整理成统一结构；结构不对直接抛 ValueError。"""
    out = []
    for e in events:
        dur = parse_dur(e["dur"])
        if dur <= 0:
            raise ValueError(f"时值必须大于 0：{e['dur']}")
        notes = []
        for n in e.get("notes", []):
            s, f = int(n["string"]), int(n["fret"])
            tech = [t for t in n.get("tech", []) if t in TECHS]
            notes.append(
                {
                    "string": s,
                    "fret": f,
                    "tech": tech,
                    "grace": int(n.get("grace", -1)),
                    "bend": float(n.get("bend", 0) or 0),
                }
            )
        out.append({"dur": str(dur), "notes": notes})
    return out


def check_bar(events: list[dict], meta: dict) -> list[str]:
    problems = []
    total = sum((parse_dur(e["dur"]) for e in events), Fraction(0))
    want = bar_length(meta)
    if total != want:
        problems.append(f"时值合计 {float(total):g} 拍，应为 {float(want):g} 拍")
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


def create_song(uploads: list[tuple[str, bytes]], title: str = "") -> str:
    if not uploads:
        raise ValueError("没有上传文件")
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


# ---------------------------------------------------------------------------
# 识别
# ---------------------------------------------------------------------------
_running: set[str] = set()
_running_lock = threading.Lock()


def llm_config() -> LLMConfig:
    c = store.load_config()
    return LLMConfig(
        api_key=c.get("api_key", ""),
        base_url=c.get("base_url", ""),
        model=c.get("model") or LLMConfig.model,
        effort=c.get("effort") or LLMConfig.effort,
    )


def start_parse(sid: str) -> bool:
    with _running_lock:
        if sid in _running:
            return False
        _running.add(sid)
    threading.Thread(target=_run_parse_safe, args=(sid,), daemon=True).start()
    return True


def is_running(sid: str) -> bool:
    return sid in _running


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
            systems.append({"page": pi, "crop": crop, "spans": s["bars"], "status": "pending"})
    return systems


def _transcribe_system(
    claude: Claude, sid: str, song: dict, si: int, first_bar: int, hint: str = ""
) -> list[dict]:
    sysinfo = song["systems"][si]
    page = song["pages"][sysinfo["page"]]
    x0, x1, y0, y1 = sysinfo["crop"]
    img = _page_image(sid, page).crop((x0, y0, x1, y1))
    spans = [(a - x0, b - x0) for a, b in sysinfo["spans"]] if sysinfo["spans"] else None
    meta = song["meta"]

    def run(previous=None, problems=None):
        raw = claude.transcribe(img, meta, first_bar, spans, hint=hint, previous=previous, problems=problems)
        bars, issues = [], []
        for i, b in enumerate(raw.get("bars", [])):
            try:
                events = clean_events(b.get("events", []))
            except (KeyError, ValueError, TypeError) as e:
                events = []
                issues.append(f"第 {first_bar + i} 小节数据格式错误：{e}")
            probs = check_bar(events, meta)
            issues += [f"第 {first_bar + i} 小节：{p}" for p in probs]
            bars.append(
                {
                    "events": events,
                    "tip": b.get("tip", ""),
                    "uncertain": b.get("uncertain", ""),
                    "warn": probs,
                }
            )
        if spans and len(bars) != len(spans):
            issues.append(f"应有 {len(spans)} 个小节，返回了 {len(bars)} 个")
        return raw, bars, issues

    raw, bars, issues = run()
    if issues:
        log.info("request_id=unknown song=%s system=%d retry: %s", sid, si, issues)
        _raw2, bars2, issues2 = run(previous=raw, problems=issues)
        if len(issues2) <= len(issues):
            bars, issues = bars2, issues2

    out = []
    for i, b in enumerate(bars):
        if spans and i < len(spans):
            a, c = sysinfo["spans"][i]
            region = [max(0, a - 8), min(page["width"], c + 8), y0, y1]
        else:
            region = [x0, x1, y0, y1]
        out.append({"system": si, "page": sysinfo["page"], "region": region, **b})
    return out


def _renumber(song: dict) -> None:
    song["bars"].sort(key=lambda b: (b["system"], b.get("_order", 0)))
    for i, b in enumerate(song["bars"]):
        b["n"] = i + 1
        b.pop("_order", None)


def _set(sid: str, **kw) -> None:
    store.update(sid, lambda s: s.update(kw))


def _run_parse_safe(sid: str) -> None:
    try:
        _run_parse(sid)
    except Exception as e:
        log.exception("request_id=unknown song=%s parse failed", sid)
        _set(sid, status="error", error=_friendly(e))
    finally:
        with _running_lock:
            _running.discard(sid)


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


def _run_parse(sid: str) -> None:
    claude = Claude(llm_config())
    song = store.load(sid)
    _set(sid, status="parsing", error=None, progress={"stage": "读取谱头", "done": 0, "total": 0})
    meta = normalize_meta(claude.read_meta(_page_image(sid, song["pages"][0])), song.get("title", ""))
    _set(sid, meta=meta, progress={"stage": "分析版面", "done": 0, "total": 0})

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
            pool.submit(_transcribe_system, claude, sid, song, i, starts[i]): i for i in range(len(systems))
        }
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                bars = fut.result()
                status, err = "done", None
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

            store.update(sid, apply)

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
def reparse_system(sid: str, si: int, hint: str = "") -> None:
    with _running_lock:
        if sid in _running:
            raise RuntimeError("这首曲子正在识别中")
        _running.add(sid)
    try:
        claude = Claude(llm_config())
        song = store.load(sid)
        first = min((b["n"] for b in song["bars"] if b["system"] == si), default=1)
        bars = _transcribe_system(claude, sid, song, si, first, hint=hint)

        def apply(s):
            for k, b in enumerate(bars):
                b["_order"] = k
            s["bars"] = [b for b in s["bars"] if b["system"] != si] + bars
            _renumber(s)
            s["systems"][si].update(status="done", error=None)
            s["usage"] = {
                k: s.get("usage", {}).get(k, 0) + claude.usage[k] for k in ("input_tokens", "output_tokens")
            }

        store.update(sid, apply)
    finally:
        with _running_lock:
            _running.discard(sid)


def edit_bar(sid: str, n: int, events: list, tip: str | None) -> dict:
    def apply(s):
        bar = next((b for b in s["bars"] if b["n"] == n), None)
        if bar is None:
            raise KeyError(n)
        bar["events"] = clean_events(events)
        bar["warn"] = check_bar(bar["events"], s["meta"])
        bar["uncertain"] = ""
        if tip is not None:
            bar["tip"] = tip

    return store.update(sid, apply)


def edit_meta(sid: str, raw: dict) -> dict:
    def apply(s):
        merged = {**(s.get("meta") or {}), **raw}
        s["meta"] = normalize_meta(merged)
        if "title" in raw:
            s["title"] = raw["title"].strip()
        for b in s["bars"]:
            b["warn"] = check_bar(b["events"], s["meta"])

    return store.update(sid, apply)
