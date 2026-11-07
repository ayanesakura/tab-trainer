"""曲子包：导出再导入后数据完整；多首、多个包一起导入；重复、损坏、外来文件里的危险内容。"""

import io
import json
import shutil
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import bundles, main, model, practice, recognize, songs, store, views
from app.errors import Invalid
from tests.fakes import derived_bars, score_png

GP5 = b"\x18FICHIER GUITAR PRO v5.10" + b"\x00" * 64


def _image_song(title="图片曲") -> str:
    sid = songs.create_song([("p1.png", score_png())], title)
    recognize.run_parse(sid)

    def extra(s):  # 用户在这台电脑上积累的东西：提示、建议、问答、练习记录
        s["bars"][0]["tip"] = "第一小节慢一点"
        s["bars"][0]["qa"] = [{"q": "怎么按", "a": "食指横按"}]
        s["plan"] = {"text": "先练前四小节"}

    store.update(sid, extra)
    practice.record(sid, [store.load(sid)["bars"][0]["id"]], 3, 60, 30)
    return sid


def _gp_song() -> str:
    sid = songs.create_song([("Romance.gp5", GP5)])
    songs.set_derived(sid, {"title": "Romance"}, derived_bars(2), 0, None)
    return sid


def _zip(path) -> zipfile.ZipFile:
    return zipfile.ZipFile(path)


def _wipe():
    shutil.rmtree(store.SONGS)


def test_roundtrip_keeps_everything(data_dir):
    a, b = _image_song(), _gp_song()
    before = {sid: store.load(sid) for sid in (a, b)}
    played = practice.aggregate(a, before[a])
    path, name = bundles.export_bundle([a, b])
    assert name.startswith("曲子包 - 2 首")
    with _zip(path) as zf:
        names = set(zf.namelist())
        assert f"songs/{a}/pages/p1.png" in names and f"songs/{b}/source.gp5" in names
        assert f"songs/{a}/practice.jsonl" in names and f"songs/{b}/practice.jsonl" not in names
        stored = json.loads(zf.read(f"songs/{a}/song.json"))
        assert "n" not in stored["bars"][0] and "title" not in stored["meta"]  # 派生字段不进包
        assert not any(n.endswith(".bak") for n in names)
        assert "config.json" not in " ".join(names)
    _wipe()

    with open(path, "rb") as f:
        r = bundles.import_bundles([("x.zip", f)])
    assert [s["id"] for s in r["imported"]] == [a, b] and r["skipped"] == []
    for sid, old in before.items():
        new = store.load(sid)
        assert new.pop("imported")
        assert new == model.with_defaults(old)  # 原有的都在，只多了有默认值的字段（tags: [] 之类）
    assert (store.song_dir(a) / "pages" / "p1.png").read_bytes()
    assert (store.song_dir(b) / "source.gp5").read_bytes() == GP5
    assert {s["id"] for s in views.list_library()} == {a, b}
    assert practice.aggregate(a, store.load(a)) == played


def test_export_all_and_single_name(data_dir):
    a = _image_song("勿听")
    _gp_song()
    path, _ = bundles.export_bundle([])
    with _zip(path) as zf:
        assert len(json.loads(zf.read("tab-trainer.json"))["songs"]) == 2
    _, name = bundles.export_bundle([a])
    assert name == "勿听 - 曲子包.zip"
    with pytest.raises(Invalid):
        bundles.export_bundle(["nope-1234"])


def test_import_multiple_bundles_and_skip_duplicates(data_dir):
    a, b = _image_song(), _gp_song()
    pa, _ = bundles.export_bundle([a])
    pb, _ = bundles.export_bundle([a, b])
    shutil.rmtree(store.song_dir(a))  # b 还在曲库里
    with open(pa, "rb") as fa, open(pb, "rb") as fb:
        r = bundles.import_bundles([("a.zip", fa), ("ab.zip", fb), ("note.txt", io.BytesIO(b"hello"))])
    assert [s["id"] for s in r["imported"]] == [a]
    reasons = {(s["title"], s["reason"][:6]) for s in r["skipped"]}
    # 第二个包里的 a 刚导入过、b 本来就在：都跳过；txt 不是包
    assert len(r["skipped"]) == 3 and ("note.txt", "不是曲子包（") in reasons
    assert [s["duplicate"] for s in r["skipped"]] == [True, True, False]


