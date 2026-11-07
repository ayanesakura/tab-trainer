"""song.json 的格式升级（migrate.SCHEMA_VERSION = 2）：旧数据读出来就是当前格式，启动时写回。"""

import json

from app import migrate, practice, songs, store, views
from tests.fakes import score_png

MOJI = "押尾光太郎".encode("gbk").decode("latin-1")  # GBK 当成 Latin-1 读出来的乱码


def _write_old(sid: str, song: dict) -> None:
    d = store.SONGS / sid
    (d / "pages").mkdir(parents=True, exist_ok=True)
    (d / "song.json").write_text(json.dumps(song, ensure_ascii=False))


def _old_gp_song() -> dict:
    return {
        "id": "20250101-000000-aaaaaa",
        "title": MOJI + " - Fight",
        "created": "2025-01-01 10:00",
        "kind": "score",
        "source": "source.gp5",
        "status": "parsing",  # 更早的版本：讲解期间借用了 status="parsing"
        "job": "explain",
        "job_prev": {"status": "ready", "error": None},
        "error": None,
        "progress": {"stage": "生成讲解", "done": 1, "total": 2},
        "meta": {
            "title": MOJI,
            "artist": MOJI,
            "tuning": ["E4", "B3", "G3", "D3", "A2", "E2"],
            "tuning_midi": [64, 59, 55, 50, 45, 40],
            "capo": 0,
            "time_signature": [4, 4],
        },
        "bars": [
            {"n": 1, "events": [{"dur": "4", "notes": []}], "warn": ["导入时这一小节的数据无法使用：x"]},
            {"id": "b2", "n": 2, "events": [{"dur": "3", "notes": []}], "warn": ["时值合计 3 拍，应为 4 拍"]},
        ],
        "plan": {"overview": "ok", "steps": [], "stale": True, "changed_bars": []},
        "practice": {
            "bars": {"b2": {"loops": 5, "seconds": 60, "best_bpm": 70, "last": "2025-01-02 10:00"}},
            "last": {"a": "b2", "b": "b2", "bpm": 70, "at": "2025-01-02 10:00"},
            "total_seconds": 90,
        },
    }


