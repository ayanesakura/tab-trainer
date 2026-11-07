"""曲库：上传、Guitar Pro / MusicXML 导入与重新转换、手工修改、启动恢复。"""

import json

import pytest

from app import errors, music, practice, recognize, songs, store
from tests.fakes import FakeClaude, derived_bars, score_png


def W(code, msg):
    return {"code": code, "msg": msg}


def test_edit_bar_and_meta(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)

    bar_id = store.load(sid)["bars"][1]["id"]
    song = songs.edit_bar(sid, bar_id, [{"dur": "3", "notes": []}], tip="改过")
    bar = song["bars"][1]
    assert bar["tip"] == "改过" and bar["warn"] == [W("length", "时值合计 3 拍，应为 4 拍")]
    assert bar["edited"] and bar["_orig"]["events"] != bar["events"]

    with pytest.raises(ValueError):
        songs.edit_bar(sid, bar_id, [{"dur": "abc", "notes": []}], tip=None)

    song = songs.edit_meta(sid, {"time_signature": [3, 4], "tuning": ["E4", "B3", "G3", "D3", "A2", "E2"]})
    assert song["meta"]["tuning_midi"] == [64, 59, 55, 50, 45, 40]
    assert song["bars"][1]["warn"] == []  # 3 拍的小节在 3/4 下合法
    assert song["bars"][0]["warn"]  # 4 拍的小节在 3/4 下不合法


