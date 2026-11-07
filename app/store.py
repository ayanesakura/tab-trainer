"""本地存储：data/ 目录下一首曲子一个文件夹（song.json + 页面图片），另有 config.json 存模型设置。

写入：先写临时文件并 fsync，再把旧文件改名成 .bak、临时文件改名成正式文件。断电或崩溃时最多丢最后一次修改；
读取时正式文件坏了（截断、不是合法 JSON）就用 .bak。两份都坏的曲子在曲库里显示成“数据损坏”，不影响其他曲子。
song.json 带 schema_version，以后改数据结构时在 _migrate 里升级旧数据。
"""

from __future__ import annotations

import json
import logging
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
SCHEMA_VERSION = 1
log = logging.getLogger("tab_trainer.store")


class CorruptSong(ValueError):
    """song.json 和备份都读不出来。"""

    def __init__(self, song_id: str, reason: str):
        super().__init__(f"曲子数据损坏，无法读取：{reason}")
        self.song_id = song_id


def new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def song_dir(song_id: str) -> Path:
    if not _ID_RE.match(song_id):
        raise KeyError(song_id)
    return SONGS / song_id


def _bak(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def _write_json(path: Path, data: dict, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=1))
        f.flush()
        os.fsync(f.fileno())
    if private:
        tmp.chmod(0o600)
    if path.exists():
        os.replace(path, _bak(path))  # 上一版留作备份；这两次改名之间崩溃的话，读取时会用备份
    os.replace(tmp, path)
    try:  # 目录项也落盘（改名在断电后才不会丢）
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _read_json(path: Path) -> dict:
    """读正式文件，坏了或不见了就读备份。两份都不行时抛出第一个错误（文件都不存在时是 FileNotFoundError）。"""
    first: Exception | None = None
    for p in (path, _bak(path)):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("内容不是 JSON 对象")  # noqa: TRY004 - 和 JSON 解析错误一样按“文件坏了”处理
        except (OSError, ValueError) as e:
            first = first or e
            continue
        if p != path:
            log.warning("%s 读不出来（%s），用了备份 %s", path, first, p.name)
        return data
    raise first  # type: ignore[misc]


def _migrate(song: dict, song_id: str) -> dict:
    """旧数据补齐字段、升级到当前 schema_version。"""
    song.setdefault("id", song_id)
    for k, default in (("bars", []), ("systems", []), ("pages", []), ("meta", {})):
        if not isinstance(song.get(k), type(default)):
            song[k] = default
    song.setdefault("status", "ready" if song["bars"] else "error")
    song.setdefault("title", "")
    song["schema_version"] = SCHEMA_VERSION
    return song


def load(song_id: str) -> dict:
    path = song_dir(song_id) / "song.json"
    with _lock:
        if not path.exists() and not _bak(path).exists():
            raise KeyError(song_id)
        try:
            data = _read_json(path)
        except FileNotFoundError:
            raise KeyError(song_id) from None
        except (OSError, ValueError) as e:
            raise CorruptSong(song_id, str(e)) from None
    return _migrate(data, song_id)


def save(song: dict) -> None:
    with _lock:
        song["schema_version"] = SCHEMA_VERSION
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


_MUSICXML_EXT = (".musicxml", ".xml", ".mxl")


def source_type(s: dict) -> str:
    """导入渠道：image / pdf / gp / musicxml。早期的曲子没记，按文件推断（图片和 PDF 当时没区分，算图片）。"""
    if s.get("source_type"):
        return s["source_type"]
    if s.get("kind") == "score":
        return "musicxml" if str(s.get("source", "")).lower().endswith(_MUSICXML_EXT) else "gp"
    return "image"


def list_songs() -> list[dict]:
    out = []
    if SONGS.exists():
        for d in sorted(SONGS.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            try:
                s = load(d.name)
            except KeyError:
                continue
            except CorruptSong as e:  # 一首坏了不能让整个曲库打不开：列出来，可以删除
                out.append({"id": d.name, "title": d.name, "status": "corrupt", "error": str(e), "bars": 0})
                continue
            meta = s.get("meta") if isinstance(s.get("meta"), dict) else {}
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
                    "source_type": source_type(s),
                    "tags": [t for t in s.get("tags") or [] if isinstance(t, str)],
                    "last_practiced": ((s.get("practice") or {}).get("last") or {}).get("at") or "",
                    **cost_summary(s.get("usage")),
                }
            )
    return out


def cost_summary(usage) -> dict:
    """曲库里显示的花费（美元）。早期的曲子只记了 token：按默认模型（Opus）的价格估算，标成估算。
    用了价格表里没有的模型（自定义）的那部分只有 token，没法算钱，也标出来。"""
    from app.llm import DEFAULT_MODEL, price_of

    u = usage if isinstance(usage, dict) else {}
    tin, tout = u.get("input_tokens") or 0, u.get("output_tokens") or 0
    if "cost_usd" in u:
        cost, estimated = float(u.get("cost_usd") or 0), False
    else:
        price = price_of(DEFAULT_MODEL) or (0, 0)
        cost, estimated = (tin * price[0] + tout * price[1]) / 1e6, bool(tin or tout)
    return {
        "cost_usd": round(cost, 4),
        "cost_estimated": estimated,
        "unpriced_tokens": int(u.get("unpriced_tokens") or 0),
        "tokens": tin + tout,
    }


def load_config() -> dict:
    """模型设置。文件和备份都坏了时，把坏文件改名留着（里面可能有 key，方便手工找回），按没有设置处理。"""
    with _lock:
        if not CONFIG.exists() and not _bak(CONFIG).exists():
            return {}
        try:
            return _read_json(CONFIG)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            keep = CONFIG.with_name(f"config.corrupt-{time.strftime('%Y%m%d-%H%M%S')}.json")
            log.error("模型设置文件损坏（%s），已改名为 %s，请重新填写设置", e, keep.name)
            if CONFIG.exists():
                os.replace(CONFIG, keep)
            return {}


def save_config(cfg: dict) -> None:
    with _lock:
        _write_json(CONFIG, cfg, private=True)


_lock_file = None


def lock_data_dir() -> bool:
    """服务启动时调用：锁住数据目录，同一个目录只能有一个 tab-trainer 在跑（进程退出时自动释放）。
    已经被别的进程锁住时返回 False。不支持文件锁的系统上不做限制。"""
    global _lock_file
    try:
        import fcntl
    except ImportError:  # Windows
        return True
    DATA.mkdir(parents=True, exist_ok=True)
    f = open(DATA / ".lock", "w")  # noqa: SIM115 - 进程运行期间一直打开着，锁才有效
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _lock_file = f
    return True
