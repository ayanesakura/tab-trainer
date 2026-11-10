import os
import tempfile

# 兜底：测试进程里默认的数据目录是一个临时目录（在导入 app 之前设好）。没用 data_dir 的测试、或者把 data_dir 的
# 替换撤销掉的测试（monkeypatch.undo()），也只会写到这里，不会写进真的曲库 data/
os.environ["TAB_TRAINER_DATA"] = tempfile.mkdtemp(prefix="tab-trainer-test-")

import pytest

from app import clock, config, jobs, recognize, store
from tests.fakes import FakeClaude


@pytest.fixture(autouse=True)
def _real_clock():
    """测试里用 clock.set_now 固定的时间，测完恢复成系统时间。"""
    yield
    clock.set_now(None)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """曲库放到临时目录，模型换成替身。"""
    monkeypatch.setattr(store, "DATA", tmp_path)
    monkeypatch.setattr(store, "SONGS", tmp_path / "songs")
    monkeypatch.setattr(store, "CONFIG", tmp_path / "config.json")
    monkeypatch.setattr(config, "llm_factory", FakeClaude)
    FakeClaude.calls = []
    FakeClaude.tab_lines = []
    monkeypatch.setattr(FakeClaude, "verify_result", None)  # 精修默认什么都不改
    monkeypatch.setattr(recognize, "OCR_CHECK", False)  # 合成谱子上的数字和替身模型的结果对不上；OCR 单独测
    monkeypatch.setattr(store, "STRICT", True)  # 每次写盘都检查不变量（model.problems）
    monkeypatch.setattr(recognize, "SPLIT_ROWS", False)  # 合成谱子的行也够宽，切半会改变调用次数；切半单独测
    return tmp_path


@pytest.fixture
def sync_jobs(monkeypatch):
    """后台任务（jobs.spawn）在当前线程里同步执行，测试可以直接检查结果。"""

    def run_now(sid, target):
        try:
            target()
        finally:
            jobs.jobs.release(sid)

    monkeypatch.setattr(jobs, "spawn", run_now)
