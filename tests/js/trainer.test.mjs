// 练习页的纯函数和 store：node --test tests/js/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createStore, equal } from '../../app/static/js/trainer/store.js';
import {
  deriveView, sectionsOf, stepSection, practiceLines, songTimeText, resumeInfo, rangeLabel, loopLabel, parseBarInput, barTags, planRange,
  songSubtitle, explainState, needsReview, warnText, showNote, tuningInput, nameToMidi, noteMidi, beatNumber, eventsText,
} from '../../app/static/js/trainer/view.js';
import { refineConfirm, barRefineHint, rowRefineHint, refineTime, refineScopeLabel } from '../../app/static/js/lib/refine.js';
import { gbkFromMojibake, scoreNeedsGbk } from '../../app/static/js/score.js';

const N = (string, fret, tech = [], grace = -1) => ({ string, fret, tech, grace, bend: 0 });
const bar = (id, system, events, extra = {}) => ({ id, n: 0, system, page: 0, region: [10, 50, 0, 80], events, tip: '', warn: [], uncertain: '', ...extra });
const SONG = {
  id: 's1', source_type: 'image', title: '测试', pages: [{ file: 'p1.png' }],
  meta: { title: '测试', artist: '某人', tuning: ['D4', 'A3', 'F#3', 'D3', 'A2', 'D2'], tuning_midi: [62, 57, 54, 50, 45, 38], capo: 2, time_signature: [4, 4] },
  bars: [
    bar('a', 0, [{ dur: '1', notes: [N(1, 0), N(6, 0)] }, { dur: '1', notes: [N(2, 3, ['h'])] }, { dur: '2', notes: [] }]),
    bar('b', 0, [{ dur: '1/3', notes: [N(3, 2)] }, { dur: '1/3', notes: [N(3, 4, ['sl'])] }, { dur: '1/3', notes: [N(3, 5, [], 4)] }, { dur: '3', notes: [N(1, 12, ['nh'])] }]),
    bar('c', 1, [], { time_signature: [3, 4], warn: [{ code: 'length', msg: '时值合计不对' }], needs_review: true }),
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
  const gp = deriveView({ ...SONG, source_type: 'gp', is_score: true, track: 1 });
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
  assert.deepEqual(st(4, 5, 4, 1), { a: 5, b: 6 }); // 不够一整段时平移到头，不越界
  assert.deepEqual(st(5, 6, 5, 1), { msg: '已经到曲尾了' }); // 已经在曲尾：不从段首重新播放
  assert.deepEqual(st(0, 1, 0, -1), { msg: '已经到开头了' });
  assert.equal(stepSection({ a: 0, b: 0, n: 0, shown: 0, secs: [] }, 1), null);
  assert.deepEqual(sectionsOf([{ system: 0 }, { system: 0 }, { system: 2 }]), [{ sys: 0, a: 0, z: 1 }, { sys: 2, a: 2, z: 2 }]);
});

