// 往返校验：我们的数据 → alphaTex → alphaTab 解析 → 转回我们的数据，应该和原来一致。
// 运行：node --test tests/js/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import * as at from '../../app/static/vendor/alphatab/alphaTab.core.min.mjs';
import { toAlphaTex, deriveSong, splitTicks, durToTicks, ticksToDur, eventIndexAt } from '../../app/static/js/score.js';

const parse = tex => {
  const settings = new at.Settings();
  const imp = new at.importer.AlphaTexImporter();
  imp.initFromString(tex, settings);
  const score = imp.readScore();
  return score;
};
const norm = bars => bars.map(b => b.events.map(e => ({
  dur: ticksToDur(durToTicks(e.dur)),
  notes: e.notes.map(n => `${n.string}-${n.fret}` + (n.grace >= 0 ? `g${n.grace}` : '') + [...n.tech].sort().join('')).sort(),
})));

const N = (string, fret, tech = [], grace = -1) => ({ string, fret, tech, grace, bend: 0 });
const SYNTH = {
  meta: { title: '测试', tuning: ['D4', 'A3', 'F#3', 'C#3', 'A2', 'D2'], capo: 2, time_signature: [4, 4] },
  bars: [
    { events: [{ dur: '1', notes: [] }, { dur: '1/4', notes: [N(6, 4, ['pm'])] }, { dur: '7/4', notes: [N(6, 7, ['sl', 'pm'])] }, { dur: '1', notes: [N(5, 0)] }] },
    { events: [{ dur: '2', notes: [N(4, 13, ['roll']), N(3, 15, ['roll']), N(2, 16, ['roll']), N(1, 14, ['roll'])] }, { dur: '1', notes: [N(3, 11)] }, { dur: '1/2', notes: [N(1, 12), N(4, 11)] }, { dur: '1/2', notes: [N(2, 12)] }] },
    { events: [{ dur: '1/3', notes: [N(1, 7), N(5, 9)] }, { dur: '1/3', notes: [N(2, 7)] }, { dur: '1/3', notes: [N(3, 8)] }, { dur: '1', notes: [N(4, 8)] }, { dur: '2', notes: [N(2, 5, ['nh'])] }] },
    { events: [{ dur: '1/4', notes: [N(2, 0), N(6, 0)] }, { dur: '1/2', notes: [N(2, 2, ['h'])] }, { dur: '1/4', notes: [N(2, 0, ['p'])] }, { dur: '1', notes: [N(3, 2, ['ah'])] }, { dur: '1/6', notes: [N(1, 0)] }, { dur: '1/6', notes: [N(1, 2)] }, { dur: '2/3', notes: [N(1, 4, ['h'], 2)] }, { dur: '1', notes: [N(2, 5, ['si'])] }] },
  ],
};

test('durations split into writable beats', () => {
  assert.deepEqual(splitTicks(960).map(u => u[1]), [4]);
  assert.deepEqual(splitTicks(1680).map(u => u[0]), [1440, 240]); // 7/4 拍 = 附点四分 + 十六分
  assert.equal(ticksToDur(320), '1/3');
  assert.equal(eventIndexAt([0, 480, 960], 700), 1);
});

test('round trip keeps notes, rhythm and techniques (synthetic)', () => {
  const { tex, beatMap } = toAlphaTex(SYNTH);
  const score = parse(tex);
  assert.equal(score.masterBars.length, 4);
  assert.equal(score.tracks[0].staves[0].capo, 2);
  const back = deriveSong(score, 0);
  assert.deepEqual(back.meta.tuning, ['D4', 'A3', 'F#3', 'C#3', 'A2', 'D2']);
  assert.deepEqual(norm(back.bars), norm(SYNTH.bars));
  assert.deepEqual(beatMap[2], [0, 320, 640, 960, 1920]);
});

const EXAMPLE = new URL('../../data/songs/example-wuting/song.json', import.meta.url);
test('round trip on the local example song', { skip: !existsSync(EXAMPLE) && '本地没有示例曲' }, () => {
  const song = JSON.parse(readFileSync(EXAMPLE, 'utf8'));
  const score = parse(toAlphaTex(song).tex);
  assert.equal(score.masterBars.length, song.bars.length);
  const back = deriveSong(score, 0);
  const a = norm(back.bars), b = norm(song.bars);
  for (let i = 0; i < b.length; i++) assert.deepEqual(a[i], b[i], `第 ${i + 1} 小节不一致`);
});

