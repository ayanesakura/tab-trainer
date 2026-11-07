// 侧栏的小节卡片：编号、技巧标签、这一小节的和弦（弹唱谱）、提示、待核对说明（可以确认没问题）、原谱截图。正在看的小节是 store 的 ui.bar。
// 敲节奏打开时（rhythm.on）这张卡片收起来，位置让给节奏卡片。

import { get, on } from './store.js';
import { $, UI, esc, songUrl, setButton } from './dom.js';
import { put, withBusy } from '../lib/api.js';
import { applySong, conflict } from './song.js';
import { barTags, warnText, isScore } from './view.js';
import { images, loadImages } from './page-images.js';
import { barChords, chordShapes, chordSVG } from './chords.js';

function renderCard() {
  const bi = get('ui.bar'), s = get('song'), v = get('view');
  if (bi < 0 || !s || !v || !s.bars[bi]) return;
  const sb = s.bars[bi];
  $('barNo').textContent = bi + 1;
  $('barTags').innerHTML = barTags(v.bars[bi] || []).map(t => `<span class="badge accent">${esc(t)}</span>`).join('');
  const chords = barChords(s.bars, bi), shapes = chords.length ? chordShapes(s.bars) : {};
  $('barChords').hidden = !chords.length;
  $('barChords').innerHTML = chords.map(c => `<div class="bar-chord${c.carried ? ' carried' : ''}" title="${c.carried ? '从前面延续下来的和弦' : ''}">${
    shapes[c.name] ? chordSVG(c.name, shapes[c.name]) : `<span class="cd-only">${esc(c.name)}</span>`}</div>`).join('');
  $('tipText').textContent = v.tips[bi] || '';
  const warn = [...(sb.warn || []).map(warnText), sb.uncertain].filter(Boolean);
  $('tipWarn').hidden = !warn.length;
  $('tipWarn').innerHTML = warn.map(w => `<div>${UI.icon('alert', 13)}<span>${esc(w)}</span></div>`).join('');
  $('barReview').hidden = !v.review[bi];
  // 待核对的小节：看过原谱觉得没问题就点确认（之后改了音要重新核对）；确认过的可以撤销
  $('reviewRow').hidden = !(sb.needs_review || sb.reviewed);
  $('tipWarn').classList.toggle('reviewed', !!sb.reviewed);
  setButton($('reviewBtn'), sb.reviewed ? `${UI.icon('x')}撤销确认` : `${UI.icon('check')}确认没问题`);
  $('reviewHint').textContent = sb.reviewed ? '已确认没问题，不再算待核对' : '对照原谱看过，识别是对的';
  drawCrop();
}

async function toggleReview() {
  const bi = get('ui.bar'), sb = (get('song').bars || [])[bi];
  if (!sb) return;
  await withBusy($('reviewBtn'), async () => {
    try {
      applySong(await put(songUrl(`/bars/${encodeURIComponent(sb.id)}/review`), { ok: !sb.reviewed, rev: sb.rev }));
      UI.toast(sb.reviewed ? `已撤销第 ${bi + 1} 小节的确认` : `第 ${bi + 1} 小节已确认`, 'ok');
    } catch (e) {
      if (e.status === 409) await conflict(e);
      else UI.toast(e.message, 'err', 5000);
    }
  });
  renderCard(); // withBusy 结束时把按钮还原成点之前的样子：按新状态重画一次
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
  $('reviewBtn').onclick = toggleReview;
  on('view', v => { if (v) loadImages(v.pages); });
  document.addEventListener('tt-images', drawCrop);
  on(['ui.bar', 'view', 'song.bars', 'song.is_score'], renderCard);
  on(['ui.view', 'ui.drawer'], drawCrop); // 隐藏时量不出大小：切回练习页 / 打开抽屉时重画
  // 敲节奏打开时节奏卡片占这个位置（rhythm-drill.js）；关掉后回来，截图重画一次
  on('rhythm.on', v => { $('barCard').hidden = v; drawCrop(); });
  let t = 0;
  addEventListener('resize', () => { clearTimeout(t); t = setTimeout(drawCrop, 100); });
}
