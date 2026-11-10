// 指板（SVG）：标出正在弹的音（play.note）和下一个音（play.next），品格数字从变调夹算起。
// 颜色全在 trainer.css 里（.fb-* 类，跟主题走）。停着的时候显示正在看的小节的第一个音。
// 大小：高度由 CSS 按屏幕高度定（字号跟着高度走），宽度铺满停靠条——viewBox 的宽按停靠条的宽高比算，
// 宽屏上品格拉开、不在两边留大块空白（太宽时封顶，指板居中）。

import { get, set, on } from './store.js';
import { svgEl as el } from './dom.js';

const FB = { W: 960, H: 146, L: 74, R: 14, T: 16, G: 20, MAXF: 17 };
const W_MIN = 760, W_MAX = 1500;
let capo = 0, dyn = null;
const pfr = r => 1 - Math.pow(2, -r / 12);
const fx = n => FB.L + (pfr(n + capo) - pfr(capo)) / (pfr(FB.MAXF + capo) - pfr(capo)) * (FB.W - FB.L - FB.R);
const nx = n => (n === 0 ? FB.L - 16 : (fx(n - 1) + fx(n)) / 2);
const sy = s => FB.T + (s - 1) * FB.G;

/** 停靠条现在的宽高比 → viewBox 的宽（量不出来时不变） */
function fitWidth() {
  const svg = document.getElementById('fb'), w = svg.clientWidth, h = svg.clientHeight;
  if (!w || !h) return FB.W;
  return Math.round(Math.max(W_MIN, Math.min(W_MAX, w / h * FB.H)));
}

function renderBoard(v) {
  const svg = document.getElementById('fb');
  svg.replaceChildren();
  FB.W = fitWidth();
  svg.setAttribute('viewBox', `0 0 ${FB.W} ${FB.H}`);
  capo = v.capo;
  el('rect', { x: FB.L, y: FB.T - 8, width: FB.W - FB.L - FB.R, height: 5 * FB.G + 16, rx: 4, class: 'fb-board' }, svg);
  const inlay = (n, s) => { if (n >= 1 && n <= FB.MAXF) el('circle', { cx: nx(n), cy: sy(s), r: 4.5, class: 'fb-inlay' }, svg); };
  for (const r of [3, 5, 7, 9, 15, 17, 19]) inlay(r - capo, 3.5);
  inlay(12 - capo, 2); inlay(12 - capo, 5);
  for (let n = 1; n <= FB.MAXF; n++) {
    el('line', { x1: fx(n), x2: fx(n), y1: FB.T - 8, y2: sy(6) + 8, class: 'fb-fret' }, svg);
    el('text', { x: nx(n), y: sy(6) + 24, 'text-anchor': 'middle', class: 'fb-label' }, svg).textContent = n;
  }
  el('rect', { x: fx(0) - 3, y: FB.T - 10, width: 6, height: 5 * FB.G + 20, rx: 2, class: 'fb-nut' }, svg);
  el('text', { x: fx(0), y: sy(6) + 24, 'text-anchor': 'middle', class: 'fb-label' }, svg).textContent = capo ? `夹${capo}品` : '空弦';
  for (let s = 1; s <= 6; s++) {
    el('line', { x1: FB.L - 26, x2: FB.W - FB.R, y1: sy(s), y2: sy(s), 'stroke-width': 0.7 + s * 0.25, class: 'fb-string' }, svg);
    el('text', { x: 4, y: sy(s) + 4, class: 'fb-label' }, svg).textContent = `${s}弦 ${v.openName[s]}`;
  }
  dyn = el('g', {}, svg);
}

function diamond(x, y, r, cls) {
  return el('polygon', { points: `${x},${y - r} ${x + r},${y} ${x},${y + r} ${x - r},${y}`, class: cls }, dyn);
}
function drawEvent(e, ghost) {
  const k = ghost ? 'fb-next' : 'fb-cur';
  for (const n of e.notes) {
    const y = sy(n.s);
    if (n.tech.has('nh')) { diamond(fx(n.f), y, 8.5, k + (ghost ? '' : ' fill')); continue; }
    const x = nx(n.f);
    if (ghost) el('circle', { cx: x, cy: y, r: 7.5, class: k }, dyn);
    else {
      el('circle', { cx: x, cy: y, r: 8.5, class: k + (n.f === 0 ? '' : ' fill') }, dyn);
      el('text', { x, y: y + 4, 'text-anchor': 'middle', class: 'fb-num' + (n.f === 0 ? ' open' : '') }, dyn).textContent = n.f;
    }
    if (n.tech.has('ah') && n.f + 12 <= FB.MAXF) diamond(fx(n.f + 12), y, 8.5, k);
    if (n.tech.has('th') && n.harm > n.f && n.harm <= FB.MAXF) diamond(fx(n.harm), y, 8.5, k); // 点弦泛音：右手点的品
  }
}
function paintNotes() {
  const v = get('view');
  if (!dyn || !v) return;
  dyn.replaceChildren();
  const cur = v.all[get('play.note')], next = v.all[get('play.next')];
  if (next) drawEvent(next, true);
  if (cur) drawEvent(cur, false);
}

/** 第一个有音的事件在全曲里的序号（停着时指板显示它） */
const firstNote = (v, bi) => { const e = (v.bars[bi] || []).find(x => x.notes.length); return e ? e.idx : -1; };

export function initFretboard() {
  on('view', v => { if (v) { renderBoard(v); paintNotes(); } });
  // 停靠条变宽变窄、展开（从 hidden 变成看得见）：宽高比变了就按新的宽度重画
  new ResizeObserver(() => { const v = get('view'); if (v && Math.abs(fitWidth() - FB.W) > 8) { renderBoard(v); paintNotes(); } }).observe(document.getElementById('fb'));
  on(['play.note', 'play.next'], paintNotes);
  on('ui.bar', bi => { if (!get('play.playing') && get('view')) set({ 'play.note': firstNote(get('view'), bi), 'play.next': -1 }); });
}
