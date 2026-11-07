"""本地存储：data/ 目录下一首曲子一个文件夹（song.json + 页面图片），另有 config.json 存模型设置。"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("TAB_TRAINER_DATA", ROOT / "data"))
SONGS = DATA / "songs"
CONFIG = DATA / "config.json"

_lock = threading.RLock()
_ID_RE = re.compile(r"^[a-z0-9-]{4,64}$")


def new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def song_dir(song_id: str) -> Path:
    if not _ID_RE.match(song_id):
        raise KeyError(song_id)
    return SONGS / song_id


def _write_json(path: Path, data: dict, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    if private:
        tmp.chmod(0o600)
    tmp.replace(path)


def load(song_id: str) -> dict:
    p = song_dir(song_id) / "song.json"
    if not p.exists():
        raise KeyError(song_id)
    with _lock:
        return json.loads(p.read_text())


def save(song: dict) -> None:
    with _lock:
        _write_json(song_dir(song["id"]) / "song.json", song)


def update(song_id: str, fn: Callable[[dict], None]) -> dict:
    """加锁读-改-写，后台识别线程和接口请求同时改同一首曲子时不会互相覆盖。"""
    with _lock:
        song = load(song_id)
        fn(song)
        save(song)
        return song


def patch(song_id: str, **fields) -> dict:
    """update 的简写：直接覆盖几个顶层字段。"""
    return update(song_id, lambda s: s.update(fields))


def page_image(song_id: str, page: dict) -> Image.Image:
    """曲子某一页的原图（RGB）。"""
    return Image.open(song_dir(song_id) / "pages" / page["file"]).convert("RGB")


def list_songs() -> list[dict]:
    out = []
    if SONGS.exists():
        for d in sorted(SONGS.iterdir(), reverse=True):
            try:
                s = load(d.name)
            except (KeyError, json.JSONDecodeError):
                continue
            meta = s.get("meta") or {}
            out.append(
                {
                    "id": s["id"],
                    "title": s.get("title") or meta.get("title") or "未命名",
                    "artist": meta.get("artist", ""),
                    "status": s["status"],
                    "progress": s.get("progress"),
                    "job": s.get("job"),
                    "error": s.get("error"),
                    "bars": len(s.get("bars", [])),
                    "warn_bars": sum(1 for b in s.get("bars", []) if b.get("warn")),
                    "created": s.get("created"),
                    "kind": s.get("kind", "image"),
                }
            )
    return out


def load_config() -> dict:
    if CONFIG.exists():
        return json.loads(CONFIG.read_text())
    return {}


def save_config(cfg: dict) -> None:
    with _lock:
        _write_json(CONFIG, cfg, private=True)
