"""曲库里的曲子：新建（图片 / PDF / Guitar Pro / MusicXML）、保存浏览器转换的小节、手工修改、练习记录、启动恢复。

新建图片曲子只保存页面，识别由调用方另外启动（recognize.start_parse）。
"""

from __future__ import annotations

import io
import logging
import shutil
import time

from PIL import Image, ImageOps

from app import staleness, store
from app.errors import BarNotFound, Busy, Invalid
from app.music import (
    apply_time_signatures,
    check_bar,
    clean_events,
    clean_repeats,
    clean_time_signature,
    new_bar_id,
    normalize_meta,
    rest_bar,
)

log = logging.getLogger("tab_trainer.songs")

PDF_DPI = 200
# 上传上限：防止一个超大文件把内存、CPU 和磁盘吃光（开了局域网访问时其他设备也能上传）
MAX_FILES = 40
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_PAGES = 60  # 一首曲子最多几页（PDF 的页数也算在内）
MAX_PIXELS = 60_000_000  # 一页最多多少像素（微信长截图 1080×20000 约 2200 万）
SCORE_EXT = (".gp", ".gpx", ".gp3", ".gp4", ".gp5", ".musicxml", ".xml", ".mxl")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def _new_song(sid: str, **fields) -> dict:
    return {
        "id": sid,
        "title": "",
        "created": _now(),
        "status": "queued",
        "progress": None,
        "error": None,
        "pages": [],
        "meta": None,
        "systems": [],
        "bars": [],
        "plan": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        **fields,
    }


def find_bar(song: dict, bar_id: str) -> dict:
    bar = next((b for b in song["bars"] if b.get("id") == bar_id), None)
    if bar is None:
        raise BarNotFound(bar_id)
    return bar


def is_score(song: dict) -> bool:
    """Guitar Pro / MusicXML 导入的曲子（小节数据由浏览器转换，不需要 AI 识别）。"""
    return song.get("kind") == "score"


# ---------------------------------------------------------------------------
# 新建
# ---------------------------------------------------------------------------
def create_song(uploads: list[tuple[str, bytes]], title: str = "") -> str:
    """保存上传的文件，返回曲子 id。图片 / PDF 曲子状态为 queued，等调用方启动识别。"""
    check_upload_sizes(uploads)
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
        raise Invalid("文件里没有可用的页面")
    store.save(
        _new_song(
            sid,
            title=title.strip(),
            progress={"stage": "排队中", "done": 0, "total": 0},
            pages=pages,
        )
    )
    return sid


def check_upload_sizes(uploads: list[tuple[str, bytes]]) -> None:
    if not uploads:
        raise Invalid("没有收到文件")
    if len(uploads) > MAX_FILES:
        raise Invalid(f"一次最多上传 {MAX_FILES} 个文件")
    for name, data in uploads:
        if len(data) > MAX_FILE_BYTES:
            raise Invalid(f"“{name}”太大了（单个文件最大 {MAX_FILE_BYTES // 1024 // 1024} MB）")
    if sum(len(d) for _, d in uploads) > MAX_TOTAL_BYTES:
        raise Invalid(f"文件总大小超过 {MAX_TOTAL_BYTES // 1024 // 1024} MB")


def _page_images(uploads: list[tuple[str, bytes]]):
    """逐页产出 RGB 图片（PDF 一页一页渲染，不把整份文件的页面都留在内存里）。"""
    count = 0

    def one_more():
        nonlocal count
        count += 1
        if count > MAX_PAGES:
            raise Invalid(f"一首曲子最多 {MAX_PAGES} 页")

    for name, data in uploads:
        if name.lower().endswith(".pdf") or data[:5] == b"%PDF-":
            import fitz  # pymupdf

            with fitz.open(stream=data, filetype="pdf") as doc:
                if count + len(doc) > MAX_PAGES:
                    raise Invalid(f"一首曲子最多 {MAX_PAGES} 页（“{name}”有 {len(doc)} 页）")
                for page in doc:
                    one_more()
                    # 页面特别大（海报尺寸之类）时降低分辨率，免得渲染出几亿像素
                    w_in, h_in = page.rect.width / 72, page.rect.height / 72
                    dpi = min(PDF_DPI, int((MAX_PIXELS / max(1e-6, w_in * h_in)) ** 0.5))
                    if dpi < 30:
                        raise Invalid(f"“{name}”的页面尺寸太大")
                    pix = page.get_pixmap(dpi=dpi)
                    yield Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        else:
            one_more()
            img = Image.open(io.BytesIO(data))  # 只读文件头，还没解码像素
            if img.width * img.height > MAX_PIXELS:
                raise Invalid(f"“{name}”分辨率太大（{img.width}×{img.height}），请缩小后再上传")
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", img.size, "white")
                bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
                img = bg
            yield img.convert("RGB")


