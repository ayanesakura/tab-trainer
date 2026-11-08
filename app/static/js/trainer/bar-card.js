// 侧栏的小节卡片：编号、技巧标签、这一小节的和弦（弹唱谱）、提示、待核对说明（可以确认没问题）、精修改了什么（可以撤销）、
// 精修这个小节 / 这一行、原谱截图。
// 正在看的小节是 store 的 ui.bar。
// 敲节奏打开时（rhythm.on）这张卡片收起来，位置让给节奏卡片。

import { get, on } from './store.js';
import { $, UI, esc, songUrl, setButton } from './dom.js';
import { post, put, withBusy } from '../lib/api.js';
import { applySong, writeFailed, refresh, isLocked, lockedToast } from './song.js';
import { barTags, warnText, isScore, eventsText } from './view.js';
import { images, loadImages } from './page-images.js';
import { barChords, chordShapes, chordSVG } from './chords.js';
import { barRefineHint, rowRefineHint, refineScopeLabel } from '../lib/refine.js';

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
  // 没确认：一个“无误”按钮（说明在悬停提示里）；确认过：左边“已核对”，右边小的“撤销”
  $('reviewRow').classList.toggle('done', !!sb.reviewed);
  setButton($('reviewBtn'), sb.reviewed ? '撤销' : `${UI.icon('check')}无误`);
  $('reviewBtn').classList.toggle('ghost', !!sb.reviewed);
  $('reviewBtn').title = sb.reviewed ? '撤销确认，重新算作待核对' : '对照原谱看过，识别是对的';
  $('reviewHint').innerHTML = sb.reviewed ? `${UI.icon('check', 13)}已核对` : '';
  // 精修改过这个小节：模型说的改动 + 撤销（恢复精修之前的读法）
  const rf = sb.refine;
  $('refineRow').hidden = !rf;
  if (rf) {
    $('refineDiff').textContent = `精修改过：${rf.diff || '（没有说明）'}`;
    $('refinePrev').textContent = eventsText((rf.prev && rf.prev.events) || []);
    $('undoRefineBtn').disabled = !!sb.locked;
    $('undoRefineBtn').title = sb.locked || '恢复成精修之前的读法';
  }
  renderRefineScope(s, sb);
  drawCrop();
}

// 精修这个小节 / 这一行（便宜，不弹确认框）：手改过、确认过的也核对；“识别有误？”里写了哪里不对就一起告诉 AI。
// 正在精修的小节显示“精修中…”，别的小节的按钮先禁用（同一时间只能有一个任务）。
function renderRefineScope(s, sb) {
  const scopes = s.refine_scopes, rowEst = scopes && (scopes.rows || [])[sb.system];
  const canBar = !!sb.refinable && !!scopes, canRow = !!rowEst;
  $('refineScope').hidden = isScore(s) || !(canBar || canRow);
  if ($('refineScope').hidden) return;
  const a = s.activity, refining = !!a && a.kind === 'refine', mine = refining && (a.pending || []).includes(sb.id);
  $('refineScopeBtns').hidden = mine;
  $('refineBusy').hidden = !mine;
  $('refineBarBtn').hidden = !canBar;
  $('refineRowBtn').hidden = !canRow;
  const why = refining ? '正在精修，完成后再试' : s.busy ? '正在处理中，完成后再试' : sb.locked || '';
  const barHint = canBar ? barRefineHint(scopes.bar) : '', rowHint = canRow ? rowRefineHint(rowEst) : '';
  const note = $('hintInput').value.trim() ? '，会带上“哪里不对”里写的说明' : '';
  // 预估费用和时间只放在悬停提示里，卡片上不再多一行字
  for (const [btn, est] of [[$('refineBarBtn'), barHint && `这个小节${barHint}`], [$('refineRowBtn'), rowHint]]) {
    btn.disabled = !!why;
    btn.title = why || `AI 放大核对原谱：${est}${note}`;
  }
  $('refineScopeHint').textContent = mine ? `正在精修${refineScopeLabel(a, s.bars.map(b => b.id))}` : '';
}

async function refineScope(kind) {
  const bi = get('ui.bar'), s = get('song'), sb = s && s.bars[bi];
  if (!sb) return;
  const btn = kind === 'bar' ? $('refineBarBtn') : $('refineRowBtn');
  const body = kind === 'bar' ? { bars: [sb.id] } : { system: sb.system };
  const hint = $('hintInput').value.trim();
  if (hint) body.hint = hint;
  await withBusy(btn, async () => {
    try {
      await post(songUrl('/refine'), body);
      UI.toast(kind === 'bar' ? `已开始精修第 ${bi + 1} 小节` : `已开始精修第 ${sb.system + 1} 行`, 'info');
    } catch (e) {
      if (isLocked(e)) lockedToast(e); else UI.toast(e.message, 'err', 5000);
    }
  });
  await refresh();
  scopeNow(); // withBusy 结束时把按钮还原成点之前的样子：按新状态重画一次
}
function scopeNow() { const s = get('song'), sb = s && s.bars[get('ui.bar')]; if (sb) renderRefineScope(s, sb); }

async function undoRefine() {
  const bi = get('ui.bar'), sb = (get('song').bars || [])[bi];
  if (!sb || !sb.refine) return;
  await withBusy($('undoRefineBtn'), async () => {
    try {
      applySong(await put(songUrl(`/bars/${encodeURIComponent(sb.id)}/undo-refine`), { rev: sb.rev }));
      UI.toast(`已撤销第 ${bi + 1} 小节的精修`, 'ok');
    } catch (e) {
      await writeFailed(e);
    }
  });
  renderCard();
}

async function toggleReview() {
  const bi = get('ui.bar'), sb = (get('song').bars || [])[bi];
  if (!sb) return;
  await withBusy($('reviewBtn'), async () => {
    try {
      applySong(await put(songUrl(`/bars/${encodeURIComponent(sb.id)}/review`), { ok: !sb.reviewed, rev: sb.rev }));
      UI.toast(sb.reviewed ? `已撤销第 ${bi + 1} 小节的确认` : `第 ${bi + 1} 小节已确认`, 'ok');
    } catch (e) {
      await writeFailed(e);
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
  $('undoRefineBtn').onclick = undoRefine;
  $('refineBarBtn').onclick = () => refineScope('bar');
  $('refineRowBtn').onclick = () => refineScope('row');
  $('hintInput').addEventListener('input', scopeNow);
  on(['song.activity', 'song.busy', 'song.refine_scopes'], scopeNow); // 精修开始 / 核对完一个小节：只重画这一行（不重画截图）
  on('view', v => { if (v) loadImages(v.pages); });
  document.addEventListener('tt-images', drawCrop);
  on(['ui.bar', 'view', 'song.bars', 'song.is_score'], renderCard);
  on(['ui.view', 'ui.drawer'], drawCrop); // 隐藏时量不出大小：切回练习页 / 打开抽屉时重画
  // 敲节奏打开时节奏卡片占这个位置（rhythm-drill.js）；关掉后回来，截图重画一次
  on('rhythm.on', v => { $('barCard').hidden = v; drawCrop(); });
  let t = 0;
  addEventListener('resize', () => { clearTimeout(t); t = setTimeout(drawCrop, 100); });
}