test('practice line, resume and labels', () => {
  assert.deepEqual(practiceLines(null).lines, ['还没练过']);
  assert.deepEqual(practiceLines({ loops: 3, seconds: 150, best_bpm: 72, last: '昨天' }).lines, ['循环 3 遍 · 最快 72 BPM', '播放 3 分钟 · 上次 昨天']);
  assert.deepEqual(practiceLines({ loops: 0, seconds: 20, last: '今天' }).lines, ['播放 不到 1 分钟（还没弹完一遍） · 上次 今天']);
  const clean = practiceLines({ loops: 5, seconds: 4000, best_bpm: 80, clean: 2, clean_bpm: 76, clean_streak: 2, clean_streak_bpm: 76, last: '今天' });
  assert.deepEqual(clean.lines, ['循环 5 遍 · 最快 80 BPM', '干净 2 遍 · 最快 76 BPM', '播放 1 小时 7 分钟 · 上次 今天']);
  assert.match(clean.title, /最近连续干净 2 遍（76 BPM）/);
  assert.equal(songTimeText({ total_seconds: 0 }), null);
  assert.equal(songTimeText({ total_seconds: 20 }).text, '练了 不到 1 分钟'); // 和曲库同一个格式
  assert.equal(songTimeText({ total_seconds: 3590 }).text, '练了 1 小时');
  assert.equal(songTimeText({ total_seconds: 7170 }).text, '练了 2 小时'); // 以前是“1 小时 60 分钟”
  assert.equal(songTimeText({ total_seconds: 600, undated_seconds: 0 }).text, '练了 10 分钟');
  assert.doesNotMatch(songTimeText({ total_seconds: 600, undated_seconds: 0 }).title, /旧版本/);
  assert.match(songTimeText({ total_seconds: 600, undated_seconds: 120 }).title, /其中 2 分钟是旧版本/);
  const s = { ...SONG, practice: { last: { a: 'c', b: 'b', bpm: 60, at: '昨天' } } };
  assert.deepEqual(resumeInfo(s), { a: 1, z: 2, label: '第 2–3 小节', bpm: 60, at: '昨天' });
  assert.equal(resumeInfo({ ...SONG, practice: { last: { a: 'x', b: 'y', bpm: 60 } } }), null);
  assert.equal(resumeInfo({ ...SONG, practice: { last: { a: 'x', b: 'y', a_n: 4, b_n: 4, bpm: 60 } } }).label, '第 4 小节'); // id 没了用位置
  assert.equal(rangeLabel(0, 3, 4), '全曲');
  assert.equal(loopLabel(0, 3, 4), '第 1–4 小节');
  assert.equal(loopLabel(2, 2, 4), '第 3 小节');
});

test('parseBarInput: typed bar numbers (from 1) become clamped indexes', () => {
  assert.equal(parseBarInput('5', 86), 4);
  assert.equal(parseBarInput(' 12 ', 86), 11);
  assert.equal(parseBarInput('１２', 86), 11); // 中文输入法打出的全角数字
  assert.equal(parseBarInput('0', 86), 0);
  assert.equal(parseBarInput('200', 86), 85);
  for (const bad of ['', 'abc', '5-8', '-3', '2.5']) assert.equal(parseBarInput(bad, 86), null);
  assert.equal(parseBarInput('3', 0), null);
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
  assert.deepEqual(explainState({ ...SONG, source_type: 'musicxml', is_score: true, plan: null }), { missing: 4, noPlan: true, planStale: false, fresh: true });
  assert.equal(explainState({ ...SONG, plan: { stale: true } }).planStale, true);
  assert.equal(needsReview({ needs_review: false, uncertain: '有点怪' }), false); // 后端说了算
  assert.equal(needsReview({ needs_review: true }), true);
  assert.equal(warnText({ code: 'length', msg: '第 3 小节时值不对' }), '第 3 小节时值不对');
  assert.equal(warnText({ code: 'ocr_mismatch' }), '和本机 OCR 对不上');
  assert.equal(showNote('F#3'), 'F♯3');
  assert.equal(tuningInput(' F♯3 '), 'F#3');
  assert.equal(nameToMidi('E4'), 64);
  assert.equal(nameToMidi('F♯3'), 54);
  assert.ok(Number.isNaN(nameToMidi('H2')));
});