def _save_pages(uploads: list[tuple[str, bytes]], pages_dir) -> list[dict]:
    pages_dir.mkdir(parents=True, exist_ok=True)
    pages = []
    for i, img in enumerate(_page_images(uploads)):
        fn = f"p{i + 1}.png"
        img.save(pages_dir / fn, optimize=True)
        pages.append({"file": fn, "width": img.width, "height": img.height})
    return pages


def _score_ext(name: str) -> str | None:
    low = name.lower()
    return next((e for e in SCORE_EXT if low.endswith(e)), None)


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


def _create_score_song(uploads: list[tuple[str, bytes]], title: str) -> str:
    """Guitar Pro / MusicXML：本来就是精确数据，不用 AI 识别。原文件保存下来，
    练习页用 alphaTab 打开后把小节数据回传（set_derived）。"""
    if len(uploads) != 1:
        raise Invalid("Guitar Pro / MusicXML 文件一次只能上传一个，也不能和图片混在一起")
    name, data = uploads[0]
    ext = _score_ext(name)
    if not _looks_like_score(ext, data):
        raise Invalid(
            f"“{name.rsplit('/', 1)[-1]}”不是有效的 Guitar Pro / MusicXML 文件（文件内容和扩展名对不上），可能已损坏或只是改了扩展名"
        )
    sid = store.new_id()
    d = store.song_dir(sid)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"source{ext}").write_bytes(data)
    stem = name.rsplit("/", 1)[-1][: -len(ext)]
    store.save(
        _new_song(
            sid,
            kind="score",
            source=f"source{ext}",
            title=title.strip() or stem,
            status="imported",  # 文件在浏览器里第一次成功解析后才变成 ready（见 set_derived / import_failed）
            track=0,
        )
    )
    return sid


# ---------------------------------------------------------------------------
# Guitar Pro / MusicXML：浏览器转换的结果
# ---------------------------------------------------------------------------
def import_failed(sid: str, error: str) -> dict:
    """浏览器解析 GP / MusicXML 失败：记到曲库里，不再显示成“可以练习”。"""

    def apply(s):
        if not is_score(s):
            raise Invalid("只有 Guitar Pro / MusicXML 曲子会这样报错")
        if s.get("bars"):  # 之前已经成功解析过：一次临时的加载错误不代表文件坏了
            log.info("song=%s import error ignored (already parsed): %s", sid, error)
            return
        s.update(status="error", error=(str(error or "").strip() or "曲谱文件无法读取")[:300])

    return store.update(sid, apply)


def set_derived(
    sid: str, meta_raw: dict, bars_raw: list, track: int = 0, derived: dict | None = None
) -> dict:
    """保存练习页从 GTP / MusicXML 解析出的调弦和小节。
    derived 记下转换器和 alphaTab 的版本：它们升级后练习页会重新转换一次，这时同一音轨的话
    把新旧小节对上（见 _carry_over），内容没变的保留 id、提示、问答和练习记录，变了的按“改过音”处理。
    换音轨就是一首新的谱子，旧的讲解和练习记录都清掉。"""

    def apply(s):
        if not is_score(s):
            raise Invalid("只有 Guitar Pro / MusicXML 曲子可以这样保存")
        meta = normalize_meta(meta_raw or {}, s.get("title", ""))
        bars = [_derived_bar(i, b or {}, meta) for i, b in enumerate(bars_raw or [])]
        old, track_n = s.get("bars") or [], int(track or 0)
        if old and int(s.get("track") or 0) == track_n:
            _carry_over(s, meta, bars)
        else:
            s["plan"] = None
        s.update(meta=meta, bars=bars, track=track_n, status="ready", error=None)
        if isinstance(derived, dict):
            s["derived"] = {
                "version": derived.get("version"),
                "alphatab": str(derived.get("alphatab") or ""),
            }
        staleness.prune_practice(s)

    return store.update(sid, apply)


