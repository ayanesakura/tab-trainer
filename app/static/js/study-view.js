// 练琴页（index.html 的“练琴”视图）和练习页打分卡共用的纯函数：时长 / 日期文案、今日清单和计划里的一行、
// 进度环、练琴日历的网格。不碰 DOM，有 node 测试（tests/js/study.test.mjs）。
// 后端来的字段（包括数字）拼进 HTML 时一律 esc：曲子包里的 song.json、study.jsonl 可能被改过。
// 数据的形状见 README 接口约定（GET /api/study/today、/api/study/calendar、/api/study/day/{day}）。

import { esc, icon } from './lib/html.js';
import { STUDY_RATINGS, STUDY_ISSUES } from './vocab.js';

// ---------------- 时长、日期 ----------------
/** 计时器：12:34 / 1:02:03（秒） */
export function fmtClock(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  const pad = n => String(n).padStart(2, '0');
  return h ? `${h}:${pad(m)}:${pad(r)}` : `${pad(m)}:${pad(r)}`;
}
/** 分钟数 → “45 分钟” / “1 小时 5 分钟” / “2 小时”；不到 1 分钟但不是 0 时“不到 1 分钟” */
export function fmtMin(min) {
  const m = Math.round(Number(min) || 0);
  if (m < 1) return min > 0 ? '不到 1 分钟' : '0 分钟';
  if (m < 60) return `${m} 分钟`;
  return `${Math.floor(m / 60)} 小时${m % 60 ? ` ${m % 60} 分钟` : ''}`;
}
/** 统计用的短写：不到 1 小时写分钟，否则保留一位小数的小时（“3.2 小时”） */
export function fmtShort(min) {
  const m = Number(min) || 0;
  if (m < 60) return { n: String(Math.round(m)), unit: '分钟' };
  const h = Math.round(m / 6) / 10;
  return { n: String(h), unit: '小时' };
}

const WEEKDAYS = ['日', '一', '二', '三', '四', '五', '六'];
/** “YYYY-MM-DD” → UTC 零点的 Date（只当日历日期用，不受夏令时影响）；认不出返回 null */
export function parseDay(day) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(day || ''));
  if (!m) return null;
  const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
  return Number.isNaN(+d) ? null : d;
}
export const dayKey = d => d.toISOString().slice(0, 10);
export const addDays = (d, n) => new Date(+d + n * 86400000);
/** “11 月 9 日 周日” */
export function dayLabel(day, { weekday = true } = {}) {
  const d = parseDay(day);
  if (!d) return String(day || '');
  return `${d.getUTCMonth() + 1} 月 ${d.getUTCDate()} 日${weekday ? ` 周${WEEKDAYS[d.getUTCDay()]}` : ''}`;
}

/** 距离下次练的天数 → 文案：逾期 2 天 / 今天 / 明天 / 3 天后 */
export function dueText(n) {
  if (n == null) return '';
  if (n < 0) return `逾期 ${-n} 天`;
  if (n === 0) return '今天';
  if (n === 1) return '明天';
  return `${n} 天后`;
}
/** 打分卡上每个按钮下面的“下次”：明天 / 3 天 / 2 周 / 1 个月 */
export function gapText(n) {
  if (n == null) return '';
  if (n <= 0) return '今天';
  if (n === 1) return '明天';
  if (n < 14) return `${n} 天`;
  if (n < 30) return `${Math.round(n / 7)} 周`;
  return `${Math.round(n / 30)} 个月`;
}

