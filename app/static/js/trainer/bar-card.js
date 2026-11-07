// 侧栏的小节卡片：编号、技巧标签、提示、待核对说明、原谱截图。正在看的小节是 store 的 ui.bar。

import { get, on } from './store.js';
import { $, UI, esc } from './dom.js';
import { barTags, warnText } from './view.js';

// 原谱页面图片：按 view.pages 加载，加载好了重画（截图、原图弹窗都用）
export const images = { list: [], src: [] };
function loadImages(pages) {
  if (images.src.join() === pages.join()) return;
  images.src = pages.slice();
  images.list = pages.map(src => {
    const im = new Image();
    im.onload = () => { im.ok = true; drawCrop(); document.dispatchEvent(new Event('tt-images')); };
    im.onerror = () => { im.bad = true; drawCrop(); document.dispatchEvent(new Event('tt-images')); };
    im.src = src;
    return im;
  });
}

function renderCard() {
  const bi = get('ui.bar'), s = get('song'), v = get('view');
  if (bi < 0 || !s || !v || !s.bars[bi]) return;
  const sb = s.bars[bi];
  $('barNo').textContent = bi + 1;
  $('barTags').innerHTML = barTags(v.bars[bi] || []).map(t => `<span class="badge accent">${esc(t)}</span>`).join('');
  $('tipText').textContent = v.tips[bi] || '';
  const warn = [...(sb.warn || []).map(warnText), sb.uncertain].filter(Boolean);
  $('tipWarn').hidden = !warn.length;
  $('tipWarn').innerHTML = warn.map(w => `<div>${UI.icon('alert', 13)}<span>${esc(w)}</span></div>`).join('');
  $('barReview').hidden = !v.review[bi];
  drawCrop();
}

function drawCrop() {
  const bi = get('ui.bar'), v = get('view'), s = get('song');
  const card = $('cropBox');
  card.hidden = !s || s.kind === 'score' || !v || !v.regions[bi];
  if (card.hidden || $('view-practice').hidden) return;
  const [p, x0, x1, y0, y1] = v.regions[bi], im = images.list[p], c = $('crop'), msg = $('cropMsg');
  const bad = !im || im.bad;
  c.hidden = bad; msg.hidden = !bad;
  if (bad) { msg.textContent = '原谱图片加载失败'; return; }
  if (!im.ok) return;
  const box = c.parentElement, bw = box.clientWidth - 16, bh = 200;
  const sc = Math.min(bw / (x1 - x0), bh / (y1 - y0), 2.5), dpr = window.devicePixelRatio || 1;
  c.style.width = (x1 - x0) * sc + 'px'; c.style.height = (y1 - y0) * sc + 'px';
  c.width = (x1 - x0) * sc * dpr; c.height = (y1 - y0) * sc * dpr;
  c.getContext('2d').drawImage(im, x0, y0, x1 - x0, y1 - y0, 0, 0, c.width, c.height);
}

export function initBarCard() {
  on('view', v => { if (v) loadImages(v.pages); });
  on(['ui.bar', 'view', 'song.bars', 'song.kind'], renderCard);
  on(['ui.view', 'ui.drawer'], drawCrop); // 隐藏时量不出大小：切回练习页 / 打开抽屉时重画
  let t = 0;
  addEventListener('resize', () => { clearTimeout(t); t = setTimeout(drawCrop, 100); });
}
