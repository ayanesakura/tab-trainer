// 练琴页 / 打分卡的纯函数：js/study-view.js。运行：node --test tests/js/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  fmtClock, fmtMin, fmtShort, dayLabel, dueText, gapText, studyBadge, itemNotes, queueRowHTML, doneRowHTML, planRowHTML,
  ringHTML, level, calendarModel, calendarHTML, sessionText, ratingTip, planNext, stuckText, timeTip, calTabDay, calMove, studyDay,
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
  // 不是今年的日子带上年份
  assert.equal(dayLabel('2025-12-20', { today: '2026-11-11' }), '2025 年 12 月 20 日 周六');
  assert.equal(dayLabel('2026-01-02', { today: '2026-11-11' }), '1 月 2 日 周五');
  assert.equal(dueText(-2), '逾期 2 天');
  assert.equal(dueText(0), '今天');
  assert.equal(dueText(1), '明天');
  assert.equal(dueText(5), '5 天后');
  assert.equal(gapText(1), '明天');
  assert.equal(gapText(9), '9 天');
  assert.equal(gapText(16), '2 周');
  assert.equal(gapText(60), '2 个月');
});

test('练琴日：凌晨 4 点换日，按本地时区', () => {
  assert.equal(studyDay(Date.UTC(2026, 10, 11, 3, 59), 0), '2026-11-10');
  assert.equal(studyDay(Date.UTC(2026, 10, 11, 4, 0), 0), '2026-11-11');
  // UTC+8（getTimezoneOffset 是 -480）：本地 11 月 11 日 04:00 = UTC 11 月 10 日 20:00
  assert.equal(studyDay(Date.UTC(2026, 10, 10, 19, 59), -480), '2026-11-10');
  assert.equal(studyDay(Date.UTC(2026, 10, 10, 20, 0), -480), '2026-11-11');
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
  assert.deepEqual(itemNotes(item({ kind: 'new', study: st({ state: 'new' }) })), ['第一次练']);
  // 今天已经在谱子上练过了：不再说“第一次练”
  assert.deepEqual(itemNotes(item({ kind: 'new', study: st({ state: 'new' }), today_seconds: 60 })), ['今天已练 1 分钟']);
  assert.deepEqual(itemNotes(item({ kind: 'new', study: st({ state: 'new' }), total_seconds: 360 })), []); // 以前练过（累计另外写）
  // 不能练（还在识别）：没有链接
  assert.doesNotMatch(queueRowHTML(item({ playable: false })), /<a /);
  // 曲名、艺人都转义
  assert.match(queueRowHTML(item({ title: '<b>x</b>', artist: 'a&b' })), /&lt;b&gt;x&lt;\/b&gt;[\s\S]*a&amp;b/);
});

test('卡住的小节：连着的并成一段，多了写“等”，提示里列全', () => {
  assert.equal(stuckText([]), null);
  assert.equal(stuckText(null), null);
  assert.deepEqual(stuckText([{ id: 'b12', n: 12 }]), { text: '卡在第 12 小节', tip: '上次打分时卡住的小节：第 12 小节' });
  assert.equal(stuckText([{ n: 14 }, { n: 12 }, { n: 13 }]).text, '卡在 12–14 小节');
  const x = stuckText([{ n: 3 }, { n: 12 }, { n: 13 }, { n: 14 }, { n: 20 }]);
  assert.equal(x.text, '卡在 3、12–14 等小节');
  assert.equal(x.tip, '上次打分时卡住的小节：第 3、12–14、20 小节');
  // 今日和计划里：曲名后面的徽章，链接照旧带 from=today（练习页从卡住的小节开始）
  const html = queueRowHTML(item({ study: st({ stuck_bars: [{ id: 'b', n: 12 }, { id: 'c', n: 13 }] }) }));
  assert.match(html, /class="title">华尔兹 C 大调<\/span><span class="badge stuck" title="[^"]*12–13[^"]*">卡在 12–13 小节/);
  assert.match(html, /&from=today/);
  assert.match(planRowHTML({ id: 'a', title: 'A', study: st({ stuck_bars: [{ id: 'b', n: 5 }] }) }, 0, 1), /卡在第 5 小节/);
});

test('一首曲子的累计时长：今日里和曲库同一个数、同一句说明', () => {
  const html = queueRowHTML(item({ total_seconds: 1500, undated_seconds: 600 }));
  assert.match(html, /<span class="tot" title="累计 25 分钟：[^"]*含早期未记日期的 10 分钟">累计 25 分钟<\/span>/);
  assert.equal(timeTip(1500, 600), rowHTML({ id: 's', title: 'x', source_type: 'gp', is_score: true, status: 'ready', bars: 2, practice: { last_at: '2026-11-01 10:00', total_seconds: 1500, undated_seconds: 600 } }).match(/class="practice" title="([^"]*)"/)[1]);
  assert.doesNotMatch(queueRowHTML(item()), /class="tot"/); // 没练过不写
});

