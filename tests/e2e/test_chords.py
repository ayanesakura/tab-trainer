"""弹唱谱：和弦名、和弦图、扫弦在谱面上画出来，播放时按扫弦处理。"""

import os

import pytest

from tests.e2e.conftest import _seed

pytestmark = pytest.mark.e2e

C, D11 = [0, 1, 0, 2, 3, -1], [0, 3, 0, 4, 5, -1]


def _strum(frets, direction, chord=None):
    ev = {
        "dur": "1",
        "strum": direction,
        "notes": [
            {"string": i + 1, "fret": f, "tech": [], "grace": -1, "bend": 0}
            for i, f in enumerate(frets)
            if f >= 0
        ],
    }
    return {**ev, "chord": chord} if chord else ev


@pytest.fixture
def chord_song(server):
    """一首弹唱谱（曲库是整个会话共用的：测完删掉，不影响按曲子数量检查的测试）。"""
    sid = _chord_song(server)
    yield sid
    server["store"].delete(sid)


def _chord_song(server):
    store = server["store"]
    sid = _seed()

    def apply(s):
        for i, b in enumerate(s["bars"]):
            first, second = (C, "C"), (D11, "D11")
            b["events"] = [
                _strum(first[0], "down", first[1] if i % 2 == 0 else None),
                _strum(first[0], "up"),
                _strum(second[0], "down", second[1]),
                _strum(second[0], "up"),
            ]
            b["shapes"] = {"C": C, "D11": D11}
            b["events"][0]["lyrics"] = [f"第{i + 1}", "二段"]
            b["events"][2]["lyrics"] = ["句"]

    store.update(sid, apply)
    return sid


def test_chord_names_diagrams_and_strums_are_drawn(chord_song, trainer):
    b = trainer(chord_song)
    beats = b.js(
        """tt.api.score.tracks[0].staves[0].bars[0].voices[0].beats.map(x => [x.chord ? x.chord.name : '', x.brushType])"""
    )
    assert beats == [["C", 2], ["", 1], ["D11", 2], ["", 1]]
    names = b.js("""[...document.querySelectorAll('#score text')].map(t => t.textContent)""")
    assert "C" in names and "D11" in names and "第1" not in names  # 歌词不让 alphaTab 画（它画在谱表上方）
    # 歌词画在每行六线谱的下方：第 1 段一行、第 2 段在它下面
    b.wait_for("document.querySelectorAll('#lyricLayer .lyric').length >= 18")
    lyr = b.js(
        """[...document.querySelectorAll('#lyricLayer .lyric')].slice(0, 3).map(e => [e.textContent, e.getBoundingClientRect().top])"""
    )
    assert [x[0] for x in lyr] == ["第1", "二段", "句"]
    assert lyr[1][1] > lyr[0][1] and lyr[2][1] == lyr[0][1]
    # 歌词在第一行六线谱（谱线）的下面、第二行的上面
    tab_bottom, next_top = b.js(
        """(() => { const top = document.querySelector('#score .at-surface').getBoundingClientRect().top, s = tt.api.boundsLookup.staffSystems;
          const v = s[0].bars[0].bars[0].visualBounds; return [top + v.y + v.h, top + s[1].realBounds.y]; })()"""
    )
    assert tab_bottom <= lyr[0][1] and lyr[1][1] + 16 <= next_top
    # 弹唱谱默认只看六线谱；和弦图画在谱里（不在开头列一排）
    assert b.js("tt.get('ui.notation')") == "tab"
    assert b.js("tt.api.score.stylesheet.globalDisplayChordDiagramsInScore") is True
    assert b.js("tt.api.score.stylesheet.globalDisplayChordDiagramsOnTop") is False
    if os.environ.get("E2E_SHOTS"):
        b.screenshot(os.path.join(os.environ["E2E_SHOTS"], "chords.png"))
    assert not b.errors, b.errors


def test_bar_card_shows_the_bars_chords_and_the_editor_changes_them(server, chord_song, trainer):
    import json
    import urllib.request

    sid = chord_song
    b = trainer(sid)
    b.js("tt.set('ui.bar', 1)")
    b.wait_for("!document.getElementById('barChords').hidden")
    names = b.js(
        "[...document.querySelectorAll('#barChords .bar-chord')].map(x => [x.querySelector('.cd-name').textContent, x.classList.contains('carried')])"
    )
    # 第 2 小节开头没写和弦：延续第 1 小节最后的 D11（后面写的 D11 和它一样，不重复列）
    assert names == [["D11", True]]
    b.js("tt.set('ui.bar', 2)")
    b.wait_for("document.querySelector('#barChords .cd-name').textContent === 'C'")
    assert b.js("document.querySelectorAll('#barChords .bar-chord').length") == 2
    if os.environ.get("E2E_SHOTS"):
        b.screenshot(os.path.join(os.environ["E2E_SHOTS"], "chords-card.png"))

    b.js("""(() => { tt.setEditMode(true);
      const beat = tt.api.score.tracks[0].staves[0].bars[0].voices[0].beats[1];
      tt.openNotePop(beat, beat.notes[0]); })()""")
    b.wait_for("!document.getElementById('notePop').hidden")
    assert b.js("document.getElementById('npChord').value") == ""
    b.js("""(() => { document.getElementById('npChord').value = 'Am7';
      document.querySelector('#npStrum button[data-v=""]').click();
      document.getElementById('npSave').click(); })()""")
    b.wait_for(
        "(() => { const e = tt.get('song').bars[0].events[1]; return e.chord === 'Am7' && !e.strum; })()",
        timeout=10,
    )
    saved = json.load(urllib.request.urlopen(f"{server['base']}/api/songs/{sid}"))["bars"][0]["events"][1]
    assert saved.get("chord") == "Am7" and "strum" not in saved
    assert not b.errors, b.errors


