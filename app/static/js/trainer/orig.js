// 原谱弹窗：谱面上点选小节后，小节右上角浮出“原谱”按钮（ui.pick），点开看这一整行的原谱，当前小节框出来。
// 小节卡片里的截图也能点开。弹窗打开时 ui.orig 是小节下标，关掉是 -1。

import { get, set, on } from './store.js';
import { $, UI } from './dom.js';
import { barBox } from './player.js';
import { images } from './bar-card.js';
import { needsReview, isScore } from './view.js';

let origLayer = null;
const hasOrig = bi => { const s = get('song'), v = get('view'); return !!(s && v && !isScore(s) && v.regions[bi]); };

function placeBtn() {
  const btn = $('origBtn'), bi = get('ui.pick');
  const box = !get('play.playing') && hasOrig(bi) && barBox(bi);
  btn.hidden = !box;
  if (!box) return;
  btn.style.left = box.x + box.w - btn.offsetWidth + 'px';
  btn.style.top = Math.max(0, box.y - btn.offsetHeight - 2) + 'px'; // 放在小节上方，不挡音符
}

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
  const s = get('song'), v = get('view'), b = s.bars[bi], [p, bx0, bx1, by0, by1] = v.regions[bi], im = images.list[p], c = $('origCanvas');
  const sys = s.systems && s.systems[b.system];
  const [x0, x1, y0, y1] = sys && sys.crop && sys.page === p ? sys.crop : [bx0, bx1, by0, by1]; // 整行；没有行信息时只看这一小节
  $('origTitle').textContent = `原谱 · 第 ${bi + 1} 小节`;
  $('origHint').textContent = needsReview(b) ? '待核对' : '';
  $('origPrev').disabled = bi === 0; $('origNext').disabled = bi === s.bars.length - 1;
  if (!im || im.bad) { c.hidden = true; $('origHint').textContent = '原谱图片加载失败'; return; }
  c.hidden = false;
  if (!im.ok) return; // 加载好了会再画（tt-images）
  const box = c.parentElement, w = x1 - x0, h = y1 - y0;
  const sc = Math.min((box.clientWidth - 24) / w, (innerHeight * 0.72) / h, 2), dpr = window.devicePixelRatio || 1;
  c.style.width = w * sc + 'px'; c.style.height = h * sc + 'px';
  c.width = Math.round(w * sc * dpr); c.height = Math.round(h * sc * dpr);
  const g = c.getContext('2d'), k = sc * dpr;
  g.drawImage(im, x0, y0, w, h, 0, 0, c.width, c.height);
  // 其他小节淡一点，当前小节描边
  const hx0 = (bx0 - x0) * k, hx1 = (bx1 - x0) * k;
  g.fillStyle = 'rgba(255,255,255,.55)';
  g.fillRect(0, 0, Math.max(0, hx0), c.height); g.fillRect(hx1, 0, Math.max(0, c.width - hx1), c.height);
  g.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
  g.lineWidth = 2 * dpr;
  g.strokeRect(hx0 + dpr, dpr, hx1 - hx0 - 2 * dpr, c.height - 2 * dpr);
}

export function initOrig() {
  const btn = $('origBtn');
  btn.innerHTML = UI.icon('image', 13) + '原谱';
  btn.onclick = e => { e.stopPropagation(); openOrig(get('ui.pick')); };
  btn.addEventListener('mousedown', e => e.stopPropagation()); // 别让 alphaTab 当成点了谱面
  btn.addEventListener('pointerdown', e => e.stopPropagation());
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
  on(['ui.pick', 'play.playing', 'ui.renderGen', 'view'], placeBtn);
  document.addEventListener('tt-images', draw);
  addEventListener('resize', draw);
}