def _derived_bar(i: int, b: dict, meta: dict) -> dict:
    ts = clean_time_signature(b.get("time_signature") or meta["time_signature"]) or meta["time_signature"]
    bar = {"time_signature": ts} if ts != meta["time_signature"] else {}
    try:
        events = clean_events(b.get("events", []))
        warn = check_bar(events, meta, bar=bar) if events else []
    except ValueError as e:
        events, warn = [], [f"导入时这一小节的数据无法使用：{e}"]
    bar.update(
        {
            "id": new_bar_id(),
            "n": i + 1,
            "system": i // 4,  # 没有原谱图片，按每 4 小节一段
            "page": None,
            "region": None,
            "events": events or rest_bar(meta, bar),
            "tip": "",
            "uncertain": "",
            "warn": warn,
            **clean_repeats(b),
        }
    )
    return bar


def _carry_over(s: dict, meta: dict, bars: list[dict]) -> None:
    """重新转换同一个音轨：把原来小节的 id 和 AI 内容搬到新小节上。
    按内容做序列比对（小节数变了也能对上，比如转换器改了反复记号的展开方式）：
    内容一样的原样保留；对不上但位置相当的（比对里的“替换”段）保留 id 和练习记录，提示清掉、问答标成过时；
    新多出来的是新小节，没了的小节连同它的练习记录一起去掉。"""
    import difflib
    import json

    old_meta = s.get("meta") or {}
    meta["title"] = old_meta.get("title") or meta["title"]  # 用户可能改过曲名
    setting_changed = staleness.setting_changed(old_meta, meta)
    old = s["bars"]

    def key(b):
        return json.dumps([b["events"], b.get("time_signature")], sort_keys=True)

    pairs = []  # (原来的小节, 新小节, 内容是否一样)
    sm = difflib.SequenceMatcher(None, [key(b) for b in old], [key(b) for b in bars], autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op in ("equal", "replace"):
            pairs += [(old[i], bars[j], op == "equal") for i, j in zip(range(i1, i2), range(j1, j2))]
    for prev, new, same in pairs:
        new["id"] = prev["id"]
        new["tip"], new["uncertain"] = prev.get("tip", ""), prev.get("uncertain", "")
        if prev.get("qa"):
            new["qa"] = prev["qa"]
        if setting_changed or not same:
            new["tip"] = ""
            staleness.mark_qa_stale(new)
            if not setting_changed:
                staleness.invalidate_plan(s, new["id"])
    if setting_changed or len(old) != len(bars):  # 小节编号整体移动了，练习建议里的小节号对不上
        staleness.invalidate_plan(s)


# ---------------------------------------------------------------------------
# 手工修改
# ---------------------------------------------------------------------------
def _recognizing(s: dict) -> bool:
    """整首识别（或重新识别）正在进行（生成讲解也借用 parsing 状态，但不改谱面，不算）。"""
    return s.get("status") in ("parsing", "queued") and s.get("job") != "explain"


def _bar_locked(s: dict, bar: dict) -> str | None:
    """这个小节现在不能改的原因：改了会被正在进行的识别结果覆盖。"""
    if s.get("restaging"):
        return "正在重新识别整首，完成后再改（识别失败或取消时原来的结果会保留）"
    systems = s.get("systems") or []
    si = bar.get("system")
    if isinstance(si, int) and 0 <= si < len(systems) and systems[si].get("status") == "parsing":
        return "这一行正在重新识别，完成后再改"
    return None


def edit_bar(sid: str, bar_id: str, events, tip: str | None) -> dict:
    """按小节的稳定 id 修改（编号在识别过程中会变，按编号改可能改到别的小节）。"""
    try:
        cleaned = clean_events(events)
    except Invalid as e:
        raise Invalid(f"小节数据格式不对：{e}") from None

    def apply(s):
        bar = find_bar(s, bar_id)
        if why := _bar_locked(s, bar):
            raise Busy(why)
        new = cleaned or rest_bar(s["meta"], bar)
        if "_orig" in bar and new == bar["_orig"]["events"]:
            staleness.restore_bar(bar)
            staleness.restore_plan_bar(s, bar_id)
        elif new != bar["events"]:
            staleness.invalidate_bar(bar)
            staleness.invalidate_plan(s, bar_id)
        bar["events"] = new
        bar.pop("placeholder", None)  # 用户自己写的内容，不再是识别失败的占位休止
        bar["warn"] = check_bar(bar["events"], s["meta"], bar=bar)
        if tip is not None:
            bar["tip"] = tip

    return store.update(sid, apply)


def edit_meta(sid: str, raw: dict) -> dict:
    def apply(s):
        old = s.get("meta") or {}
        s["meta"] = normalize_meta({**old, **raw}, strict=True)
        if is_score(s) and staleness.setting_changed(old, s["meta"]):
            # 播放用的是原文件：这里改了只会让指板、示范音和听到的对不上，重新转换时还会被文件里的设定覆盖
            raise Invalid(
                "Guitar Pro / MusicXML 曲子的调弦、变调夹和拍号来自原文件，不能在这里改（可以改曲名）"
            )
        if staleness.setting_changed(old, s["meta"]) and (
            _recognizing(s) or any(x.get("status") == "parsing" for x in s.get("systems") or [])
        ):
            # 识别时按调弦、拍号检查和返修，改了会和正在写回的结果对不上（曲名之类随时能改）
            raise Busy("正在识别，完成后再改调弦、变调夹和拍号")
        if staleness.setting_changed(old, s["meta"]):
            staleness.invalidate_tips_for_setting(s)
        if "title" in raw:
            s["title"] = str(raw["title"]).strip()
        if is_score(s):
            for b in s["bars"]:
                b["warn"] = check_bar(b["events"], s["meta"], bar=b)
        else:  # 图片曲子：谱上写了变拍号的小节按变拍号，其余跟着新的全曲拍号
            apply_time_signatures(s["bars"], s["meta"])

    return store.update(sid, apply)


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
        raise Invalid("练习记录格式不对") from None
    if not ids or not 0 <= loops <= 10000 or not 0 < bpm <= 400 or not 0 <= seconds <= 6 * 3600:
        raise Invalid("练习记录格式不对")
    now = _now()

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


# ---------------------------------------------------------------------------
# 旧数据 / 启动恢复
# ---------------------------------------------------------------------------
def ensure_bar_ids(song: dict) -> None:
    for b in song.get("bars", []):
        if not b.get("id"):
            b["id"] = new_bar_id()


def recover_interrupted() -> None:
    """服务启动时调用：上次没跑完的任务（服务被关掉）标成中断，否则曲子会一直卡在“识别中”。
    同时给老数据里没有 id 的小节补上 id。"""
    if not store.SONGS.exists():
        return
    for d in store.SONGS.iterdir():
        try:
            song = store.load(d.name)
        except (KeyError, ValueError):
            continue
        if _recover(song):
            store.save(song)


def _recover(song: dict) -> bool:
    changed = False
    if song.get("job") == "explain":
        song.pop("job")
        prev = song.pop("job_prev", None)  # 旧版本：讲解期间借用了 status="parsing"
        if prev and song.get("status") in ("parsing", "queued"):
            song["status"] = prev["status"]
        song.update(progress=None, error="服务重启，讲解生成中断，已完成的部分会保留，可以重新生成。")
        song["error_kind"] = "explain"
        changed = True
    elif song.get("status") in ("parsing", "queued"):
        if is_score(song):
            song.update(status="ready", error="服务重启，讲解生成中断，可以重新生成。", progress=None)
        else:
            song["status"] = "error"
            song["error"] = "服务重启，识别中断。可以在曲库的“…”菜单里重新识别。"
        changed = True
    if song.pop("restaging", None):  # 重新识别时原来的结果没动过，只是新结果丢了
        song.update(status="ready", error="服务重启，重新识别中断，原来的结果保留不变。", progress=None)
        changed = True
    for s in song.get("systems", []):
        if s.get("status") in ("parsing", "pending"):
            s.update(status="error", error="服务重启，识别中断")
            changed = True
    if any(not b.get("id") for b in song.get("bars", [])):
        ensure_bar_ids(song)
        changed = True
    return changed
