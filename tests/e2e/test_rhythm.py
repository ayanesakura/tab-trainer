"""敲节奏的回归测试（主流程在 test_trainer.py）。"""

from __future__ import annotations

import itertools
import time

import pytest

pytestmark = pytest.mark.e2e

RH = "tt.rhythm.state"


def test_rhythm_latency_compensation_does_not_mark_notes_missed_early(trainer):
    """延迟补偿 +250 ms：耳朵比音频时钟晚 250 ms 听到，判漏也要按同一个时钟，
    不能在用户还没听到这个音的时候就判漏（以前判漏用的是没扣补偿的时钟）。"""
    b = trainer()
    b.js(
        f"tt.set('play.countIn', false); tt.setLoop(0, 0); document.activeElement.blur(); {RH}.latency = 250; true"
    )
    b.js("document.getElementById('rhythmBtn').click(); tt.setTempo(120); tt.play(); true")
    b.wait_for(f"{RH}.run && {RH}.run.loops.some(l => l.kind === 'loop')")
    note0 = f"{RH}.run.loops.find(l => l.kind === 'loop').notes[0]"
    # 第一个音四分音符、120 BPM：窗口 200 ms，补偿后 T + 0.23 s 才判漏，也就是没扣补偿的时钟 T + 0.48 s
    b.wait_for(f"tt.rhythm.now() + 0.25 > {note0}.T + 0.3", timeout=10)
    assert b.js(f"{note0}.missed") is False
    b.js(f"tt.rhythm.tapAt({note0}.T + 0.02); true")
    assert abs(b.js(f"{note0}.err") - 0.02) < 1e-6
    b.js("tt.rhythm.stop(); true")
    b.js("localStorage.removeItem('tt.tapLatency'); true")
    assert not b.errors, b.errors


def test_rhythm_survives_song_getting_shorter(trainer):
    """敲节奏开着、循环全曲时曲子变短（重新识别、删页）：节奏条跟着变，不能读不存在的小节报错。"""
    b = trainer()
    n = b.js("tt.view.bars.length")
    assert n >= 3
    b.js(f"tt.setLoop(0, {n - 1}); document.getElementById('rhythmBtn').click(); true")
    assert b.js("tt.get('rhythm.on')")
    b.js("const s = tt.song; tt.set('song', { ...s, bars: s.bars.slice(0, 2) }); true")
    assert b.js("tt.view.bars.length") == 2
    assert b.js("tt.state.b") == 1
    assert b.js("document.querySelectorAll('#rhSvg .rh-note').length") == b.js(f"{RH}.prog.onsets.length")
    assert b.js(f"{RH}.prog.bars.length") == 2
    assert not b.errors, b.errors


def test_rhythm_count_in_before_every_loop(trainer):
    """敲节奏开着预备拍：每一遍前都有一小节预备拍（以前只有第一遍前有）。"""
    b = trainer()
    b.js("tt.set('play.countIn', true); tt.setLoop(0, 0); document.activeElement.blur(); true")
    b.js("document.getElementById('rhythmBtn').click(); tt.setTempo(200); tt.play(); true")
    # 第一遍弹完以后排进来的预备拍（开始时间晚于整个练习的开头）
    b.wait_for(
        f"{RH}.run && {RH}.run.loops.some(l => l.kind === 'pre' && l.start > {RH}.run.t0 + 0.01)", timeout=15
    )
    # 排好的顺序：遍与遍之间都隔着一个预备拍，前后相接
    seq = b.js(f"{RH}.run.loops.map(l => [l.kind, l.start, l.end])")
    for (k1, _, e1), (k2, s2, _) in itertools.pairwise(seq):
        assert abs(e1 - s2) < 1e-9
        assert k1 != k2, seq
    b.js("tt.rhythm.stop(); true")
    b.js("tt.set('play.countIn', false); true")
    assert not b.errors, b.errors


CAPTURE = """(() => { window.__posts = []; const f = window.fetch;
  window.fetch = (u, o) => { if (String(u).endsWith('/practice')) __posts.push(JSON.parse(o.body)); return f(u, o); };
  return true; })()"""


def test_rhythm_drill_time_is_logged_as_practice(trainer):
    """敲节奏的时间记成练习时间（kind: rhythm，不算遍数）；延迟校准不算。"""
    b = trainer()
    b.js(CAPTURE)
    b.js("tt.set('play.countIn', false); tt.setLoop(0, 0); document.activeElement.blur(); true")
    b.js("document.getElementById('rhythmBtn').click(); tt.setTempo(120); tt.play(); true")
    b.wait_for("tt.get('rhythm.running')")
    time.sleep(2.6)
    b.js("tt.stop(); true")
    b.wait_for("!tt.get('rhythm.running')")
    b.wait_for("__posts.length === 1")
    p = b.js("__posts[0]")
    assert p["kind"] == "rhythm" and p["loops"] == 0 and p["bpm"] == 120, p
    assert p["bar_ids"] == ["bar1"] and p["seconds"] >= 2, p
    # 校准：不记
    b.js("tt.rhythm.start(true); true")
    time.sleep(2.3)
    b.js("tt.rhythm.stop(); true")
    time.sleep(0.3)
    assert b.js("__posts.length") == 1
    b.js("document.getElementById('rhClose').click(); true")
    assert not b.errors, b.errors
