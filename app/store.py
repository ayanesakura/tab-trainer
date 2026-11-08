"""本地存储：data/ 目录下一首曲子一个文件夹（song.json、practice.jsonl、页面图片 / 原文件），另有 config.json 存模型设置。

只管文件：路径、加锁的读-改-写、备份、删除。数据格式的升级在 app/migrate.py，派生字段在 app/model.py。

写入：先写临时文件并 fsync，再把旧文件改名成 .bak、临时文件改名成正式文件。断电或崩溃时最多丢最后一次修改；
读取时正式文件坏了（截断、不是合法 JSON）就用 .bak。两份都坏的曲子抛 CorruptSong（曲库里显示成“数据损坏”，可以删除）。
每次保存 rev +1（保存计数）；update 顺便维护按对象的版本号 bar.rev / meta_rev（见 model.bump_revs，接口用它们做乐观锁）。删除在锁里先把目录改名（之后的写入一律 NotFound，不会把目录重新建出来）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from PIL import Image

from app import migrate, model
from app.errors import CorruptSong, NotFound

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("TAB_TRAINER_DATA", ROOT / "data"))  # 测试和 e2e 会替换这三个
SONGS = DATA / "songs"
CONFIG = DATA / "config.json"
SCHEMA_VERSION = migrate.SCHEMA_VERSION

_lock = threading.RLock()
_ID_RE = re.compile(r"^[a-z0-9-]{4,64}$")
PRACTICE_FILE = "practice.jsonl"
log = logging.getLogger("tab_trainer.store")

__all__ = ["CorruptSong", "NotFound"]


def new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def valid_id(song_id: object) -> bool:
    return isinstance(song_id, str) and bool(_ID_RE.match(song_id))


def song_dir(song_id: str) -> Path:
    if not valid_id(song_id):
        raise NotFound()
    return SONGS / song_id


def exists(song_id: str) -> bool:
    return valid_id(song_id) and (SONGS / song_id).is_dir()


def song_ids() -> list[str]:
    """曲库里所有曲子的 id（新的在前）。临时目录（导入中、删除中）不算。"""
    if not SONGS.exists():
        return []
    return [d.name for d in sorted(SONGS.iterdir(), reverse=True) if d.is_dir() and valid_id(d.name)]


def _bak(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def write_json(path: Path, data: dict, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    # private（存 API key 的配置）：建文件时就是 0600，不是写完再改（中间别人能读到）；fchmod 管上次残留的同名临时文件
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o666)
    if private:
        os.fchmod(fd, 0o600)
    with open(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=1))
        f.flush()
        os.fsync(f.fileno())
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
    """读正式文件，坏了或不见了就读备份。两份都不行时抛出读不出来的那个错误；
    只有两份都不存在时才是 FileNotFoundError（正式文件不见了、备份是坏的，算坏了而不是不存在）。"""
    errs: list[Exception] = []
    for p in (path, _bak(path)):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("内容不是 JSON 对象")  # noqa: TRY004 - 和 JSON 解析错误一样按“文件坏了”处理
        except (OSError, ValueError) as e:
            errs.append(e)
            continue
        if p != path:
            log.warning("%s 读不出来（%s），用了备份 %s", path, errs[0], p.name)
        return data
    raise next((e for e in errs if not isinstance(e, FileNotFoundError)), errs[0])


# ---------------------------------------------------------------------------
# song.json
# ---------------------------------------------------------------------------
def load(song_id: str) -> dict:
    """读一首曲子（升级到当前格式、补上派生字段）。不存在抛 NotFound，两份文件都坏了抛 CorruptSong。"""
    path = song_dir(song_id) / "song.json"
    with _lock:
        if not path.exists() and not _bak(path).exists():
            raise NotFound()
        try:
            data = _read_json(path)
        except FileNotFoundError:
            raise NotFound() from None
        except (OSError, ValueError) as e:
            raise CorruptSong(song_id, str(e)) from None
    return model.derive(migrate.migrate(data, song_id))


# 写盘前检查不变量（model.problems）：平时只记错误日志（不能因为一处旧数据让用户存不了），测试里设成 True 直接报错
STRICT = False


def _check(song: dict) -> None:
    bad = model.problems(song)
    if bad and STRICT:
        raise AssertionError(f"song={song.get('id')}：" + "；".join(bad))
    if bad:
        log.error("song=%s 写入的数据有问题：%s", song.get("id"), "；".join(bad))


def save(song: dict, create: bool = False) -> None:
    """保存（rev +1）。create=False 时曲子目录必须已经存在：删掉的曲子不会被后台任务的写回重新建出来。"""
    with _lock:
        d = song_dir(song["id"])
        if not create and not d.is_dir():
            raise NotFound()
        song["rev"] = int(song.get("rev") or 0) + 1
        write_song(d, song)


def write_song(d: Path, song: dict) -> None:
    """把曲子写进目录 d（曲子目录，或导入时的临时目录）：去掉派生字段；旧数据升级时
    原来记在 song.json 里的练习记录转成 practice.jsonl 的第一行。"""
    with _lock:
        seed = song.pop("_practice_seed", None)
        if seed:
            _write_seed(d, seed)
        song["schema_version"] = SCHEMA_VERSION
        _check(song)
        write_json(d / "song.json", model.strip(song))


def update(song_id: str, fn: Callable[[dict], None]) -> dict:
    """加锁读-改-写，后台识别线程和接口请求同时改同一首曲子时不会互相覆盖。返回改完的曲子（带派生字段）。
    内容变了的小节 rev +1、可改谱头变了 meta_rev +1（model.bump_revs）。"""
    with _lock:
        song = load(song_id)
        before = model.content_snapshot(song)
        fn(song)
        model.bump_revs(song, before)
        model.derive(song)  # 先算派生字段再写盘：改完的内容读不回来（算的时候出错）就不写，原来的数据不受影响
        save(song)
        return song


def patch(song_id: str, **fields) -> dict:
    """update 的简写：直接覆盖几个顶层字段。"""
    return update(song_id, lambda s: s.update(fields))


def serialize(song: dict) -> dict:
    """song.json 里实际存的内容（去掉派生字段），曲子包导出用。"""
    return model.strip(dict(song))


def delete(song_id: str) -> None:
    """删除一首曲子（数据坏了的也能删）。在锁里先把目录改名：之后同一首的写入都会 NotFound。"""
    with _lock:
        d = song_dir(song_id)
        if not d.is_dir():
            raise NotFound()
        trash = SONGS / f".trash-{song_id}-{uuid.uuid4().hex[:6]}"
        os.rename(d, trash)
    shutil.rmtree(trash, ignore_errors=True)


def clean_temp_dirs() -> None:
    """启动时删掉上次没清理完的临时目录（删除中、导入中）。"""
    if SONGS.exists():
        for d in SONGS.iterdir():
            if d.is_dir() and d.name.startswith((".trash-", ".import-")):
                shutil.rmtree(d, ignore_errors=True)


def staging_dir() -> Path:
    """导入用的临时目录（名字不符合 id 格式，曲库列表和启动恢复都会忽略）。"""
    SONGS.mkdir(parents=True, exist_ok=True)
    d = SONGS / f".import-{uuid.uuid4().hex[:8]}"
    (d / "pages").mkdir(parents=True)
    return d


def install(tmp: Path, song_id: str) -> bool:
    """把准备好的临时目录改名成曲子目录。同一个 id 已经存在时返回 False（不覆盖）。"""
    with _lock:
        try:
            os.rename(tmp, song_dir(song_id))
        except OSError:
            return False
    return True


def page_image(song_id: str, page: dict) -> Image.Image:
    """曲子某一页的原图（RGB）。"""
    return Image.open(song_dir(song_id) / "pages" / page["file"]).convert("RGB")


# ---------------------------------------------------------------------------
# practice.jsonl：追加写，一行一条 JSON
# ---------------------------------------------------------------------------
def practice_path(song_id: str) -> Path:
    return song_dir(song_id) / PRACTICE_FILE


def practice_text(song_id: str, song: dict | None = None) -> str:
    """practice.jsonl 的全部内容（曲子包导出用）。旧曲子还没存过时带上内存里的种子行。"""
    try:
        text = practice_path(song_id).read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    seed = (song or {}).get("_practice_seed")
    if seed and not any(r.get("seed") for r in read_jsonl(practice_path(song_id))):
        text = json.dumps(seed, ensure_ascii=False) + "\n" + text
    return text


def append_practice(song_id: str, record: dict) -> None:
    with _lock:
        d = song_dir(song_id)
        if not d.is_dir():
            raise NotFound()
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with open(d / PRACTICE_FILE, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())


def read_jsonl(path: Path) -> list[dict]:
    """读 jsonl，跳过坏行（断电时可能留下半行）。"""
    out = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _write_seed(d: Path, seed: dict) -> None:
    path = d / PRACTICE_FILE
    if any(r.get("seed") for r in read_jsonl(path)):
        return
    d.mkdir(parents=True, exist_ok=True)
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(seed, ensure_ascii=False) + "\n" + old, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# config.json（模型设置，内容由 app/config.py 解释）
# ---------------------------------------------------------------------------
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
        write_json(CONFIG, cfg, private=True)


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