// ---------------- 状态、打分 ----------------
// 曲子在计划里的阶段：在学（每天练，直到打“顺”）/ 复习（按间隔）/ 新曲（排队，等有空位）
export const KINDS = Object.freeze({
  learning: { label: '在学', cls: 'accent', tip: '每天都练，打“顺”或“轻松”就毕业转入复习' },
  review: { label: '复习', cls: 'ok', tip: '按记忆曲线隔几天练一次，越熟间隔越长' },
  new: { label: '新曲', cls: '', tip: '在学的曲子少于设置的数量时按顺序开始' },
});
export const RATING_CLS = Object.freeze({ 1: 'err', 2: 'warn', 3: 'ok', 4: 'accent' });
export const ratingName = r => STUDY_RATINGS[String(r)] || '';
export const issueNames = list => (list || []).map(c => STUDY_ISSUES[c]).filter(Boolean);
export function ratingBadge(r, extra = '') {
  const name = ratingName(r);
  return name ? `<span class="badge ${RATING_CLS[r] || ''} rating"${extra}>${esc(name)}</span>` : '';
}
/** 一次打分的说明（悬停提示用，纯文本）：顺 · 杂音、呼吸 · 72 BPM · 备注 */
export function ratingTip(last) {
  if (!last) return '';
  const issues = issueNames(last.issues);
  return [ratingName(last.rating), issues.length && issues.join('、'), last.bpm > 0 && `${last.bpm} BPM`, last.note].filter(Boolean).join(' · ');
}

/** 曲库一行曲名后面的计划徽章（纯 HTML）；不在计划里返回 '' */
export function studyBadge(st) {
  if (!st) return '';
  // 描边 + 圆点，和标签（淡强调色底）区分开；圆点颜色：在学 = 强调色，今天到期 = 绿，其他 = 灰
  const b = (cls, text, tip) => `<span class="badge plan-badge${cls ? ` ${cls}` : ''}" title="${esc(tip)}"><i aria-hidden="true"></i>${esc(text)}</span>`;
  if (st.paused) return b('', '暂停', '练琴计划：已暂停');
  if (st.state === 'new') return b('', '待学', '练琴计划：排队中的新曲');
  if (st.state === 'learning') return b('learn', '在学', `练琴计划：${KINDS.learning.tip}`);
  const n = st.due_in, today = n != null && n <= 0;
  return b(today ? 'due' : '', today ? '今天复习' : `${dueText(n)}复习`, `练琴计划：复习${st.due ? `，下次 ${st.due}` : ''}`);
}

// ---------------- 今日清单 ----------------
export const trainerHref = id => `/trainer?id=${encodeURIComponent(id)}&resume=1&from=today`;

/** 清单里一行的说明：上次打分 · 下次 / 逾期 · 今天在软件里练了多久（纯文本片段数组，调用方 esc） */
export function itemNotes(it) {
  const st = it.study || {}, out = [];
  if (it.kind === 'review' && st.due_in != null && st.due_in < 0) out.push(dueText(st.due_in));
  if (st.last) out.push(`上次${ratingName(st.last.rating)}`);
  else if (it.kind === 'new') out.push('还没开始');
  if (it.today_seconds >= 30) out.push(`今天已练 ${fmtMin(it.today_seconds / 60)}`);
  return out;
}

/** 今天要练的一首。primary：清单第一首用主色按钮 */
export function queueRowHTML(it, { primary = false } = {}) {
  const k = KINDS[it.kind] || KINDS.new;
  const notes = itemNotes(it), st = it.study || {};
  const sub = [it.artist, ...notes].filter(Boolean).map(esc).join(' · ');
  const href = trainerHref(it.id);
  const action = it.playable
    ? `<a class="btn sm act${primary ? ' primary' : ''}" data-k="go" href="${href}">${icon('play', 12)}练习</a>`
    : '<span class="hint">还不能练</span>';
  const tip = st.last ? ratingTip(st.last) : '';
  return `<div class="row study-row${it.playable ? ' click' : ''}" data-id="${esc(it.id)}" data-href="${it.playable ? href : ''}">
    <span class="kind ${k.cls}" title="${esc(k.tip)}">${esc(k.label)}</span>
    <div class="who"><div class="name"><span class="title">${esc(it.title)}</span></div>
      ${sub ? `<div class="sub"${tip ? ` title="${esc(tip)}"` : ''}>${sub}</div>` : ''}</div>
    <div class="ctl">${action}</div>
  </div>`;
}

