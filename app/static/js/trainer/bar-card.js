// 侧栏的小节卡片：编号、技巧标签、提示、待核对说明、原谱截图。正在看的小节是 store 的 ui.bar。
// 敲节奏打开时（rhythm.on）这张卡片收起来，位置让给节奏卡片。

import { get, on } from './store.js';
import { $, UI, esc } from './dom.js';
import { barTags, warnText, isScore } from './view.js';
import { images, loadImages } from './page-images.js';

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
  card.hidden = !s || isScore(s) || !v || !v.regions[bi];
  if (card.hidden || $('view-practice').hidden || $('barCard').hidden) return; // 隐藏着量不出宽度
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
  document.addEventListener('tt-images', drawCrop);
  on(['ui.bar', 'view', 'song.bars', 'song.is_score'], renderCard);
  on(['ui.view', 'ui.drawer'], drawCrop); // 隐藏时量不出大小：切回练习页 / 打开抽屉时重画
  // 敲节奏打开时节奏卡片占这个位置（rhythm-drill.js）；关掉后回来，截图重画一次
  on('rhythm.on', v => { $('barCard').hidden = v; drawCrop(); });
  let t = 0;
  addEventListener('resize', () => { clearTimeout(t); t = setTimeout(drawCrop, 100); });
}
