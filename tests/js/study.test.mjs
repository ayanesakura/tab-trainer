// 练琴页 / 打分卡的纯函数：js/study-view.js。运行：node --test tests/js/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  fmtClock, fmtMin, fmtShort, dayLabel, dueText, gapText, studyBadge, itemNotes, queueRowHTML, doneRowHTML, planRowHTML,
  ringHTML, level, calendarModel, calendarHTML, sessionText, ratingTip, planNext,
} from '../../app/static/js/study-view.js';
import { rowHTML } from '../../app/static/js/library-view.js';

const st = (o = {}) => ({ state: 'review', paused: false, order: 0, due: '2026-11-12', due_in: 3, last: null, rated_today: null, reps: 2, lapses: 0, preview: {}, ...o });
const item = (o = {}) => ({ id: 'a1', title: '华尔兹 C 大调', artist: 'Carcassi', kind: 'review', study: st(), today_seconds: 0, playable: true, ...o });

test('时长、日期文案', () => {
  assert.equal(fmtClock(0), '00:00');
  assert.equal(fmtClock(754), '12:34');
  assert.equal(fmtClock(3723), '1:02:03');
  assert.equal(fmtMin(0), '0 分钟');
  assert.equal(fmtMin(0.3), '不到 1 分钟');
  assert.equal(fmtMin(45), '45 分钟');
  assert.equal(fmtMin(65), '1 小时 5 分钟');
  assert.equal(fmtMin(120), '2 小时');
  assert.deepEqual(fmtShort(42), { n: '42', unit: '分钟' });
  assert.deepEqual(fmtShort(192), { n: '3.2', unit: '小时' });
  assert.equal(dayLabel('2026-11-09'), '11 月 9 日 周一');
  assert.equal(dayLabel('2026-11-15'), '11 月 15 日 周日');
  assert.equal(dayLabel('2026-11-10', { weekday: false }), '11 月 10 日');
  assert.equal(dayLabel('坏的'), '坏的');
  assert.equal(dueText(-2), '逾期 2 天');
  assert.equal(dueText(0), '今天');
  assert.equal(dueText(1), '明天');
  assert.equal(dueText(5), '5 天后');
  assert.equal(gapText(1), '明天');
  assert.equal(gapText(9), '9 天');
  assert.equal(gapText(16), '2 周');
  assert.equal(gapText(60), '2 个月');
});

test('曲库一行的计划徽章', () => {
  assert.equal(studyBadge(null), '');
  assert.match(studyBadge(st({ state: 'learning' })), />在学</);
  assert.match(studyBadge(st({ state: 'new', due: null, due_in: null })), />待学</);
  assert.match(studyBadge(st({ paused: true })), />暂停</);
  assert.match(studyBadge(st({ due_in: 0 })), /plan-badge due[^>]*><i[^>]*><\/i>今天复习/);
  assert.match(studyBadge(st({ due_in: -1 })), />今天复习/);
  assert.match(studyBadge(st({ due_in: 3 })), />3 天后复习</);
  // 曲库行里跟在曲名后面
  const row = rowHTML({ id: 's1', title: '曲子', source_type: 'gp', is_score: true, status: 'ready', bars: 8, study: st({ state: 'learning' }) });
  assert.match(row, /class="title"[^>]*>曲子<\/a><span class="badge plan-badge learn"/);
});

test('今日清单的一行：说明、链接、转义', () => {
  const it = item({ study: st({ due_in: -2, last: { day: '2026-11-06', rating: 2, issues: ['noise'], note: '', bpm: 72 } }), today_seconds: 600 });
  assert.deepEqual(itemNotes(it), ['逾期 2 天', '上次磕绊', '今天已练 10 分钟']);
  const html = queueRowHTML(it, { primary: true });
  assert.match(html, /href="\/trainer\?id=a1&resume=1&from=today"/);
  assert.match(html, /btn sm act primary/);
  assert.match(html, /title="磕绊 · 杂音 · 72 BPM"/);
  assert.deepEqual(itemNotes(item({ kind: 'new', study: st({ state: 'new' }) })), ['还没开始']);
  // 不能练（还在识别）：没有链接
  assert.doesNotMatch(queueRowHTML(item({ playable: false })), /<a /);
  // 曲名、艺人都转义
  assert.match(queueRowHTML(item({ title: '<b>x</b>', artist: 'a&b' })), /&lt;b&gt;x&lt;\/b&gt;[\s\S]*a&amp;b/);
});

