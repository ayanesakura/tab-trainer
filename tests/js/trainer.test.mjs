// 练习页的纯函数和 store：node --test tests/js/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createStore, equal } from '../../app/static/js/trainer/store.js';
import {
  deriveView, sectionsOf, stepSection, practiceText, resumeInfo, rangeLabel, loopLabel, barTags, planRange,
  songSubtitle, explainState, needsReview, warnText, showNote, tuningInput, nameToMidi, noteMidi,
} from '../../app/static/js/trainer/view.js';
import { gbkFromMojibake, scoreNeedsGbk } from '../../app/static/js/score.js';

const N = (string, fret, tech = [], grace = -1) => ({ string, fret, tech, grace, bend: 0 });
const bar = (id, system, events, extra = {}) => ({ id, n: 0, system, page: 0, region: [10, 50, 0, 80], events, tip: '', warn: [], uncertain: '', ...extra });
const SONG = {
  id: 's1', kind: 'image', title: '测试', pages: [{ file: 'p1.png' }],
  meta: { title: '测试', artist: '某人', tuning: ['D4', 'A3', 'F#3', 'D3', 'A2', 'D2'], tuning_midi: [62, 57, 54, 50, 45, 38], capo: 2, time_signature: [4, 4] },
  bars: [
    bar('a', 0, [{ dur: '1', notes: [N(1, 0), N(6, 0)] }, { dur: '1', notes: [N(2, 3, ['h'])] }, { dur: '2', notes: [] }]),
    bar('b', 0, [{ dur: '1/3', notes: [N(3, 2)] }, { dur: '1/3', notes: [N(3, 4, ['sl'])] }, { dur: '1/3', notes: [N(3, 5, [], 4)] }, { dur: '3', notes: [N(1, 12, ['nh'])] }]),
    bar('c', 1, [], { time_signature: [3, 4], warn: [{ code: 'length', msg: '时值合计不对' }] }),
    bar('d', 1, [{ dur: '4', notes: [N(5, 7, ['xx'])] }, { dur: '1', notes: [N(1, 1)] }]), // 超出小节长度的事件不播放
  ],
};

test('store: set / get / on with structural dedupe and sub-path subscriptions', () => {
  const st = createStore({ song: null, ui: { bar: -1 } });
  const calls = [];
  st.on('ui.bar', (v, old) => calls.push(['bar', v, old]));
  st.on('song.plan', v => calls.push(['plan', v]));
  st.on(['song.bars', 'ui.bar'], () => calls.push(['multi']));
  st.set('ui.bar', 3);
  st.set('ui.bar', 3); // 没变：不通知
  st.set('song', { plan: null, bars: [1] });
  st.set('song', { plan: null, bars: [1], other: 1 }); // plan、bars 都没变
  st.set('song', { plan: { a: 1 }, bars: [1] });
  st.set({ 'ui.bar': 4, 'song.bars': [2] }); // 一次改两处：同一个订阅者只调一次
  assert.deepEqual(calls, [['bar', 3, -1], ['multi'], ['plan', null], ['multi'], ['plan', { a: 1 }], ['bar', 4, 3], ['multi']]);
  const before = st.get('song');
  st.set('song.bars.0', 9);
  assert.deepEqual(before.bars, [2]); // 不改原来的对象
  assert.deepEqual(st.get('song.bars'), [9]);
  assert.ok(equal({ a: [1, { b: 2 }] }, { a: [1, { b: 2 }] }));
  assert.ok(!equal(new Set([1]), new Set([1])));
});

test('deriveView: events, pitches, bar lengths, regions, sections, review flags', () => {
  const v = deriveView(SONG);
  assert.equal(v.capo, 2);
  assert.deepEqual(v.open, { 1: 62, 2: 57, 3: 54, 4: 50, 5: 45, 6: 38 });
  assert.equal(v.openName[3], 'F♯');
  assert.deepEqual(v.barLens, [4, 4, 3, 4]);
  assert.equal(v.bars[2].length, 1); // 空小节 → 一个整小节休止
  assert.equal(v.bars[2][0].d, 3);
  assert.equal(v.bars[3].length, 1); // 4 拍以后的事件不播放
  assert.deepEqual(v.all.map(e => e.idx), [...v.all.keys()]);
  assert.equal(v.all[4].bar, 1);
  assert.ok(v.bars[0][1].tech.has('h'));
  assert.equal(v.bars[1][2].notes[0].grace, 4);
  assert.deepEqual(v.regions[0], [0, 10, 50, 0, 80]);
  assert.deepEqual(v.pages, ['/files/s1/p1.png']);
  assert.deepEqual(v.sections, [{ sys: 0, a: 0, z: 1 }, { sys: 1, a: 2, z: 3 }]);
  assert.deepEqual(v.review, [false, false, true, false]);
  assert.equal(v.source.kind, 'tex');
  assert.deepEqual(v.beatMap[1], [0, 320, 640, 960]);
  assert.equal(noteMidi(v.bars[0][0].notes[0], v), 62 + 2);
  assert.equal(noteMidi(v.bars[1][3].notes[0], v), 62 + 2 + 12); // 12 品泛音高八度
  const gp = deriveView({ ...SONG, kind: 'score', track: 1 });
  assert.deepEqual(gp.source, { kind: 'score', id: 's1', track: 1 });
  assert.deepEqual(gp.beatMap[0], [0, 960, 1920]);
});

