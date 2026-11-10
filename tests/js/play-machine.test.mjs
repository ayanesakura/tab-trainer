// 播放状态机（js/trainer/play-machine.js）：node --test tests/js/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { initial, step, isPlaying } from '../../app/static/js/trainer/play-machine.js';

// 依次喂事件，返回最后的状态和所有 fx
function run(s, ...events) {
  const fx = [];
  for (const ev of events) { const r = step(s, ev); s = r.state; fx.push(...r.fx); }
  return { s, fx };
}

test('播放 / 停止：开着预备拍先进 countIn，位置往前走了才算数完', () => {
  let { s, fx } = run(initial(true), { type: 'seek', tick: 3840 }, { type: 'play' });
  assert.equal(s.phase, 'countIn');
  assert.equal(s.from, 3840);
  assert.deepEqual(fx.slice(-3), [['resetLoops'], ['seek', 3840], ['play']]); // 先放到确认过的位置，再开始
  ({ s } = run(s, { type: 'state', playing: true }, { type: 'position', tick: 3840 }, { type: 'position', tick: 3843 }));
  assert.equal(s.phase, 'countIn'); // 预备拍里位置不动
  ({ s } = run(s, { type: 'position', tick: 3900 }));
  assert.equal(s.phase, 'playing');
  ({ s, fx } = run(s, { type: 'stop' }));
  assert.equal(s.phase, 'idle');
  assert.deepEqual(fx, [['pause']]);
  assert.equal(isPlaying(s), false);
});

test('预备拍里停下：重新生成 MIDI，好了放回开始的位置；期间按播放就等它好了再开始', () => {
  let { s, fx } = run(initial(true), { type: 'seek', tick: 1920 }, { type: 'play' }, { type: 'stop' });
  assert.equal(s.phase, 'reloadingMidi');
  assert.deepEqual(fx.slice(-2), [['pause'], ['reloadMidi']]);
  assert.equal(isPlaying(s), false);
  ({ s } = run(s, { type: 'play' }));
  assert.equal(isPlaying(s), true); // 播放键马上显示“停止”
  ({ s, fx } = run(s, { type: 'midiLoaded', range: { start: 0, end: Infinity } }));
  assert.equal(s.phase, 'countIn');
  assert.deepEqual(fx, [['seek', 1920], ['play']]);
});

test('预备拍里点了别的小节再停：放回的是点的小节', () => {
  const { s, fx } = run(initial(true), { type: 'seek', tick: 7680 }, { type: 'play' }, { type: 'seek', tick: 0 }, { type: 'stop' }, { type: 'midiLoaded', range: { start: 0, end: Infinity } });
  assert.equal(s.phase, 'idle');
  assert.deepEqual(fx.slice(-1), [['seek', 0]]);
});

test('bug：开着预备拍、播放中换到前面的段——预备拍从新的段首数，停下以后也放回新的段首', () => {
  // 在第 5 小节附近放着（位置 19200），换到第 1–2 小节（0..7680）
  let { s, fx } = run(initial(true), { type: 'seek', tick: 15360 }, { type: 'play' }, { type: 'state', playing: true }, { type: 'position', tick: 15500 }, { type: 'position', tick: 19200 });
  assert.equal(s.phase, 'playing');
  ({ s, fx } = run(s, { type: 'jumped' }, { type: 'restart', tick: 0 }));
  assert.equal(s.phase, 'restarting');
  assert.deepEqual(fx, [['resetLoops'], ['stop']]); // 不在 stop() 之后马上读位置、开始：等它真的停下
  assert.equal(isPlaying(s), true); // 中间不闪成“停止”
  // worker 停下前还会发旧位置：不算弹完一遍，也不影响要开始的位置
  ({ s, fx } = run(s, { type: 'position', tick: 19300, rangeStart: 0 }, { type: 'state', playing: false }));
  assert.equal(s.phase, 'countIn');
  assert.equal(s.from, 0);
  assert.deepEqual(fx, [['seek', 0], ['play']]);
  // 重新开始前后的抖动（停在 1，又报一个 11，再回到 1）：还没报“在放”之前的位置不算预备拍数完
  ({ s } = run(s, { type: 'position', tick: 11, rangeStart: 0 }, { type: 'position', tick: 1, rangeStart: 0 }));
  assert.equal(s.phase, 'countIn');
  // 预备拍期间位置停在 0：一直在 countIn；以前 from 记成了 19200，预备拍永远“数不完”
  ({ s } = run(s, { type: 'state', playing: true }, { type: 'position', tick: 0, rangeStart: 0 }));
  assert.equal(s.phase, 'countIn');
  // 停下：重新生成 MIDI，放回新的段首（以前放回 19200，被夹到段尾，再播放马上就“弹完”）
  ({ s, fx } = run(s, { type: 'stop' }, { type: 'midiLoaded', range: { start: 0, end: 7680 } }));
  assert.equal(s.phase, 'idle');
  assert.deepEqual(fx.slice(-1), [['seek', 0]]);
  assert.ok(!fx.some(f => f[0] === 'loop'));
});