// 和后端 migrate.fix_mojibake 同一套规则、同一批例子（FIX-CONTRACT.md）
// 下面的乱码都是“GBK 字节按 windows-1252 读”的结果（0x80–0x9F 区的 ‚ƒ 和 C1 控制字符也在里面）
const MUST_FIX = [
  ['\u00d1\u00ba\u00ce\u00b2\u00b9\u00e2\u00cc\u00ab\u00c0\u00c9', '押尾光太郎'],
  ['\u00ce\u00d2\u00c3\u00c7\u00b5\u00c4\u00b8\u00e8', '我们的歌'],
  ['\u00c7\u00e7\u00cc\u00ec', '晴天'],
  ['\u00d6\u00dc\u00bd\u00dc\u201a\u0090', '周杰倫'],
  ['\u00ce\u00d2\u201a\u0192\u00b5\u00c4\u00b8\u00e8', '我們的歌'],
  ['\u00c4\u00d6\u00c4\u00be \u00b0\u00c7\u00c6\u00d7\u00a3\u00a8TransfmrC\u00d0\u00de\u00b8\u00c4\u00a3\u00a9', '闹木 扒谱（TransfmrC修改）'],
  ['ÌìÂÕÖ®Áµ', '天抡之恋'],
];
const MUST_NOT_CHANGE = ['Canção', 'Coração', 'Ação', 'Größe', 'Mötley Crüe', 'Café', 'Für Elise', 'Señor', 'Ñandú', 'Beyoncé',
  'Romance', 'Jeux Interdits – Romance', '天空之城', ''];
const GP5 = new Uint8Array([0x18, ...new TextEncoder().encode('FICHIER GUITAR PRO v5.10'), 0, 0, 0]);
const GPX = new Uint8Array([...new TextEncoder().encode('BCFZ'), ...new Array(20).fill(0)]);
const GP7 = new Uint8Array([0x50, 0x4b, 3, 4, ...new Array(20).fill(0)]); // zip

test('GBK mojibake from Guitar Pro 3-5 / GPX files is recognised, Western titles are left alone', () => {
  for (const [m, want] of MUST_FIX) assert.equal(gbkFromMojibake(m), want, want);
  for (const s of MUST_NOT_CHANGE) assert.equal(gbkFromMojibake(s), null, s);
  const cn = { title: 'Romance', artist: '', tracks: [{ name: 'Ã÷ÔÂ' }] };
  assert.equal(scoreNeedsGbk(cn, GP5), true);
  assert.equal(scoreNeedsGbk(cn, GPX), true);
  assert.equal(scoreNeedsGbk(cn, GP7), false); // GP7 / MusicXML 本来就是 UTF-8
  assert.equal(scoreNeedsGbk(cn), false);
  assert.equal(scoreNeedsGbk({ title: 'Romance', artist: 'Anon', tracks: [{ name: 'Guitar' }] }, GP5), false);
  assert.equal(scoreNeedsGbk({ title: 'Canção do Mar', artist: 'Mötley Crüe', tracks: [{ name: 'Größe' }] }, GP5), false);
});

test('chord sheets: chords per bar (with the one carried in) and the diagram SVG', async () => {
  const { barChords, chordShapes, chordSVG } = await import('../../app/static/js/trainer/chords.js');
  const ev = (chord) => ({ dur: '1', notes: [], ...(chord ? { chord } : {}) });
  const bars = [
    { events: [ev('C'), ev(), ev('D11'), ev()], shapes: { C: [0, 1, 0, 2, 3, -1], D11: [0, 3, 0, 4, 5, -1] } },
    { events: [ev(), ev(), ev('Em'), ev('Em')], shapes: { Em: [0, 0, 0, 2, 2, 0] } },
    { events: [ev('G')] },
    { events: [ev()] },
  ];
  assert.deepEqual(barChords(bars, 0), [{ name: 'C', carried: false }, { name: 'D11', carried: false }]);
  assert.deepEqual(barChords(bars, 1), [{ name: 'D11', carried: true }, { name: 'Em', carried: false }]);
  assert.deepEqual(barChords(bars, 2), [{ name: 'G', carried: false }]);
  assert.deepEqual(barChords([{ events: [ev()] }], 0), []);
  assert.deepEqual(Object.keys(chordShapes(bars)), ['C', 'D11', 'Em']);
  const svg = chordSVG('C<x>', [0, 1, 0, 2, 3, -1]);
  assert.match(svg, /C&lt;x&gt;/);
  assert.equal((svg.match(/class="cd-dot"/g) || []).length, 3);
  assert.equal((svg.match(/class="cd-open"/g) || []).length, 2);
  assert.match(svg, />×</);
  assert.match(chordSVG('Bm7', [7, 7, 7, 9, 9, 7]), /class="cd-base">7</);
});

