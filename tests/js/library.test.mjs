// 曲库页的纯函数：js/library-view.js。运行：node --test tests/js/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ago, duration, rangeText, practiceText, stateHTML, barsText, costText, fixAction, rowHTML, sortSongs, filterSongs, canPlay } from '../../app/static/js/library-view.js';

const NOW = new Date(2026, 10, 7, 20, 0); // 2026-11-07 20:00（本地时间）
const song = (o = {}) => ({ id: 's1', title: '曲子', source_type: 'image', is_score: false, status: 'ready', bars: 8, created: '2026-11-01 10:00', ...o });

test('ago / duration：相对时间和时长', () => {
  assert.equal(ago('2026-11-07 19:59:30', NOW), '刚刚');
  assert.equal(ago('2026-11-07 19:15', NOW), '45 分钟前');
  assert.equal(ago('2026-11-07 08:00', NOW), '今天');
  assert.equal(ago('2026-11-06 23:00', NOW), '昨天');
  assert.equal(ago('2026-11-01 12:00', NOW), '6 天前');
  assert.equal(ago('2026-08-01 12:00', NOW), '8 月 1 日');
  assert.equal(ago('2025-08-01 12:00', NOW), '2025 年 8 月');
  assert.equal(ago('不是时间', NOW), '不是时间');
  assert.equal(duration(20), '不到 1 分钟');
  assert.equal(duration(600), '10 分钟');
  assert.equal(duration(3900), '1 小时 5 分钟');
  assert.equal(duration(7200), '2 小时');
});

test('rangeText / practiceText：上次练到哪里', () => {
  const last = (a_n, b_n) => ({ practice: { last: { a_n, b_n }, last_at: '2026-11-06 23:00', total_seconds: 600 } });
  assert.equal(rangeText(song(last(1, 8))), '全曲');
  assert.equal(rangeText(song(last(3, 3))), '第 3 小节');
  assert.equal(rangeText(song(last(3, 5))), '第 3–5 小节');
  assert.equal(rangeText(song(last(null, 5))), ''); // 小节被删了
  assert.equal(practiceText(song(last(3, 5)), NOW), '昨天 · 练到第 3–5 小节 · 累计 10 分钟');
  assert.equal(practiceText(song(), NOW), '');
});

test('stateHTML / barsText：状态说明，后端文字要转义', () => {
  assert.equal(stateHTML(song()), '');
  assert.match(stateHTML(song({ status: 'error', notice: { kind: 'error', msg: '<b>坏</b>' } })), /识别失败：&lt;b&gt;坏&lt;\/b&gt;/);
  assert.match(stateHTML(song({ status: 'error', is_score: true })), /曲谱读取失败/);
  assert.match(stateHTML(song({ busy: true, recognizing: true, progress: { stage: '识别', done: 2, total: 4 } })), /2\/4 行.*aria-valuenow="50"/);
  assert.match(stateHTML(song({ needs_review_bars: 3 })), /3 小节待核对/);
  assert.equal(barsText(song()), '8 小节');
  assert.equal(barsText(song({ bars: 0, is_score: true })), '已导入');
  assert.equal(barsText(song({ status: 'error' })), '');
});

test('costText：精确 / 估算 / 只有 token', () => {
  assert.equal(costText(song()), '');
  assert.match(costText(song({ tokens: 1000, cost_usd: 1.234 })), />AI 花费 \$1\.23</);
  assert.match(costText(song({ tokens: 1000, cost_usd: 18, cost_estimated: true })), /AI 花费约 \$18\.00/);
  assert.match(costText(song({ tokens: 1000, cost_usd: 0.001 })), /&lt;\$0\.01/);
  assert.match(costText(song({ tokens: 1234 })), /AI 用量 1,234 token/);
});

