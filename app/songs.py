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
from typing import BinaryIO

from PIL import Image, ImageOps

from app import state, store
from app.config import settings
from app.errors import STALE_REV, AppError, BarNotFound, Busy, Conflict, Invalid, NotFound
from app.model import SCORE_EXTS, is_score, new_song, notes_key, page_file_name, page_number, touch_bar
from app.music import (
    GBK_EXT,
    SCORE_FILES,
    apply_time_signatures,
    clean_events,
    clean_repeats,
    clean_shapes,
    clean_time_signature,
    new_bar_id,
    normalize_meta,
    rest_bar,
    same_events,
    warn,
    warn_texts,
)
from app.staleness import (
    DERIVE,
    invalidate_bar,
    invalidate_plan,
    invalidate_tips_for_setting,
    note_user_tip,
    orig_plan_change,
    plan_has_bar,
    reconcile_bars,
    restore_bar,
    restore_plan_bar,
    setting_changed,
)
from app.text import fix_meta_text

log = logging.getLogger("tab_trainer.songs")

SCORE_EXT = SCORE_EXTS
MIN_SIDE = 32  # 图片最短边（像素）：再小就不可能看清谱子
# 上传的图片只按这些格式解码（Pillow 默认什么都试，包括会调用外部程序的 EPS）；PDF 走 pymupdf
IMAGE_FORMATS = ("PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF")


def find_bar(song: dict, bar_id: str) -> dict:
    bar = next((b for b in song["bars"] if b.get("id") == bar_id), None)
    if bar is None:
        raise BarNotFound(bar_id)
    return bar


def check_rev(current, rev: int | None) -> None:
    """乐观锁（按对象）：改小节时比这个小节的 rev，改谱头时比曲子的 meta_rev。请求带的和现在的对不上，
    说明这期间别处（另一个页面、重新识别）改过这个小节 / 谱头。写问答、讲解补提示、识别其他行都不算。"""
    if rev is not None and int(current or 0) != rev:
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
    with _staged(sid) as tmp:
        pages = save_pages(uploads, tmp / "pages")
        if not pages:
            raise Invalid("文件里没有可用的页面")
        song = new_song(
            sid,
            "pdf" if any(_is_pdf(n, data) for n, data in uploads) else "image",
            title=title.strip(),
            progress={"stage": "排队中", "done": 0, "total": 0},
            pages=pages,
        )
        _write_new(tmp, song)
    return sid