test('计划里归档的曲子：留在原位、淡下去，写明不排进今日', () => {
  const html = planRowHTML({ id: 'a', title: 'A', archived: true, study: st({ stuck_bars: [{ id: 'b', n: 5 }] }) }, 0, 2);
  assert.match(html, /plan-row paused archived/);
  assert.match(html, /class="kind muted"[^>]*>归档</);
  assert.match(html, /已归档，不排进今日/);
  assert.doesNotMatch(html, /卡在/);
});

test('今天练过的一行：打分徽章和下次', () => {
  const html = doneRowHTML(item({ study: st({ rated_today: 3, due_in: 3, last: { rating: 3, issues: [], note: '', bpm: 0 } }), today_seconds: 900 }));
  assert.match(html, /badge ok rating[^>]*>顺</);
  assert.match(html, />Carcassi · 3 天后复习 · 练了 15 分钟</);
  assert.match(doneRowHTML(item({ study: st({ rated_today: 3, due_in: 1, last: { rating: 3 } }) })), /Carcassi · 明天复习</);
  assert.match(doneRowHTML(item({ study: st({ state: 'learning', rated_today: 2, last: { rating: 2 } }) })), /Carcassi · 明天继续</);
  assert.doesNotMatch(doneRowHTML(item({ study: st({ rated_today: 1, due_in: 0, last: { rating: 1 } }) })), /下次|复习/);
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
  assert.equal(planNext(st({ due_in: 0 }), { today: true }), '今天复习');
  assert.equal(planNext(st({ due_in: 4 })), '4 天后复习');
  // 新曲：轮到今天的写“今天开始”，没轮到的才是“排队中”；到期复习超出每天上限的顺延
  assert.equal(planNext(st({ state: 'new' }), { today: true }), '今天开始');
  assert.equal(planNext(st({ state: 'new' })), '排队中');
  assert.equal(planNext(st({ due_in: -1 }), { today: true }), '今天复习');
  assert.equal(planNext(st({ due_in: -1 })), '已到期，明天再排');
  assert.equal(planNext(st({ due_in: 0, rated_today: 3 })), '今天复习过了');
  assert.match(planRowHTML({ id: 'n', title: 'N', study: st({ state: 'new' }) }, 0, 1, { today: true }), /今天开始/);
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
  assert.match(html, /data-day="2025-11-10"[^>]*title="2025 年 11 月 10 日 周一 · 没练"/); // 去年的格子带年份
});

test('日历的键盘：只有一格能 Tab 到，方向键按网格走', () => {
  const m = calendarModel([], '2026-11-11', 45, 3); // 周三；第一格是 2026-10-26（周一）
  assert.equal(calTabDay(m), '2026-11-11'); // 默认今天
  assert.equal(calTabDay(m, '', '2026-11-02'), '2026-11-02'); // 选中的那天
  assert.equal(calTabDay(m, '2026-10-27', '2026-11-02'), '2026-10-27'); // 键盘走到的那天优先
  assert.equal(calTabDay(m, '2026-11-14', '2025-01-01'), '2026-11-11'); // 未来的、日历外面的不算
  const html = calendarHTML(m, '2026-11-02');
  assert.equal((html.match(/tabindex="0"/g) || []).length, 1);
  assert.match(html, /data-day="2026-11-02" tabindex="0"/);
  assert.match(html, /data-day="2026-11-11" tabindex="-1"/);
  assert.match(calendarHTML(m), /data-day="2026-11-11" tabindex="0"/);
  // 一列一周：←→ 隔一周，↑↓ 隔一天；走出日历停在原处；Home / End
  assert.equal(calMove(m, '2026-11-04', 'ArrowLeft'), '2026-10-28');
  assert.equal(calMove(m, '2026-11-04', 'ArrowRight'), '2026-11-11');
  assert.equal(calMove(m, '2026-11-04', 'ArrowDown'), '2026-11-05');
  assert.equal(calMove(m, '2026-11-02', 'ArrowUp'), '2026-11-01');
  assert.equal(calMove(m, '2026-11-05', 'ArrowRight'), '2026-11-05'); // 下周四还没到
  assert.equal(calMove(m, '2026-10-27', 'ArrowLeft'), '2026-10-27');
  assert.equal(calMove(m, '2026-11-11', 'ArrowDown'), '2026-11-11');
  assert.equal(calMove(m, '2026-11-04', 'Home'), '2026-10-26');
  assert.equal(calMove(m, '2026-11-04', 'End'), '2026-11-11');
  assert.equal(calMove(m, '2026-11-04', 'Enter'), null);
});

test('某一天的记录：计时 / 补记的文案', () => {
  assert.deepEqual(sessionText({ kind: 'manual', minutes: 30, note: '爬格子' }), { label: '补记', sub: '爬格子' });
  assert.deepEqual(sessionText({ kind: 'timer', segments: [['2026-11-09 20:10:00', '2026-11-09 20:30:00'], ['2026-11-09 20:40:00', '2026-11-09 20:55:12']], minutes: 35, note: '' }),
    { label: '计时 20:10–20:55', sub: '暂停 1 次' });
  assert.equal(ratingTip({ rating: 4, issues: ['rhythm', 'phrasing', 'bogus'], note: '很顺', bpm: 0 }), '轻松 · 节奏、呼吸 · 很顺');
});