def test_edit_meta_rejects_bad_values(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    # 变调夹的范围在 schemas.MetaIn 里检查（见 test_api）
    for bad in ({"time_signature": [None, 4]}, {"tuning": ["E4", "B3", "G3", "D3", "A2", "Q"]}):
        with pytest.raises(ValueError):
            songs.edit_meta(sid, bad)


def test_recover_interrupted_parse(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)

    def stuck(s):
        s["status"] = "parsing"
        s["activity"] = {"kind": "parse", "restaging": False, "systems": []}
        s["systems"][1]["status"] = "parsing"

    store.update(sid, stuck)
    songs.recover_interrupted()
    song = store.load(sid)
    assert song["status"] == "error" and "中断" in song["notice"]["msg"] and song["notice"]["kind"] == "error"
    assert song["activity"] is None
    assert song["systems"][1]["status"] == "error"


def _bad_song(data_dir, sid: str, content: dict) -> None:
    d = data_dir / "songs" / sid
    d.mkdir(parents=True)
    (d / "song.json").write_text(json.dumps({"schema_version": 2, "source_type": "image", **content}))


def test_recover_interrupted_skips_structurally_bad_songs(data_dir):
    """song.json 是合法 JSON、但结构不对（手改坏了）：跳过这一首，不能让服务起不来。"""
    sid = songs.create_song([("p1.png", score_png())])
    store.update(sid, lambda s: s.update(status="parsing"))
    _bad_song(data_dir, "20260101-000000-aaaaaa", {"status": "ready", "bars": [None]})
    _bad_song(data_dir, "20260101-000000-aaaaab", {"status": "parsing", "systems": [1]})
    songs.recover_interrupted()
    assert store.load(sid)["status"] == "error"  # 别的曲子照常恢复


def test_guitar_pro_upload_skips_ai_and_keeps_file(data_dir):
    sid = songs.create_song([("我的曲子.gp5", b"\x18FICHIER GUITAR PRO v5.10")])
    song = store.load(sid)
    assert song["source_type"] == "gp" and song["status"] == "imported" and song["title"] == "我的曲子"
    assert "kind" not in song
    assert (store.song_dir(sid) / song["source"]).read_bytes() == b"\x18FICHIER GUITAR PRO v5.10"
    assert not FakeClaude.calls
    with pytest.raises(ValueError):
        songs.create_song([("a.gp5", b"x"), ("b.png", score_png())])


@pytest.mark.parametrize(
    ("name", "data", "ok"),
    [
        ("a.gp5", b"\x18FICHIER GUITAR PRO v5.10", True),
        ("a.gp5", b"not a gp file", False),
        ("a.gpx", b"BCFZ....", True),
        ("a.gp", b"PK\x03\x04....", True),
        ("a.gp", b"%PDF-1.7", False),
        ("a.musicxml", b'<?xml version="1.0"?>\n<!DOCTYPE x>\n<score-partwise version="4.0">', True),
        ("a.musicxml", b"<html>hello</html>", False),
        ("a.mxl", b"PK\x03\x04", True),
    ],
)
def test_score_upload_checks_file_header(data_dir, name, data, ok):
    if ok:
        assert store.load(songs.create_song([(name, data)]))["status"] == "imported"
    else:
        with pytest.raises(ValueError, match="不是有效"):
            songs.create_song([(name, data)])


def test_import_lifecycle(data_dir):
    sid = songs.create_song([("x.gp5", b"\x18FICHIER GUITAR PRO v5.10")])
    song = songs.import_failed(sid, "Unsupported format")  # 浏览器解析失败
    assert song["status"] == "error" and song["notice"]["msg"] == "Unsupported format"
    song = songs.set_derived(sid, {"tuning": music.STANDARD}, derived_bars(2))  # 之后成功解析（比如换了音轨）
    assert song["status"] == "ready" and song["notice"] is None
    with pytest.raises(ValueError):
        songs.import_failed(songs.create_song([("p1.png", score_png())]), "x")


def test_set_derived_keeps_repeats(data_dir):
    sid = songs.create_song([("曲子.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    bars = derived_bars(2)
    bars[0]["repeat_start"] = True
    bars[1].update(repeat_end=3, directions=["fine"])
    song = songs.set_derived(sid, {"tuning": music.STANDARD}, bars)
    assert song["bars"][0]["repeat_start"] is True
    assert song["bars"][1]["repeat_end"] == 3 and song["bars"][1]["directions"] == ["fine"]


def test_set_derived_per_bar_time_signature(data_dir):
    sid = songs.create_song([("曲子.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    bars = derived_bars(4)
    bars[1] = {"time_signature": [3, 4], "events": [{"dur": "3", "notes": [{"string": 1, "fret": 27}]}]}
    bars[2] = {"time_signature": [6, 8], "events": [{"dur": "3", "notes": []}]}
    bars[3]["time_signature"] = [4, 4]  # 和全曲一样：不单独存
    song = songs.set_derived(sid, {"tuning": music.STANDARD, "time_signature": [4, 4]}, bars)
    b = song["bars"]
    assert [x.get("time_signature") for x in b] == [None, [3, 4], [6, 8], None]
    assert all(not x["warn"] for x in b), [x["warn"] for x in b]  # 按各自的拍号检查时值
    assert b[1]["events"][0]["notes"][0]["fret"] == 27
    # GP 曲子的小节来自原文件（只读的缓存），不能手改；拍号也来自原文件
    with pytest.raises(errors.Invalid, match="来自原文件"):
        songs.edit_bar(sid, b[1]["id"], [{"dur": "4", "notes": []}], None)
    with pytest.raises(ValueError, match="来自原文件"):
        songs.edit_meta(sid, {"time_signature": [2, 4]})
    # 按小节自己的拍号算时值提示（读取时算）；AI 看到的文字里带拍号
    store.update(sid, lambda s: s["bars"][1].update(events=[{"dur": "4", "notes": []}]))
    song = store.load(sid)
    assert song["bars"][1]["warn"] == [W("length", "时值合计 4 拍，应为 3 拍")]
    from app.prompts import bar_text

    assert bar_text(song["bars"][2]).startswith("第3小节（6/8 拍）:")


def test_image_bar_with_its_own_time_signature(data_dir):
    """图片曲子：清空的小节按它自己的拍号补休止。"""
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    store.update(sid, lambda s: s["bars"][2].update(ts_change=[6, 8]))
    store.update(sid, lambda s: music.apply_time_signatures(s["bars"], s["meta"]))
    bar = store.load(sid)["bars"][2]
    song = songs.edit_bar(sid, bar["id"], [], None)
    assert song["bars"][2]["events"] == [{"dur": "3", "notes": []}] and not song["bars"][2]["warn"]


def test_rederive_keeps_unchanged_bars(data_dir):
    """转换器 / alphaTab 升级后重新转换：内容没变的小节保留 id、提示、问答和练习记录，变了的按改过音处理。"""
    sid = songs.create_song([("曲子.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    meta = {"tuning": music.STANDARD}
    song = songs.set_derived(sid, meta, derived_bars(3), derived={"version": 1, "alphatab": "1.8.0"})
    assert song["derived"] == {"version": 1, "alphatab": "1.8.0"}
    ids = [b["id"] for b in song["bars"]]

    def seed(s):
        for i, b in enumerate(s["bars"]):
            b["tip"] = f"提示 {i}"
            b["qa"] = [{"q": "问", "a": "答"}]
        s["plan"] = {"overview": "ok", "steps": []}

    store.update(sid, seed)
    practice.record(sid, ids, 3, 60, 30)
    songs.edit_meta(sid, {"title": "我改的名字"})
    bars = derived_bars(3)
    bars[1]["events"][0]["notes"][0]["fret"] = 9  # 新的转换规则读出来不一样
    song = songs.set_derived(sid, meta, bars, derived={"version": 2, "alphatab": "1.9.0"})
    b = song["bars"]
    assert [x["id"] for x in b] == ids
    assert b[0]["tip"] == "提示 0" and not b[0]["qa"][0].get("stale")
    assert b[1]["tip"] == "" and b[1]["qa"][0]["stale"]
    assert song["plan"]["stale"] and song["plan"]["changed_bars"] == [ids[1]]
    assert set(practice.aggregate(sid, song)["bars"]) == set(ids)
    assert song["meta"]["title"] == song["title"] == "我改的名字"
    assert song["derived"] == {"version": 2, "alphatab": "1.9.0"}

    # 换音轨：是另一份谱子，全部重来
    song = songs.set_derived(sid, meta, bars, track=1, derived={"version": 2, "alphatab": "1.9.0"})
    assert not {x["id"] for x in song["bars"]} & set(ids)
    assert song["plan"] is None and not practice.aggregate(sid, song)["bars"]
    assert practice.aggregate(sid, song)["total_seconds"] == 30  # 记录本身还在，只是小节对不上
    assert all(x["tip"] == "" for x in song["bars"])


def test_import_error_is_ignored_once_the_song_was_parsed(data_dir):
    sid = songs.create_song([("x.gp5", b"\x18FICHIER GUITAR PRO v5.10")])
    songs.set_derived(sid, {"tuning": music.STANDARD}, derived_bars(2))
    assert songs.import_failed(sid, "临时的网络错误")["status"] == "ready"


def test_recover_interrupted_explain_keeps_score_song_playable(data_dir):
    sid = songs.create_song([("x.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    songs.set_derived(sid, {}, derived_bars(2))
    store.update(sid, lambda s: s.update(activity={"kind": "explain", "restaging": False, "systems": []}))
    songs.recover_interrupted()
    song = store.load(sid)
    assert song["status"] == "ready" and "中断" in song["notice"]["msg"] and song["activity"] is None


def test_score_song_settings_come_from_the_file(data_dir):
    """GP 曲子播放用的是原文件：调弦、变调夹、拍号不能改（改了指板和听到的对不上），曲名可以。"""
    sid = songs.create_song([("曲子.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    songs.set_derived(sid, {"tuning": music.STANDARD, "capo": 2}, derived_bars(2))
    for change in ({"capo": 5}, {"tuning": ["D4", "B3", "G3", "D3", "A2", "D2"]}, {"time_signature": [3, 4]}):
        with pytest.raises(ValueError, match="来自原文件"):
            songs.edit_meta(sid, change)
    song = songs.edit_meta(sid, {"title": "新名字", "capo": 2})  # 设定没变，只改曲名
    assert song["title"] == "新名字" and song["meta"]["capo"] == 2


def test_rederive_with_a_different_bar_count_keeps_matching_bars(data_dir):
    """转换器升级后小节数变了（比如反复记号展开方式变了）：按内容对上，没变的小节保留讲解和练习记录。"""
    sid = songs.create_song([("曲子.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    meta = {"tuning": music.STANDARD}

    def bar(f):
        return {"events": [{"dur": "4", "notes": [{"string": 2, "fret": f}]}]}

    song = songs.set_derived(sid, meta, [bar(f) for f in (1, 2, 3, 4)], derived={"version": 1})
    ids = [b["id"] for b in song["bars"]]

    def seed(s):
        for i, b in enumerate(s["bars"]):
            b["tip"] = f"提示 {i}"
        s["plan"] = {"overview": "ok", "steps": []}

    store.update(sid, seed)
    practice.record(sid, ids, 3, 60, 30)
    # 新的转换：第 2 小节后面多了一个小节，第 4 小节没了，第 3 小节内容变了
    song = songs.set_derived(sid, meta, [bar(f) for f in (1, 2, 9, 7)], derived={"version": 2})
    b = song["bars"]
    assert [x["id"] for x in b[:2]] == ids[:2] and [x["tip"] for x in b[:2]] == ["提示 0", "提示 1"]
    # 1 2 3 4 → 1 2 9 7：后两个是“替换”段，位置相当，保留 id 但提示清掉
    assert [x["id"] for x in b[2:]] == ids[2:] and [x["tip"] for x in b[2:]] == ["", ""]
    song = songs.set_derived(sid, meta, [bar(f) for f in (1, 2, 5, 9, 7)], derived={"version": 3})
    b = song["bars"]
    assert [x["id"] for x in b] == [ids[0], ids[1], b[2]["id"], ids[2], ids[3]] and b[2]["id"] not in ids
    assert b[0]["tip"] == "提示 0" and b[2]["tip"] == ""
    assert set(practice.aggregate(sid, song)["bars"]) == set(ids)
    assert song["plan"]["stale"] and song["plan"].get("stale_all")  # 小节号整体移动了


def test_source_type_is_recorded(data_dir):
    import fitz  # pymupdf

    with fitz.open() as doc:  # 一页 PDF，内容就是那张谱子图片
        page = doc.new_page()
        page.insert_image(page.rect, stream=score_png())
        pdf = doc.tobytes()
    xml = b'<?xml version="1.0"?>\n<score-partwise version="4.0">'
    cases = {
        "image": [("p1.png", score_png())],
        "pdf": [("谱子.pdf", pdf)],
        "gp": [("a.gp5", b"\x18FICHIER GUITAR PRO v5.10")],
        "musicxml": [("a.musicxml", xml)],
    }
    listed = {}
    for want, uploads in cases.items():
        sid = songs.create_song(uploads)
        assert store.load(sid)["source_type"] == want
        listed[sid] = want
    from app import views

    assert {s["id"]: s["source_type"] for s in views.list_library()} == listed
    # 早期的曲子没有 source_type：迁移时推断（见 test_migrate）


def test_bar_text_shows_chords_and_strums():
    from app.prompts import bar_text

    b = {
        "n": 2,
        "events": [{"dur": "1", "chord": "Em", "strum": "down", "notes": [{"string": 1, "fret": 0}]}],
    }
    assert bar_text(b) == "第2小节: [Em] 1拍 下扫 1弦0品"


def test_set_derived_keeps_chord_names_strums_and_shapes(data_dir):
    sid = songs.create_song([("x.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    bars = derived_bars(1)
    bars[0]["events"][0].update(chord="Em", strum="down")
    bars[0]["shapes"] = {"Em": [0, 0, 0, 2, 2, 0], "bad": [1, 2]}
    song = songs.set_derived(sid, {"tuning": music.STANDARD}, bars)
    b = song["bars"][0]
    assert b["events"][0]["chord"] == "Em" and b["events"][0]["strum"] == "down"
    assert b["shapes"] == {"Em": [0, 0, 0, 2, 2, 0]}


def test_lyrics_are_cleaned_per_verse():
    assert music.clean_lyrics([" 天真 ", "多浅", "", ""]) == ["天真", "多浅"]
    assert music.clean_lyrics(["", "第二段"]) == ["", "第二段"]
    assert music.clean_lyrics(["", ""]) == [] and music.clean_lyrics("天真") == []
    ev = music.clean_events([{"dur": "1", "notes": [], "lyrics": ["我", None]}])
    assert ev == [{"dur": "1", "notes": [], "lyrics": ["我"]}]
