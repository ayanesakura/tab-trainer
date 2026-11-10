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
    assert rows[bad]["status"] == "corrupt" and "数据损坏" in rows[bad]["notice"]["msg"]
    c = TestClient(main.app, base_url="http://127.0.0.1:8765")
    r = c.get(f"/api/songs/{bad}")
    assert r.status_code == 500 and "数据损坏" in r.json()["detail"]
    assert c.get("/api/songs").status_code == 200
    assert c.delete(f"/api/songs/{bad}").status_code == 200 and not d.exists()


def test_missing_song_json_with_corrupt_backup_is_corrupt_not_gone(data_dir):
    """正式文件不在、备份又是坏的：是“数据损坏”（曲库里显示、可以删除），不能当成曲子不存在而从曲库里消失。"""
    sid = _gp()
    d = store.song_dir(sid)
    (d / "song.json").unlink()
    (d / "song.json.bak").write_text("{trunc")
    with pytest.raises(errors.CorruptSong):
        store.load(sid)
    rows = {s["id"]: s for s in views.list_library()}
    assert rows[sid]["status"] == "corrupt"


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


def test_copied_song_folder_saves_into_its_own_folder(data_dir):
    """手动复制了一份曲子目录：song.json 里的 id 是原来那首的，以目录名为准，改副本不能写到原曲子里。"""
    import shutil

    sid = _gp("原曲")
    copy = "20991231-000000-copy01"
    shutil.copytree(store.song_dir(sid), store.SONGS / copy)
    assert store.load(copy)["id"] == copy
    songs.edit_meta(copy, {"title": "改的是副本"})
    assert store.load(sid)["title"] == "原曲" and store.load(copy)["title"] == "改的是副本"


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


def test_concurrent_settings_saves_do_not_lose_a_field(data_dir, monkeypatch):
    """设置页两次保存同时到（输入框失焦自动保存 + 点按钮）：读-改-写整个在锁里，后一个看得到前一个改的字段。"""
    import threading

    from app import config

    real, other = store.load_config, []

    def slow_load():
        cfg = real()
        if not other:  # 第一次读完、还没写回时，另一个请求来改别的字段
            other.append(threading.Thread(target=config.update_settings, args=(None, False, {"model": "m2"})))
            other[0].start()
            other[0].join(0.3)
        return cfg

    monkeypatch.setattr(store, "load_config", slow_load)
    config.update_settings(None, False, {"base_url": "http://gw"})
    other[0].join()
    assert {k: real().get(k) for k in ("base_url", "model")} == {"base_url": "http://gw", "model": "m2"}


