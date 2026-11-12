// 曲库页的纯函数：js/library-view.js。运行：node --test tests/js/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ago, duration, rangeText, practiceText, practiceTip, latestPracticed, importReport, stateHTML, barsText, costText, fixAction, rowHTML, sortSongs, filterSongs, canPlay, paginate, pageNumbers, pagerHTML, PAGE_SIZE } from '../../app/static/js/library-view.js';
import { saveReturn, markBack, takeReturn, cleanReturn } from '../../app/static/js/lib/lib-return.js';

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

test('practiceTip：累计时长的说明，提到早期没记日期的', () => {
  assert.equal(practiceTip(song()), '');
  assert.match(practiceTip(song({ practice: { total_seconds: 1500, undated_seconds: 0 } })), /^累计 25 分钟：.*重叠的只算一次$/);
  assert.match(practiceTip(song({ practice: { total_seconds: 1500, undated_seconds: 600 } })), /含早期未记日期的 10 分钟$/);
  // 曲库行里：说明挂在“累计”那一段上
  const row = rowHTML(song({ practice: { last_at: '2026-11-06 23:00', total_seconds: 1500, undated_seconds: 600, last: null } }));
  assert.match(row, /class="practice" title="累计 25 分钟：[^"]*含早期未记日期的 10 分钟">[^<]*累计 25 分钟</);
});