test('repeats, endings and jumps survive the round trip and drive playback order', () => {
  const bar = (fret, extra = {}) => ({ events: [{ dur: '4', notes: [N(1, fret)] }], ...extra });
  const song = {
    meta: { tuning: ['E4', 'B3', 'G3', 'D3', 'A2', 'E2'], time_signature: [4, 4] },
    bars: [
      bar(1, { repeat_start: true }),
      bar(2, { endings: [1], repeat_end: 2 }),
      bar(3, { endings: [2] }),
      bar(4, { directions: ['segno'] }),
      bar(5, { directions: ['dalsegnoalfine', 'fine'] }),
    ],
  };
  const score = parse(toAlphaTex(song).tex);
  const back = deriveSong(score, 0);
  assert.deepEqual(back.bars.map(({ events, ...rest }) => rest), song.bars.map(({ events, ...rest }) => rest));

  const settings = new at.Settings();
  const midi = new at.midi.MidiFile();
  const gen = new at.midi.MidiFileGenerator(score, settings, new at.midi.AlphaSynthMidiFileHandler(midi));
  gen.generate();
  assert.deepEqual(gen.tickLookup.masterBars.map(m => m.masterBar.index + 1), [1, 2, 1, 3, 4, 5, 4, 5]);
});

test('retiming an onset keeps the bar length', async () => {
  const { retimeEvent } = await import('../../app/static/js/score.js');
  const bar = [{ dur: '1', notes: [N(1, 0)] }, { dur: '1', notes: [N(2, 1)] }, { dur: '2', notes: [N(3, 2)] }];
  const total = evs => evs.reduce((a, e) => a + durToTicks(e.dur), 0);
  const a = retimeEvent(bar, 0, '1/2');
  assert.deepEqual(a.map(e => e.dur), ['1/2', '3/2', '2']);
  const b = retimeEvent(bar, 2, '1/2'); // 最后一拍改短：补休止
  assert.deepEqual(b.map(e => [e.dur, e.notes.length]), [['1', 1], ['1', 1], ['1/2', 1], ['3/2', 0]]);
  assert.equal(total(a), total(bar)); assert.equal(total(b), total(bar));
  assert.throws(() => retimeEvent(bar, 2, '3'), /只能改短/);
  assert.throws(() => retimeEvent(bar, 0, '2'), /盖过下一个音/);
  assert.equal(bar[0].dur, '1'); // 原数据不变
});

test('odd durations never lose time: exact split, grid-aligned onsets', () => {
  assert.deepEqual(splitTicks(140).map(u => u[0]), [80, 60]); // 贪心会丢掉 20 tick
  assert.equal(splitTicks(192), null); // 1/5 拍写不出来
  const quint = Array.from({ length: 5 }, (_, i) => ({ dur: '1/5', notes: [N(1, i)] }));
  const song = {
    meta: { tuning: ['E4', 'B3', 'G3', 'D3', 'A2', 'E2'], time_signature: [4, 4] },
    bars: [
      { events: [...quint, { dur: '3', notes: [N(2, 0)] }] }, // 五连音 + 附点二分
      { events: [{ dur: '7/48', notes: [N(1, 1)] }, { dur: '41/48', notes: [N(1, 2)] }, { dur: '3', notes: [N(1, 3)] }] },
    ],
  };
  const { tex, beatMap } = toAlphaTex(song);
  const score = parse(tex);
  for (const bar of score.tracks[0].staves[0].bars) {
    const beats = bar.voices[0].beats;
    const last = beats[beats.length - 1];
    assert.equal(last.playbackStart + last.playbackDuration, 3840, `第 ${bar.index + 1} 小节总长不对`);
  }
  // 五连音的起拍：0、192、384、576、768 对齐到网格 → 0、200、380、580、760（误差不超过 10 tick，不累积）
  beatMap[0].slice(0, 5).forEach((t, i) => assert.ok(Math.abs(t - i * 192) <= 10, `第 ${i + 1} 个音在 ${t}`));
  assert.equal(beatMap[0][5], 960);
  assert.deepEqual(beatMap[1], [0, 140, 960]); // 7/48 拍精确表示
});

test('retiming next to septuplets keeps the exact bar length', async () => {
  const { retimeEvent } = await import('../../app/static/js/score.js');
  const out = retimeEvent([{ dur: '1/7', notes: [] }, { dur: '1/7', notes: [] }, { dur: '2/7', notes: [] }, { dur: '25/7', notes: [] }], 0, '1/6');
  assert.deepEqual(out.map(e => e.dur), ['1/6', '5/42', '2/7', '25/7']);
});
