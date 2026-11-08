// 金标准：tests/fixtures/golden.gp（alphaTab 的 Gp7Exporter 导出的 Guitar Pro 7 文件）经 score.js 的 deriveSong
// 转换后，必须和 tests/fixtures/golden_derived.json 完全一致。Python 端（tests/test_golden.py）把同一份 JSON
// 喂给 PUT /api/songs/{id}/derived，检查后端照单全收。改了 deriveSong（DERIVE_VERSION 要 +1）后重新生成：
//   UPDATE_GOLDEN=1 node --test tests/js/fixture.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import * as at from '../../app/static/vendor/alphatab/alphaTab.core.min.mjs';
import { toAlphaTex, deriveSong, DERIVE_VERSION } from '../../app/static/js/score.js';

const GP = new URL('../fixtures/golden.gp', import.meta.url);
const JSON_PATH = new URL('../fixtures/golden_derived.json', import.meta.url);

const N = (string, fret, tech = [], grace = -1) => ({ string, fret, tech, grace, bend: 0 });
// 覆盖：调弦、变调夹、三连音 / 六连音、技巧（含点弦泛音的 harm）、倚音、中途变拍号、反复、跳房子、跳转记号
const SOURCE = {
  meta: { title: 'Golden', tuning: ['D4', 'A3', 'F#3', 'C#3', 'A2', 'D2'], capo: 2, time_signature: [4, 4] },
  bars: [
    { repeat_start: true, events: [{ dur: '1', notes: [] }, { dur: '1/4', notes: [N(6, 4, ['pm'])] }, { dur: '7/4', notes: [N(6, 7, ['sl', 'pm'])] }, { dur: '1', notes: [N(5, 0)] }] },
    { endings: [1], repeat_end: 2, events: [{ dur: '2', notes: [N(4, 13, ['roll']), N(3, 15, ['roll'])] }, { dur: '1', notes: [N(3, 11)] }, { dur: '1/2', notes: [N(1, 12)] }, { dur: '1/2', notes: [N(2, 12)] }] },
    { endings: [2], events: [{ dur: '1/3', notes: [N(1, 7)] }, { dur: '1/3', notes: [N(2, 7)] }, { dur: '1/3', notes: [N(3, 8)] }, { dur: '1', notes: [N(4, 8)] }, { dur: '2', notes: [N(2, 5, ['nh'])] }] },
    { time_signature: [3, 4], events: [{ dur: '1/6', notes: [N(1, 0)] }, { dur: '1/6', notes: [N(1, 2)] }, { dur: '2/3', notes: [N(1, 4, ['h'], 2)] }, { dur: '2', notes: [N(2, 5, ['si'])] }] },
    { directions: ['fine'], events: [{ dur: '2', notes: [N(3, 2, ['ah'])] }, { dur: '2', notes: [{ ...N(1, 0, ['th']), harm: 19 }, { ...N(3, 5, ['th']), harm: 22 }] }] },
  ],
};

const settings = () => new at.Settings();
const load = bytes => at.importer.ScoreLoader.loadScoreFromBytes(new Uint8Array(bytes), settings());

function regenerate() {
  const imp = new at.importer.AlphaTexImporter();
  imp.initFromString(toAlphaTex(SOURCE).tex, settings());
  const gp = new at.exporter.Gp7Exporter().export(imp.readScore(), settings());
  writeFileSync(GP, Buffer.from(gp));
  const derived = deriveSong(load(gp), 0);
  writeFileSync(JSON_PATH, JSON.stringify(derived, null, 1) + '\n');
}

if (process.env.UPDATE_GOLDEN || !existsSync(GP) || !existsSync(JSON_PATH)) regenerate();

test('deriveSong on the golden GP file matches the golden JSON', () => {
  const golden = JSON.parse(readFileSync(JSON_PATH, 'utf8'));
  const got = JSON.parse(JSON.stringify(deriveSong(load(readFileSync(GP)), 0)));
  assert.equal(golden.version, DERIVE_VERSION, 'deriveSong 改了（DERIVE_VERSION 变了）：重新生成金标准');
  assert.deepEqual(got, golden);
});

test('golden JSON covers what the backend has to accept', () => {
  const golden = JSON.parse(readFileSync(JSON_PATH, 'utf8'));
  assert.equal(golden.bars.length, SOURCE.bars.length);
  assert.deepEqual(golden.meta.tuning, SOURCE.meta.tuning);
  assert.deepEqual(golden.bars[3].time_signature, [3, 4]);
  assert.ok(golden.bars[0].repeat_start && golden.bars[1].repeat_end === 2);
  assert.deepEqual(golden.bars[4].directions, ['fine']);
  assert.deepEqual(golden.bars[4].events[1].notes.map(n => [n.fret, n.tech, n.harm]), [[0, ['th'], 19], [5, ['th'], 22]]);
  assert.ok(golden.bars.every((b, i) => b.events.every((e, k) => e.notes.every(n => ('harm' in n) === (i === 4 && k === 1)))), '只有点弦泛音有 harm');
});