test('chord sheets: the carried chord follows first / second endings', async () => {
  const { barChords } = await import('../../app/static/js/trainer/chords.js');
  const ev = (chord) => ({ dur: '1', notes: [], ...(chord ? { chord } : {}) });
  const carried = (bars, bi) => barChords(bars, bi).filter(c => c.carried).map(c => c.name);
  // |: C | G（第一房子）:| （第二房子，没写和弦）| … 第二房子接在 C 后面弹，不是 G
  const bars = [
    { events: [ev('C')], repeat_start: true },
    { events: [ev('G')], endings: [1], repeat_end: 2 },
    { events: [ev()], endings: [2] },
    { events: [ev()] },
  ];
  assert.deepEqual(carried(bars, 2), ['C']);
  assert.deepEqual(carried(bars, 3), ['C']); // 房子后面接的是第二房子（它延续的 C），不是第一房子的 G
  // 第一房子有两个小节：第二个小节延续第一房子自己的 G
  const two = [
    { events: [ev('C')], repeat_start: true },
    { events: [ev('G')], endings: [1] },
    { events: [ev()], endings: [1], repeat_end: 2 },
    { events: [ev('D')], endings: [2] },
    { events: [ev()] },
  ];
  assert.deepEqual(carried(two, 2), ['G']);
  assert.deepEqual(carried(two, 4), ['D']);
  // 前一段反复的房子不影响后一段：过了反复开始就重新算（后一段的第二房子接前一段最后弹的第二房子）
  const later = [
    { events: [ev('C')], repeat_start: true },
    { events: [ev('F')], endings: [1], repeat_end: 2 },
    { events: [ev('Am')], endings: [2] },
    { events: [ev()], repeat_start: true },
    { events: [ev('G')], endings: [1], repeat_end: 2 },
    { events: [ev()], endings: [2] },
  ];
  assert.deepEqual(carried(later, 5), ['Am']);
  assert.deepEqual(carried(later, 3), ['Am']);
});

test('status line counts beats in the time signature denominator', () => {
  assert.equal(beatNumber(0, 4), 1);
  assert.equal(beatNumber(2880, 4), 4);
  // 6/8：八分音符一拍，一小节 6 拍（原来按四分音符数，只有第 1–3 拍）
  assert.deepEqual([0, 480, 960, 1440, 1920, 2400].map(t => beatNumber(t, 8)), [1, 2, 3, 4, 5, 6]);
  assert.equal(beatNumber(470, 8), 1);
  assert.equal(beatNumber(1920, 2), 2); // 2/2：二分音符一拍
  assert.equal(beatNumber(960), 2); // 没有拍号按四分音符
});

test('eventsText：小节的文字版（精修之前的读法）', () => {
  assert.equal(eventsText([{ dur: '1', notes: [N(1, 0), N(2, 3, ['h'])], chord: 'Am' }, { dur: '3', notes: [] }]), '[Am] 1拍 1弦0品 2弦3品[h] | 3拍 休止');
  assert.equal(eventsText([]), '');
});

test('refineConfirm：确认框写出要核对的小节数、预估费用和时间', () => {
  const c = refineConfirm({ bars: 40, cost_usd: 2.2, minutes: 10 }, '曲子');
  assert.equal(c.title, '精修整首“曲子”？');
  assert.match(c.text, /40 个小节/);
  assert.match(c.text, /约 \$2\.20，约 10 分钟/);
  assert.equal(refineConfirm({ bars: 0, cost_usd: 0, minutes: 0 }), null);
  assert.equal(refineConfirm(null), null);
});