def test_import_normalizes_running_song(data_dir):
    sid = _image_song()
    store.patch(
        sid,
        bars=[],
        status="parsing",
        activity={"kind": "parse", "restaging": False, "systems": []},
        progress={"stage": "识别中", "done": 1, "total": 3},
    )
    path, _ = bundles.export_bundle([sid])
    _wipe()
    with open(path, "rb") as f:
        bundles.import_bundles([("x.zip", f)])
    s = store.load(sid)
    assert s["status"] == "error" and "中断" in s["notice"]["msg"] and s["activity"] is None


def _bundle(song: dict, files: dict[str, bytes], manifest: dict | None = None) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        m = manifest or {"format": bundles.FORMAT, "version": 1, "songs": [{"id": song["id"], "title": "t"}]}
        zf.writestr(bundles.MANIFEST, json.dumps(m))
        zf.writestr(f"songs/{song['id']}/song.json", json.dumps(song))
        for n, d in files.items():
            zf.writestr(n, d)
    buf.seek(0)
    return buf


@pytest.mark.parametrize(
    ("song", "files", "reason"),
    [
        ({"id": "evil-0001", "pages": [{"file": "../../x.png"}]}, {}, "页面文件名无效"),
        ({"id": "evil-0002", "pages": [{"file": "p1.png"}]}, {}, "缺少文件"),
        (
            {"id": "evil-0003", "pages": [{"file": "p1.png"}]},
            {"songs/evil-0003/pages/p1.png": b"nope"},
            "不是有效的图片",
        ),
        ({"id": "evil-0004", "source": "../source.gp5"}, {}, "原文件名无效"),
        ({"id": "evil-0005", "source": "source.gp5"}, {"songs/evil-0005/source.gp5": b"junk"}, "不是有效"),
        ({"id": "evil-0006", "schema_version": 99}, {}, "更新版本"),
    ],
)
def test_import_rejects_bad_songs(data_dir, song, files, reason):
    r = bundles.import_bundles([("x.zip", _bundle(song, files))])
    assert r["imported"] == [] and reason in r["skipped"][0]["reason"]
    assert not store.SONGS.exists() or [p.name for p in store.SONGS.iterdir()] == []  # 临时目录也清理掉了


def test_import_ignores_entries_outside_the_song(data_dir):
    song = {"id": "ok-00001", "pages": [], "bars": [], "status": "ready"}
    buf = _bundle(song, {"../../escape.txt": b"x", "songs/ok-00001/../../evil.txt": b"x"})
    r = bundles.import_bundles([("x.zip", buf)])
    assert [s["id"] for s in r["imported"]] == ["ok-00001"]
    assert sorted(p.name for p in store.song_dir("ok-00001").rglob("*")) == ["pages", "song.json"]
    assert not (data_dir / "escape.txt").exists() and not (data_dir / "evil.txt").exists()


def test_import_rejects_bad_manifest(data_dir):
    song = {"id": "ok-00001"}
    for m in (
        {"format": "other"},
        {"format": bundles.FORMAT, "version": 9, "songs": []},
        {"format": bundles.FORMAT},
    ):
        r = bundles.import_bundles([("x.zip", _bundle(song, {}, m))])
        assert r["imported"] == [] and "曲子包" in r["skipped"][0]["reason"]
    r = bundles.import_bundles(
        [("x.zip", _bundle(song, {}, {"format": bundles.FORMAT, "songs": [{"id": "../x"}]}))]
    )
    assert "id 无效" in r["skipped"][0]["reason"]


def test_import_size_budget(data_dir, monkeypatch):
    sid = _image_song()
    path, _ = bundles.export_bundle([sid])
    _wipe()
    monkeypatch.setattr(bundles.settings, "max_import_bytes", 100)
    with open(path, "rb") as f:
        r = bundles.import_bundles([("x.zip", f)])
    assert r["imported"] == [] and r["skipped"]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
@pytest.fixture
def client(data_dir, monkeypatch):
    monkeypatch.setattr(recognize, "start_parse", lambda sid: True)
    return TestClient(main.app, base_url="http://127.0.0.1:8765")


def test_http_export_then_import(client):
    a, b = _image_song("勿听"), _gp_song()
    r = client.get("/api/export", params={"ids": [a, b]})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "filename*=utf-8''" in r.headers["content-disposition"]
    one = client.get("/api/export", params={"ids": [a]})
    _wipe()
    r = client.post(
        "/api/import", files=[("files", ("all.zip", r.content)), ("files", ("one.zip", one.content))]
    ).json()
    assert {s["id"] for s in r["imported"]} == {a, b} and len(r["skipped"]) == 1
    assert client.get(f"/api/songs/{a}").json()["plan"]["text"] == "先练前四小节"

    assert client.get("/api/export", params={"ids": ["nope-1234"]}).status_code == 400
    assert client.get("/api/export").status_code == 200  # 整个曲库


