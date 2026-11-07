"""曲库里的曲子：新建（图片 / PDF / Guitar Pro / MusicXML）、保存浏览器转换的小节、手工修改、启动恢复。

新建图片曲子只保存页面，识别由调用方另外启动（service.upload → recognize.start_parse）。
请求里的长度 / 范围在 app/schemas.py 里检查过；这里检查内容（音名、时值、弦品）和曲子当前的状态。
"""

from __future__ import annotations

import io
import logging
import shutil
from collections.abc import Iterator
from contextlib import contextmanager

from PIL import Image, ImageOps

from app import state, store
from app.config import settings
from app.errors import STALE_REV, AppError, BarNotFound, Busy, Conflict, Invalid
from app.migrate import fix_meta_text
from app.model import is_score, new_song
from app.music import (
    apply_time_signatures,
    clean_events,
    clean_repeats,
    clean_time_signature,
    new_bar_id,
    normalize_meta,
    rest_bar,
    warn,
)
from app.staleness import (
    DERIVE,
    invalidate_bar,
    invalidate_plan,
    invalidate_tips_for_setting,
    reconcile_bars,
    restore_bar,
    restore_plan_bar,
    setting_changed,
)

log = logging.getLogger("tab_trainer.songs")

SCORE_EXT = (".gp", ".gpx", ".gp3", ".gp4", ".gp5", ".musicxml", ".xml", ".mxl")
MUSICXML_EXT = (".musicxml", ".xml", ".mxl")


def find_bar(song: dict, bar_id: str) -> dict:
    bar = next((b for b in song["bars"] if b.get("id") == bar_id), None)
    if bar is None:
        raise BarNotFound(bar_id)
    return bar


def check_rev(s: dict, rev: int | None) -> None:
    """乐观锁：请求带的 rev 和曲子现在的对不上，说明这期间别处（另一个页面、后台识别）改过。"""
    if rev is not None and int(s.get("rev") or 0) != rev:
        raise Conflict(STALE_REV)


# ---------------------------------------------------------------------------
# 新建
# ---------------------------------------------------------------------------
def create_song(uploads: list[tuple[str, bytes]], title: str = "") -> str:
    """保存上传的文件，返回曲子 id。图片 / PDF 曲子状态为 queued，等调用方启动识别。"""
    check_upload_sizes(uploads)
    if any(score_ext(n) for n, _ in uploads):
        return _create_score_song(uploads, title)
    sid = store.new_id()
    d = store.song_dir(sid)
    try:
        pages = save_pages(uploads, d / "pages")
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    if not pages:
        shutil.rmtree(d, ignore_errors=True)
        raise Invalid("文件里没有可用的页面")
    song = new_song(
        sid,
        "pdf" if any(_is_pdf(n, data) for n, data in uploads) else "image",
        title=title.strip(),
        progress={"stage": "排队中", "done": 0, "total": 0},
        pages=pages,
    )
    store.save(song, create=True)
    return sid


def check_upload_sizes(uploads: list[tuple[str, bytes]]) -> None:
    if not uploads:
        raise Invalid("没有收到文件")
    if len(uploads) > settings.max_files:
        raise Invalid(f"一次最多上传 {settings.max_files} 个文件")
    for name, data in uploads:
        if len(data) > settings.max_file_bytes:
            raise Invalid(f"“{name}”太大了（单个文件最大 {settings.max_file_bytes // 1024 // 1024} MB）")
    if sum(len(d) for _, d in uploads) > settings.max_total_bytes:
        raise Invalid(f"文件总大小超过 {settings.max_total_bytes // 1024 // 1024} MB")


@contextmanager
def _decoding(name: str) -> Iterator[None]:
    """图片 / PDF 解码失败（文件坏了、不是图片）→ 400“文件无法读取”。别的错误（磁盘满了之类）照常是 500。"""
    try:
        yield
    except AppError:
        raise
    except (OSError, ValueError, SyntaxError, RuntimeError, Image.DecompressionBombError) as e:
        raise Invalid(f"“{name}”文件无法读取：{e}") from None


