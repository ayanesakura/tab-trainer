"""本地存储：写入留备份、坏文件回退到备份、坏曲子不拖垮曲库、配置文件损坏、单实例锁。"""

import json
import subprocess
import sys

from fastapi.testclient import TestClient

from app import main, songs, store
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
    rows = {s["id"]: s for s in store.list_songs()}
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
    assert any(x["id"] == sid for x in store.list_songs())


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
