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