def test_http_import_rejects_cross_site(client):
    r = client.post(
        "/api/import", files={"files": ("x.zip", b"x")}, headers={"Origin": "http://evil.example"}
    )
    assert r.status_code == 403


def test_import_rejects_songs_that_do_not_match_the_model(data_dir):
    song = {"id": "bad-00001", "status": "ready", "bars": [{"id": "a", "events": "nope"}]}
    r = bundles.import_bundles([("x.zip", _bundle(song, {}))])
    assert r["imported"] == [] and "格式不对" in r["skipped"][0]["reason"]
    assert not store.exists("bad-00001")


# ---------------------------------------------------------------------------
# 外来数据：时值、拍号、练习记录要按模型检查；读取一侧也不能因为一首坏数据让整个曲库 500
# ---------------------------------------------------------------------------
_META = {"tuning": ["E4", "B3", "G3", "D3", "A2", "E2"], "time_signature": [4, 4]}


def _v2(sid: str, **over) -> dict:
    return {
        "schema_version": 2,
        "id": sid,
        "source_type": "image",
        "status": "ready",
        "meta": _META,
        "bars": [{"id": "b1", "events": [{"dur": "4", "notes": []}]}],
        **over,
    }


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"bars": [{"id": "b1", "events": [{"dur": "1/0", "notes": []}]}]}, "时值"),
        ({"bars": [{"id": "b1", "events": [{"dur": "-1", "notes": []}]}]}, "时值"),
        ({"bars": [{"id": "b1", "events": [{"dur": "abc", "notes": []}]}]}, "时值"),
        ({"meta": {**_META, "time_signature": [4, 0]}}, "time_signature"),
        ({"meta": {**_META, "time_signature": [3, 3]}}, "time_signature"),
        ({"bars": [{"id": "b1", "events": [], "time_signature": [0, 4]}]}, "time_signature"),
        ({"bars": [{"id": "b1", "events": [], "ts_change": [7, 6]}]}, "ts_change"),
    ],
)
def test_import_rejects_bad_durations_and_time_signatures(client, over, reason):
    """reviewer 的 bad_dur / bad_more：以前这些能导入（200），之后曲库和曲子都 500。"""
    buf = _bundle(_v2("bad-song", **over), {})
    r = client.post("/api/import", files={"files": ("b.zip", buf.getvalue())})
    assert r.status_code == 200 and r.json()["imported"] == []
    assert reason in r.json()["skipped"][0]["reason"]
    assert client.get("/api/songs").status_code == 200 and not store.exists("bad-song")


def test_import_drops_malformed_practice_lines(client):
    lines = [
        {"ids": ["b1"], "loops": "x", "seconds": 1},
        {"ids": "b1", "loops": 1},
        {"ids": [], "loops": 1},
        {"ids": ["b1"], "loops": 1, "seconds": -5},
        {"seed": True, "bars": {"b1": {"loops": "many"}}},
        [1, 2],
        {"at": "2026-01-01 10:00", "ids": ["b1"], "loops": 2, "bpm": 60, "seconds": 30},
    ]
    text = "\n".join(json.dumps(x) for x in lines) + "\nnot json\n"
    buf = _bundle(_v2("ok-song"), {"songs/ok-song/practice.jsonl": text.encode()})
    r = client.post("/api/import", files={"files": ("b.zip", buf.getvalue())})
    assert [s["id"] for s in r.json()["imported"]] == ["ok-song"]
    kept = store.read_jsonl(store.practice_path("ok-song"))
    assert [x["loops"] for x in kept] == [2]
    assert client.get("/api/songs").status_code == 200
    v = client.get("/api/songs/ok-song").json()
    assert v["practice"]["bars"]["b1"]["loops"] == 2 and v["practice"]["total_seconds"] == 30