test('stepSection: whole song, whole sections, custom ranges', () => {
  const secs = [{ a: 0, z: 2 }, { a: 3, z: 5 }, { a: 6, z: 6 }];
  const st = (a, b, shown, dir) => stepSection({ a, b, n: 7, shown, secs }, dir);
  assert.deepEqual(st(0, 6, 4, 1), { a: 3, b: 5 }); // 全曲：从正在看的那一段开始
  assert.deepEqual(st(0, 6, 4, -1), { a: 0, b: 2 });
  assert.deepEqual(st(3, 5, 3, 1), { a: 6, b: 6 });
  assert.deepEqual(st(3, 5, 3, -1), { a: 0, b: 2 });
  assert.deepEqual(st(6, 6, 6, 1), { msg: '已经是最后一段了' });
  assert.deepEqual(st(0, 2, 0, -1), { msg: '已经是第一段了' });
  assert.deepEqual(st(1, 2, 1, 1), { a: 3, b: 4 }); // 自选范围按长度平移
  assert.deepEqual(st(5, 6, 5, 1), { a: 5, b: 6 }); // 到头了不越界
  assert.equal(stepSection({ a: 0, b: 0, n: 0, shown: 0, secs: [] }, 1), null);
  assert.deepEqual(sectionsOf([{ system: 0 }, { system: 0 }, { system: 2 }]), [{ sys: 0, a: 0, z: 1 }, { sys: 2, a: 2, z: 2 }]);
});

test('practice line, resume and labels', () => {
  assert.equal(practiceText(null), '还没练过这一小节');
  assert.equal(practiceText({ loops: 3, seconds: 150, best_bpm: 72, last: '昨天' }), '循环 3 遍 · 累计 3 分钟 · 最快 72 BPM · 上次 昨天');
  assert.equal(practiceText({ loops: 0, seconds: 20, last: '今天' }), '听过 不到 1 分钟 · 上次 今天');
  const s = { ...SONG, practice: { last: { a: 'c', b: 'b', bpm: 60, at: '昨天' } } };
  assert.deepEqual(resumeInfo(s), { a: 1, z: 2, label: '第 2–3 小节', bpm: 60, at: '昨天' });
  assert.equal(resumeInfo({ ...SONG, practice: { last: { a: 'x', b: 'y', bpm: 60 } } }), null);
  assert.equal(resumeInfo({ ...SONG, practice: { last: { a: 'x', b: 'y', a_n: 4, b_n: 4, bpm: 60 } } }).label, '第 4 小节'); // id 没了用位置
  assert.equal(rangeLabel(0, 3, 4), '全曲');
  assert.equal(loopLabel(0, 3, 4), '第 1–4 小节');
  assert.equal(loopLabel(2, 2, 4), '第 3 小节');
});

test('tags, plan ranges, subtitle, explain state, warnings, note names', () => {
  const v = deriveView(SONG);
  assert.deepEqual(barTags(v.bars[0]), ['击弦']);
  assert.deepEqual(barTags(v.bars[1]), ['滑音', '倚音', '自然泛音', '三连音']);
  assert.deepEqual(planRange('3-5', 4), { a: 2, z: 3 });
  assert.deepEqual(planRange('2', 4), { a: 1, z: 1 });
  assert.equal(planRange('9-12', 4), null);
  assert.equal(planRange('前奏', 4), null);
  assert.equal(songSubtitle(SONG, v), '某人 · 调弦 D A D F♯ A D · 变调夹 2 品 · 4/4 拍（有变拍号） · 4 小节');
  const std = { ...SONG, meta: { time_signature: [3, 4] } };
  assert.equal(songSubtitle(std, deriveView(std)), '标准调弦 · 3/4 拍（有变拍号） · 4 小节');
  assert.deepEqual(explainState({ ...SONG, kind: 'score', plan: null }), { missing: 4, noPlan: true, planStale: false, fresh: true });
  assert.equal(explainState({ ...SONG, plan: { stale: true } }).planStale, true);
  assert.equal(needsReview({ needs_review: false, uncertain: '有点怪' }), false); // 后端说了算
  assert.equal(needsReview({ uncertain: '有点怪' }), true);
  assert.equal(warnText({ code: 'length', msg: '第 3 小节时值不对' }), '第 3 小节时值不对');
  assert.equal(warnText({ code: 'ocr_mismatch' }), '和本机 OCR 对不上');
  assert.equal(warnText('旧格式'), '旧格式');
  assert.equal(showNote('F#3'), 'F♯3');
  assert.equal(tuningInput(' F♯3 '), 'F#3');
  assert.equal(nameToMidi('E4'), 64);
  assert.equal(nameToMidi('F♯3'), 54);
  assert.ok(Number.isNaN(nameToMidi('H2')));
});

test('GBK mojibake from Guitar Pro 3-5 files is recognised', () => {
  assert.equal(gbkFromMojibake('ÌìÂÕÖ®Áµ'), '天抡之恋'); // GBK 字节被当成 windows-1252
  for (const s of ['Romance', 'Café', 'Für Elise', 'Jeux Interdits – Romance', '天空之城', '']) assert.equal(gbkFromMojibake(s), null, s);
  assert.equal(scoreNeedsGbk({ title: 'Romance', artist: '', tracks: [{ name: 'Ã÷ÔÂ' }] }), true);
  assert.equal(scoreNeedsGbk({ title: 'Romance', artist: 'Anon', tracks: [{ name: 'Guitar' }] }), false);
});