test('fixAction / rowHTML：失败的曲子给下一步，重试失败的行优先', () => {
  const keyErr = song({ status: 'error', bars: 0, notice: { kind: 'error', msg: '还没有设置 API key' } });
  assert.match(fixAction(keyErr), /去设置/);
  // app/llm.py friendly() 的文字：改设置才会好的 → 去设置；暂时的（429 额度、连不上、连接中断提到 Base URL）→ 重试
  const err = (msg, o) => song({ status: 'error', bars: 0, failed_rows: 2, notice: { kind: 'error', msg }, ...o });
  for (const msg of ['API key 无效，请在曲库页顶部的“设置”里检查。', '模型不存在或无权访问，请检查“设置”里的模型名。',
    '模型服务拒绝了这次请求（403）：forbidden', '第 1 行：还没有设置 API key：请在曲库页顶部的“设置”里填写'])
    assert.match(fixAction(err(msg)), /去设置/, msg);
  for (const msg of ['第 1 行：调用太频繁或额度不足（429），稍后再试。；第 2 行：调用太频繁或额度不足（429），稍后再试。',
    '第 1 行：和模型服务的连接中断了，请稍后重试（网络不稳定时可以检查网络或 Base URL）。', '连不上模型服务，请检查网络或 Base URL。',
    '模型服务返回错误 529：Overloaded']) {
    assert.match(fixAction(err(msg)), /重试失败的 2 行/, msg);
    assert.match(rowHTML(err(msg, { bars: 4 })), /重试失败的 2 行/, msg); // 有小节可练时也给重试
  }
  assert.match(fixAction(song({ status: 'error', bars: 0 })), /data-retry="s1".*重新识别/);
  assert.match(fixAction(song({ status: 'error', bars: 0, failed_rows: 2 })), /重试失败的 2 行/);
  assert.equal(fixAction(song({ status: 'error', bars: 0, is_score: true })), '');
  // 有小节可练又有失败的行：重试在“练习”前面；只有“接着练”是主色按钮
  const row = rowHTML(song({ status: 'error', failed_rows: 1 }));
  assert.ok(row.indexOf('重试失败的 1 行') < row.indexOf('练习') && !/btn primary/.test(row));
  assert.match(rowHTML(song({ practice: { last: { a_n: 1, b_n: 2, bpm: 60 }, last_at: '2026-11-06 23:00' } })), /btn primary sm act" data-k="resume"/);
  assert.match(rowHTML(song({ title: '<i>x</i>' })), /title="&lt;i&gt;x&lt;\/i&gt;"/);
  // 选择模式：复选框、选中状态，操作按钮藏起来；坏的曲子不能选
  const sel = rowHTML(song(), { selecting: true, selected: new Set(['s1']) });
  assert.match(sel, /class="pick"[^>]*checked/);
  assert.match(sel, /class="ctl" hidden/);
  assert.match(rowHTML(song({ status: 'corrupt' }), { selecting: true }), /class="pick"[^>]*disabled/);
  assert.equal(canPlay(song({ bars: 0 })), false);
  assert.equal(canPlay(song({ bars: 0, is_score: true })), true);
});

test('sortSongs / filterSongs：排序、搜索、标签', () => {
  const list = [
    song({ id: 'a', title: '第10首', created: '2026-11-03', practice: null }),
    song({ id: 'b', title: '第2首', created: '2026-11-01', practice: { last_at: '2026-11-05 10:00' }, tags: ['指弹'] }),
    song({ id: 'c', title: 'Amy', artist: 'Tommy', created: '2026-11-02', practice: { last_at: '2026-11-06 10:00' } }),
  ];
  const ids = l => l.map(s => s.id);
  assert.deepEqual(ids(sortSongs([...list], 'practiced')), ['c', 'b', 'a']); // 没练过的排在后面
  assert.deepEqual(ids(sortSongs([...list], 'created')), ['a', 'c', 'b']);
  const t = ids(sortSongs([...list], 'title'));
  assert.ok(t.indexOf('b') < t.indexOf('a')); // 曲名里的数字按大小：第2首在第10首前面
  assert.deepEqual(ids(filterSongs(list, { q: 'tommy', sort: 'created' })), ['c']);
  assert.deepEqual(ids(filterSongs(list, { tag: '指弹' })), ['b']);
  assert.deepEqual(ids(list), ['a', 'b', 'c']); // filterSongs 不改原数组
});