def test_confirming_a_doubtful_bar(server, trainer):
    """待核对的小节：卡片里点“确认没问题”，不再算待核对；可以撤销。"""
    store = server["store"]
    sid = _seed()
    store.update(sid, lambda s: s["bars"][1].update(uncertain="第 3 拍看不清"))
    try:
        b = trainer(sid)
        b.js("tt.set('ui.bar', 1)")
        marks = "document.querySelectorAll('#warnLayer .warn-mark').length"
        b.wait_for(
            "!document.getElementById('reviewRow').hidden && !document.getElementById('barReview').hidden"
        )
        b.wait_for(f"{marks} === 1", timeout=10)  # 谱面上这一小节标着黄框
        b.js("document.getElementById('reviewBtn').click()")
        b.wait_for(
            "document.getElementById('barReview').hidden && tt.get('song').bars[1].reviewed", timeout=10
        )
        b.wait_for(f"{marks} === 0", timeout=5)  # 确认后谱面不重画，黄框也要去掉
        assert "撤销确认" in b.js("document.getElementById('reviewBtn').textContent")
        assert b.js("tt.get('view').review[1]") is False
        b.js("document.getElementById('reviewBtn').click()")
        b.wait_for(
            "!document.getElementById('barReview').hidden && !tt.get('song').bars[1].reviewed", timeout=10
        )
        b.wait_for(f"{marks} === 1", timeout=5)  # 撤销后黄框回来
        assert not b.errors, b.errors
    finally:
        store.delete(sid)


def test_gp_lyrics_on_the_practised_track_that_is_not_track_0(server, trainer):
    """GP 文件里练的音轨不是第 0 个（前面有贝斯）：歌词按画出来的那个音轨找，下面留出地方、写一次。"""
    from app import songs
    from tests.e2e.conftest import gp_from_tex

    tex = r"""\title "Two"
.
\track "Bass" { instrument 33 }
\staff {tabs}
\tuning (G2 D2 A1 E1)
0.4.4 0.4.4 0.4.4 0.4.4 | 0.4.4 0.4.4 0.4.4 0.4.4
\track "Gtr" { instrument 25 }
\staff {score tabs}
\tuning (E4 B3 G3 D3 A2 E2)
0.1.4{lyrics "la"} 1.2.4{lyrics "li"} 0.3.4 2.4.4 | 0.1.4{lyrics "lo"} 1.2.4 0.3.4 2.4.4
"""
    sid = songs.create_song([("two.gp", gp_from_tex(tex))])
    try:
        b = trainer(sid)
        assert b.js("tt.api.tracks.map(t => t.index)") == [1]
        b.wait_for("document.querySelectorAll('#lyricLayer .lyric').length > 0", timeout=10)
        texts = b.js("[...document.querySelectorAll('#lyricLayer .lyric')].map(e => e.textContent)")
        assert texts == ["la", "li", "lo"]  # 五线谱 + 六线谱两行谱表，歌词也只写一次
        assert b.js("tt.api.settings.display.systemPaddingBottom") > 10  # 下面留了写歌词的地方
        assert not b.errors, b.errors
    finally:
        server["store"].delete(sid)


def test_chords_differing_only_by_case_render_and_export_separately(server, trainer):
    """Am7 和 AM7：谱面上各画各的和弦图，导出的 GP 里也是两个和弦、名字不带区分用的零宽空格。"""
    am7, maj7 = [0, 1, 0, 2, 0, -1], [0, 2, 1, 2, 0, -1]
    store, sid = server["store"], _seed()

    def apply(s):
        for i, b in enumerate(s["bars"]):
            name, frets = ("Am7", am7) if i % 2 == 0 else ("AM7", maj7)
            b["events"] = [
                _strum(frets, "down", name),
                _strum(frets, "up"),
                _strum(frets, "down"),
                _strum(frets, "up"),
            ]
            b["shapes"] = {name: frets}

    store.update(sid, apply)
    try:
        b = trainer(sid)
        got = b.js(
            """tt.api.score.tracks[0].staves[0].bars.slice(0, 2).map(x => { const c = x.voices[0].beats[0].chord;
              return [c.name.replaceAll('\\u200b', ''), [...c.strings]]; })"""
        )
        assert got == [["Am7", am7], ["AM7", maj7]]
        # 侧栏小节卡片按我们的数据画：第 2 小节是 AM7 的指法
        b.js("tt.set('ui.bar', 1)")
        b.wait_for("(document.querySelector('#barChords .cd-name') || {}).textContent === 'AM7'")
        exported = b.js(
            """(async () => {
              let blob = null; const orig = URL.createObjectURL, click = HTMLAnchorElement.prototype.click;
              URL.createObjectURL = x => { blob = x; return 'blob:x'; }; HTMLAnchorElement.prototype.click = () => {};
              try { tt.player.exportGp(); } finally { URL.createObjectURL = orig; HTMLAnchorElement.prototype.click = click; }
              const at = await import('/static/vendor/alphatab/alphaTab.min.mjs');
              const sc = at.importer.ScoreLoader.loadScoreFromBytes(new Uint8Array(await blob.arrayBuffer()), new at.Settings());
              return sc.tracks[0].staves[0].bars.slice(0, 2).map(x => { const c = x.voices[0].beats[0].chord; return [c.name, [...c.strings]]; });
            })()"""
        )
        assert exported == [["Am7", am7], ["AM7", maj7]]
        # 导出完名字放回去：谱面上还是两个不同的和弦
        assert b.js("tt.api.score.tracks[0].staves[0].bars[1].voices[0].beats[0].chord.name") == "AM7\u200b"
        assert not b.errors, b.errors
    finally:
        store.delete(sid)