test('MIDI 重新生成期间换段：好了以后从新的段首开始', () => {
  let { s } = run(initial(true), { type: 'seek', tick: 7680 }, { type: 'play' }, { type: 'stop' }, { type: 'play' });
  assert.equal(s.phase, 'reloadingMidi');
  ({ s } = run(s, { type: 'restart', tick: 960 }));
  const { s: s2, fx } = run(s, { type: 'midiLoaded', range: { start: 960, end: 4800 } });
  assert.equal(s2.phase, 'countIn');
  assert.deepEqual(fx, [['seek', 960], ['play']]);
  // 停着的时候（play: false）换段也一样放回新的段首
  ({ s } = run(initial(true), { type: 'seek', tick: 7680 }, { type: 'play' }, { type: 'stop' }, { type: 'seek', tick: 960 }));
  const r = run(s, { type: 'midiLoaded', range: { start: 960, end: 4800 } });
  assert.deepEqual(r.fx, [['seek', 960]]);
});

test('段落循环开着预备拍：段尾记一遍、停下后从段首带预备拍重新开始，中间一直算“在放”', () => {
  let { s, fx } = run(initial(true), { type: 'seek', tick: 3840 }, { type: 'play' }, { type: 'state', playing: true }, { type: 'position', tick: 4000 });
  assert.equal(s.phase, 'playing');
  ({ s, fx } = run(s, { type: 'finished', whole: false, rangeStart: 3840 }));
  assert.deepEqual(fx, [['loop']]);
  assert.equal(s.phase, 'restarting');
  ({ s, fx } = run(s, { type: 'position', tick: 3840, rangeStart: 3840 }, { type: 'state', playing: false }));
  assert.deepEqual(fx, [['seek', 3840], ['play']]);
  assert.equal(s.phase, 'countIn');
  // 段尾正要重新开始时按了停止：就停在段首
  ({ s, fx } = run(s, { type: 'position', tick: 4000 }, { type: 'finished', whole: false, rangeStart: 3840 }, { type: 'stop' }));
  assert.equal(s.phase, 'idle');
  assert.deepEqual(fx.slice(-2), [['pause'], ['seek', 3840]]);
  ({ s, fx } = run(s, { type: 'state', playing: false }));
  assert.deepEqual(fx, []);
});

test('关着预备拍的段落循环（alphaTab 自己循环）：从段尾跳回段首才算一遍，换段跳回不算', () => {
  let { s, fx } = run(initial(false), { type: 'seek', tick: 3840 }, { type: 'play' }, { type: 'state', playing: true });
  assert.equal(s.phase, 'playing');
  ({ s, fx } = run(s, { type: 'position', tick: 5000, rangeStart: 3840 }, { type: 'position', tick: 7600, rangeStart: 3840 }, { type: 'position', tick: 3850, rangeStart: 3840 }));
  assert.deepEqual(fx, [['loop']]);
  ({ s, fx } = run(s, { type: 'position', tick: 6000, rangeStart: 3840 }, { type: 'jumped' }, { type: 'position', tick: 3840, rangeStart: 3840 }));
  assert.deepEqual(fx, []);
});

