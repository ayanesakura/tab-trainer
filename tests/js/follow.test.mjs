// 自动跟随的规则（js/trainer/follow-plan.js）：node --test tests/js/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { followShift, glideMs, ease } from '../../app/static/js/trainer/follow-plan.js';

// 每行 200 高、行距 20；scroll：页面已经往上滚了多少。可见范围 110..720，一行顶到 120
const VIS = { top: 110, bottom: 720, target: 120 };
const rows = (n, scroll = 0, first = 230) => i => (i >= 0 && i < n ? { y0: first + i * 220 - scroll, y1: first + i * 220 - scroll + 200 } : null);

test('followShift：每换一行只走一行的距离，新的一行落在上一行刚才的位置', () => {
  let scroll = 0;
  const moves = [];
  for (let r = 0; r < 6; r++) {
    const d = followShift(rows(20, scroll), r, 19, null, VIS);
    moves.push(d);
    scroll += d;
    assert.equal(rows(20, scroll)(r).y0, VIS.target); // 这一行顶在控制条下面
  }
  assert.deepEqual(moves, [110, 220, 220, 220, 220, 220]); // 开头从页面顶上收 110，之后每行正好一行
});

test('followShift：以前的毛病——三行放得下时弹到第三行，一下跨两行把它甩到顶上；现在弹到第二行就先走一行', () => {
  const at = rows(20, 110); // 第 1 行顶着，第 1–3 行看得见（y1 = 760 > 720 的第 3 行其实露不全）
  assert.equal(followShift(at, 1, 19, null, VIS), 220);
});

test('followShift：从这一行到最后一行都看得见（短曲子、快弹完了）就不滚；差一点点也不滚', () => {
  assert.equal(followShift(rows(2), 0, 1, null, VIS), 0);
  assert.equal(followShift(rows(20, 110 + 18 * 220), 18, 19, null, VIS), 0); // 最后两行都露着
  assert.equal(followShift(rows(20, 100), 0, 19, null, VIS), 0); // 离要去的位置 10px
});

test('followShift：循环的一段整段放得下——露全了就不动（循环里不来回滚），没露全把段首顶上去；放不下按一行一行走', () => {
  const loop = { first: 3, last: 4 };
  const shown = rows(20, 230 + 3 * 220 - 120);
  assert.equal(followShift(shown, 3, 4, loop, VIS), 0);
  assert.equal(followShift(shown, 4, 4, loop, VIS), 0); // 弹到段尾那一行：不动，回到段首也还露着
  assert.equal(followShift(rows(20, 230 + 4 * 220 - 120), 4, 4, loop, VIS), -220); // 段首滚出去了：往回一行
  const tall = { first: 2, last: 9 }; // 8 行放不下
  assert.equal(followShift(rows(20, 0), 2, 9, tall, VIS), 230 + 2 * 220 - 120);
  assert.equal(followShift(rows(20), 25, 19, null, VIS), null); // 量不出来
});

test('glideMs / ease：一行大约 0.65 秒，最多 1 秒；慢起慢停', () => {
  assert.equal(glideMs(220), 662);
  assert.equal(glideMs(-220), 662);
  assert.equal(glideMs(110), 541);
  assert.equal(glideMs(3000), 1000);
  assert.equal(ease(0), 0);
  assert.equal(ease(1), 1);
  assert.equal(ease(0.5), 0.5);
  assert.ok(ease(0.1) < 0.01); // 前十分之一的时间只走了不到 1%：起步很轻
  assert.ok(ease(0.9) > 0.99);
  for (let k = 0; k < 1; k += 0.05) assert.ok(ease(k + 0.05) >= ease(k));
});