test('精修这个小节 / 这一行：预估文字和进行中的范围', () => {
  assert.equal(barRefineHint({ bars: 1, cost_usd: 0.2, minutes: 2, seconds: 75 }), '约 $0.2，1–2 分钟');
  assert.equal(rowRefineHint({ bars: 4, cost_usd: 0.8, minutes: 2, seconds: 75 }), '这一行 4 个小节，约 $0.8，1–2 分钟');
  assert.equal(rowRefineHint({ bars: 8, cost_usd: 1.6, minutes: 3, seconds: 150 }), '这一行 8 个小节，约 $1.6，2–3 分钟');
  assert.equal(rowRefineHint(null), '');
  assert.equal(refineTime(120), '约 2 分钟');
  assert.equal(refineTime(30), '约 1 分钟');
  const ids = ['a', 'b', 'c'];
  assert.equal(refineScopeLabel({ scope: 'bars', bars: ['c'] }, ids), '第 3 小节');
  assert.equal(refineScopeLabel({ scope: 'row', row: 2, bars: ['a', 'b'] }, ids), '第 3 行（2 个小节）');
  assert.equal(refineScopeLabel({ scope: 'bars', bars: ['a', 'b'] }, ids), '2 个小节');
  assert.equal(refineScopeLabel({ scope: 'song', bars: ids }, ids), '整首');
  assert.equal(refineScopeLabel({ kind: 'refine' }, ids), '整首'); // 早先的任务没有范围
});

test('tempoAverage：一遍里换了速度，按时间加权平均（快结束才调快不会把整遍记成新速度）', async () => {
  const { tempoSpan, tempoChange, tempoAverage } = await import('../../app/static/js/trainer/view.js');
  const s = tempoSpan(0, 60);
  assert.equal(tempoAverage(s, 10000), 60);
  tempoChange(s, 9000, 120); // 9 秒 60、最后 1 秒 120
  assert.equal(tempoAverage(s, 10000), 66);
  const z = tempoSpan(5, 80);
  assert.equal(tempoAverage(z, 5), 80); // 还没走时间：就是当前速度
  tempoChange(z, 5, 100); // 一开始就换：完全按新的
  assert.equal(tempoAverage(z, 15), 100);
});

test('hooks：只认登记过的名字，重复注册会警告，invoke 返回提供者的结果', async () => {
  const { createHooks } = await import('../../app/static/js/trainer/hooks.js');
  const warned = [];
  const h = createHooks(['a', 'b'], m => warned.push(m));
  assert.equal(h.has('a'), false);
  assert.equal(h.invoke('a', 1), undefined); // 没人挂：什么都不做
  const off = h.provide('a', x => x + 1);
  assert.equal(h.invoke('a', 1), 2);
  assert.deepEqual(warned, []);
  h.provide('a', x => x * 10);
  assert.equal(warned.length, 1);
  assert.equal(h.invoke('a', 2), 20);
  off(); // 已经被换掉的那个取消：不影响现在的
  assert.equal(h.invoke('a', 3), 30);
  assert.throws(() => h.provide('c', () => {}), /未知的钩子/);
  assert.throws(() => h.invoke('typo'), /未知的钩子/);
});

test('click：播放和敲节奏共用的节拍器 / 预备拍音量换算', async () => {
  const c = await import('../../app/static/js/trainer/click.js');
  const p = { metro: true, metroVol: 70, countIn: true };
  assert.equal(c.metronomeLevel(p), 0.7);
  assert.equal(c.metronomeLevel({ ...p, metro: false }), 0);
  assert.equal(c.countInLevel({ ...p, metroVol: 10 }), 0.5); // 预备拍不低于一半
  assert.equal(c.countInLevel({ ...p, countIn: false }), 0);
  assert.equal(c.metronomeGain(p), 0.7 * c.GAIN); // 设给 alphaTab 的
  assert.equal(c.clickGain(0.7, true), 0.7 * c.GAIN); // 敲节奏的重拍和 alphaTab 一样响
  assert.ok(c.clickGain(0.7, false) < c.clickGain(0.7, true));
  assert.equal(c.audibleLevel({ metroVol: 0 }), 0.5);
});
