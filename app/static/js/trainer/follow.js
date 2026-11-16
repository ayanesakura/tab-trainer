// 自动跟随：播放到新的一行，就把这一行顶到吸顶的控制条下面——一次只走一行的距离，下面几行露着好提前看；
// 滚得比浏览器自带的平滑滚动慢、起步柔和（规则和时长在 follow-plan.js，纯函数）。
// 两手在琴上，不能要求去点“回到播放位置”。用户自己滚动（滚轮、触控板、翻页键、拖滚动条）后停几秒，不和用户抢。
// 开关是“更多”里的“自动跟随”（store 的 play.follow，记在本机，见 prefs.js）。不用 alphaTab 自带的滚动：
// 它不知道吸顶控制条和底部指板挡住了哪里，而且每一行都会滚。这里用 boundsLookup 算出的小节位置自己滚。

import { get, on } from './store.js';
import { $ } from './dom.js';
import { scoreRow, rowOfBar } from './player.js';
import { followShift, glideMs, ease } from './follow-plan.js';

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

/** 第 i 行在视口里的上下沿（含上面的和弦、下面的歌词）；没有这一行返回 null */
function rowBox(i) {
  const r = scoreRow(i);
  if (!r) return null;
  const y0 = $('score').getBoundingClientRect().top + r.y;
  return { y0, y1: y0 + r.h };
}
const lastRow = () => { let i = 0; while (scoreRow(i + 1)) i++; return i; };

/**
 * 播放到第 bi 小节时要滚多少（量不出来返回 null）。a..b：循环的小节范围（全曲不用给）。规则见 follow-plan.js 的 followShift
 */
export function followDelta(bi, a = 0, b = Infinity) {
  const r = rowOfBar(bi);
  if (r < 0) return null;
  const vis = visible();
  if (b === Infinity) return followShift(rowBox, r, lastRow(), null, vis);
  const ra = rowOfBar(a), rb = rowOfBar(b);
  if (ra < 0 || rb < 0) return null;
  return followShift(rowBox, r, rb, { first: ra, last: rb }, vis);
}

// 自己一帧一帧地滚（不用 behavior: 'smooth'：它的时长和缓动由浏览器定，偏快）。anim：正在滚去的位置
let anim = null;
const reduced = () => typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
function glide(by) {
  const max = Math.max(0, document.documentElement.scrollHeight - innerHeight);
  const from = scrollY, to = Math.max(0, Math.min(max, Math.round(from + by)));
  if (anim && Math.abs(anim.to - to) < 2) return; // 已经在往那儿滚了（每个音都会再问一次）
  if (Math.abs(to - from) < 2) { anim = null; return; }
  if (reduced()) { anim = null; scrollTo({ top: to, behavior: 'instant' }); return; }
  const a = anim = { to }, t0 = performance.now(), ms = glideMs(to - from);
  const step = now => {
    if (anim !== a) return; // 换了目的地，或者用户自己滚了
    const k = Math.min(1, (now - t0) / ms);
    scrollTo({ top: from + (to - from) * ease(k), behavior: 'instant' });
    if (k < 1) requestAnimationFrame(step); else anim = null;
  };
  requestAnimationFrame(step);
}

function follow() {
  const p = get('play');
  if (!p.follow || !p.playing || Date.now() < pausedUntil || p.note < 0) return;
  const v = get('view'), ev = v && v.all[p.note];
  if (!ev) return;
  const whole = v && p.a === 0 && p.b === v.bars.length - 1;
  const d = whole ? followDelta(ev.bar) : followDelta(ev.bar, p.a, p.b);
  if (d) glide(d);
}

/** 选了新的一段：整段（从段首那一行到段尾那一行）露出来，放不下就让段首那一行顶到控制条下面 */
function reveal(r) {
  if (!r) return;
  const ra = rowOfBar(r.a), rb = rowOfBar(r.b);
  if (ra < 0 || rb < 0) return;
  const d = followShift(rowBox, ra, rb, { first: ra, last: rb }, visible());
  if (d) glide(d);
}

export function initFollow() {
  on('play.note', follow);
  on('play.follow', follow);
  on('ui.reveal', reveal);
  const pause = () => { anim = null; if (get('play.playing')) pausedUntil = Date.now() + PAUSE_MS; }; // 正在滚也马上停：不和用户抢
  addEventListener('wheel', pause, { passive: true });
  addEventListener('touchmove', pause, { passive: true });
  addEventListener('keydown', e => { if (!e.defaultPrevented && ['PageUp', 'PageDown', 'Home', 'End'].includes(e.key)) pause(); }); // Home 被快捷键用了（回到段首）就不是滚动
  addEventListener('mousedown', e => { if (e.clientX >= document.documentElement.clientWidth) pause(); }); // 拖滚动条
  $('followBtn').addEventListener('click', () => { pausedUntil = 0; }); // 点了“回到播放位置”：恢复跟随
}

export const followDebug = { get pausedUntil() { return pausedUntil; }, get gliding() { return !!anim; } };
