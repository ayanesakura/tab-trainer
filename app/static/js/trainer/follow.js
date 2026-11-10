// 自动跟随：播放中让正在弹的这一行和“下一行”都完整露出来（含和弦名、歌词），好提前看下一行要弹什么；
// 下一行快被挡住时把谱面平滑地滚过去（这一行顶到吸顶的控制条下面），露在控制条和底部指板之间。
// 两手在琴上，不能要求去点“回到播放位置”。用户自己滚动（滚轮、触控板、翻页键、拖滚动条）后停几秒，不和用户抢。
// 开关是“更多”里的“自动跟随”（store 的 play.follow，记在本机，见 prefs.js）。不用 alphaTab 自带的滚动：
// 它不知道吸顶控制条和底部指板挡住了哪里，而且每一行都会滚。这里用 boundsLookup 算出的小节位置自己滚。

import { get, on } from './store.js';
import { $ } from './dom.js';
import { scoreRow, rowOfBar } from './player.js';

const PAUSE_MS = 4000; // 用户手动滚动后多久不自动跟随
let pausedUntil = 0;

const dockH = () => { const d = $('fbDock'); return d && !d.hidden ? d.offsetHeight : 0; };
/** 控制条吸顶以后的下沿（视口坐标）；窄屏时控制条不吸顶，只算顶栏 */
function stuckTop() {
  const t = $('transport'), cs = getComputedStyle(t);
  if (cs.position === 'sticky') return parseFloat(cs.top) + t.offsetHeight;
  const bar = document.querySelector('.topbar');
  return bar ? bar.offsetHeight : 0;
}

/** 可见区域（视口坐标）：上面去掉吸顶的控制条，下面去掉底部指板；target 是滚动时让一行顶到的位置 */
function visible() {
  return {
    top: Math.max($('transport').getBoundingClientRect().bottom, stuckTop()) + 8,
    bottom: innerHeight - dockH() - 8,
    target: stuckTop() + 16,
  };
}

/**
 * 让第 first..last 行（行号）露出来要滚多少；已经都看得见返回 0。
 * 放得下：把第 first 行顶到控制条下面。放不下（窗口太矮）：只管 must 这一行（默认 first），把它顶上去，下面能露多少算多少
 */
function rowsDelta(first, last, must = first) {
  const sy = $('score').getBoundingClientRect().top, rf = scoreRow(first), rl = scoreRow(last), rm = scoreRow(must);
  if (!rf || !rl || !rm) return null;
  const { top, bottom, target } = visible();
  const y0 = sy + rf.y, y1 = sy + rl.y + rl.h;
  if (y0 >= top && y1 <= bottom) return 0;
  if (y1 - y0 <= bottom - target) return y0 - target;
  const m0 = sy + rm.y;
  return Math.abs(m0 - target) <= 24 ? 0 : m0 - target; // 已经顶在上面了：再滚也看不全
}

/**
 * 播放到第 bi 小节时要滚多少（量不出来返回 null）：这一行和下一行都要完整露出来。
 * 下一行一般是下面那一行；循环一段（a..b 不是全曲）而这一行就是段尾所在的行时，弹完要回到段首，下一行是段首那一行。
 */
export function followDelta(bi, a = 0, b = Infinity) {
  const r = rowOfBar(bi);
  if (r < 0) return null;
  const cur = scoreRow(r);
  if (b !== Infinity && cur && cur.a <= b && b <= cur.b) {
    const ra = rowOfBar(a);
    if (ra >= 0 && ra < r) return rowsDelta(ra, r, r);
    return rowsDelta(r, r);
  }
  return rowsDelta(r, scoreRow(r + 1) ? r + 1 : r, r);
}

function follow() {
  const p = get('play');
  if (!p.follow || !p.playing || Date.now() < pausedUntil || p.note < 0) return;
  const v = get('view'), ev = v && v.all[p.note];
  if (!ev) return;
  const whole = v && p.a === 0 && p.b === v.bars.length - 1;
  const d = whole ? followDelta(ev.bar) : followDelta(ev.bar, p.a, p.b);
  if (d) scrollTo({ top: Math.max(0, scrollY + d), behavior: 'smooth' });
}

/** 选了新的一段：整段（从段首那一行到段尾那一行）露出来，放不下就让段首那一行顶到控制条下面 */
function reveal(r) {
  if (!r) return;
  const ra = rowOfBar(r.a), rb = rowOfBar(r.b);
  if (ra < 0 || rb < 0) return;
  const d = rowsDelta(ra, rb);
  if (d) scrollTo({ top: Math.max(0, scrollY + d), behavior: 'smooth' });
}

export function initFollow() {
  on('play.note', follow);
  on('play.follow', follow);
  on('ui.reveal', reveal);
  const pause = () => { if (get('play.playing')) pausedUntil = Date.now() + PAUSE_MS; };
  addEventListener('wheel', pause, { passive: true });
  addEventListener('touchmove', pause, { passive: true });
  addEventListener('keydown', e => { if (!e.defaultPrevented && ['PageUp', 'PageDown', 'Home', 'End'].includes(e.key)) pause(); }); // Home 被快捷键用了（回到段首）就不是滚动
  addEventListener('mousedown', e => { if (e.clientX >= document.documentElement.clientWidth) pause(); }); // 拖滚动条
  $('followBtn').addEventListener('click', () => { pausedUntil = 0; }); // 点了“回到播放位置”：恢复跟随
}

export const followDebug = { get pausedUntil() { return pausedUntil; } };
