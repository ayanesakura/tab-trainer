// 往返校验：我们的数据 → alphaTex → alphaTab 解析 → 转回我们的数据，应该和原来一致。
// 运行：node --test tests/js/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import * as at from '../../app/static/vendor/alphatab/alphaTab.core.min.mjs';
import { toAlphaTex, deriveSong, splitTicks, durToTicks, ticksToDur, eventIndexAt, DERIVE_VERSION, CHORD_PAD } from '../../app/static/js/score.js';

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

// 一个“真”的 Guitar Pro 文件：两个声部、中途变拍号、24 品以上的音。用 alphaTab 导出成 GP7 再按 GP 文件导入，
// 走的是练习页打开 .gp 文件的同一条路径（GP 导入器 → deriveSong）
const MIXED_TEX = `\\title "混合"
.
\\track "Guitar" { instrument 25 }
\\staff {score tabs}
\\voice
0.6.4 2.5.4 2.4.4 1.3.4 | \\ts (3 4) 27.1.4 25.2.4 24.1.4 | \\ts (6 8) 0.1.8 2.1.8 3.1.8 5.1.4{d} | \\ts (4 4) 0.1.1
\\voice
0.6.2 0.6.2 | 0.5.2{d} | 0.4.4{d} 0.4.4{d} | r.1`;

const gpRoundTrip = tex => {
  const settings = new at.Settings();
  const bytes = new at.exporter.Gp7Exporter().export(parse(tex), settings);
  return at.importer.ScoreLoader.loadScoreFromBytes(bytes, settings);
};

test('GP file with two voices, time signature changes and high frets', () => {
  const d = deriveSong(gpRoundTrip(MIXED_TEX), 0);
  assert.equal(d.version, DERIVE_VERSION);
  assert.deepEqual(d.meta.time_signature, [4, 4]);
  assert.deepEqual(d.bars.map(b => b.time_signature), [undefined, [3, 4], [6, 8], undefined]);
  // 每个小节的时值合计等于它自己的拍号
  const len = b => { const t = b.time_signature || d.meta.time_signature; return t[0] * 4 / t[1]; };
  for (const b of d.bars) assert.equal(b.events.reduce((a, e) => a + durToTicks(e.dur), 0), len(b) * 960);
  const sig = b => b.events.map(e => e.dur + ':' + e.notes.map(n => `${n.string}/${n.fret}`).sort().join(','));
  // 第 1 小节：两个声部都在 6 弦空弦起拍，只算一个音；第二个声部的第二个音和第一个声部的第三拍合并
  assert.deepEqual(sig(d.bars[0]), ['1:6/0', '1:5/2', '1:4/2,6/0', '1:3/1']);
  assert.deepEqual(sig(d.bars[1]), ['1:1/27,5/0', '1:2/25', '1:1/24']);
  assert.deepEqual(sig(d.bars[2]), ['1/2:1/0,4/0', '1/2:1/2', '1/2:1/3', '3/2:1/5,4/0']);
});

test('per-bar time signatures survive our data -> alphaTex -> alphaTab', () => {
  const d = deriveSong(gpRoundTrip(MIXED_TEX), 0);
  const score = parse(toAlphaTex(d).tex);
  assert.deepEqual(score.masterBars.map(m => [m.timeSignatureNumerator, m.timeSignatureDenominator]), [[4, 4], [3, 4], [6, 8], [4, 4]]);
  assert.deepEqual(norm(deriveSong(score, 0).bars), norm(d.bars));
});

// 拍上倚音（GP 里常见）：alphaTab 播放时把主音往后推一个三十二分（playbackStart 120），
// 起拍要按谱面位置算，否则小节开头会多出一个八分之一拍的假休止
test('on-beat grace notes do not shift the main note or add a rest', () => {
  const tex = `\\track "G" \\staff {tabs} \\tuning (E4 B3 G3 D3 A2 E2) . 2.3.4 {gr ob} 4.3.4 5.3.4 7.3.2 | 3.2.4 5.2.4 {gr ob} 7.2.4 0.1.4`;
  const score = gpRoundTrip(tex);
  const beats = score.tracks[0].staves[0].bars[0].voices[0].beats;
  assert.equal(beats[1].playbackStart, 120); // 前提：alphaTab 确实把主音推后了
  const d = deriveSong(score, 0);
  const sig = b => b.events.map(e => e.dur + ':' + e.notes.map(n => `${n.string}/${n.fret}` + (n.grace >= 0 ? `g${n.grace}` : '')).join(','));
  assert.deepEqual(sig(d.bars[0]), ['1:3/4g2', '1:3/5', '2:3/7']);
  assert.deepEqual(sig(d.bars[1]), ['1:2/3', '1:2/7g5', '2:1/0']);
  // 播放时用 playbackStart 找当前事件：被推后的主音仍然对应它自己的事件
  const onsets = [0, 960, 1920];
  for (const b of beats) assert.equal(eventIndexAt(onsets, b.playbackStart), Math.min(2, Math.floor(b.displayStart / 960)));
});

