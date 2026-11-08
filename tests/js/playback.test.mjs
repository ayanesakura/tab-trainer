// 播放：连着的滑音（勿听第 8 小节 1 弦 2→4→2）。中间的音不重新拨弦，真正在响的是这一串的第一个音，
// 弯音必须加在它的 key 上。alphaTab 1.9.0 原版把第二段滑音的弯音加在中间那个音自己的 key 上（没有按下的音），
// 第二段滑音就没声音了；vendor/alphatab 里打了补丁（ttChain），见 VERSION.txt。
// 运行：node --test tests/js/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as at from '../../app/static/vendor/alphatab/alphaTab.core.min.mjs';
import { toAlphaTex } from '../../app/static/js/score.js';

const T = at.midi.MidiEventType;
const CENTER = 2147483648; // 每个音单独的弯音（32 位）不弯时的值

const midiOf = events => {
  const song = { meta: { time_signature: [4, 4] }, bars: [{ events }] };
  const { tex } = toAlphaTex(song, { tempo: 60 });
  const settings = new at.Settings();
  const imp = new at.importer.AlphaTexImporter();
  imp.initFromString(tex, settings);
  const midi = new at.midi.MidiFile();
  new at.midi.MidiFileGenerator(imp.readScore(), settings, new at.midi.AlphaSynthMidiFileHandler(midi)).generate();
  return midi.events;
};
const N = (string, fret, tech = []) => ({ string, fret, tech, grace: -1, bend: 0 });
const E = (dur, ...notes) => ({ dur, notes });

// 每个弯音事件都要落在“那一刻正在响”的音上
const bendsOnSoundingNotes = events => {
  const on = new Map(); // key → 正在响的次数
  // 同一个 tick 上：先按下、再弯音、最后松开（揉弦在音的结尾处会再发一个弯音）
  const order = e => (e.type === T.NoteOn ? 0 : e.type === T.NoteOff ? 2 : 1);
  const sorted = events.map((e, i) => [e, i]).sort(([a, i], [b, j]) => a.tick - b.tick || order(a) - order(b) || i - j);
  for (const [e] of sorted) {
    if (e.type === T.NoteOn) on.set(e.noteKey, (on.get(e.noteKey) || 0) + 1);
    else if (e.type === T.NoteOff) on.set(e.noteKey, (on.get(e.noteKey) || 0) - 1);
    else if (e.type === T.PerNotePitchBend && e.value !== CENTER) {
      assert.ok(on.get(e.noteKey) > 0, `tick ${e.tick}: bend on key ${e.noteKey}, which is not sounding`);
    }
  }
};
const bends = (events, key) => events.filter(e => e.type === T.PerNotePitchBend && e.noteKey === key);

test('chained legato slides bend the note that is actually sounding (勿听 bar 8: 2→4→2)', () => {
  const ev = midiOf([E('1/4', N(1, 2), N(5, 0)), E('1/2', N(1, 4, ['sl'])), E('2', N(1, 2, ['sl'])), E('5/4')]);
  bendsOnSoundingNotes(ev);
  const b = bends(ev, 66); // 1 弦 2 品 = F#4
  assert.ok(Math.max(...b.map(e => e.value)) > CENTER, 'first slide goes up');
  assert.equal(b.at(-1).value, CENTER, 'second slide comes back down to fret 2');
});

test('a slide target held across a beat (tied pieces) still slides on the sounding note', () => {
  // 4 品从 1/4 拍开始、持续 1 拍，跨过拍点会拆成连音线连着的两段
  const ev = midiOf([E('1/4', N(1, 2)), E('1', N(1, 4, ['sl'])), E('11/4', N(1, 2, ['sl']))]);
  bendsOnSoundingNotes(ev);
  assert.equal(bends(ev, 66).at(-1).value, CENTER);
});

test('vibrato on a slide target wobbles the sounding note', () => {
  const ev = midiOf([E('1', N(2, 5)), E('3', N(2, 7, ['sl', 'vib']))]);
  bendsOnSoundingNotes(ev);
  const key = 64; // 2 弦 5 品 = E4
  const after = bends(ev, key).filter(e => e.tick >= 960).map(e => e.value);
  assert.ok(new Set(after).size > 2, 'vibrato bends present on the sounding key');
});
