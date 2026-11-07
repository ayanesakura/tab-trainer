// 自动跟随：播放中光标走到看不见（或只露出一部分）的行时，把谱面平滑地滚过去，让这一行露在吸顶的控制条和底部指板之间。
// 两手在琴上，不能要求去点“回到播放位置”。用户自己滚动（滚轮、触控板、翻页键、拖滚动条）后停几秒，不和用户抢。
// 开关是“更多”里的“自动跟随”（store 的 play.follow，记在本机，见 prefs.js）。不用 alphaTab 自带的滚动：
// 它不知道吸顶控制条和底部指板挡住了哪里，而且每一行都会滚。这里用 boundsLookup 算出的小节位置自己滚。

import { get, on } from './store.js';
import { $ } from './dom.js';
import { barBox } from './player.js';

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

/**
 * 第 bi 小节所在行要滚多少才能完整露出来；已经看得见（或行比可见区域还高、顶上已经对齐）返回 0，量不出来返回 null。
 * 滚的时候让这一行的上沿刚好在控制条下面（往下能多看几行）；但循环段从第 a 小节那一行到这一行一屏放得下时，
 * 对齐到段首那一行，这样跨两行的循环滚一次就不用再来回滚。
 */
export function followDelta(bi, a = bi) {
  const box = barBox(bi);
  if (!box) return null;
  const sy = $('score').getBoundingClientRect().top, y0 = sy + box.y, y1 = y0 + box.h;
  const top = Math.max($('transport').getBoundingClientRect().bottom, stuckTop()) + 8, bottom = innerHeight - dockH() - 8;
  if (y0 >= top && y1 <= bottom) return 0;
  if (y0 >= top && y0 <= top + 24) return 0; // 这一行比可见区域还高：顶上已经对齐了，再滚也看不全
  const target = stuckTop() + 16, boxA = a !== bi && barBox(a), ya = boxA ? sy + boxA.y : y0;
  if (ya < y0 && y1 - ya <= bottom - target) return ya - target;
  return y0 - target;
}

function follow() {
  const p = get('play');
  if (!p.follow || !p.playing || Date.now() < pausedUntil || p.note < 0) return;
  const v = get('view'), ev = v && v.all[p.note];
  if (!ev) return;
  const d = followDelta(ev.bar, p.a <= ev.bar ? p.a : ev.bar);
  if (d) scrollTo({ top: Math.max(0, scrollY + d), behavior: 'smooth' });
}

export function initFollow() {
  on('play.note', follow);
  on('play.follow', follow);
  const pause = () => { if (get('play.playing')) pausedUntil = Date.now() + PAUSE_MS; };
  addEventListener('wheel', pause, { passive: true });
  addEventListener('touchmove', pause, { passive: true });
  addEventListener('keydown', e => { if (['PageUp', 'PageDown', 'Home', 'End'].includes(e.key)) pause(); });
  addEventListener('mousedown', e => { if (e.clientX >= document.documentElement.clientWidth) pause(); }); // 拖滚动条
  $('followBtn').addEventListener('click', () => { pausedUntil = 0; }); // 点了“回到播放位置”：恢复跟随
}

export const followDebug = { get pausedUntil() { return pausedUntil; } };