@contextmanager
def _staged(sid: str) -> Iterator:
    """新曲子先在临时目录里准备好（页面 / 原文件、song.json），最后改名成曲子目录：中途出错或崩溃不会留下
    没有 song.json 的曲子目录（曲库里看不见、也不会被清理）。临时目录启动时会被清掉（store.clean_temp_dirs）。"""
    tmp = store.staging_dir()
    try:
        yield tmp
        if not store.install(tmp, sid):
            raise RuntimeError(f"曲子目录已经存在：{sid}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _write_new(tmp, song: dict) -> None:
    song["rev"] = int(song.get("rev") or 0) + 1  # 和 store.save 一样算一次保存
    store.write_song(tmp, song)


# ---------------------------------------------------------------------------
# 上传上限（唯一一处：接口读文件和新建 / 补页都按这里检查）
# ---------------------------------------------------------------------------
def _check_count(n: int) -> None:
    if n > settings.max_files:
        raise Invalid(f"一次最多上传 {settings.max_files} 个文件")


def _check_size(name: str, n: int) -> None:
    if n > settings.max_file_bytes:
        raise Invalid(f"“{name}”太大了（单个文件最大 {settings.max_file_bytes // 1024 // 1024} MB）")


def check_upload_sizes(uploads: list[tuple[str, bytes]]) -> None:
    """上传的文件：至少一个、个数、单个文件和总大小都在上限内（config.settings）。"""
    if not uploads:
        raise Invalid("没有收到文件")
    _check_count(len(uploads))
    for name, data in uploads:
        _check_size(name, len(data))
    if sum(len(d) for _, d in uploads) > settings.max_total_bytes:
        raise Invalid(f"文件总大小超过 {settings.max_total_bytes // 1024 // 1024} MB")


def read_uploads(files: list[tuple[str, int | None, BinaryIO]]) -> list[tuple[str, bytes]]:
    """接口收到的 (文件名, 声明的大小, 文件对象) → (文件名, 内容)，按上面的上限检查。声明的大小已经超了的不读，
    每个文件最多读到上限多一个字节（不会把超大的文件整个读进内存）。没有文件是 []（补页时可以不传文件）。"""
    if not files:
        return []
    _check_count(len(files))
    out = []
    for name, size, f in files:
        if size is not None:
            _check_size(name, size)
        out.append((name, f.read(settings.max_file_bytes + 1)))
    check_upload_sizes(out)
    return out


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
            import pymupdf  # 旧名字 fitz 已经弃用（导入时发 FutureWarning）

            with _decoding(name):
                doc = pymupdf.open(stream=data, filetype="pdf")
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
                        img = Image.open(io.BytesIO(pix.tobytes("png")), formats=["PNG"]).convert("RGB")
                    yield img
        else:
            one_more()
            with _decoding(name):
                img = Image.open(io.BytesIO(data), formats=IMAGE_FORMATS)  # 只读文件头，还没解码像素
                if img.width * img.height > settings.max_pixels:
                    raise Invalid(f"“{name}”分辨率太大（{img.width}×{img.height}），请缩小后再上传")
                if min(img.width, img.height) < MIN_SIDE:
                    raise Invalid(f"“{name}”太小了（{img.width}×{img.height}），看不清谱子")
                img = ImageOps.exif_transpose(img)
                if img.mode in ("RGBA", "LA", "P"):
                    bg = Image.new("RGB", img.size, "white")
                    bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
                    img = bg
                img = img.convert("RGB")
            yield img


def save_pages(uploads: list[tuple[str, bytes]], pages_dir, existing: list[dict] | None = None) -> list[dict]:
    """把上传的图片 / PDF 存成 pages/pN.png（model.page_file_name）。补页时 existing 是已有的页：编号接着往后排，
    页数上限算上它们；曲子目录已经不在（删掉了）抛 NotFound，不把它重新建出来。"""
    try:
        pages_dir.mkdir(parents=existing is None, exist_ok=True)
    except FileNotFoundError:
        raise NotFound() from None
    taken = {pg["file"] for pg in existing or []}
    k = max((n for n in map(page_number, taken) if n is not None), default=0)
    pages = []
    try:
        for img in _page_images(uploads, len(existing or [])):
            k += 1
            while page_file_name(k) in taken or (pages_dir / page_file_name(k)).exists():
                k += 1
            fn = page_file_name(k)
            img.save(pages_dir / fn, format="PNG", optimize=True)
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
    with _staged(sid) as tmp:
        (tmp / "pages").rmdir()  # GP / MusicXML 没有页面
        (tmp / f"source{ext}").write_bytes(data)
        stem = name.rsplit("/", 1)[-1][: -len(ext)]
        song = new_song(
            sid,
            next(k for k, exts in SCORE_FILES.items() if ext in exts),
            source=f"source{ext}",
            title=title.strip() or stem,
            status="imported",  # 文件在浏览器里第一次成功解析后才变成 ready（见 set_derived / import_failed）
            track=0,
        )
        _write_new(tmp, song)
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
        raw = meta_raw or {}
        if str(s.get("source") or "").lower().endswith(GBK_EXT):  # 只有老格式会把中文存成 GBK
            raw = fix_meta_text(raw)
        meta = normalize_meta(raw, s.get("title", ""))
        bars = [_derived_bar(i, b or {}, meta) for i, b in enumerate(bars_raw or [])]
        old, track_n = s.get("bars") or [], int(track or 0)
        if old and not bars:  # 和 import_failed 一样：之前解析成功过的，一次空的结果不代表谱子就是空的
            raise Invalid("转换结果里没有小节，原来的小节保留不变")
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
    shapes = clean_shapes(b.get("shapes"))
    if shapes:
        bar["shapes"] = shapes
    return bar


# ---------------------------------------------------------------------------
# 手工修改（图片 / PDF 曲子）
# ---------------------------------------------------------------------------
def edit_bar(sid: str, bar_id: str, events, tip: str | None, rev: int | None = None) -> dict:
    """按小节的稳定 id 修改（编号在识别过程中会变，按编号改可能改到别的小节）。rev = 这个小节的 rev（见 check_rev）。"""
    try:
        cleaned = clean_events(events)
    except Invalid as e:
        raise Invalid(f"小节数据格式不对：{e}") from None

    def apply(s):
        if not state.can_edit_bars(s):
            raise Invalid(state.SCORE_BARS_LOCKED)
        bar = find_bar(s, bar_id)
        check_rev(bar.get("rev"), rev)
        if why := state.bar_locked(s, bar):
            raise Busy(why)
        new = cleaned or rest_bar(s["meta"], bar)
        changed = not same_events(new, bar["events"])
        if "_orig" in bar and same_events(new, bar["_orig"]["events"]):
            keep = orig_plan_change(bar)  # 手改之前就记在练习建议里的（精修改过之类）：改回原样也还是变过
            restore_bar(bar)  # 识别时留下的提示（OCR 不一致等）也恢复
            if not keep:
                restore_plan_bar(s, bar_id)
        elif changed:
            # 识别时留下的提示说的是原来的音：清掉（记在 _orig 里，撤销时恢复）
            invalidate_bar(bar, plan_has_bar(s, bar_id))
            invalidate_plan(s, bar_id)
        if changed:  # 原样保存（JSON 编辑器没改就保存）不动占位标记和提示，小节照样“待核对”
            bar.pop("placeholder", None)  # 用户自己写的内容，不再是识别失败的占位休止
        bar["events"] = new
        if tip is not None:
            if tip != bar.get("tip", ""):
                touch_bar(bar)  # 只改了提示（音符变了的话 store.update 会自己 +1，不会重复）
                note_user_tip(bar, tip)  # 精修改过的小节：撤销精修时保留用户写的提示
            bar["tip"] = tip

    return store.update(sid, apply)


def review_bar(sid: str, bar_id: str, ok: bool, rev: int | None = None) -> dict:
    """待核对的小节：用户看过原谱、确认识别没问题（ok=False 撤销）。记下当时音符的指纹（views.notes_key），
    音再变就要重新核对（提示变了不算：确认说的是识别得对不对）。
    不算改谱（不动 rev，不让提示、问答、练习建议失效）；GP 曲子的小节也能确认。"""

    def apply(s):
        bar = find_bar(s, bar_id)
        check_rev(bar.get("rev"), rev)
        if not is_score(s) and (why := state.bar_locked(s, bar)):
            raise Busy(why)  # 正在重新识别：确认会被识别结果盖掉
        bar.pop("reviewed_rev", None)  # 早先按 rev 记的确认
        if ok:
            bar["reviewed_key"] = notes_key(bar)
            bar["reviewed_warn"] = warn_texts(bar)
        else:
            bar.pop("reviewed_key", None)
            bar.pop("reviewed_warn", None)

    return store.update(sid, apply)


def edit_meta(sid: str, raw: dict, rev: int | None = None) -> dict:
    """改谱头：曲名随时能改；调弦 / 变调夹 / 拍号在识别、精修进行中不能改，GP / MusicXML 曲子来自原文件不能改。"""

    def apply(s):
        check_rev(s.get("meta_rev"), rev)
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
        if changed and (why := state.settings_lock_reason(s)):
            # 识别、精修时按调弦、拍号检查和返修，改了会和正在写回的结果对不上（曲名之类随时能改）
            raise Busy(why)
        s["meta"] = new
        if new["artist"] != old.get("artist", ""):
            s.setdefault("meta_user", {})["artist"] = new["artist"]  # 重新识别时同样以用户的为准
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


def set_archived(sid: str, archived: bool) -> bool:
    """归档 / 取消归档（model.Song.archived）：只是曲库里的一个标记，数据都留着；归档的不排进今天的练琴清单。"""

    def apply(s: dict) -> None:
        if archived:
            s["archived"] = True
        else:
            s.pop("archived", None)

    return bool(store.update(sid, apply).get("archived"))


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
            if state.recover(song) or song.get("_migrated"):
                store.save(song)
        except (LookupError, ValueError):  # 不存在 / 刚被删掉 / 数据坏了
            continue
        except Exception:  # 合法 JSON 但结构不对（手改坏了之类）：只跳过这一首，服务照常启动
            log.exception("song=%s 启动恢复时跳过：曲子数据格式不对", sid)