def test_private_file_is_never_readable_by_others_while_written(data_dir, monkeypatch):
    """存 API key 的配置：临时文件一建出来就是 0600，不是写完内容才改权限（中间别人能读到 key）。"""
    import os

    seen = []
    real = os.fsync

    def fsync(fd):
        seen.extend(oct(p.stat().st_mode & 0o777) for p in store.DATA.glob(".config.json.*.tmp"))
        real(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    store.save_config({"api_key": "sk-1"})
    assert seen == ["0o600"]


def test_second_instance_cannot_lock_the_same_data_dir(data_dir):
    assert store.lock_data_dir()
    code = (
        "import sys; from pathlib import Path; from app import store; "
        f"store.DATA = Path({str(data_dir)!r}); sys.exit(0 if store.lock_data_dir() else 3)"
    )
    r = subprocess.run([sys.executable, "-c", code], check=False, cwd=str(store.ROOT), capture_output=True)
    assert r.returncode == 3, r.stderr
    assert store.lock_data_dir()  # 同一个进程再锁：直接成功
    store.unlock_data_dir()
    r = subprocess.run([sys.executable, "-c", code], check=False, cwd=str(store.ROOT), capture_output=True)
    assert r.returncode == 0, r.stderr


def test_startup_through_lifespan_takes_the_data_dir_lock(data_dir):
    """`uvicorn app.main:app` 不经过 main()：lifespan 里也要锁数据目录，被别的进程锁着时不启动（也不做启动恢复）。"""
    code = (
        "import sys, time; from pathlib import Path; from app import store; "
        f"store.DATA = Path({str(data_dir)!r}); assert store.lock_data_dir(); "
        "print('locked', flush=True); time.sleep(30)"
    )
    holder = subprocess.Popen([sys.executable, "-c", code], cwd=str(store.ROOT), stdout=subprocess.PIPE)
    try:
        assert holder.stdout.readline().strip() == b"locked"
        with pytest.raises(RuntimeError, match="另一个 tab-trainer"), TestClient(main.app):
            pass
    finally:
        holder.kill()
        holder.wait()
    with TestClient(main.app) as c:  # 没人占着：启动时锁上
        assert c.get("/api/songs", headers={"host": "127.0.0.1"}).status_code == 200
    assert store.DATA.resolve() in store._lock_files
    store.unlock_data_dir()


def test_every_save_bumps_rev(data_dir):
    sid = _gp()
    rev = store.load(sid)["rev"]
    assert store.patch(sid, title="x")["rev"] == rev + 1 == store.load(sid)["rev"]


def test_per_object_revs_follow_content_only(data_dir):
    """bar.rev 只跟着这个小节的音符（和手动 touch_bar），meta_rev 只跟着可改的谱头字段；顶层 rev 每次保存都加。"""
    from app import model

    sid = _gp()
    store.update(
        sid,
        lambda s: s.update(
            meta={"tuning": ["E4", "B3", "G3", "D3", "A2", "E2"], "time_signature": [4, 4], "capo": 0},
            bars=[{"id": "a", "events": [{"dur": "4", "notes": []}]}, {"id": "b", "events": []}],
        ),
    )
    s0 = store.load(sid)
    assert [b["rev"] for b in s0["bars"]] == [0, 0]
    s1 = store.update(sid, lambda s: s["bars"][0].update(qa=[{"q": "q", "a": "a"}], tip="AI"))
    assert [b["rev"] for b in s1["bars"]] == [0, 0] and s1["meta_rev"] == s0["meta_rev"]
    assert s1["rev"] == s0["rev"] + 1
    s2 = store.update(sid, lambda s: s["bars"][1].update(events=[{"dur": "4", "notes": []}]))
    assert [b["rev"] for b in s2["bars"]] == [0, 1]
    # 重新识别换上新 dict（同一个 id）：内容一样沿用 rev，不一样 +1
    s3 = store.update(
        sid,
        lambda s: s.update(
            bars=[
                {"id": "a", "events": [{"dur": "4", "notes": []}]},
                {"id": "b", "events": [{"dur": "2", "notes": []}]},
            ]
        ),
    )
    assert [b["rev"] for b in s3["bars"]] == [0, 2]
    s4 = store.update(sid, lambda s: model.touch_bar(s["bars"][0]))
    assert s4["bars"][0]["rev"] == 1
    s5 = store.update(sid, lambda s: s["meta"].update(notes="x", tempo=90))  # 不是可改字段
    assert s5["meta_rev"] == s0["meta_rev"]
    s6 = store.update(sid, lambda s: s.update(title="新名字"))
    s7 = store.update(sid, lambda s: s["meta"].update(capo=2))
    assert s6["meta_rev"] == s0["meta_rev"] + 1 and s7["meta_rev"] == s0["meta_rev"] + 2


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


def test_practice_record_after_a_torn_line_is_kept(data_dir):
    """上次追加写到一半断电，practice.jsonl 末尾留下半行：下一条记录另起一行，不和半行粘在一起被一起跳过。"""
    sid = _gp()
    path = store.practice_path(sid)
    path.write_text(
        '{"at": "2026-01-01 10:00", "ids": ["a"], "loops": 1}\n{"at": "2026-01-0', encoding="utf-8"
    )
    store.append_practice(sid, {"at": "2026-01-02 10:00", "ids": ["b"], "loops": 2})
    assert [r["ids"] for r in store.read_jsonl(path)] == [["a"], ["b"]]


def test_valid_id_rejects_trailing_newline():
    """re.match + $ 会放过结尾的换行（"abcd\\n"）：用 fullmatch。"""
    assert store.valid_id("abcd-1234")
    assert not store.valid_id("abcd\n") and not store.valid_id("abcd-1234\n")
    with pytest.raises(errors.NotFound):
        store.song_dir("abcd\n")


# ---------------------------------------------------------------------------
# 按曲子的锁、读缓存
# ---------------------------------------------------------------------------
def _in_thread(fn):
    import threading

    out = {}
    t = threading.Thread(target=lambda: out.update(r=fn()), daemon=True)
    t.start()
    t.join(timeout=5)
    return (not t.is_alive()), out.get("r")


def test_locks_are_per_song(data_dir):
    """一首曲子的锁拿着时，别的曲子、练琴计划、设置照常能读写；同一首要等。"""
    a, b = _gp("甲"), _gp("乙")
    with store.song_lock(a):
        assert _in_thread(lambda: store.patch(b, title="乙2")["title"])[0]
        assert _in_thread(lambda: store.update_study(lambda d: d.update(x=1)))[0]
        assert _in_thread(lambda: store.update_config(lambda c: c.update(model="m")))[0]
        assert _in_thread(lambda: views.list_library())[0]  # 曲库列表不被一首曲子的写入挡住
        done, _ = _in_thread(lambda: store.patch(a, title="甲2"))
        assert not done  # 同一首：等锁
    assert store.load(b)["title"] == "乙2"


def test_read_is_cached_until_the_file_changes(data_dir):
    sid = _gp()
    first = store.read(sid)
    assert store.read(sid) is first  # 没改过：同一份
    store.patch(sid, title="新")
    again = store.read(sid)
    assert again is not first and again["title"] == "新"
    # 别的进程 / 手工改了文件：按文件签名也能发现
    path = store.song_dir(sid) / "song.json"
    data = json.loads(path.read_text())
    data["title"] = "手改"
    path.write_text(json.dumps(data))
    assert store.read(sid)["title"] == "手改"
    store.delete(sid)
    assert not any(k.parent.name == sid for k in store._songs_cache)
    with pytest.raises(errors.NotFound):
        store.read(sid)


def test_records_cache_follows_the_file_and_is_dropped_on_delete(data_dir):
    from app import practice

    sid = _gp()
    bar = store.load(sid)["bars"][0]["id"]
    assert practice.records(sid) == []
    practice.record(sid, [bar], 1, 60, 10)
    assert len(practice.records(sid)) == 1
    store.delete(sid)
    assert not any(k.parent.name == sid for k in store._records_cache)


def test_concurrent_plan_adds_get_distinct_orders(data_dir):
    """同时把几首曲子加进计划：序号各不相同（取号在 study.json 的锁里，扫曲库不拿锁）。"""
    import threading

    from app import study

    ids = [_gp(f"曲{i}") for i in range(6)]
    threads = [threading.Thread(target=study.set_plan, args=(sid, True)) for sid in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    orders = [store.load(sid)["study"]["order"] for sid in ids]
    assert sorted(orders) == list(range(6))
