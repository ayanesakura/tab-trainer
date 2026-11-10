"""song.json 的格式升级（migrate.SCHEMA_VERSION = 2）：旧数据读出来就是当前格式，启动时写回。"""

import json

import pytest

from app import migrate, practice, songs, store, views
from app.text import fix_mojibake
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


def _as_cp1252(text: str) -> str:
    """GBK 字节按 windows-1252 读出来的样子（alphaTab 的读法；1252 没定义的 5 个字节按 Latin-1）。"""
    out = []
    for b in text.encode("gbk"):
        try:
            out.append(bytes([b]).decode("cp1252"))
        except UnicodeDecodeError:
            out.append(chr(b))
    return "".join(out)


MUST_FIX = ["押尾光太郎", "我们的歌", "晴天", "周杰倫", "我們的歌", "闹木 扒谱（TransfmrC修改）"]
MUST_NOT_CHANGE = [
    "Canção",
    "Coração",
    "Ação",
    "Größe",
    "Mötley Crüe",
    "Café",
    "Für Elise",
    "Señor",
    "Ñandú",
    "Beyoncé",
]


@pytest.mark.parametrize("text", MUST_FIX)
def test_mojibake_must_fix(text):
    """FIX-CONTRACT 的统一规则（前端 score.js 用同一套）：cp1252 读法和 Latin-1 读法都能还原。"""
    assert fix_mojibake(_as_cp1252(text)) == text
    assert fix_mojibake(text.encode("gbk").decode("latin-1")) == text


@pytest.mark.parametrize(
    "text", [*MUST_NOT_CHANGE, "Pokémon", "Café del Mar", "押尾光太郎", "Romance", "", "Ångström"]
)
def test_mojibake_must_not_change(text):
    assert fix_mojibake(text) == text


def test_mojibake_cp1252_range_and_edge_cases():
    moji = _as_cp1252("我們的歌")
    assert "\u201a" in moji and "\u0192" in moji  # 1252 的 0x80–0x9F 区字符（‚ƒ，latin-1 还原不了）
    assert "\x90" in _as_cp1252("周杰倫")  # 1252 没定义的字节按 Latin-1
    assert MOJI == "Ñº\xce²¹âÌ«ÀÉ" and fix_mojibake("Ñº¹ãÌ«ÀÉ") != "Ñº¹ãÌ«ÀÉ"
    assert fix_mojibake("Ab€") == "Ab€"  # 还原出的字节解不成 GBK
    assert fix_mojibake("中文" + _as_cp1252("晴天")) == "中文" + _as_cp1252("晴天")  # 有字符还原不成字节


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


def test_old_notices_get_the_source_that_clears_them(data_dir, sync_jobs):
    """reviewer 的 gp_notice：旧 GP 曲子解析失败的 error 没记 error_kind，升级后 source 是空的，
    之后浏览器解析成功（set_derived）也清不掉，提示永远挂着。"""
    from app import coach
    from tests.fakes import derived_bars

    gp = {"title": "x", "created": "2025", "kind": "score", "source": "source.gp", "meta": {}}
    gp |= {"progress": None, "pages": [], "systems": [], "bars": [], "plan": None, "schema_version": 1}
    cases = {
        "20250101-000000-aaaaaa": ({**gp, "status": "error", "error": "曲谱文件无法读取"}, "import"),
        "20250101-000000-bbbbbb": (
            {
                **gp,
                "status": "ready",
                "error": "服务重启，讲解生成中断，可以重新生成。",
                "bars": [{"events": [{"dur": "4", "notes": []}]}],
                "meta": {"tuning": ["E4", "B3", "G3", "D3", "A2", "E2"], "time_signature": [4, 4]},
            },
            "explain",
        ),
        "20250101-000000-cccccc": (
            {"title": "y", "status": "error", "error": "识别失败", "bars": []},
            "parse",
        ),
        "20250101-000000-dddddd": (
            {"title": "z", "status": "ready", "error": "补页失败", "error_kind": "pages"},
            "reparse_row",
        ),
    }
    for sid, (old, _) in cases.items():
        _write_old(sid, {"id": sid, **old})
    songs.recover_interrupted()
    for sid, (_, source) in cases.items():
        assert store.load(sid)["notice"]["source"] == source, sid

    # 成功的路径清掉自己的提示
    v = songs.set_derived("20250101-000000-aaaaaa", {"tuning": ["E4"] * 6}, derived_bars(1), 0, None)
    assert v["status"] == "ready" and v["notice"] is None
    coach.start_explain("20250101-000000-bbbbbb")
    assert store.load("20250101-000000-bbbbbb")["notice"] is None


@pytest.mark.parametrize(
    ("source", "fixed"), [("x.gp5", True), ("x.gpx", True), ("x.gp", False), ("x.xml", False)]
)
def test_set_derived_fixes_mojibake_only_for_old_gp_formats(data_dir, source, fixed):
    from tests.fakes import derived_bars

    data = b"\x18FICHIER GUITAR PRO v5.10" if source.endswith("gp5") else b"PK\x03\x04"
    if source.endswith("gpx"):
        data = b"BCFZ"
    if source.endswith("xml"):
        data = b"<score-partwise>"
    sid = songs.create_song([(source, data)])
    moji = _as_cp1252("押尾光太郎")
    s = songs.set_derived(sid, {"tuning": ["E4"] * 6, "artist": moji}, derived_bars(1), 0, None)
    assert s["meta"]["artist"] == ("押尾光太郎" if fixed else moji)  # GP7+ / MusicXML 本来就是 UTF-8


def test_newer_schema_is_refused_and_never_written_back(data_dir):
    """更新版本的 tab-trainer 存的曲子：旧代码不读（说明要升级），也不会按旧格式写回去、丢掉新字段。"""
    from app import errors

    sid = "20260101-000000-bbbbbb"
    newer = {**_old_gp_song(), "id": sid, "schema_version": migrate.SCHEMA_VERSION + 1, "future": {"x": 1}}
    _write_old(sid, newer)
    path = store.SONGS / sid / "song.json"
    before = path.read_bytes()
    with pytest.raises(errors.NewerSchema):
        store.load(sid)
    with pytest.raises(errors.NewerSchema):
        store.update(sid, lambda s: s.update(title="x"))
    songs.recover_interrupted()
    assert path.read_bytes() == before and not (path.parent / "song.json.bak").exists()
    row = next(x for x in views.list_library() if x["id"] == sid)
    assert row["status"] == "corrupt" and "更新版本" in row["notice"]["msg"]
    store.delete(sid)  # 还是可以删掉