def test_bad_data_on_disk_does_not_break_the_library(client):
    """读取一侧：song.json 被改坏（时值 1/0）只让这一首显示成“数据损坏”；practice.jsonl 的坏行跳过。"""
    good = _gp_song()
    d = store.SONGS / "20260101-000000-baddur"
    d.mkdir(parents=True)
    (d / "song.json").write_text(
        json.dumps(_v2(d.name, bars=[{"id": "b1", "events": [{"dur": "1/0", "notes": []}]}]))
    )
    bad_practice = '{"ids": ["x"], "loops": "x"}\n{"ids": "abc"}\n{"seed": true, "bars": []}\n'
    store.practice_path(good).write_text(bad_practice)
    r = client.get("/api/songs")
    assert r.status_code == 200
    rows = {x["id"]: x for x in r.json()}
    assert rows[d.name]["status"] == "corrupt" and "格式不对" in rows[d.name]["notice"]["msg"]
    assert rows[good]["status"] == "ready" and rows[good]["practice"] is None
    assert client.get(f"/api/songs/{good}").json()["practice"]["total_seconds"] == 0
    assert client.delete(f"/api/songs/{d.name}").status_code == 200


def test_old_songs_with_empty_meta_roundtrip(data_dir):
    """reviewer 的 bundle_meta：早期版本没读出谱头时存的是 meta: {}（排队中、识别失败、GP 还没打开过），
    升级后应变成 None，导出再导入不会因为 Meta 缺调弦被跳过。"""
    from PIL import Image

    old = {
        "20250101-000000-aaaaaa": {"status": "queued", "error": None},
        "20250101-000000-bbbbbb": {"status": "error", "error": "识别失败"},
        "20250101-000000-cccccc": {"status": "imported", "kind": "score", "source": "source.gp5"},
    }
    for sid, extra in old.items():
        d = store.SONGS / sid
        (d / "pages").mkdir(parents=True)
        song = {"id": sid, "title": sid[-6:], "created": "2025", "progress": None, "pages": [], "meta": {}}
        song |= {"systems": [], "bars": [], "plan": None, "usage": {}, "schema_version": 1, **extra}
        if "source" in extra:
            (d / "source.gp5").write_bytes(GP5)
        else:
            Image.new("RGB", (10, 10), "white").save(d / "pages" / "p1.png")
            song["pages"] = [{"file": "p1.png", "width": 10, "height": 10}]
        (d / "song.json").write_text(json.dumps(song, ensure_ascii=False))
    songs.recover_interrupted()
    for sid in old:
        assert json.loads((store.SONGS / sid / "song.json").read_text())["meta"] is None
    before = {sid: views.song_view(model.with_defaults(store.load(sid))) for sid in old}
    path, _ = bundles.export_bundle([])
    _wipe()
    with open(path, "rb") as f:
        r = bundles.import_bundles([("b.zip", f)])
    assert r["skipped"] == [] and {s["id"] for s in r["imported"]} == set(old)
    for sid, v in before.items():
        after = views.song_view(store.load(sid))
        assert {k: x for k, x in after.items() if k not in ("rev", "imported")} == {
            k: x for k, x in v.items() if k not in ("rev", "imported")
        }, sid
    assert before["20250101-000000-bbbbbb"]["notice"]["msg"] == "识别失败"


def test_import_accepts_empty_meta_from_older_bundles(data_dir):
    song = _v2("old-meta-1", meta={}, bars=[], status="error")
    r = bundles.import_bundles([("x.zip", _bundle(song, {}))])
    assert [s["id"] for s in r["imported"]] == ["old-meta-1"] and store.load("old-meta-1")["meta"] is None


@pytest.mark.parametrize(
    "bad",
    [
        {"activity": "parse"},
        {"bars": ["x"]},
        {"systems": [1]},
        {"plan": []},
        {"notice": "oops"},
        {"schema_version": 1, "bars": [{"id": "a", "events": [], "_orig": 5}]},
    ],
)
def test_one_malformed_song_does_not_abort_the_import(data_dir, bad):
    """reviewer 的 bad_bundle：结构不对的 song.json 以前抛 AttributeError → 500，整个包都没导入。"""
    buf = io.BytesIO()
    good = {"schema_version": 2, "id": "good-song", "source_type": "image", "status": "ready", "bars": []}
    broken = {"schema_version": 2, "id": "bad-song", "source_type": "image", "status": "ready", **bad}
    with zipfile.ZipFile(buf, "w") as zf:
        ids = ["bad-song", "good-song"]
        zf.writestr(
            bundles.MANIFEST,
            json.dumps({"format": bundles.FORMAT, "version": 1, "songs": [{"id": i} for i in ids]}),
        )
        zf.writestr("songs/bad-song/song.json", json.dumps(broken))
        zf.writestr("songs/good-song/song.json", json.dumps(good))
    buf.seek(0)
    r = bundles.import_bundles([("b.zip", buf)])
    assert [s["id"] for s in r["imported"]] == ["good-song"]
    assert [s["title"] for s in r["skipped"]] == ["bad-song"] and "格式不对" in r["skipped"][0]["reason"]
    assert not store.exists("bad-song") and [p.name for p in store.SONGS.iterdir()] == ["good-song"]