def _is_pdf(name: str, data: bytes) -> bool:
    return name.lower().endswith(".pdf") or data[:5] == b"%PDF-"


def _page_images(uploads: list[tuple[str, bytes]], existing: int = 0):
    """逐页产出 RGB 图片（PDF 一页一页渲染，不把整份文件的页面都留在内存里）。existing：曲子已有的页数（补页时）。"""
    count = existing

    max_pages = settings.max_pages

    def one_more():
        nonlocal count
        count += 1
        if count > max_pages:
            raise Invalid(f"一首曲子最多 {max_pages} 页")

    for name, data in uploads:
        if _is_pdf(name, data):
            import fitz  # pymupdf

            with _decoding(name):
                doc = fitz.open(stream=data, filetype="pdf")
            with doc:
                if count + len(doc) > max_pages:
                    raise Invalid(f"一首曲子最多 {max_pages} 页（“{name}”有 {len(doc)} 页）")
                for page in doc:
                    one_more()
                    # 页面特别大（海报尺寸之类）时降低分辨率，免得渲染出几亿像素
                    w_in, h_in = page.rect.width / 72, page.rect.height / 72
                    dpi = min(settings.pdf_dpi, int((settings.max_pixels / max(1e-6, w_in * h_in)) ** 0.5))
                    if dpi < 30:
                        raise Invalid(f"“{name}”的页面尺寸太大")
                    with _decoding(name):
                        pix = page.get_pixmap(dpi=dpi)
                        img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
                    yield img
        else:
            one_more()
            with _decoding(name):
                img = Image.open(io.BytesIO(data))  # 只读文件头，还没解码像素
                if img.width * img.height > settings.max_pixels:
                    raise Invalid(f"“{name}”分辨率太大（{img.width}×{img.height}），请缩小后再上传")
                img = ImageOps.exif_transpose(img)
                if img.mode in ("RGBA", "LA", "P"):
                    bg = Image.new("RGB", img.size, "white")
                    bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
                    img = bg
                img = img.convert("RGB")
            yield img


def save_pages(
    uploads: list[tuple[str, bytes]], pages_dir, existing: list[dict] | None = None
) -> list[dict]:
    """把上传的图片 / PDF 存成 pages/pN.png。补页时 existing 是已有的页：编号接着往后排，页数上限算上它们。"""
    pages_dir.mkdir(parents=True, exist_ok=True)
    taken = {pg["file"] for pg in existing or []}
    nums = [int(f[1:-4]) for f in taken if f.startswith("p") and f.endswith(".png") and f[1:-4].isdigit()]
    k = max(nums, default=0)
    pages = []
    try:
        for img in _page_images(uploads, len(existing or [])):
            k += 1
            while f"p{k}.png" in taken or (pages_dir / f"p{k}.png").exists():
                k += 1
            fn = f"p{k}.png"
            img.save(pages_dir / fn, optimize=True)
            pages.append({"file": fn, "width": img.width, "height": img.height})
    except Exception:
        if existing is not None:  # 补页失败：已经存下的新页删掉（新建曲子时整个目录会被删掉）
            for pg in pages:
                (pages_dir / pg["file"]).unlink(missing_ok=True)
        raise
    return pages


def score_ext(name: str) -> str | None:
    low = name.lower()
    return next((e for e in SCORE_EXT if low.endswith(e)), None)


def looks_like_score(ext: str | None, data: bytes) -> bool:
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
    ext = score_ext(name)
    if not looks_like_score(ext, data):
        raise Invalid(
            f"“{name.rsplit('/', 1)[-1]}”不是有效的 Guitar Pro / MusicXML 文件（文件内容和扩展名对不上），可能已损坏或只是改了扩展名"
        )
    sid = store.new_id()
    d = store.song_dir(sid)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"source{ext}").write_bytes(data)
    stem = name.rsplit("/", 1)[-1][: -len(ext)]
    song = new_song(
        sid,
        "musicxml" if ext in MUSICXML_EXT else "gp",
        source=f"source{ext}",
        title=title.strip() or stem,
        status="imported",  # 文件在浏览器里第一次成功解析后才变成 ready（见 set_derived / import_failed）
        track=0,
    )
    store.save(song, create=True)
    return sid




