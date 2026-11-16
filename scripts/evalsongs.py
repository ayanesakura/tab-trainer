"""评测脚本删除 / 覆盖曲子目录的唯一入口。

data/songs 里是用户真的曲库。脚本只能动自己生成的评测曲（id 以 eval- 开头），以及这次运行里自己刚建的曲子；
scripts/ 和 exp/ 里别的地方不直接调用 shutil.rmtree（tests/test_guards.py 会查）。

来由：2026-11-08 一次批量生成里目标 id 和原曲同名，脚本“目标已存在就先删”把用户的曲子删掉了（见 LESSONS.md）。
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from app import store

EVAL_PREFIX = "eval-"
_STARTED = time.time()  # 这个进程开始的时间：之后才出现的曲子目录才算“自己刚建的”


def check_target(dst_id: str, *, src_id: str | None = None, prefix: str = EVAL_PREFIX) -> Path:
    """目标 id 是这个脚本可以覆盖的评测曲就返回它的目录，否则退出。什么都不删；在开始干活之前调用。"""
    if not prefix.startswith(EVAL_PREFIX):
        raise SystemExit(f"前缀要以 {EVAL_PREFIX} 开头：{prefix}")
    if not store.valid_id(dst_id) or not dst_id.startswith(prefix) or dst_id == prefix:
        raise SystemExit(f"目标 id 要以 {prefix} 开头（只覆盖脚本自己生成的评测曲）：{dst_id!r}")
    if src_id is not None and dst_id == src_id:
        raise SystemExit(f"目标 id 不能和原曲相同：{dst_id}")
    return store.SONGS / dst_id


def clear_target(dst_id: str, *, src_id: str | None = None, prefix: str = EVAL_PREFIX) -> Path:
    """检查后删掉已有的目标评测曲目录，返回它的路径（此时不存在）。"""
    dst = check_target(dst_id, src_id=src_id, prefix=prefix)
    if dst.exists():
        shutil.rmtree(dst)
    return dst


def discard_created(song_id: str) -> None:
    """删掉这次运行里刚建、又不要了的曲子。目录比这个进程早就在的不删。"""
    d = store.SONGS / song_id
    if not store.valid_id(song_id) or not d.is_dir():
        raise SystemExit(f"没有这首曲子：{song_id!r}")
    st = d.stat()
    born = getattr(st, "st_birthtime", st.st_ctime)
    if born < _STARTED - 1:
        raise SystemExit(f"{song_id} 不是这次运行建的，不删")
    shutil.rmtree(d)