test('latestPracticed：只有最近练过的一首用主色“接着练”', () => {
  const p = (at, o = {}) => song({ practice: { last_at: at, last: { a_n: 1, b_n: 2 } }, ...o });
  const list = [p('2026-11-05 10:00', { id: 'a' }), p('2026-11-06 09:00', { id: 'b' }), p('2026-11-07 09:00', { id: 'c', archived: true }),
    p('2026-11-07 10:00', { id: 'd', status: 'corrupt' }), song({ id: 'e' })];
  assert.equal(latestPracticed(list), 'b'); // 归档的、坏的不算
  assert.equal(latestPracticed([song()]), null);
  assert.match(rowHTML(list[1], { primary: true }), /btn primary sm act" data-k="resume"/);
  assert.match(rowHTML(list[0]), /btn ghost sm act" data-k="resume"/);
  assert.match(rowHTML(song()), /btn ghost sm act" data-k="play"/);
  assert.match(rowHTML(list[2]), /song-row click archived/);
});

test('importReport：按包列出导入 / 已有 / 失败', () => {
  const ok = importReport({ imported: [{ id: 'x', title: 'A', bundle: '1.zip' }], skipped: [], bundles: [{ file: '1.zip', ok: true, imported: 1, skipped: 0, error: null }] });
  assert.deepEqual([ok.summary, ok.kind, ok.simple], ['已导入 1 首', 'ok', true]);
  const r = importReport({
    imported: [{ id: 'x', title: 'A', bundle: '1.zip' }],
    skipped: [{ title: 'B', reason: '曲库里已经有这首', duplicate: true, bundle: '1.zip' }, { title: '<C>', reason: '时值不对', duplicate: false, bundle: '1.zip' },
      { title: '坏.zip', reason: '不是 zip 文件', duplicate: false, bundle: '坏.zip' }],
    bundles: [{ file: '1.zip', ok: true, imported: 1, skipped: 2, error: null }, { file: '坏.zip', ok: false, imported: 0, skipped: 1, error: '不是 zip 文件' }],
  });
  // 读不了的包不算“一首失败”，单独写“几个包无法导入”
  assert.deepEqual([r.summary, r.kind, r.simple], ['已导入 1 首 · 1 首已有 · 1 首失败 · 1 个包无法导入', 'info', false]);
  const [one, bad] = r.html.split('</section>');
  assert.match(one, /1\.zip[\s\S]*导入 1 · 跳过 2/);
  assert.match(one, />A<\/span><span class="badge ok">导入/);
  assert.match(one, />B<\/span><span class="badge ">已有/);
  assert.match(one, /&lt;C&gt;：时值不对/); // 后端文字转义
  assert.match(bad, /dot err[\s\S]*坏\.zip[\s\S]*无法导入[\s\S]*不是 zip 文件/);
  assert.doesNotMatch(bad, /<li>/); // 整个包读不了：只写原因，不把包名当成一首曲子
  assert.equal(importReport({ imported: [], skipped: [{ title: 'B', reason: 'x', duplicate: false }] }).kind, 'err');
  const onlyBroken = importReport({ imported: [], skipped: [{ title: '坏.zip', reason: 'x', duplicate: false, bundle: '坏.zip' }],
    bundles: [{ file: '坏.zip', ok: false, imported: 0, skipped: 1, error: 'x' }] });
  assert.deepEqual([onlyBroken.summary, onlyBroken.kind], ['已导入 0 首 · 1 个包无法导入', 'err']);
});

test('归档的曲子：曲库行里不显示计划徽章', () => {
  const study = { state: 'new', paused: false, due_in: null };
  assert.match(rowHTML(song({ study })), /plan-badge/);
  assert.doesNotMatch(rowHTML(song({ study, archived: true })), /plan-badge/);
});

test('stateHTML / barsText：状态说明，后端文字要转义', () => {
  assert.equal(stateHTML(song()), '');
  assert.match(stateHTML(song({ status: 'error', notice: { kind: 'error', msg: '<b>坏</b>' } })), /识别失败：&lt;b&gt;坏&lt;\/b&gt;/);
  assert.match(stateHTML(song({ status: 'error', is_score: true })), /曲谱读取失败/);
  assert.match(stateHTML(song({ busy: true, recognizing: true, progress: { stage: '识别', done: 2, total: 4 } })), /2\/4 行.*aria-valuenow="50"/);
  assert.match(stateHTML(song({ needs_review_bars: 3 })), /3 小节待核对/);
  // 精修：进度按小节数；做完是“已精修”，刚做完的说明（info）用绿色，不重复显示“已精修”
  assert.match(stateHTML(song({ busy: true, activity: { kind: 'refine' }, progress: { stage: '精修', done: 3, total: 12 } })), /3\/12 小节/);
  const refined = { at: '2026-11-08 10:00', checked: 12, changed: 2 };
  assert.match(stateHTML(song({ refined })), /badge ok[^>]*改了 2 个">已精修/);
  const done = stateHTML(song({ refined, notice: { kind: 'info', msg: '精修完成：检查了 12 个小节，改了 2 个。' } }));
  assert.match(done, /badge ok notice-badge/);
  assert.doesNotMatch(done, />已精修</);
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
  // 没计价的 token（模型不在价格表里，比如服务端换成的兜底模型）：提示里说清楚是哪个模型，金额后面带 +
  const un = costText(song({ tokens: 2000, cost_usd: 1, unpriced_tokens: 1200, unpriced_models: ['claude-x'] }));
  assert.match(un, /另有 1,200 token 未计价（模型 claude-x 不在价格表里）/);
  assert.match(un, /\$1\.00 \+/);
  // 早期的曲子只记了数，没记模型名
  assert.match(costText(song({ tokens: 2000, cost_usd: 1, unpriced_tokens: 5 })), /另有 5 token 未计价（模型不在价格表里）/);
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
  assert.match(rowHTML(song({ practice: { last: { a_n: 1, b_n: 2, bpm: 60 }, last_at: '2026-11-06 23:00' } }), { primary: true }), /btn primary sm act" data-k="resume"/);
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
  // 归档的平时不显示，archived: true 时只看归档的
  const arch = [...list, song({ id: 'z', title: 'Amy 2', archived: true })];
  assert.deepEqual(ids(filterSongs(arch, { sort: 'created' })), ['a', 'c', 'b']);
  assert.deepEqual(ids(filterSongs(arch, { archived: true })), ['z']);
  assert.deepEqual(ids(list), ['a', 'b', 'c']); // filterSongs 不改原数组
});

test('paginate：分页、页码越界时取最近的一页', () => {
  const list = Array.from({ length: 45 }, (_, i) => i + 1);
  assert.equal(PAGE_SIZE, 20);
  let p = paginate(list, 1);
  assert.deepEqual([p.page, p.pages, p.from, p.to, p.items.length, p.items[0]], [1, 3, 1, 20, 20, 1]);
  p = paginate(list, 3);
  assert.deepEqual([p.page, p.from, p.to, p.items], [3, 41, 45, [41, 42, 43, 44, 45]]);
  assert.equal(paginate(list, 9).page, 3); // 删到只剩 3 页：停在最后一页
  assert.equal(paginate(list, 0).page, 1);
  assert.equal(paginate(list, 'x').page, 1);
  assert.equal(paginate(list, 2.7).page, 2);
  p = paginate([], 4);
  assert.deepEqual([p.page, p.pages, p.from, p.to, p.items], [1, 1, 0, 0, []]);
  assert.equal(paginate(list.slice(0, 20), 2).page, 1); // 正好一页
});

test('pageNumbers：页多时省略中间，总是 7 格', () => {
  assert.deepEqual(pageNumbers(1, 1), [1]);
  assert.deepEqual(pageNumbers(3, 7), [1, 2, 3, 4, 5, 6, 7]);
  assert.deepEqual(pageNumbers(1, 10), [1, 2, 3, 4, 5, null, 10]);
  assert.deepEqual(pageNumbers(4, 10), [1, 2, 3, 4, 5, null, 10]);
  assert.deepEqual(pageNumbers(5, 10), [1, null, 4, 5, 6, null, 10]);
  assert.deepEqual(pageNumbers(7, 10), [1, null, 6, 7, 8, 9, 10]);
  assert.deepEqual(pageNumbers(10, 10), [1, null, 6, 7, 8, 9, 10]);
  assert.deepEqual(pageNumbers(4, 8), [1, 2, 3, 4, 5, null, 8]);
  for (let n = 8; n <= 30; n++) for (let p = 1; p <= n; p++) assert.equal(pageNumbers(p, n).length, 7); // 宽度不随页码变，翻页时按钮不跳
});

test('pagerHTML：只有一页时不显示；当前页、首尾页的按钮状态', () => {
  assert.equal(pagerHTML(paginate([1, 2, 3], 1), 3), '');
  const list = Array.from({ length: 45 }, (_, i) => i);
  let h = pagerHTML(paginate(list, 1), 45);
  assert.match(h, /1–20 \/ 45 首/);
  assert.match(h, /data-page="prev"[^>]*disabled/);
  assert.doesNotMatch(h, /data-page="next"[^>]*disabled/);
  assert.match(h, /data-page="1"[^>]*aria-current="page"/);
  h = pagerHTML(paginate(list, 3), 45);
  assert.match(h, /41–45 \/ 45 首/);
  assert.match(h, /data-page="next"[^>]*disabled/);
  assert.equal((h.match(/aria-current/g) || []).length, 1);
});

// 假的 sessionStorage；blocked：网站存储被禁用，读写都抛错
function fakeStorage(blocked = false) {
  const m = new Map(), no = () => { throw new Error('SecurityError'); };
  return blocked ? { getItem: no, setItem: no, removeItem: no } : { getItem: k => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: k => m.delete(k), m };
}

test('lib-return：从曲库点开、练习页“返回”回来时恢复离开前的状态，只用一次', () => {
  const s = fakeStorage();
  saveReturn('a b', { page: 3, tag: '指弹', archived: true, q: ' Wonder ', y: 812.5 }, s);
  assert.equal(takeReturn(s), null); // 没打“回去”的标记（输网址、点品牌链接回来）：不恢复，记录删掉
  assert.equal(s.m.size, 0);
  saveReturn('a b', { page: 3, tag: '指弹', archived: true, q: ' Wonder ', y: 812.5 }, s);
  assert.equal(markBack('other', '/', s), false); // 不是这一首
  assert.equal(markBack('a b', '/#study', s), false); // 从今日进来的回练琴页
  assert.equal(markBack('a b', 'http://127.0.0.1:8765/?x=1', s), false);
  assert.equal(markBack('a b', 'http://127.0.0.1:8765/', s), true);
  assert.deepEqual(takeReturn(s), { page: 3, tag: '指弹', archived: true, q: ' Wonder ', y: 812.5 });
  assert.equal(takeReturn(s), null); // 只用一次
  assert.equal(markBack('a b', '/', s), false); // 记录已经用掉了
  saveReturn('c', {}, s);
  assert.equal(markBack('c', '/#library', s), true);
  assert.deepEqual(takeReturn(s), { page: 1, tag: '', archived: false, q: '', y: 0 });
});

test('lib-return：记录坏了、类型不对、存储被禁用时静默退化', () => {
  assert.deepEqual(cleanReturn({ page: '0', tag: 5, archived: 'true', q: null, y: -3 }), { page: 1, tag: '', archived: false, q: '', y: 0 });
  assert.deepEqual(cleanReturn(null), { page: 1, tag: '', archived: false, q: '', y: 0 });
  assert.equal(cleanReturn({ page: 2.7 }).page, 2);
  const s = fakeStorage();
  s.setItem('tt.libReturn', '{oops');
  assert.equal(markBack('a', '/', s), false);
  assert.equal(takeReturn(s), null);
  const blocked = fakeStorage(true);
  assert.doesNotThrow(() => saveReturn('a', { page: 2 }, blocked));
  assert.equal(markBack('a', '/', blocked), false);
  assert.equal(takeReturn(blocked), null);
  // Node 里没有 sessionStorage：用默认参数也不抛错
  assert.doesNotThrow(() => saveReturn('a', {}));
  assert.equal(markBack('a', '/'), false);
  assert.equal(takeReturn(), null);
});