/** 今天已经打过分的一首：打勾、打分徽章、下次什么时候 */
export function doneRowHTML(it) {
  const st = it.study || {}, last = st.last;
  const next = st.paused ? '已暂停' : st.state === 'learning' ? '明天继续' : st.due_in != null ? `下次${dueText(st.due_in)}` : '';
  const tip = ratingTip(last);
  return `<div class="row study-row done${it.playable ? ' click' : ''}" data-id="${esc(it.id)}" data-href="${it.playable ? trainerHref(it.id) : ''}">
    <span class="kind done-mark" title="今天练过了">${icon('check', 14)}</span>
    <div class="who"><div class="name"><span class="title">${esc(it.title)}</span>${last ? ratingBadge(last.rating, tip ? ` title="${esc(tip)}"` : '') : ''}</div>
      <div class="sub">${esc([it.artist, next, it.today_seconds >= 30 && `练了 ${fmtMin(it.today_seconds / 60)}`].filter(Boolean).join(' · '))}</div></div>
  </div>`;
}

// ---------------- 计划（全部曲目，可调顺序） ----------------
export function planNext(st) {
  if (!st) return '';
  if (st.paused) return '已暂停';
  if (st.state === 'new') return '排队中';
  if (st.state === 'learning') return st.rated_today ? '明天继续' : '今天练';
  return dueText(st.due_in) + (st.due_in != null && st.due_in <= 0 ? '' : '复习');
}
/** 计划里的一首：i 是位置（从 0 数），n 是总数（上下移动按钮到头时不可用） */
export function planRowHTML(s, i, n) {
  const st = s.study || {}, k = st.paused ? { label: '暂停', cls: 'muted', tip: '暂停的曲子不会排进今日清单' } : KINDS[st.state] || KINDS.new;
  const meta = [s.artist, planNext(st), st.reps ? `打分 ${st.reps} 次` : ''].filter(Boolean).map(esc).join(' · ');
  const btn = (dir, ic, label, off) => `<button class="icon-btn sm" data-mv="${dir}" data-k="mv${dir}" title="${label}" aria-label="${label}"${off ? ' disabled' : ''}>${icon(ic, 14)}</button>`;
  return `<div class="row plan-row${st.paused ? ' paused' : ''}" data-id="${esc(s.id)}">
    <span class="ord">${i + 1}</span>
    <span class="kind ${k.cls}" title="${esc(k.tip)}">${esc(k.label)}</span>
    <div class="who"><div class="name"><span class="title">${esc(s.title)}</span>${st.last ? ratingBadge(st.last.rating, ` title="${esc(`上次：${ratingTip(st.last)}`)}"`) : ''}</div>
      ${meta ? `<div class="sub">${meta}</div>` : ''}</div>
    <div class="ctl">${btn('-1', 'up', '往前移', i === 0)}${btn('1', 'down', '往后移', i === n - 1)}
      <button class="icon-btn sm" data-plan-more data-k="more" title="更多" aria-label="${esc(s.title)}：更多操作" aria-haspopup="menu">${icon('more')}</button></div>
  </div>`;
}

// ---------------- 进度环 ----------------
/** 今日练琴时间 / 目标：一个圆环，中间是分钟数 */
export function ringHTML(total, goal) {
  const r = 34, c = 2 * Math.PI * r, g = Math.max(1, Number(goal) || 1), m = Math.max(0, Number(total) || 0);
  const pct = Math.min(1, m / g), reached = m >= g;
  return `<div class="ring${reached ? ' reached' : ''}" role="img" aria-label="${esc(`今天练了 ${fmtMin(m)}，目标 ${g} 分钟`)}">
    <svg viewBox="0 0 80 80" aria-hidden="true"><circle class="track" cx="40" cy="40" r="${r}"/>
      <circle class="bar" cx="40" cy="40" r="${r}" stroke-dasharray="${c.toFixed(2)}" stroke-dashoffset="${(c * (1 - pct)).toFixed(2)}"/></svg>
    <div class="ring-c"><b>${Math.round(m)}</b><span>/ ${g} 分钟</span></div>
  </div>`;
}