// GP 文件里：闷音可能留着按弦时的品格（谱上只画 x）；自然泛音品格记 0、泛音点另存（谱上画 <12>）。
// 我们的品格要和谱上写的一致
test('dead notes get fret 0 and natural harmonics take their harmonic node', () => {
  const score = gpRoundTrip(`\\track "G" \\staff {tabs} \\tuning (E4 B3 G3 D3 A2 E2) . 9.3.4 7.2.4 5.4.4 3.5.4`);
  const [b0, b1, b2, b3] = score.tracks[0].staves[0].bars[0].voices[0].beats;
  b0.notes[0].isDead = true; // 3 弦 9 品，画成 x
  Object.assign(b1.notes[0], { fret: 0, harmonicType: 1, harmonicValue: 12 }); // 2 弦 <12>
  Object.assign(b2.notes[0], { harmonicType: 1, harmonicValue: 5 }); // 4 弦 <5>（品格本来就是 5）
  Object.assign(b3.notes[0], { fret: 3, harmonicType: 2, harmonicValue: 12 }); // 人工泛音：品格是按的那一品
  const d = deriveSong(score, 0);
  const sig = d.bars[0].events.map(e => e.notes.map(n => `${n.string}/${n.fret}${n.tech.join('')}`).join(','));
  assert.deepEqual(sig, ['3/0x', '2/12nh', '4/5nh', '5/3ah']);
  assert.equal(d.version, DERIVE_VERSION);
  // 我们的数据 → alphaTex → alphaTab → 我们的数据：x / nh 的品格不变
  assert.deepEqual(norm(deriveSong(parse(toAlphaTex(d).tex), 0).bars), norm(d.bars));
});

// GP 里的小节可以比拍号长：多出来的拍从小节长度处（或更后面）开始，最后一个音的时值按它自己的算，不能是 0 或负数
test('overfull GP bars: every event keeps a positive duration', () => {
  const d = deriveSong(parse('.\n0.6.4 2.6.4 3.6.4 5.6.4 7.6.2 | 0.6.4 2.6.4 3.6.4 5.6.4 7.6.8 8.6.8 | 0.6.1'), 0);
  assert.deepEqual(d.bars.map(b => b.events.map(e => e.dur)), [['1', '1', '1', '1', '2'], ['1', '1', '1', '1', '1/2', '1/2'], ['4']]);
  // 没有时值信息的拍（合成数据）也不会出 0
  const beat = (ds, fret) => ({ displayStart: ds, notes: [{ string: 6, fret }], isRest: false, graceType: 0 });
  const mb = { timeSignatureNumerator: 4, timeSignatureDenominator: 4, calculateDuration: () => 3840 };
  const score = { title: '', artist: '', tab: '', tempo: 90, masterBars: [mb], tracks: [{ name: 'g', staves: [{ tuning: [64, 59, 55, 50, 45, 40], capo: 0,
    bars: [{ voices: [{ beats: [beat(0, 0), beat(1920, 2), beat(3840, 3)] }] }] }] }] };
  const durs = deriveSong(score).bars[0].events.map(e => durToTicks(e.dur));
  assert.deepEqual(durs, [1920, 1920, 960]);
});

