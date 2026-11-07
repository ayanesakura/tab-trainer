// 敲节奏的纯计算：要敲的点、口令、判定窗口、配对、打分、诊断、常错位置。
// 运行：node --test tests/js/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildProgram, syllable, windows, matchTap, grade, summarize, diagnose, weakSpots, placeName, GOOD, MAX_WIN, MIN_WIN } from '../../app/static/js/rhythm.js';

// 和练习页 applySong 一样的事件：t 是小节内起点（四分音符），d 是到下一个事件的时长，notes 为空是休止 / 延音
const evs = list => { let t = 0; return list.map(([d, n]) => { const e = { t, d, notes: n ? [{ s: 1, f: 0 }] : [] }; t += d; return e; }); };

test('syllables: 1 e & a, triplets, compound meter', () => {
  assert.deepEqual([0, 0.25, 0.5, 0.75, 1, 3.5].map(t => syllable(t, 1)), ['1', 'e', '&', 'a', '2', '&']);
  assert.deepEqual([1 / 3, 2 / 3, 1 + 1 / 3].map(t => syllable(t, 1)), ['trip', 'let', 'trip']);
  assert.equal(syllable(1 / 8, 1), '·');
  // 6/8：一拍是八分音符（0.5 个四分音符）
  assert.deepEqual([0, 0.5, 0.75, 2.5].map(t => syllable(t, 0.5)), ['1', '2', '&', '6']);
});

test('program: onsets skip rests and ties, chords count once, beats follow time signature', () => {
  const p = buildProgram([
    { bi: 4, len: 4, ts: [4, 4], events: evs([[1, 1], [0.5, 0], [0.5, 1], [2, 1]]) },
    { bi: 5, len: 3, ts: [6, 8], events: evs([[1.5, 1], [1.5, 1]]) },
  ]);
  assert.equal(p.Q, 7);
  assert.deepEqual(p.onsets.map(o => o.q), [0, 1.5, 2, 4, 5.5]);
  assert.deepEqual(p.onsets.map(o => o.label), ['1', '&', '3', '1', '4']);
  assert.deepEqual(p.onsets.map(o => o.bi), [4, 4, 4, 5, 5]);
  assert.deepEqual(p.bars[0].beats.map(b => b.q), [0, 1, 2, 3]);
  assert.equal(p.bars[1].beats.length, 6);
  assert.deepEqual(p.bars[1].beats.filter(b => b.accent).map(b => b.q), [4]);
  assert.equal(placeName(p.onsets[1]), '第 5 小节 · 第 2 拍的 &');
  assert.equal(placeName(p.onsets[2]), '第 5 小节 · 第 3 拍');
  assert.equal(placeName({ bi: 0, beat: 1, label: '·' }), '第 1 小节 · 第 1 拍中间');
  // 同一时刻的两个事件（多声部）只敲一下；超出小节长度的事件不算
  const q = buildProgram([{ bi: 0, len: 2, ts: [2, 4], events: [{ t: 0, d: 0, notes: [{}] }, { t: 0, d: 1, notes: [{}] }, { t: 1, d: 1, notes: [{}] }, { t: 2, d: 1, notes: [{}] }] }]);
  assert.deepEqual(q.onsets.map(o => o.q), [0, 1]);
});

test('windows: half the gap to neighbours, wrapping around the loop, clamped', () => {
  // 60 BPM：四分音符 1 秒。两个十六分音符 + 一个长音：前后较近的那个间隔的一半
  const p = buildProgram([{ bi: 0, len: 4, ts: [4, 4], events: evs([[0.25, 1], [0.25, 1], [3.5, 1]]) }]);
  assert.deepEqual(windows(p.onsets, p.Q, 1), [0.125, 0.125, 0.125]);
  // 只有一个音：前后都是绕回来的下一遍，间隔 4 s → 封顶；可以单独放宽上限（校准用）
  const one = buildProgram([{ bi: 0, len: 4, ts: [4, 4], events: evs([[4, 1]]) }]);
  assert.deepEqual(windows(one.onsets, one.Q, 1), [MAX_WIN]);
  assert.deepEqual(windows(one.onsets, one.Q, 1, 0.35), [0.35]);
  // 很快的时候不小于 MIN_WIN
  assert.equal(windows(p.onsets, p.Q, 0.1)[0], MIN_WIN);
});

test('matching picks the nearest unhit note inside its window', () => {
  const notes = [{ T: 1, win: 0.1, err: null }, { T: 1.2, win: 0.1, err: null }, { T: 2, win: 0.2, err: null }];
  assert.equal(matchTap(notes, 1.12), 1);
  assert.equal(matchTap(notes, 1.05), 0);
  assert.equal(matchTap(notes, 1.5), -1); // 离谁都远：多敲
  notes[0].err = 0.01;
  assert.equal(matchTap(notes, 1.02), -1); // 已经敲过的音不再配
  notes[2].missed = true;
  assert.equal(matchTap(notes, 2), -1);
});

test('grading and summary', () => {
  assert.equal(grade(0.03, 0.2), 'good');
  assert.equal(grade(-GOOD, 0.2), 'good');
  assert.equal(grade(0.06, 0.2), 'ok');
  assert.equal(grade(0.035, 0.03), 'ok'); // 窗口比“准”还窄时以窗口为准
  const notes = [0.01, -0.02, 0.07, null].map(err => ({ err, win: 0.2 }));
  const s = summarize(notes, 1);
  assert.deepEqual({ ...s, sd: undefined }, { n: 4, good: 2, ok: 1, miss: 1, extra: 1, mean: 20, sd: undefined, score: 50 });
  assert.equal(s.sd, 46); // 偏差 10 / -20 / 70 ms 的样本标准差
  assert.equal(summarize([{ err: 0, win: 0.1 }, { err: 0.01, win: 0.1 }]).score, 100);
  assert.equal(summarize([]).score, 0);
});

test('diagnosis names the worst problem first', () => {
  const base = { n: 10, good: 8, ok: 2, miss: 0, extra: 0, mean: 0, sd: 10, score: 90 };
  assert.match(diagnose({ ...base, good: 4, ok: 2, miss: 4, score: 50 }).text, /漏了 4 个音/);
  assert.match(diagnose({ ...base, mean: 40 }).text, /偏晚 40 ms/);
  assert.match(diagnose({ ...base, mean: -30 }).text, /偏早 30 ms/);
  assert.match(diagnose({ ...base, sd: 50 }).text, /不稳/);
  assert.match(diagnose({ ...base, extra: 2, score: 75 }).text, /多敲了 2 下/);
  assert.equal(diagnose({ ...base, good: 10, ok: 0, score: 100 }).tone, 'good');
  assert.equal(diagnose({ ...base, n: 0 }).text, '');
});

test('weak spots: notes missed or off the same way across loops', () => {
  const p = buildProgram([{ bi: 2, len: 4, ts: [4, 4], events: evs([[1, 1], [0.5, 1], [0.5, 1], [2, 1]]) }]);
  assert.deepEqual(weakSpots(p.onsets, [[0, 0, 0, 0]]), []); // 一遍不够
  const loops = [[0.01, -0.07, null, 0.02], [0, -0.06, null, 0.09], [0.02, -0.08, 0.01, -0.01]];
  const w = weakSpots(p.onsets, loops);
  assert.deepEqual(w.map(x => [x.i, x.kind]), [[2, 'miss'], [1, 'early']]);
  assert.equal(w[1].text, '总是偏早 70 ms');
  assert.equal(w[0].bi, 2);
});
