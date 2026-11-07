"""敲节奏的回归测试（主流程在 test_trainer.py）。"""

from __future__ import annotations

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