// ---------------- 练琴日历 ----------------
/** 颜色深浅 0–4：没练 / 不到目标的 1/3 / 2/3 / 不到目标 / 达到目标 */
export function level(min, goal) {
  const m = Number(min) || 0, g = Math.max(1, Number(goal) || 1);
  if (m <= 0) return 0;
  if (m < g / 3) return 1;
  if (m < (2 * g) / 3) return 2;
  if (m < g) return 3;
  return 4;
}
/**
 * 日历网格：weeks 列（每列一周，周一在上），最后一列是今天所在的周，今天以后的格子 future = true（不画）。
 * days：[{day, minutes, ratings}]（只有练过的日子）。→ {cols: [[cell × 7]], months: [{col, label}], total, active}
 */
export function calendarModel(days, today, goal, weeks = 53) {
  const t = parseDay(today);
  if (!t) return { cols: [], months: [], total: 0, active: 0 };
  const byDay = new Map((days || []).map(d => [d.day, d]));
  const dow = (t.getUTCDay() + 6) % 7; // 周一 = 0
  const start = addDays(t, -dow - 7 * (weeks - 1));
  const cols = [], months = [];
  let total = 0, active = 0, lastMonth = -1;
  for (let w = 0; w < weeks; w++) {
    const col = [];
    for (let i = 0; i < 7; i++) {
      const d = addDays(start, w * 7 + i), key = dayKey(d), rec = byDay.get(key);
      const future = d > t;
      const minutes = rec ? Number(rec.minutes) || 0 : 0, ratings = rec ? Number(rec.ratings) || 0 : 0;
      if (!future) { total += minutes; if (minutes > 0 || ratings > 0) active++; }
      col.push({ day: key, minutes, ratings, level: level(minutes, goal), future, today: key === today });
    }
    // 月份标签放在这个月第一天所在的那一列（第一列不完整时不标，免得和下一列挤在一起）
    const first = col.find(c => c.day.endsWith('-01'));
    const m = first ? +first.day.slice(5, 7) : w === 0 ? null : -1;
    if (first && m !== lastMonth) { months.push({ col: w, label: `${m} 月` }); lastMonth = m; }
    cols.push(col);
  }
  return { cols, months, total, active };
}
export function cellTip(c) {
  return `${dayLabel(c.day)} · ${c.minutes > 0 ? fmtMin(c.minutes) : '没练'}${c.ratings ? ` · 打分 ${c.ratings} 首` : ''}`;
}
/** 日历的 HTML：上面一行月份，左边周一 / 三 / 五，格子是按钮（点开那一天的明细）。selected：选中的日子 */
export function calendarHTML(model, selected = '') {
  const n = model.cols.length;
  const months = model.months.map(m => `<span style="grid-column:${m.col + 1}">${esc(m.label)}</span>`).join('');
  const cells = model.cols.map(col => col.map(c => c.future
    ? '<span class="cell future" aria-hidden="true"></span>'
    : `<button class="cell l${c.level}${c.today ? ' today' : ''}${c.day === selected ? ' sel' : ''}" data-day="${c.day}" title="${esc(cellTip(c))}" aria-label="${esc(cellTip(c))}"${c.day === selected ? ' aria-pressed="true"' : ''}></button>`).join('')).join('');
  return `<div class="cal" style="--weeks:${n}">
    <div class="cal-months" aria-hidden="true">${months}</div>
    <div class="cal-days" aria-hidden="true"><span>一</span><span></span><span>三</span><span></span><span>五</span><span></span><span></span></div>
    <div class="cal-grid">${cells}</div>
  </div>`;
}

// ---------------- 某一天的明细 ----------------
const hhmm = at => String(at || '').slice(11, 16);
/** 一条计时 / 补记（GET /api/study/day 的 sessions 里一项）→ {label, sub}（纯文本） */
export function sessionText(s) {
  if (s.kind === 'manual') return { label: '补记', sub: s.note || '' };
  const segs = (s.segments || []).filter(x => x && x[0]);
  const a = segs.length ? hhmm(segs[0][0]) : '', b = segs.length ? hhmm(segs[segs.length - 1][1]) : '';
  return { label: a && b ? `计时 ${a}–${b}` : '计时', sub: [segs.length > 1 && `暂停 ${segs.length - 1} 次`, s.note].filter(Boolean).join(' · ') };
}