test('全曲弹完：停在最后弹到的小节，再播放从头开始', () => {
  let { s, fx } = run(initial(false), { type: 'play' }, { type: 'position', tick: 100 }, { type: 'finished', whole: true, end: { tick: 9600, bar: 5 } });
  assert.equal(s.phase, 'finishing');
  assert.equal(isPlaying(s), false);
  ({ s, fx } = run(s, { type: 'state', playing: false }));
  assert.deepEqual(fx, [['seek', 9600], ['showBar', 5]]);
  assert.equal(s.atEnd, 9600);
  ({ s, fx } = run(s, { type: 'play' }));
  assert.deepEqual(fx, [['resetLoops'], ['seek', 0], ['play']]);
  // 停在曲末时点了小节：从点的地方开始
  ({ s } = run(initial(false), { type: 'play' }, { type: 'finished', whole: true, end: { tick: 9600, bar: 5 } }, { type: 'state', playing: false }, { type: 'seek', tick: 1920 }));
  assert.equal(s.atEnd, null);
  assert.deepEqual(run(s, { type: 'play' }).fx.slice(-2), [['seek', 1920], ['play']]);
});

test('换音色：在放的话重新生成 MIDI 后接着放（以前停下就不再继续）', () => {
  let { s, fx } = run(initial(false), { type: 'seek', tick: 960 }, { type: 'play' }, { type: 'position', tick: 2000 });
  ({ s, fx } = run(s, { type: 'timbre' }));
  assert.deepEqual(fx, [['pause'], ['reloadMidi']]);
  assert.equal(isPlaying(s), true);
  ({ s, fx } = run(s, { type: 'midiLoaded', range: { start: 0, end: Infinity } }));
  assert.deepEqual(fx, [['seek', 2000], ['play']]);
  assert.equal(s.phase, 'playing');
  // 停着换音色：位置不动，不开始
  ({ s, fx } = run(initial(false), { type: 'seek', tick: 960 }, { type: 'timbre' }, { type: 'midiLoaded', range: { start: 0, end: Infinity } }));
  assert.equal(s.phase, 'idle');
  assert.deepEqual(fx.slice(-1), [['seek', 960]]);
});

test('谱子重新加载：期间的位置事件不算一遍；好了放回原位置（落在范围外就放到段首），原来在放就接着放', () => {
  let { s, fx } = run(initial(false), { type: 'seek', tick: 3840 }, { type: 'play' }, { type: 'position', tick: 4500, rangeStart: 3840 });
  ({ s, fx } = run(s, { type: 'reloadScore' }, { type: 'state', playing: false }, { type: 'position', tick: 0 }, { type: 'reloadScore' }));
  assert.equal(s.phase, 'reloadingScore');
  assert.deepEqual(fx, []);
  assert.equal(isPlaying(s), true);
  ({ s, fx } = run(s, { type: 'midiLoaded', range: { start: 3840, end: 7680 } }));
  assert.deepEqual(fx, [['seek', 4500], ['play']]);
  ({ s, fx } = run(initial(false), { type: 'seek', tick: 9000 }, { type: 'reloadScore' }, { type: 'midiLoaded', range: { start: 3840, end: 7680 } }));
  assert.deepEqual(fx.slice(-1), [['seek', 3840]]);
  assert.equal(s.phase, 'idle');
});

test('谱子加载好之前设过循环范围：MIDI 好了把光标放到段首；出错就回到停止', () => {
  const { fx } = run(initial(), { type: 'midiLoaded', range: { start: 1920, end: 3840 }, cursorPending: true });
  assert.deepEqual(fx, [['seek', 1920]]);
  const { s } = run(initial(), { type: 'seek', tick: 0 }, { type: 'play' }, { type: 'stop' }, { type: 'play' }, { type: 'error' });
  assert.equal(s.phase, 'idle');
  assert.equal(isPlaying(s), false);
  assert.throws(() => step(initial(), { type: 'nope' }), /未知的播放事件/);
});
