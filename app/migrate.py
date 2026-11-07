"""song.json 的格式升级：所有旧数据的兼容处理都在这里，业务代码只认当前格式（SCHEMA_VERSION）。

store.load 每次读取都会调用（纯函数，在内存里升级）；升级后的内容下次保存时写回。服务启动时
（songs.recover_interrupted）会把所有旧曲子读一遍再存一遍，平时的读取接口不会写文件。

版本：
- 0 / 1：早期格式。
- 2：kind 去掉（只用 source_type）；job / job_prev / restaging / error / error_kind 换成 status + activity + notice；
  小节警告从一句话变成 {code, msg}；手改过的小节有显式的 edited；曲名只存 song.title；
  练习记录从 song.practice 搬到 practice.jsonl（旧的聚合结果成为第一行“种子”记录）；
  曲名 / 艺人 / 编配修复 GBK 被当成 Latin-1 读出来的乱码（GP 文件读出来的，以及从 GP 复制了谱头的图片曲子）。
"""

from __future__ import annotations

import uuid

from app.music import as_warn

SCHEMA_VERSION = 2
_MUSICXML_EXT = (".musicxml", ".xml", ".mxl")


def migrate(song: dict, song_id: str) -> dict:
    """升级到当前格式（原地修改并返回）。升级过的曲子带 _migrated 标记（不存盘），旧练习记录放在 _practice_seed。"""
    song.setdefault("id", song_id)
    for k, default in (("bars", []), ("systems", []), ("pages", [])):
        if not isinstance(song.get(k), type(default)):
            song[k] = default
    if not isinstance(song.get("meta"), dict) or not song["meta"]:
        song["meta"] = None  # 早期版本还没读出谱头时存的是 {}（model.Meta 要求有调弦，{} 导出后导不回来）
    song.setdefault("title", "")
    song.setdefault("rev", 0)
    song.setdefault("meta_rev", 0)  # 按对象的版本号：旧数据（和新识别出来的小节）没有的视为 0
    try:
        ver = int(song.get("schema_version") or 0)
    except (TypeError, ValueError):
        ver = 0
    if ver < 2:
        _to_v2(song)
        song["_migrated"] = True
    if not song["meta"]:  # 去掉 title / tuning_midi 之后什么都不剩的
        song["meta"] = None
    for b in song["bars"]:
        if isinstance(b, dict):
            b.setdefault("rev", 0)
    song["schema_version"] = SCHEMA_VERSION
    return song


def source_type_of(s: dict) -> str:
    """导入渠道：早期的曲子没记，按文件推断（图片和 PDF 当时没区分，算图片）。"""
    if s.get("source_type") in ("image", "pdf", "gp", "musicxml"):
        return s["source_type"]
    if s.get("kind") == "score" or s.get("source"):
        return "musicxml" if str(s.get("source", "")).lower().endswith(_MUSICXML_EXT) else "gp"
    return "image"


def fix_mojibake(text: str) -> str:
    """GBK 编码的中文被当成 Latin-1 读出来的乱码（如 "Ñº¹ãÌ«ÀÉ"）还原成中文。
    只有按 Latin-1 编回字节再按 GBK 解码能成功、ASCII 字符一个没被吞掉、而且解出了中日文字符时才替换，
    正常的西文（"Pokémon" 之类）不动。"""
    if not text or text.isascii():
        return text
    try:
        fixed = text.encode("latin-1").decode("gbk")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    if sum(c.isascii() for c in fixed) != sum(c.isascii() for c in text):
        return text
    if not any("\u4e00" <= c <= "\u9fff" or "\u3040" <= c <= "\u30ff" for c in fixed):
        return text
    return fixed


def fix_meta_text(meta: dict) -> dict:
    """谱头里的曲名、艺人、编配：修复乱码（见 fix_mojibake）。"""
    return {
        **meta,
        **{k: fix_mojibake(meta[k]) for k in ("title", "artist", "arranger") if isinstance(meta.get(k), str)},
    }


def _to_v2(s: dict) -> None:
    s["source_type"] = source_type_of(s)
    s.pop("kind", None)
    meta = s.get("meta")
    # 乱码修复不分来源：GP 文件里读出来的，以及从 GP 复制过谱头的图片曲子（如评测用的曲子）
    s["title"] = fix_mojibake(str(s.get("title") or ""))
    if meta:
        meta = s["meta"] = fix_meta_text(meta)
    if meta is not None:
        s["title"] = str(s.get("title") or "").strip() or str(meta.get("title") or "").strip()
        meta.pop("title", None)
        meta.pop("tuning_midi", None)
    s.setdefault("status", "ready" if s["bars"] else "error")
    _state_to_v2(s)
    for b in s["bars"]:
        if not isinstance(b, dict):
            continue
        if not b.get("id"):
            b["id"] = uuid.uuid4().hex[:10]
        b["warn"] = [as_warn(w) for w in b.get("warn") or []]
        if "_orig" in b:
            b["edited"] = True
        b.pop("n", None)
    if isinstance(s.get("plan"), dict):
        s["plan"].pop("stale", None)
    practice = s.pop("practice", None)
    if isinstance(practice, dict) and (
        practice.get("bars") or practice.get("total_seconds") or practice.get("last")
    ):
        s["_practice_seed"] = {"seed": True, **practice}


def _state_to_v2(s: dict) -> None:
    job, prev, restaging = s.pop("job", None), s.pop("job_prev", None), s.pop("restaging", None)
    error, error_kind = s.pop("error", None), s.pop("error_kind", None)
    s.pop("running", None)
    status = s.get("status")
    busy_status = status in ("parsing", "queued")
    activity = None
    if job == "explain":
        if isinstance(prev, dict) and busy_status:  # 更早的版本：讲解期间借用了 status="parsing"
            s["status"] = prev.get("status") or "ready"
        activity = {"kind": "explain", "restaging": False, "systems": []}
    elif job == "pages":
        rows = [
            i
            for i, x in enumerate(s["systems"])
            if isinstance(x, dict) and x.get("status") in ("parsing", "pending")
        ]
        activity = {"kind": "reparse_row", "restaging": False, "systems": rows}
    elif restaging:  # 以前重新识别期间 status 是 parsing；现在 status 只说结果（原来的结果还在，可以练）
        s["status"] = "ready"
        activity = {"kind": "parse", "restaging": True, "systems": []}
    elif busy_status and s["source_type"] in ("gp", "musicxml"):  # GP 曲子不识别：是更早版本的讲解
        s["status"] = "ready"
        activity = {"kind": "explain", "restaging": False, "systems": []}
    elif busy_status:
        activity = {"kind": "parse", "restaging": False, "systems": []}
    s["activity"] = activity
    s["notice"] = None
    if error:
        kind = "error" if s.get("status") == "error" else "warn"
        source = {"pages": "reparse_row"}.get(error_kind or "", error_kind or "")
        s["notice"] = {"kind": kind, "msg": str(error), "source": source}
    s.setdefault("progress", None)
