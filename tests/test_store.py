"""本地存储：写入留备份、坏文件回退到备份、坏曲子不拖垮曲库、配置文件损坏、单实例锁。"""

import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app import errors, main, songs, store, views
from tests.fakes import derived_bars


def _gp(title="曲子"):
    sid = songs.create_song([(f"{title}.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    songs.set_derived(sid, {}, derived_bars(2))
    return sid


def test_every_write_keeps_the_previous_version(data_dir):
    sid = _gp()
    path = store.song_dir(sid) / "song.json"
    store.patch(sid, title="第二版")
    assert json.loads((path.parent / "song.json.bak").read_text())["title"] == "曲子"
    assert store.load(sid)["title"] == "第二版" and store.load(sid)["schema_version"] == store.SCHEMA_VERSION
    assert not list(path.parent.glob("*.tmp"))


def test_truncated_song_falls_back_to_backup(data_dir):
    sid = _gp()
    store.patch(sid, title="最新")
    path = store.song_dir(sid) / "song.json"
    path.write_text(path.read_text()[:40])  # 断电留下的半个文件
    assert store.load(sid)["title"] == "曲子"  # 用了上一版
    path.unlink()  # 两次改名之间崩溃：正式文件不在，只有备份
    assert store.load(sid)["title"] == "曲子"


def test_corrupt_song_does_not_break_the_library(data_dir):
    good, bad = _gp("好的"), _gp("坏的")
    d = store.song_dir(bad)
    (d / "song.json").write_text("{")
    (d / "song.json.bak").write_text("[1, 2]")
    rows = {s["id"]: s for s in views.list_library()}
    assert rows[good]["title"] == "好的"
    assert rows[bad]["status"] == "corrupt" and "数据损坏" in rows[bad]["error"]
    c = TestClient(main.app, base_url="http://127.0.0.1:8765")
    r = c.get(f"/api/songs/{bad}")
    assert r.status_code == 500 and "数据损坏" in r.json()["detail"]
    assert c.get("/api/songs").status_code == 200
    assert c.delete(f"/api/songs/{bad}").status_code == 200 and not d.exists()


def test_old_songs_get_missing_fields(data_dir):
    sid = _gp()
    path = store.song_dir(sid) / "song.json"
    raw = json.loads(path.read_text())
    for k in ("schema_version", "status", "systems"):
        raw.pop(k, None)
    path.write_text(json.dumps(raw))
    s = store.load(sid)
    assert s["status"] == "ready" and s["systems"] == [] and s["schema_version"] == store.SCHEMA_VERSION
    assert any(x["id"] == sid for x in views.list_library())


def test_corrupt_config_is_set_aside(data_dir):
    store.save_config({"api_key": "sk-1"})
    store.save_config({"api_key": "sk-2"})
    store.CONFIG.write_text('{"api_key": "sk-')
    assert store.load_config() == {"api_key": "sk-1"}  # 备份
    store.CONFIG.with_name("config.json.bak").write_text("garbage")
    assert store.load_config() == {}
    kept = list(store.CONFIG.parent.glob("config.corrupt-*.json"))
    assert len(kept) == 1 and "sk-" in kept[0].read_text()  # 坏文件留着，key 还能手工找回
    store.save_config({"api_key": "sk-3"})
    assert store.load_config() == {"api_key": "sk-3"}


def test_second_instance_cannot_lock_the_same_data_dir(data_dir):
    assert store.lock_data_dir()
    code = (
        "import sys; from pathlib import Path; from app import store; "
        f"store.DATA = Path({str(data_dir)!r}); sys.exit(0 if store.lock_data_dir() else 3)"
    )
    r = subprocess.run([sys.executable, "-c", code], check=False, cwd=str(store.ROOT), capture_output=True)
    assert r.returncode == 3, r.stderr
    store._lock_file.close()
    store._lock_file = None
    r = subprocess.run([sys.executable, "-c", code], check=False, cwd=str(store.ROOT), capture_output=True)
    assert r.returncode == 0, r.stderr


def test_every_save_bumps_rev(data_dir):
    sid = _gp()
    rev = store.load(sid)["rev"]
    assert store.patch(sid, title="x")["rev"] == rev + 1 == store.load(sid)["rev"]


def test_missing_song_is_not_found(data_dir):
    with pytest.raises(errors.NotFound):
        store.load("20260101-000000-abcdef")
    with pytest.raises(errors.NotFound):
        store.load("../etc")


def test_delete_leaves_a_tombstone_so_late_writes_do_not_recreate_the_song(data_dir):
    """删除时在锁里先把目录改名：之后后台任务的写回一律 NotFound，不会把删掉的曲子重新建出来。"""
    sid = _gp()
    song = store.load(sid)
    store.delete(sid)
    assert not store.exists(sid) and sid not in store.song_ids()
    with pytest.raises(errors.NotFound):
        store.save(song)
    with pytest.raises(errors.NotFound):
        store.update(sid, lambda s: s.update(title="x"))
    with pytest.raises(errors.NotFound):
        store.append_practice(sid, {"ids": ["x"]})
    with pytest.raises(errors.NotFound):
        store.delete(sid)
    assert not store.song_dir(sid).exists() and not list(store.SONGS.glob(".trash-*"))


def test_job_finishing_after_delete_is_harmless(data_dir):
    import threading

    from app import jobs

    sid = _gp()
    jobs.jobs.claim(sid)
    done = []

    def late_write():
        store.delete(sid)  # 任务进行中曲子被删掉了
        store.patch(sid, title="写回")
        done.append(True)

    jobs.spawn(sid, late_write)
    for _ in range(100):
        if not jobs.jobs.running(sid):
            break
        threading.Event().wait(0.01)
    assert not jobs.jobs.running(sid) and not done and not store.song_dir(sid).exists()