# ---------------------------------------------------------------------------
# Guitar Pro / MusicXML：浏览器转换的结果（只读的缓存：小节不能手改，原文件才是数据源）
# ---------------------------------------------------------------------------
def import_failed(sid: str, error: str) -> dict:
    """浏览器解析 GP / MusicXML 失败：记到曲库里，不再显示成“可以练习”。"""

    def apply(s):
        if not is_score(s):
            raise Invalid("只有 Guitar Pro / MusicXML 曲子会这样报错")
        if s.get("bars"):  # 之前已经成功解析过：一次临时的加载错误不代表文件坏了
            log.info("song=%s import error ignored (already parsed): %s", sid, error)
            return
        msg = (str(error or "").strip() or "曲谱文件无法读取")[:300]
        state.finish(s, status="error", notice=("error", msg), source="import")

    return store.update(sid, apply)


def set_derived(
    sid: str, meta_raw: dict, bars_raw: list, track: int = 0, derived: dict | None = None
) -> dict:
    """保存练习页从 GTP / MusicXML 解析出的调弦和小节。
    derived 记下转换器和 alphaTab 的版本：它们升级后练习页会重新转换一次，这时同一音轨的话
    把新旧小节对上（见 staleness.reconcile_bars），内容没变的保留 id、提示、问答和练习记录，变了的按“改过音”处理。
    换音轨就是一首新的谱子，旧的讲解清掉（练习记录的小节 id 都对不上了，聚合时自然忽略）。"""

    def apply(s):
        if not is_score(s):
            raise Invalid("只有 Guitar Pro / MusicXML 曲子可以这样保存")
        meta = normalize_meta(fix_meta_text(meta_raw or {}), s.get("title", ""))
        bars = [_derived_bar(i, b or {}, meta) for i, b in enumerate(bars_raw or [])]
        old, track_n = s.get("bars") or [], int(track or 0)
        if old and int(s.get("track") or 0) == track_n:
            changed = setting_changed(s.get("meta") or {}, meta)
            reconcile_bars(s, old, bars, DERIVE, changed)
        else:
            s["plan"] = None
        s.update(meta=meta, bars=bars, track=track_n)
        s["status"] = "ready"
        if (s.get("notice") or {}).get("source") == "import":  # 之前解析失败的提示
            s["notice"] = None
        if isinstance(derived, dict):
            s["derived"] = {
                "version": derived.get("version"),
                "alphatab": str(derived.get("alphatab") or ""),
            }

    return store.update(sid, apply)


def _derived_bar(i: int, b: dict, meta: dict) -> dict:
    ts = clean_time_signature(b.get("time_signature") or meta["time_signature"]) or meta["time_signature"]
    bar = {"time_signature": ts} if ts != meta["time_signature"] else {}
    try:
        events = clean_events(b.get("events", []))
        warns = []
    except ValueError as e:
        events, warns = [], [warn("other", f"导入时这一小节的数据无法使用：{e}")]
    bar.update(
        {
            "id": new_bar_id(),
            "system": i // 4,  # 没有原谱图片，按每 4 小节一段
            "page": None,
            "region": None,
            "events": events or rest_bar(meta, bar),
            "tip": "",
            "uncertain": "",
            "warn": warns,  # 时值之类按内容算的提示读取时再算（model.derive）
            **clean_repeats(b),
        }
    )
    return bar


