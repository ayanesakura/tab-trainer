"""曲子包：把一首或多首曲子（识别结果、手工修改、AI 提示和练习建议、问答、练习记录、原图页面 / 原文件）
打成一个 zip 导出，在本机或另一台电脑上再导入。

包的结构：
    tab-trainer.json            清单：{"format": "tab-trainer-bundle", "version": 1, "songs": [{"id", "title"}]}
    songs/<id>/song.json
    songs/<id>/practice.jsonl     练习记录（练过才有）
    songs/<id>/pages/<页面图片>   图片 / PDF 曲子
    songs/<id>/<source.gp 等>    Guitar Pro / MusicXML 曲子的原文件

不导出模型设置（里面有 API key）。导入时保留原来的 id，曲库里已经有同一个 id 的曲子就跳过（同一个包导入两次
不会出现两份）。包是外来文件：只按清单里的 id 和 song.json 里登记的文件名去取，不按 zip 里的路径解压；
大小、页数、像素数都有上限；先解到临时目录，检查完再整个改名成曲子目录。
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import BinaryIO

from PIL import Image
from pydantic import ValidationError

from app import migrate, model, practice, songs, state, store
from app.config import settings
from app.errors import CorruptSong, Invalid, NotFound
from app.model import now

log = logging.getLogger("tab_trainer.bundles")

FORMAT = "tab-trainer-bundle"
VERSION = 1
MANIFEST = "tab-trainer.json"
_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
def _files(song: dict) -> list[tuple[str, Path]]:
    """曲子目录里要放进包的文件：(包内相对路径, 本机路径)。只取 song.json 里登记过的，备份和临时文件不要。"""
    d = store.song_dir(song["id"])
    out = []
    for p in song.get("pages", []):
        name = p.get("file") if isinstance(p, dict) else None
        if isinstance(name, str) and _FILE_RE.match(name) and (d / "pages" / name).is_file():
            out.append((f"pages/{name}", d / "pages" / name))
    src = song.get("source")
    if isinstance(src, str) and _FILE_RE.match(src) and (d / src).is_file():
        out.append((src, d / src))
    return out


def song_title(song: dict) -> str:
    """包里一首曲子的名字（旧版本导出的包里曲名可能只在 meta 里）。"""
    meta = song.get("meta") if isinstance(song.get("meta"), dict) else {}
    return (str(song.get("title") or meta.get("title") or "未命名")).strip() or "未命名"


def _all_ids() -> list[str]:
    out = []
    for sid in store.song_ids():
        try:
            store.load(sid)
        except (CorruptSong, NotFound):
            continue
        out.append(sid)
    return out


def export_bundle(ids: list[str]) -> tuple[Path, str]:
    """把这些曲子（空列表 = 整个曲库）打成一个 zip 临时文件，返回 (路径, 建议的文件名)。调用方负责删掉临时文件。"""
    if not ids:
        ids = _all_ids()
    ids = list(dict.fromkeys(ids))  # 去重，保持顺序
    if not ids:
        raise Invalid("曲库是空的，没有可导出的曲子")
    loaded = []
    for sid in ids:
        try:
            loaded.append(store.load(sid))
        except NotFound:
            raise Invalid(f"曲子不存在：{sid}") from None
        except CorruptSong:
            raise Invalid(f"曲子 {sid} 的数据已损坏，不能导出") from None

    fd, tmp = tempfile.mkstemp(prefix="tab-trainer-export-", suffix=".zip")
    os.close(fd)
    path = Path(tmp)
    try:
        with zipfile.ZipFile(path, "w") as zf:
            manifest = {
                "format": FORMAT,
                "version": VERSION,
                "schema_version": store.SCHEMA_VERSION,
                "exported": time.strftime("%Y-%m-%d %H:%M"),
                "songs": [{"id": s["id"], "title": song_title(s)} for s in loaded],
            }
            zf.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=1), zipfile.ZIP_DEFLATED)
            for s in loaded:
                base = f"songs/{s['id']}/"
                zf.writestr(
                    base + "song.json",
                    json.dumps(store.serialize(s), ensure_ascii=False, indent=1),
                    zipfile.ZIP_DEFLATED,
                )
                practice_lines = store.practice_text(s["id"], s)
                if practice_lines:
                    zf.writestr(base + store.PRACTICE_FILE, practice_lines, zipfile.ZIP_DEFLATED)
                for rel, p in _files(s):
                    # PNG 和 GP 本身已经压缩过，再压一遍只是费 CPU
                    zf.write(
                        p,
                        base + rel,
                        zipfile.ZIP_STORED if rel.startswith("pages/") else zipfile.ZIP_DEFLATED,
                    )
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    if len(loaded) == 1:
        name = f"{song_title(loaded[0])} - 曲子包.zip"
    else:
        name = f"曲子包 - {len(loaded)} 首 - {time.strftime('%Y%m%d')}.zip"
    return path, re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------
class _Duplicate(Invalid):
    """曲库里已经有同一个 id 的曲子。"""


class _Budget:
    """一次导入解出来的总字节数。"""

    def __init__(self, limit: int):
        self.left = limit

    def take(self, n: int) -> None:
        self.left -= n
        if self.left < 0:
            raise Invalid(f"曲子包解压后超过 {settings.max_import_bytes // 1024 // 1024 // 1024} GB")


def _entry(zf: zipfile.ZipFile, name: str, limit: int, budget: _Budget, what: str) -> zipfile.ZipInfo:
    try:
        info = zf.getinfo(name)
    except KeyError:
        raise Invalid(f"包里缺少{what}") from None
    if info.is_dir() or info.file_size > limit:
        raise Invalid(f"{what}太大或格式不对")
    budget.take(info.file_size)
    return info


def _read(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    # ZipExtFile 最多解出 file_size 个字节，CRC 不对会报错，所以清单里登记的大小可信
    with zf.open(info) as f:
        return f.read(info.file_size + 1)


def _check_image(data: bytes, name: str) -> None:
    try:
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > settings.max_pixels:
            raise Invalid(f"页面 {name} 分辨率太大")
        img.verify()
    except Invalid:
        raise
    except Exception:  # noqa: BLE001 - PIL 对坏图片抛的异常类型很多
        raise Invalid(f"页面 {name} 不是有效的图片") from None


def _import_one(zf: zipfile.ZipFile, bid: str, budget: _Budget) -> dict:
    """导入包里的一首曲子，返回 {"id", "title"}。出问题抛 Invalid（这一首跳过，不影响其他的）。"""
    if not store.valid_id(bid):
        raise Invalid("清单里的曲子 id 无效")
    base = f"songs/{bid}/"
    try:
        info = _entry(zf, base + "song.json", settings.max_song_json, budget, " song.json")
        song = json.loads(_read(zf, info))
    except (ValueError, UnicodeDecodeError):
        raise Invalid("song.json 不是有效的 JSON") from None
    except (zipfile.BadZipFile, OSError) as e:
        raise Invalid(f"文件损坏：{e}") from None
    if not isinstance(song, dict):
        raise Invalid("song.json 格式不对")
    try:
        newer = int(song.get("schema_version") or 0) > store.SCHEMA_VERSION
    except (TypeError, ValueError):
        raise Invalid("song.json 格式不对") from None
    if newer:
        raise Invalid("这首曲子来自更新版本的 tab-trainer，请先升级再导入")
    title = song_title(song)
    if store.exists(bid):
        raise _Duplicate("曲库里已经有这首（如果想用包里的版本替换，先删除曲库里的那首再导入）")

    pages = song.get("pages") if isinstance(song.get("pages"), list) else []
    if len(pages) > settings.max_pages:
        raise Invalid(f"页数超过 {settings.max_pages}")
    wanted: list[tuple[str, str]] = []  # (包内路径, 曲子目录里的相对路径)
    for p in pages:
        name = p.get("file") if isinstance(p, dict) else None
        if not isinstance(name, str) or not _FILE_RE.match(name):
            raise Invalid("页面文件名无效")
        wanted.append((base + "pages/" + name, "pages/" + name))
    src = song.get("source")
    if src is not None:
        ext = songs.score_ext(src) if isinstance(src, str) else None
        if not ext or not _FILE_RE.match(src):
            raise Invalid("原文件名无效")
        wanted.append((base + src, src))

    practice_name = base + store.PRACTICE_FILE
    has_practice = practice_name in zf.namelist()

    tmp = store.staging_dir()  # 不符合 id 格式，曲库列表和启动恢复都会忽略
    try:
        for zname, rel in wanted:
            info = _entry(zf, zname, settings.max_file_bytes, budget, f"文件 {rel}")
            data = _read(zf, info)
            if rel.startswith("pages/"):
                _check_image(data, rel[6:])
            elif not songs.looks_like_score(songs.score_ext(rel), data):
                raise Invalid("原文件不是有效的 Guitar Pro / MusicXML 文件")
            (tmp / rel).write_bytes(data)
        if has_practice:
            data = _read(zf, _entry(zf, practice_name, settings.max_song_json, budget, "练习记录"))
            (tmp / store.PRACTICE_FILE).write_bytes(_clean_jsonl(data))
        song["id"] = bid
        song = migrate.migrate(song, bid)
        _check_shape(song)  # 先查形状：后面的 recover / strip 都假定字段类型是对的
        state.recover(song)  # 导出时正在识别 / 讲解的，改成“已中断”
        song["imported"] = now()
        store.write_song(tmp, song)
        if not store.install(tmp, bid):  # 目标已存在（同时导入了同一首）时失败，不会覆盖
            raise _Duplicate("曲库里已经有这首")
    except (zipfile.BadZipFile, OSError) as e:
        raise Invalid(f"文件损坏：{e}") from None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"id": bid, "title": title}


def _check_shape(song: dict) -> None:
    """外来的 song.json（已升级到当前格式）要符合 model.Song，否则这首跳过，不让它在曲库里出错。"""
    try:
        model.Song.model_validate(song)  # 多出来的字段（派生的 n、meta.title，临时的 _ 开头字段）忽略
    except ValidationError as e:
        err = e.errors()[0]
        where = ".".join(str(x) for x in err.get("loc", ()))
        why = (err.get("ctx") or {}).get("error") if err.get("type") == "value_error" else None
        raise Invalid(f"song.json 格式不对（{where}{'：' + str(why) if why else ''}）") from None


def _clean_jsonl(data: bytes) -> bytes:
    """包里的练习记录：只留格式对的行（practice.clean_record），坏行丢掉。"""
    lines = []
    for line in data.decode("utf-8", "replace").splitlines():
        try:
            rec = practice.clean_record(json.loads(line))
        except ValueError:
            continue
        if rec:
            lines.append(json.dumps(rec, ensure_ascii=False))
    return ("\n".join(lines) + "\n").encode() if lines else b""


def import_bundles(files: list[tuple[str, BinaryIO]]) -> dict:
    """导入一个或多个曲子包。返回 {"imported": [{"id", "title"}], "skipped": [{"title", "reason", "duplicate"}]}：
    整个包读不了时 title 是文件名；包里单首有问题只跳过那一首。"""
    if not files:
        raise Invalid("没有收到文件")
    budget = _Budget(settings.max_import_bytes)
    imported, skipped = [], []
    for fname, fobj in files:
        try:
            zf = zipfile.ZipFile(fobj)
        except (zipfile.BadZipFile, OSError):
            skipped.append({"title": fname, "reason": "不是曲子包（不是 zip 文件）", "duplicate": False})
            continue
        with zf:
            try:
                manifest = json.loads(_read(zf, _entry(zf, MANIFEST, 1024 * 1024, budget, "清单")))
                if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
                    raise Invalid("清单格式不对")
                if int(manifest.get("version") or 0) > VERSION:
                    raise Invalid("曲子包来自更新版本的 tab-trainer，请先升级再导入")
                entries = manifest.get("songs")
                if not isinstance(entries, list) or len(entries) > settings.max_bundle_songs:
                    raise Invalid("清单里的曲子列表无效")
            except Invalid as e:
                skipped.append(
                    {"title": fname, "reason": f"不是 tab-trainer 曲子包（{e}）", "duplicate": False}
                )
                continue
            except (ValueError, zipfile.BadZipFile, OSError):
                skipped.append({"title": fname, "reason": "不是 tab-trainer 曲子包", "duplicate": False})
                continue
            for ent in entries:
                ent = ent if isinstance(ent, dict) else {}
                title = str(ent.get("title") or ent.get("id") or fname)
                try:
                    imported.append(_import_one(zf, ent.get("id"), budget))
                except Invalid as e:
                    skipped.append({"title": title, "reason": str(e), "duplicate": isinstance(e, _Duplicate)})
                except Exception as e:  # 外来数据结构不对（检查没覆盖到的）：只跳过这一首，其他的照常导入
                    log.exception("导入曲子包：%s 无法导入", ent.get("id"))
                    reason = f"song.json 格式不对（{type(e).__name__}）"
                    skipped.append({"title": title, "reason": reason, "duplicate": False})
    log.info("导入曲子包：%d 首成功，%d 首跳过", len(imported), len(skipped))
    return {"imported": imported, "skipped": skipped}
