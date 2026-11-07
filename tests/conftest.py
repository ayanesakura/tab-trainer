import pytest

from app import jobs, recognize, store
from tests.fakes import FakeClaude


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """曲库放到临时目录，模型换成替身。"""
    monkeypatch.setattr(store, "DATA", tmp_path)
    monkeypatch.setattr(store, "SONGS", tmp_path / "songs")
    monkeypatch.setattr(store, "CONFIG", tmp_path / "config.json")
    monkeypatch.setattr(jobs, "Claude", FakeClaude)
    FakeClaude.calls = []
    FakeClaude.tab_lines = []
    monkeypatch.setattr(recognize, "OCR_CHECK", False)  # 合成谱子上的数字和替身模型的结果对不上；OCR 单独测
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
