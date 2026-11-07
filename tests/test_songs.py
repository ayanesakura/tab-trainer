"""曲库：上传、Guitar Pro / MusicXML 导入与重新转换、手工修改、启动恢复。"""

import pytest

from app import music, recognize, songs, store
from tests.fakes import FakeClaude, derived_bars, score_png


def test_edit_bar_and_meta(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)

    bar_id = store.load(sid)["bars"][1]["id"]
    song = songs.edit_bar(sid, bar_id, [{"dur": "3", "notes": []}], tip="改过")
    bar = song["bars"][1]
    assert bar["tip"] == "改过" and bar["warn"] == ["时值合计 3 拍，应为 4 拍"]

    with pytest.raises(ValueError):
        songs.edit_bar(sid, bar_id, [{"dur": "abc", "notes": []}], tip=None)

    song = songs.edit_meta(sid, {"time_signature": [3, 4], "tuning": ["E4", "B3", "G3", "D3", "A2", "E2"]})
    assert song["meta"]["tuning_midi"] == [64, 59, 55, 50, 45, 40]
    assert song["bars"][1]["warn"] == []  # 3 拍的小节在 3/4 下合法
    assert song["bars"][0]["warn"]  # 4 拍的小节在 3/4 下不合法


def test_edit_meta_rejects_bad_values(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    for bad in ({"time_signature": [None, 4]}, {"capo": 20}, {"tuning": ["E4", "B3", "G3", "D3", "A2", "Q"]}):
        with pytest.raises(ValueError):
            songs.edit_meta(sid, bad)


def test_recover_interrupted_parse(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)

    def stuck(s):
        s["status"] = "parsing"
        s["systems"][1]["status"] = "parsing"
        s["bars"][0].pop("id")

    store.update(sid, stuck)
    songs.recover_interrupted()
    song = store.load(sid)
    assert song["status"] == "error" and "中断" in song["error"]
    assert song["systems"][1]["status"] == "error"
    assert song["bars"][0]["id"]


def test_guitar_pro_upload_skips_ai_and_keeps_file(data_dir):
    sid = songs.create_song([("我的曲子.gp5", b"\x18FICHIER GUITAR PRO v5.10")])
    song = store.load(sid)
    assert song["kind"] == "score" and song["status"] == "imported" and song["title"] == "我的曲子"
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
    assert song["status"] == "error" and song["error"] == "Unsupported format"
    song = songs.set_derived(sid, {"tuning": music.STANDARD}, derived_bars(2))  # 之后成功解析（比如换了音轨）
    assert song["status"] == "ready" and song["error"] is None
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
    # 改音、改全曲拍号时也按小节自己的拍号检查
    song = songs.edit_bar(sid, b[1]["id"], [{"dur": "4", "notes": []}], None)
    assert song["bars"][1]["warn"] == ["时值合计 4 拍，应为 3 拍"]
    song = songs.edit_meta(sid, {"time_signature": [2, 4]})
    assert not song["bars"][2]["warn"] and song["bars"][0]["warn"]
    # 清空的小节按它自己的长度补休止；AI 看到的文字里带拍号
    song = songs.edit_bar(sid, b[2]["id"], [], None)
    assert song["bars"][2]["events"] == [{"dur": "3", "notes": []}]
    from app.llm import bar_text

    assert bar_text(song["bars"][2]).startswith("第3小节（6/8 拍）:")


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
        s["practice"] = {"bars": {i: {"loops": 3} for i in ids}}

    store.update(sid, seed)
    songs.edit_meta(sid, {"title": "我改的名字"})
    bars = derived_bars(3)
    bars[1]["events"][0]["notes"][0]["fret"] = 9  # 新的转换规则读出来不一样
    song = songs.set_derived(sid, meta, bars, derived={"version": 2, "alphatab": "1.9.0"})
    b = song["bars"]
    assert [x["id"] for x in b] == ids
    assert b[0]["tip"] == "提示 0" and not b[0]["qa"][0].get("stale")
    assert b[1]["tip"] == "" and b[1]["qa"][0]["stale"]
    assert song["plan"]["stale"] and song["plan"]["changed_bars"] == [ids[1]]
    assert set(song["practice"]["bars"]) == set(ids)
    assert song["meta"]["title"] == "我改的名字"
    assert song["derived"] == {"version": 2, "alphatab": "1.9.0"}

    # 换音轨：是另一份谱子，全部重来
    song = songs.set_derived(sid, meta, bars, track=1, derived={"version": 2, "alphatab": "1.9.0"})
    assert not {x["id"] for x in song["bars"]} & set(ids)
    assert song["plan"] is None and not song["practice"]["bars"]
    assert all(x["tip"] == "" for x in song["bars"])


def test_import_error_is_ignored_once_the_song_was_parsed(data_dir):
    sid = songs.create_song([("x.gp5", b"\x18FICHIER GUITAR PRO v5.10")])
    songs.set_derived(sid, {"tuning": music.STANDARD}, derived_bars(2))
    assert songs.import_failed(sid, "临时的网络错误")["status"] == "ready"


def test_recover_interrupted_explain_keeps_score_song_playable(data_dir):
    sid = songs.create_song([("x.gp", b"\x18FICHIER GUITAR PRO v5.10")])
    songs.set_derived(sid, {}, derived_bars(2))
    store.update(sid, lambda s: s.update(status="parsing"))
    songs.recover_interrupted()
    song = store.load(sid)
    assert song["status"] == "ready" and "中断" in song["error"]


def test_restart_during_explain_restores_previous_status(data_dir):
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    store.update(
        sid,
        lambda s: s.update(
            status="parsing",
            job="explain",
            job_prev={"status": "cancelled", "error": "已取消"},
            progress={"stage": "生成讲解"},
        ),
    )
    songs.recover_interrupted()
    song = store.load(sid)
    assert song["status"] == "cancelled" and "讲解生成中断" in song["error"] and "job" not in song
