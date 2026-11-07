"""曲子包：导出再导入后数据完整；多首、多个包一起导入；重复、损坏、外来文件里的危险内容。"""

import io
import json
import shutil
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import bundles, main, practice, recognize, songs, store, views
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
        assert new == old
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