test('chord sheets: chord names, diagrams and strums survive the round trip', () => {
  const C = [0, 1, 0, 2, 3, -1], D11 = [0, 3, 0, 4, 5, -1];
  const strum = (frets, dir, chord) => ({
    dur: '1', strum: dir, ...(chord ? { chord } : {}),
    notes: frets.map((f, i) => (f < 0 ? null : N(i + 1, f))).filter(Boolean),
  });
  const song = {
    meta: { title: '弹唱', tuning: ['E4', 'B3', 'G3', 'D3', 'A2', 'E2'], capo: 0, time_signature: [4, 4] },
    bars: [
      { shapes: { C, D11 }, events: [strum(C, 'down', 'C'), strum(C, 'up'), strum(D11, 'down', 'D11'), strum(D11, 'up')] },
      { events: [{ dur: '1', notes: [N(5, 3)], chord: 'C', lyrics: ['天真', '多浅'] }, { dur: '3', notes: [N(4, 2)], lyrics: ['', '薄'] }] },
    ],
  };
  const { tex } = toAlphaTex(song);
  assert.match(tex, /\\chord \("C" 0 1 0 2 3 x\)/);
  assert.match(tex, /\\chord \("D11" 0 3 0 4 5 x\) \{firstfret 3 /);
  const score = parse(tex);
  const staff = score.tracks[0].staves[0];
  const beats = staff.bars[0].voices[0].beats;
  assert.deepEqual(beats.map(b => b.brushType), [2, 1, 2, 1]); // 2 = 下扫、1 = 上扫
  assert.equal(beats[0].chord.name, 'C');
  assert.deepEqual([...beats[0].chord.strings], C);
  const back = deriveSong(score);
  assert.deepEqual(back.bars[0].events.map(e => [e.chord || '', e.strum || '']), [['C', 'down'], ['', 'up'], ['D11', 'down'], ['', 'up']]);
  assert.deepEqual(back.bars[0].shapes, { C, D11 });
  assert.equal(back.bars[1].events[0].chord, 'C');
  assert.equal(back.bars[1].events[0].strum, undefined); // 单音不算扫弦
  assert.deepEqual(back.bars[1].events.map(e => e.lyrics), [['天真', '多浅'], ['', '薄']]);
  assert.equal(back.bars[0].events[0].lyrics, undefined);
});

test('chord names that differ only by case stay separate chords (Am7 / AM7, Bm / BM)', () => {
  const Am7 = [0, 1, 0, 2, 0, -1], AM7 = [0, 2, 1, 2, 0, -1], Bm = [2, 3, 4, 4, 2, -1];
  const ev = (chord, frets) => ({ dur: '4', chord, notes: frets.map((f, i) => (f < 0 ? null : N(i + 1, f))).filter(Boolean) });
  const song = {
    meta: { tuning: ['E4', 'B3', 'G3', 'D3', 'A2', 'E2'], capo: 0, time_signature: [4, 4] },
    bars: [
      { shapes: { Am7 }, events: [ev('Am7', Am7)] },
      { shapes: { AM7 }, events: [ev('AM7', AM7)] },
      { shapes: { Bm }, events: [ev('Bm', Bm), ev('BM', [2, 4, 4, 4, 2, -1]), ev('Am7', Am7)] }, // BM 没有指法、只有名字
    ],
  };
  const { tex } = toAlphaTex(song);
  const staff = parse(tex).tracks[0].staves[0];
  const got = staff.bars.flatMap(b => b.voices[0].beats.filter(x => x.chord)).map(x => [x.chord.name.replaceAll(CHORD_PAD, ''), [...x.chord.strings]]);
  assert.deepEqual(got.map(g => g[0]), ['Am7', 'AM7', 'Bm', 'BM', 'Am7']);
  assert.deepEqual(got[0][1], Am7);
  assert.deepEqual(got[1][1], AM7);
  assert.deepEqual(got[2][1], Bm);
  assert.deepEqual(got[4][1], Am7);
  assert.equal(new Set([...staff.chords.keys()]).size, 4);
  // 第一次出现的写法原样保留，后来者只多了看不见的零宽空格
  assert.equal(staff.bars[0].voices[0].beats[0].chord.name, 'Am7');
  assert.equal(staff.bars[1].voices[0].beats[0].chord.name, 'AM7' + CHORD_PAD);
});

test('rolled chords keep their direction (wavy arrow toward string 1 = low to high)', () => {
  const roll = (dir) => ({ dur: '2', ...(dir ? { strum: dir } : {}), notes: [N(1, 0, ['roll']), N(2, 1, ['roll']), N(3, 0, ['roll'])] });
  const song = { meta: { tuning: ['E4', 'B3', 'G3', 'D3', 'A2', 'E2'], capo: 0, time_signature: [4, 4] }, bars: [{ events: [roll('down'), roll('up')] }, { events: [roll(), roll()] }] };
  const score = parse(toAlphaTex(song).tex);
  const types = score.tracks[0].staves[0].bars.map(b => b.voices[0].beats.map(x => x.brushType));
  assert.deepEqual(types, [[4, 3], [4, 4]]); // 4 = ArpeggioDown（画 ▲），没标方向的琶音也是它
  const back = deriveSong(score);
  assert.deepEqual(back.bars[0].events.map(e => e.strum), ['down', 'up']);
  assert.deepEqual(norm(back.bars), norm(song.bars));
});
