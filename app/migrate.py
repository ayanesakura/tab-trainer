"""song.json 的格式升级：所有旧数据的兼容处理都在这里，业务代码只认当前格式（SCHEMA_VERSION）。

store.load 每次读取都会调用（纯函数，在内存里升级）；升级后的内容下次保存时写回。服务启动时
（songs.recover_interrupted）会把所有旧曲子读一遍再存一遍，平时的读取接口不会写文件。

版本：
- 0 / 1：早期格式。
- 2：kind 去掉（只用 source_type）；job / job_prev / restaging / error / error_kind 换成 status + activity + notice；
  小节警告从一句话变成 {code, msg}；手改过的小节有显式的 edited；曲名只存 song.title；
  练习记录从 song.practice 搬到 practice.jsonl（旧的聚合结果成为第一行“种子”记录）；
  曲名 / 艺人 / 编配修复 GBK 被当成 cp1252 / Latin-1 读出来的乱码（GP 文件读出来的，以及从 GP 复制了谱头的图片曲子）。

不分版本的修复：早先导入曲子包时事件里被补上的空 chord / lyrics / strum 去掉（见 _drop_empty_event_keys）。
"""

from __future__ import annotations

import uuid

from app.music import SCORE_FILES, as_warn, drop_empty_event_keys
from app.text import fix_meta_text, fix_mojibake

SCHEMA_VERSION = 2


def migrate(song: dict, song_id: str) -> dict:
    """升级到当前格式（原地修改并返回）。升级过的曲子带 _migrated 标记（不存盘），旧练习记录放在 _practice_seed。"""
    song["id"] = song_id  # 以目录名为准：复制出来的目录里 song.json 写的还是原来那首的 id
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
            if _drop_empty_event_keys(b):
                song["_migrated"] = True  # 启动时写回
    song["schema_version"] = SCHEMA_VERSION
    return song


def _drop_empty_event_keys(b: dict) -> bool:
    """早先导入曲子包时补默认值往事件里加了空的 chord / lyrics / strum（正常保存的事件不写这些键）：去掉，
    和 clean_events 的结果一致。返回有没有改动。"""
    orig = b.get("_orig")
    changed = drop_empty_event_keys(b.get("events"))
    if isinstance(orig, dict):
        changed = drop_empty_event_keys(orig.get("events")) or changed
    return changed


def source_type_of(s: dict) -> str:
    """导入渠道：早期的曲子没记，按文件推断（图片和 PDF 当时没区分，算图片）。"""
    if s.get("source_type") in ("image", "pdf", "gp", "musicxml"):
        return s["source_type"]
    if s.get("kind") == "score" or s.get("source"):
        return "musicxml" if str(s.get("source", "")).lower().endswith(SCORE_FILES["musicxml"]) else "gp"
    return "image"


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
        warns = b.get("warn") or []
        b["warn"] = [as_warn(w) for w in ([warns] if isinstance(warns, str) else warns)]
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
        s["notice"] = {"kind": kind, "msg": str(error), "source": _notice_source(s, error_kind)}
    s.setdefault("progress", None)


def _notice_source(s: dict, error_kind: str | None) -> str:
    """旧的 error 是哪类任务留下的（决定之后哪类任务成功时清掉它，见 state.begin / songs.set_derived）。
    早期只有补页（pages）和讲解（explain）记了 error_kind；没记的按曲子类型推断：
    GP / MusicXML 曲子不识别，出错（status=error）只能是浏览器解析失败，否则是讲解中断；图片曲子是识别留下的。"""
    if error_kind:
        return {"pages": "reparse_row"}.get(error_kind, error_kind)
    if s["source_type"] in ("gp", "musicxml"):
        return "import" if s.get("status") == "error" else "explain"
    return "parse"