# ---------------------------------------------------------------------------
# 手工修改（图片 / PDF 曲子）
# ---------------------------------------------------------------------------
def edit_bar(sid: str, bar_id: str, events, tip: str | None, rev: int | None = None) -> dict:
    """按小节的稳定 id 修改（编号在识别过程中会变，按编号改可能改到别的小节）。rev 见 check_rev。"""
    try:
        cleaned = clean_events(events)
    except Invalid as e:
        raise Invalid(f"小节数据格式不对：{e}") from None

    def apply(s):
        if not state.can_edit_bars(s):
            raise Invalid(state.SCORE_BARS_LOCKED)
        check_rev(s, rev)
        bar = find_bar(s, bar_id)
        if why := state.bar_locked(s, bar):
            raise Busy(why)
        new = cleaned or rest_bar(s["meta"], bar)
        if "_orig" in bar and new == bar["_orig"]["events"]:
            restore_bar(bar)
            restore_plan_bar(s, bar_id)
        elif new != bar["events"]:
            invalidate_bar(bar)
            invalidate_plan(s, bar_id)
        bar["events"] = new
        bar.pop("placeholder", None)  # 用户自己写的内容，不再是识别失败的占位休止
        bar["warn"] = []  # 识别时留下的提示（OCR 不一致等）说的是原来的音；按内容算的读取时重算
        if tip is not None:
            bar["tip"] = tip

    return store.update(sid, apply)


def edit_meta(sid: str, raw: dict, rev: int | None = None) -> dict:
    """改谱头：曲名随时能改；调弦 / 变调夹 / 拍号在识别进行中不能改，GP / MusicXML 曲子来自原文件不能改。"""

    def apply(s):
        check_rev(s, rev)
        old = s.get("meta") or {}
        if "title" in raw and raw["title"] is not None:
            title = str(raw["title"]).strip()
        else:
            title = None
        if not old:  # 还没读出谱头（排队中、识别失败）：只能改曲名
            if any(v is not None for k, v in raw.items() if k != "title"):
                raise Invalid("还没读出谱头（排队中或识别失败），现在只能改曲名")
            if title:
                s["title"] = title
            return
        new = normalize_meta({**old, **{k: v for k, v in raw.items() if v is not None}}, strict=True)
        changed = setting_changed(old, new)
        if is_score(s) and (changed or new["artist"] != old.get("artist", "")):
            # 播放用的是原文件：这里改了只会让指板、示范音和听到的对不上，重新转换时还会被文件里的设定覆盖
            raise Invalid(
                "Guitar Pro / MusicXML 曲子的调弦、变调夹和拍号来自原文件，不能在这里改（可以改曲名）"
            )
        if changed and state.settings_locked(s):
            # 识别时按调弦、拍号检查和返修，改了会和正在写回的结果对不上（曲名之类随时能改）
            raise Busy("正在识别，完成后再改调弦、变调夹和拍号")
        s["meta"] = new
        if changed:
            invalidate_tips_for_setting(s)
            # 记下用户亲手改的设定：整首重新识别时以它为准，不被模型重新读出的谱头覆盖
            user = s.setdefault("meta_user", {})
            if new.get("tuning_midi") != old.get("tuning_midi"):
                user["tuning"] = new["tuning"]  # 存音名：重新识别时和读出的谱头一起再整理一遍
            user.update({k: new[k] for k in ("capo", "time_signature") if new.get(k) != old.get(k)})
        if title is not None:
            s["title"] = title
        if not is_score(s):  # 图片曲子：谱上写了变拍号的小节按变拍号，其余跟着新的全曲拍号
            apply_time_signatures(s["bars"], s["meta"])

    return store.update(sid, apply)


def set_tags(sid: str, tags: list[str]) -> list[str]:
    """曲库里用来筛选的标签（schemas.TagsIn 已经去掉空白、重复，检查过数量和长度）。"""
    return store.update(sid, lambda s: s.update(tags=list(tags)))["tags"]


# ---------------------------------------------------------------------------
# 启动恢复
# ---------------------------------------------------------------------------
def recover_interrupted() -> None:
    """服务启动时调用：上次没跑完的任务（服务被关掉）标成中断，否则曲子会一直卡在“识别中”。
    旧格式的曲子（store.load 时在内存里升级过）顺便存一遍，写成当前格式。"""
    store.clean_temp_dirs()
    for sid in store.song_ids():
        try:
            song = store.load(sid)
        except (LookupError, ValueError):  # 不存在 / 数据坏了
            continue
        if state.recover(song) or song.get("_migrated"):
            try:
                store.save(song)
            except LookupError:
                continue
