// 练习记录的纯逻辑（practice-pass.js）：遍数、零头时间、“这遍干净”、弹完以后的宽限、从段首重新开始
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { initialLog, step, GRACE_MS, MIN_SECONDS, graceMs } from '../../app/static/js/trainer/practice-pass.js';

const IDS = ['bar1'];
const next = (bpm = 60) => ({ kind: 'play', ids: IDS, bpm });
/** 依次喂事件，收集发出去的记录和 play.clean、提示 */
function run(events) {
  let s = initialLog();
  const sent = [], cleans = [], msgs = [];
  for (const ev of events) {
    const r = step(s, ev);
    s = r.state;
    sent.push(...r.send);
    if (r.clean !== undefined) cleans.push([ev.now, r.clean]);
    if (r.msg) msgs.push(r.msg);
  }
  return { s, sent, cleans, msgs };
}

test('practice-pass：弹完一遍先压着，宽限过了才发；没用过“这遍干净”不带 clean', () => {
  const { s, sent } = run([
    { type: 'start', kind: 'play', ids: IDS, bpm: 60, now: 0 },
    { type: 'end', loopDone: true, now: 4000, next: next() },
    { type: 'tick', now: 4000 + GRACE_MS - 1 },
  ]);
  assert.deepEqual(sent, []); // 还在宽限里
  const r = step(s, { type: 'tick', now: 4000 + GRACE_MS });
  assert.deepEqual(r.send, [{ ids: IDS, loops: 1, bpm: 60, seconds: 4, kind: 'play' }]);
  assert.equal(r.state.held, null);
  assert.equal(r.state.cur.t, 4000); // 下一遍从弹完的那一刻开始
});

test('practice-pass：弹着的时候标干净，弹完的这一遍带 clean: true，之后没标的遍带 clean: false', () => {
  const { sent, cleans, msgs } = run([
    { type: 'start', kind: 'play', ids: IDS, bpm: 60, now: 0 },
    { type: 'mark', now: 1000 },
    { type: 'mark', now: 1500 }, // 再按一次：已经标过了
    { type: 'end', loopDone: true, now: 4000, next: next() },
    { type: 'end', loopDone: true, now: 8000, next: next() }, // 下一遍弹完：先把上一遍发出去
    { type: 'tick', now: 8000 + GRACE_MS },
  ]);
  assert.deepEqual(msgs, ['marked', 'already']);
  assert.deepEqual(cleans, [[1000, true], [4000, false]]);
  assert.deepEqual(sent.map(x => [x.loops, x.seconds, x.clean]), [[1, 4, true], [1, 4, false]]);
});

test('practice-pass：从段首重新开始（Home）时，标记作废，断掉的那一遍不算进下一遍的时长', () => {
  // 60 BPM 一小节 4 秒：1 秒时标干净，1.5 秒按 Home（不到 2 秒的零头不记），之后完整弹完一遍
  const { sent, cleans } = run([
    { type: 'start', kind: 'play', ids: IDS, bpm: 60, now: 0 },
    { type: 'mark', now: 1000 },
    { type: 'end', loopDone: false, now: 1500, next: next() },
    { type: 'end', loopDone: true, now: 5500, next: next() },
    { type: 'tick', now: 5500 + GRACE_MS },
  ]);
  assert.deepEqual(cleans, [[1000, true], [1500, false]]);
  assert.deepEqual(sent, [{ ids: IDS, loops: 1, bpm: 60, seconds: 4, kind: 'play', clean: false }]);
  // 断掉之前已经弹了 MIN_SECONDS 以上：零头时间单独记一条（不算遍数）
  const long = run([
    { type: 'start', kind: 'play', ids: IDS, bpm: 60, now: 0 },
    { type: 'end', loopDone: false, now: MIN_SECONDS * 1000 + 500, next: next() },
  ]);
  assert.deepEqual(long.sent, [{ ids: IDS, loops: 0, bpm: 60, seconds: MIN_SECONDS + 0.5, kind: 'play' }]);
});