def test_old_gp_song_is_upgraded_on_read_and_written_back_at_startup(data_dir):
    old = _old_gp_song()
    sid = old["id"]
    _write_old(sid, old)
    s = store.load(sid)
    assert s["schema_version"] == migrate.SCHEMA_VERSION == 2
    assert s["source_type"] == "gp" and "kind" not in s
    assert s["title"] == "押尾光太郎 - Fight" and s["meta"]["artist"] == "押尾光太郎"  # 乱码修好
    assert s["meta"]["title"] == s["title"]  # 曲名只有一份，meta.title 是读出来时补上的
    assert s["status"] == "ready" and s["activity"]["kind"] == "explain" and s["notice"] is None
    for k in ("job", "job_prev", "error", "error_kind", "restaging"):
        assert k not in s
    b1, b2 = s["bars"]
    assert b1["id"] and b1["n"] == 1
    assert b1["warn"] == [{"code": "other", "msg": "导入时这一小节的数据无法使用：x"}]
    assert b2["warn"] == [{"code": "length", "msg": "时值合计 3 拍，应为 4 拍"}]
    # 旧的练习记录：还没写回时也能读到（内存里的种子）
    p = practice.aggregate(sid, s)
    assert p["bars"]["b2"]["loops"] == 5 and p["total_seconds"] == 90 and p["last"]["a_n"] == 2
    assert not (store.SONGS / sid / "song.json.bak").exists()  # 平时读不写文件

    songs.recover_interrupted()  # 启动：讲解被打断 → 中断；旧格式写回
    raw = json.loads((store.SONGS / sid / "song.json").read_text())
    assert raw["schema_version"] == 2 and "practice" not in raw and "kind" not in raw
    assert raw["activity"] is None and "讲解生成中断" in raw["notice"]["msg"]
    assert "title" not in raw["meta"] and "tuning_midi" not in raw["meta"] and "stale" not in raw["plan"]
    assert all("n" not in b for b in raw["bars"])
    assert raw["bars"][1]["warn"] == []  # 时值警告是读取时算的，不存盘
    assert raw["bars"][0]["warn"][0]["code"] == "other"
    lines = (store.SONGS / sid / "practice.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["seed"] is True
    s = store.load(sid)
    assert practice.aggregate(sid, s)["bars"]["b2"]["loops"] == 5
    practice.record(sid, ["b2"], 1, 80, 10)
    p = practice.aggregate(sid, store.load(sid))
    assert p["bars"]["b2"]["loops"] == 6 and p["bars"]["b2"]["best_bpm"] == 80 and p["total_seconds"] == 100
    assert practice.history(sid, s)[0]["bpm"] == 80  # 种子不算一段练习
    store.patch(sid, title="再存一次")  # 种子只写一次
    assert len((store.SONGS / sid / "practice.jsonl").read_text().splitlines()) == 2


def test_old_image_song_states(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    path = store.song_dir(sid) / "song.json"
    raw = json.loads(path.read_text())
    bar = {
        "events": [{"dur": "4", "notes": []}],
        "warn": [],
        "_orig": {"events": [], "tip": "", "uncertain": ""},
    }
    cases = [
        # (旧字段, 期望的 status, activity.kind, restaging, notice)
        ({"status": "parsing", "restaging": True, "bars": [bar]}, "ready", "parse", True, None),
        ({"status": "ready", "job": "pages", "bars": [bar]}, "ready", "reparse_row", False, None),
        ({"status": "queued"}, "queued", "parse", False, None),
        ({"status": "ready", "error": "第 2 行：网络断了", "bars": [bar]}, "ready", None, False, "warn"),
        ({"status": "error", "error": "没有识别出任何小节"}, "error", None, False, "error"),
        (
            {"status": "ready", "error": "讲解失败", "error_kind": "explain", "bars": [bar]},
            "ready",
            None,
            False,
            "warn",
        ),
    ]
    for old, status, kind, restaging, notice in cases:
        path.write_text(json.dumps({**{k: v for k, v in raw.items() if k != "schema_version"}, **old}))
        s = store.load(sid)
        assert s["status"] == status, old
        assert (s["activity"] or {}).get("kind") == kind, old
        assert bool((s["activity"] or {}).get("restaging")) == restaging, old
        assert (s["notice"] or {}).get("kind") == notice, old
        if s["bars"]:
            assert s["bars"][0]["edited"] is True  # 有 _orig 的是手改过的
    assert s["notice"]["source"] == "explain"


def test_source_type_is_guessed_for_old_songs():
    assert migrate.source_type_of({"kind": "score", "source": "source.gp5"}) == "gp"
    assert migrate.source_type_of({"kind": "score", "source": "source.mxl"}) == "musicxml"
    assert migrate.source_type_of({}) == "image"  # 图片和 PDF 当时没区分
    assert migrate.source_type_of({"source_type": "pdf"}) == "pdf"


def test_mojibake_fix_only_touches_real_mojibake():
    assert migrate.fix_mojibake(MOJI) == "押尾光太郎"
    assert migrate.fix_mojibake("Ñº¹ãÌ«ÀÉ") != "Ñº¹ãÌ«ÀÉ"
    for ok in ("Pokémon", "Café del Mar", "押尾光太郎", "Romance", "", "Ångström"):
        assert migrate.fix_mojibake(ok) == ok
    assert MOJI == "Ñº\xce²¹âÌ«ÀÉ"


def test_mojibake_is_fixed_for_every_source_type():
    """评测用的图片曲子（eval-r-mother 等）是从 GP 复制的谱头，艺人也是乱码：不分来源都修。"""
    for extra in (
        {},
        {"source_type": "pdf"},
        {"source_type": "musicxml", "source": "source.xml"},
        {"kind": "score", "source": "source.gp5"},
    ):
        meta = {
            "title": "Mother",
            "artist": MOJI,
            "arranger": MOJI,
            "tuning": ["E4"] * 6,
            "time_signature": [4, 4],
        }
        s = migrate.migrate({"title": "", "bars": [], "status": "ready", "meta": meta, **extra}, "x")
        assert s["meta"]["artist"] == "押尾光太郎" and s["meta"]["arranger"] == "押尾光太郎", extra
        assert s["title"] == "Mother"
    s = migrate.migrate({"title": MOJI, "bars": [], "status": "ready"}, "x")
    assert s["title"] == "押尾光太郎"
    s = migrate.migrate({"title": "Pokémon", "bars": [], "status": "ready", "schema_version": 2}, "x")
    assert s["title"] == "Pokémon"  # 已经是 v2 的不再处理；正常西文本来也不动


def test_library_lists_old_songs(data_dir):
    old = _old_gp_song()
    _write_old(old["id"], old)
    row = views.list_library()[0]
    assert row["title"] == "押尾光太郎 - Fight" and row["source_type"] == "gp"
    assert row["practice"]["total_seconds"] == 90 and row["needs_review_bars"] == 2
