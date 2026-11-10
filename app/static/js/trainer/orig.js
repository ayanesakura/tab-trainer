// 原谱弹窗：点侧栏小节卡片里的原谱截图，看这一整行的原谱，当前小节框出来。弹窗打开时 ui.orig 是小节下标，关掉是 -1。

import { get, set, on } from './store.js';
import { $, UI } from './dom.js';
import { images } from './page-images.js';
import { needsReview, isScore } from './view.js';

let origLayer = null;
const hasOrig = bi => { const s = get('song'), v = get('view'); return !!(s && v && !isScore(s) && v.regions[bi]); };

export function openOrig(bi) {
  if (!hasOrig(bi)) return;
  set('ui.orig', bi);
}
function step(d) {
  const s = get('song');
  let bi = get('ui.orig') + d;
  while (bi >= 0 && bi < s.bars.length && !hasOrig(bi)) bi += d;
  if (bi >= 0 && bi < s.bars.length) set('ui.orig', bi);
}

function draw() {
  const bi = get('ui.orig');
  if (bi < 0) return;
  // 重新识别、删页以后这个小节可能已经没有了（或者没有原谱区域了）：关掉弹窗，不去读不存在的小节
  if (!hasOrig(bi) || !get('song').bars[bi]) { set('ui.orig', -1); return; }
  const s = get('song'), v = get('view'), b = s.bars[bi], [p, bx0, bx1, by0, by1] = v.regions[bi], im = images.list[p], c = $('origCanvas'), m = $('origMark');
  const sys = s.systems && s.systems[b.system];
  const [x0, x1, y0, y1] = sys && sys.crop && sys.page === p ? sys.crop : [bx0, bx1, by0, by1]; // 整行；没有行信息时只看这一小节
  $('origTitle').textContent = `原谱 · 第 ${bi + 1} 小节`;
  $('origHint').textContent = needsReview(b) ? '待核对' : '';
  $('origPrev').disabled = bi === 0; $('origNext').disabled = bi === s.bars.length - 1;
  if (!im || im.bad) { c.hidden = m.hidden = true; $('origHint').textContent = '原谱图片加载失败'; return; }
  c.hidden = m.hidden = false;
  if (!im.ok) return; // 加载好了会再画（tt-images）
  const box = c.closest('.orig-body'), w = x1 - x0, h = y1 - y0;
  const sc = Math.min((box.clientWidth - 24) / w, (innerHeight * 0.72) / h, 2), dpr = window.devicePixelRatio || 1;
  for (const x of [c, m]) {
    x.style.width = w * sc + 'px'; x.style.height = h * sc + 'px';
    x.width = Math.round(w * sc * dpr); x.height = Math.round(h * sc * dpr);
  }
  const k = sc * dpr;
  c.getContext('2d').drawImage(im, x0, y0, w, h, 0, 0, c.width, c.height);
  // 其他小节淡一点，当前小节描边：画在上面一层（#origMark），不跟着图片在深色下反相，用的就是主题里的颜色
  const g = m.getContext('2d'), css = getComputedStyle(document.documentElement);
  const hx0 = (bx0 - x0) * k, hx1 = (bx1 - x0) * k;
  g.clearRect(0, 0, m.width, m.height);
  g.globalAlpha = 0.55;
  g.fillStyle = css.getPropertyValue('--paper-bg').trim();
  g.fillRect(0, 0, Math.max(0, hx0), m.height); g.fillRect(hx1, 0, Math.max(0, m.width - hx1), m.height);
  g.globalAlpha = 1;
  g.strokeStyle = css.getPropertyValue('--accent').trim();
  g.lineWidth = 2 * dpr;
  g.strokeRect(hx0 + dpr, dpr, hx1 - hx0 - 2 * dpr, m.height - 2 * dpr);
}

export function initOrig() {
  $('cropBox').onclick = () => openOrig(get('ui.bar'));
  $('origClose').onclick = () => set('ui.orig', -1);
  $('origPrev').onclick = () => step(-1);
  $('origNext').onclick = () => step(1);
  const modal = $('origModal');
  document.addEventListener('keydown', e => {
    if (get('ui.orig') < 0) return;
    const k = { ArrowLeft: () => step(-1), ArrowRight: () => step(1) }[e.key];
    if (k) { e.preventDefault(); e.stopPropagation(); k(); }
  });
  on('ui.orig', (bi, old) => {
    if (bi >= 0 && old < 0) origLayer = UI.layer(modal, { modal: true, focus: $('origClose'), onClose: () => { origLayer = null; set('ui.orig', -1); } });
    if (bi < 0 && origLayer) origLayer.close();
    draw();
  });
  // 曲子变了（重新识别、删页、调整页序）：弹窗开着就按新的数据重画（小节没了就关掉）
  on(['view', 'song.systems'], () => { if (get('ui.orig') >= 0) draw(); });
  document.addEventListener('tt-images', draw);
  document.addEventListener('themechange', draw); // 框和淡化的颜色跟着主题、配色走
  addEventListener('resize', draw);
}