test('今天练过的一行：打分徽章和下次', () => {
  const html = doneRowHTML(item({ study: st({ rated_today: 3, due_in: 3, last: { rating: 3, issues: [], note: '', bpm: 0 } }), today_seconds: 900 }));
  assert.match(html, /badge ok rating[^>]*>顺</);
  assert.match(html, /下次3 天后 · 练了 15 分钟/);
  assert.match(doneRowHTML(item({ study: st({ state: 'learning', rated_today: 2, last: { rating: 2 } }) })), /明天继续/);
});

test('计划的一行：位置、上下移动按钮到头不可用', () => {
  const first = planRowHTML({ id: 'a', title: 'A', artist: '', study: st({ state: 'new', due_in: null }) }, 0, 3);
  assert.match(first, /<span class="ord">1<\/span>/);
  assert.match(first, /data-mv="-1"[^>]*disabled/);
  assert.doesNotMatch(first, /data-mv="1"[^>]*disabled/);
  const last = planRowHTML({ id: 'c', title: 'C', study: st({ paused: true }) }, 2, 3);
  assert.match(last, /plan-row paused/);
  assert.match(last, /data-mv="1"[^>]*disabled/);
  assert.equal(planNext(st({ state: 'learning', rated_today: 2 })), '明天继续');
  assert.equal(planNext(st({ due_in: 0 })), '今天');
  assert.equal(planNext(st({ due_in: 4 })), '4 天后复习');
});

test('进度环：达到目标变色，超出不画满两圈', () => {
  assert.doesNotMatch(ringHTML(20, 45), /reached/);
  assert.match(ringHTML(50, 45), /ring reached/);
  assert.match(ringHTML(90, 45), /stroke-dashoffset="0.00"/);
  assert.match(ringHTML(0, 45), /<b>0<\/b><span>\/ 45 分钟<\/span>/);
});

test('日历：颜色档位、网格对齐到周一、未来的格子', () => {
  assert.deepEqual([0, 5, 20, 40, 45, 200].map(m => level(m, 45)), [0, 1, 2, 3, 4, 4]);
  // 2026-11-15 是周日：最后一列是 11-09（周一）… 11-15，没有未来的格子
  const m = calendarModel([{ day: '2026-11-15', minutes: 50, ratings: 2 }, { day: '2026-11-09', minutes: 10, ratings: 0 }, { day: '2025-01-01', minutes: 99 }], '2026-11-15', 45, 53);
  assert.equal(m.cols.length, 53);
  const lastCol = m.cols[52];
  assert.equal(lastCol[0].day, '2026-11-09');
  assert.equal(lastCol[6].day, '2026-11-15');
  assert.equal(lastCol[6].today, true);
  assert.equal(lastCol[6].level, 4);
  assert.equal(lastCol[0].level, 1);
  assert.equal(m.active, 2);
  assert.equal(m.total, 60); // 一年以前的那天不算
  assert.equal(m.cols[0][0].day, '2025-11-10'); // 往前 52 周
  // 今天是周三：周四以后是未来
  const w = calendarModel([], '2026-11-11', 45, 2);
  assert.deepEqual(w.cols[1].map(c => c.future), [false, false, false, true, true, true, true]);
  // 月份标签在每月 1 日所在的列
  assert.ok(m.months.some(x => x.label === '11 月' && m.cols[x.col].some(c => c.day === '2026-11-01')));
  const html = calendarHTML(m, '2026-11-09');
  assert.match(html, /--weeks:53/);
  assert.match(html, /class="cell l1 sel" data-day="2026-11-09"[^>]*aria-pressed="true"/);
  assert.match(html, /title="11 月 15 日 周日 · 50 分钟 · 打分 2 首"/);
});

test('某一天的记录：计时 / 补记的文案', () => {
  assert.deepEqual(sessionText({ kind: 'manual', minutes: 30, note: '爬格子' }), { label: '补记', sub: '爬格子' });
  assert.deepEqual(sessionText({ kind: 'timer', segments: [['2026-11-09 20:10:00', '2026-11-09 20:30:00'], ['2026-11-09 20:40:00', '2026-11-09 20:55:12']], minutes: 35, note: '' }),
    { label: '计时 20:10–20:55', sub: '暂停 1 次' });
  assert.equal(ratingTip({ rating: 4, issues: ['rhythm', 'phrasing', 'bogus'], note: '很顺', bpm: 0 }), '轻松 · 节奏、呼吸 · 很顺');
});