test('practice-pass：弹完以后的宽限里按，标的是刚弹完的那一遍，不会同时标到下一遍', () => {
  const { s, sent, cleans, msgs } = run([
    { type: 'start', kind: 'play', ids: IDS, bpm: 60, now: 0 },
    { type: 'end', loopDone: true, now: 4000, next: next() },
    { type: 'mark', now: 4800 }, // 弹完 0.8 秒（预备拍里）才按
    { type: 'mark', now: 5000 }, // 宽限里又按一次：不挪到下一遍
    { type: 'end', loopDone: true, now: 8000, next: next() },
    { type: 'tick', now: 8000 + GRACE_MS },
  ]);
  assert.deepEqual(msgs, ['prev', 'prevAlready']);
  assert.deepEqual(cleans, []); // 控制条上的按钮管的是正在弹的这一遍：不亮
  assert.deepEqual(sent.map(x => x.clean), [true, false]);
  assert.equal(s.cur.clean, false);
  // 宽限过了再按：标的是正在弹的这一遍
  const late = run([
    { type: 'start', kind: 'play', ids: IDS, bpm: 60, now: 0 },
    { type: 'end', loopDone: true, now: 4000, next: next() },
    { type: 'mark', now: 4000 + GRACE_MS },
    { type: 'end', loopDone: true, now: 8000, next: next() },
    { type: 'tick', now: 8000 + GRACE_MS },
  ]);
  assert.deepEqual(late.msgs, ['marked']);
  assert.deepEqual(late.sent.map(x => x.clean), [false, true]);
});

test('practice-pass：停下时马上发出压着的那一遍；敲节奏只记时间不算遍数，也不能标干净', () => {
  const stop = run([
    { type: 'start', kind: 'play', ids: IDS, bpm: 60, now: 0 },
    { type: 'end', loopDone: true, now: 4000, next: next() },
    { type: 'end', loopDone: false, now: 4500, next: null }, // 停了
  ]);
  assert.deepEqual(stop.sent.map(x => x.loops), [1]);
  assert.equal(stop.s.cur, null);
  assert.equal(stop.s.held, null);
  const rhythm = run([
    { type: 'start', kind: 'rhythm', ids: IDS, bpm: 80, now: 0 },
    { type: 'mark', now: 500 },
    { type: 'end', loopDone: true, now: 3000, next: null },
  ]);
  assert.deepEqual(rhythm.msgs, ['idle']);
  assert.deepEqual(rhythm.sent, [{ ids: IDS, loops: 0, bpm: 80, seconds: 3, kind: 'rhythm' }]);
  assert.deepEqual(run([{ type: 'mark', now: 0 }]).msgs, ['idle']); // 停着按不算
});

test('practice-pass：一遍里换速度按时间加权；状态不原地改', () => {
  const s0 = step(initialLog(), { type: 'start', kind: 'play', ids: IDS, bpm: 60, now: 0 }).state;
  const span = s0.cur.tempo;
  const s1 = step(s0, { type: 'tempo', bpm: 120, now: 9000 }).state;
  assert.deepEqual(span, { t0: 0, t: 0, bpm: 60, acc: 0 }); // 原来的状态没被改
  const r = step(step(s1, { type: 'end', loopDone: true, now: 10000, next: null }).state, { type: 'tick', now: 20000 });
  assert.equal(r.send[0].bpm, 66);
  assert.throws(() => step(initialLog(), { type: 'nope' }));
});

test('practice-pass：宽限不超过一遍的一半（很短的一遍也能标正在弹的那一遍）', () => {
  assert.equal(graceMs(4), GRACE_MS);
  assert.equal(graceMs(1), 500);
  const { msgs, sent } = run([
    { type: 'start', kind: 'play', ids: IDS, bpm: 240, now: 0 },
    { type: 'end', loopDone: true, now: 1000, next: next(240) },
    { type: 'mark', now: 1600 }, // 弹完 0.6 秒：已经过了这一遍的宽限，标的是正在弹的这一遍
    { type: 'end', loopDone: true, now: 2000, next: next(240) },
    { type: 'tick', now: 3000 },
  ]);
  assert.deepEqual(msgs, ['marked']);
  assert.deepEqual(sent.map(x => x.clean), [false, true]);
});