def test_old_bundle_with_a_single_warning_string_imports(data_dir):
    song = {
        "schema_version": 1,
        "id": "old-warn-1",
        "meta": {"title": "a"},
        "bars": [{"id": "a", "events": [], "warn": "oops"}],
    }
    r = bundles.import_bundles([("x.zip", _bundle(song, {}))])
    assert [s["id"] for s in r["imported"]] == ["old-warn-1"]
    s = store.load("old-warn-1")
    assert (
        s["title"] == "a" and s["meta"] is None and s["bars"][0]["warn"] == [{"code": "other", "msg": "oops"}]
    )


def test_export_whole_library_skips_a_structurally_bad_song(data_dir):
    """曲库里有一首 song.json 结构不对（合法 JSON）：导出整个曲库时跳过它，其余照常导出。"""
    sid = _image_song()
    d = data_dir / "songs" / "20260101-000000-aaaaaa"
    d.mkdir(parents=True)
    (d / "song.json").write_text(json.dumps({"schema_version": 2, "source_type": "image", "bars": [None]}))
    path, _ = bundles.export_bundle([])
    try:
        names = _zip(path).namelist()
        assert f"songs/{sid}/song.json" in names
        assert not any("aaaaaa" in n for n in names)
    finally:
        path.unlink()
    with pytest.raises(Invalid):
        bundles.export_bundle(["20260101-000000-aaaaaa"])


def test_practice_times_must_look_like_local_times(client):
    """练习记录的 at 会显示在曲库里：只认 "YYYY-MM-DD HH:MM"（可带秒）或空，别的（外来包里塞的 HTML 之类）整行不要。
    包里的和磁盘上手改的都一样。"""
    bad = '<img src=x onerror="alert(1)">'
    lines = [
        {"at": bad, "ids": ["b1"], "loops": 5, "bpm": 60, "seconds": 10},
        {"at": "2026-01-01 10:00\n<b>", "ids": ["b1"], "loops": 5, "bpm": 60, "seconds": 10},
        {"seed": True, "bars": {"b1": {"loops": 5, "last": bad}}, "last": {"a": "b1", "at": bad}},
        {"seed": True, "bars": {}, "last": {"a": "b1", "at": bad}},
        {"at": "2026-01-01 10:00", "ids": ["b1"], "loops": 2, "bpm": 60, "seconds": 30},
        {"at": "2026-01-02 10:00:05", "ids": ["b1"], "loops": 1, "bpm": 60, "seconds": 30},
        {"ids": ["b1"], "loops": 1, "bpm": 60, "seconds": 30},  # 没记时间的旧记录
    ]
    text = "\n".join(json.dumps(x) for x in lines) + "\n"
    buf = _bundle(_v2("ok-song"), {"songs/ok-song/practice.jsonl": text.encode()})
    r = client.post("/api/import", files={"files": ("b.zip", buf.getvalue())})
    assert [s["id"] for s in r.json()["imported"]] == ["ok-song"]
    kept = store.read_jsonl(store.practice_path("ok-song"))
    assert [x["at"] for x in kept] == ["2026-01-01 10:00", "2026-01-02 10:00:05", ""]
    store.practice_path("ok-song").write_text(text)  # 磁盘上被改成这样：读取时同样跳过
    v = client.get("/api/songs/ok-song").json()
    assert v["practice"]["bars"]["b1"]["loops"] == 4 and v["practice"]["last"]["at"] == ""
    assert bad not in json.dumps(client.get("/api/songs").json())


def test_import_fills_in_missing_fields(client):
    """包里的小节缺 notes / tip 之类有默认值的字段：导入时补上，曲子能打开、能在曲库里看到。"""
    sid = "20260101-000000-sparse"
    song = _v2(sid, bars=[{"id": "b1", "events": [{"dur": "1"}]}])
    r = bundles.import_bundles([("x.zip", _bundle(song, {}))])
    assert [x["id"] for x in r["imported"]] == [sid]
    assert client.get(f"/api/songs/{sid}").status_code == 200
    assert sid in [x["id"] for x in client.get("/api/songs").json()]
    saved = json.loads((store.SONGS / sid / "song.json").read_text())
    assert saved["bars"][0]["events"][0]["notes"] == [] and "_orig" not in saved["bars"][0]
