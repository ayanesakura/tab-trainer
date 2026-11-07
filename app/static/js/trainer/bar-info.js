// 原谱对照 + 小节说明：显示当前小节的原谱截图、提示、技巧标签和可能识别有误的说明。
// shownBar 是正在显示的小节，教练、练习记录、改谱都按它来。

const LAB = { h: 'H', p: 'P', sl: 'sl.', si: 'sl.', pm: 'P.M.', nh: '泛音', ah: 'AH', roll: '琶音', x: 'x', vib: '~', bend: 'bend' };
let shownBar = -1, imgs = [];
function loadImages() {
  imgs = PAGES.map(src => {
    const im = new Image();
    im.onload = () => { im.ok = true; if (shownBar >= 0) drawCrop(shownBar); };
    im.onerror = () => { im.bad = true; if (shownBar >= 0) drawCrop(shownBar); };
    im.src = src;
    return im;
  });
}
function showBar(bi) {
  if (bi === shownBar) return; shownBar = bi;
  document.getElementById('cropNo').textContent = bi + 1;
  document.getElementById('tipTitle').textContent = `第 ${bi + 1} 小节`;
  document.getElementById('tipText').textContent = TIPS[bi] || '';
  if (!state.playing && BARS[bi] && BARS[bi].length) showFb(BARS[bi].find(e => e.notes.length) || null, null); // 停着时指板也显示这一小节的第一个音
  const sb = SONG.bars[bi], warn = [...(sb.warn || []), sb.uncertain].filter(Boolean);
  $('tipWarn').style.display = warn.length ? '' : 'none';
  $('tipWarn').innerHTML = warn.map(w => `<div>${UI.icon('alert', 13)} ${esc(w)}</div>`).join('');
  updateBarTools();
  renderCoach();
  renderPracticeLine();
  if (sb.id !== editorBarId) closeEditor();
  const tags = new Set();
  BARS[bi].forEach(e => {
    e.tech.forEach(k => LAB[k] && tags.add(k === 'si' ? '滑入' : LAB[k]));
    if (e.notes.some(n => n.grace != null)) tags.add('倚音');
  });
  if (BARS[bi].some(e => Math.abs(e.d - T3) < 1e-6)) tags.add('三连音');
  if (BARS[bi].some(e => Math.abs(e.d - S6) < 1e-6)) tags.add('六连音');
  document.getElementById('tipTags').innerHTML = [...tags].map(t => `<span class="badge accent">${t}</span>`).join('');
  drawCrop(bi);
}
// ---- 原图弹窗：谱面上点选小节后，小节右上角浮出“原图”按钮，点开看这一整行的原图，当前小节框出来 ----
let origBar = -1;
const hasOrig = bi => SONG && SONG.kind !== 'score' && !!REG[bi];
function placeOrigBtn(bi) {
  let btn = $('origBtn');
  if (!btn) {
    btn = document.createElement('button');
    btn.id = 'origBtn'; btn.className = 'btn sm orig-btn'; btn.title = '看这一小节的原图';
    btn.innerHTML = UI.icon('image', 13) + '原图';
    btn.onclick = e => { e.stopPropagation(); openOrig(+btn.dataset.bar); };
    btn.addEventListener('mousedown', e => e.stopPropagation()); // 别让 alphaTab 当成点了谱面
    btn.addEventListener('pointerdown', e => e.stopPropagation());
    $('score').appendChild(btn);
  }
  const look = api && (api.boundsLookup || (api.renderer && api.renderer.boundsLookup)), surface = $('score').querySelector('.at-surface');
  const mb = bi >= 0 && hasOrig(bi) && !state.playing && look && surface && look.findMasterBarByIndex(bi);
  btn.hidden = !mb;
  if (!mb) return;
  const r = mb.realBounds;
  btn.dataset.bar = bi;
  btn.style.left = surface.offsetLeft + r.x + r.w - btn.offsetWidth + 'px';
  btn.style.top = Math.max(0, surface.offsetTop + r.y - btn.offsetHeight - 2) + 'px'; // 放在小节上方，不挡音符
}
function openOrig(bi) {
  if (!hasOrig(bi)) return;
  origBar = bi;
  $('origModal').hidden = false;
  drawOrig();
}
function closeOrig() { $('origModal').hidden = true; origBar = -1; }
function stepOrig(d) {
  let bi = origBar + d;
  while (bi >= 0 && bi < SONG.bars.length && !hasOrig(bi)) bi += d;
  if (bi >= 0 && bi < SONG.bars.length) { origBar = bi; drawOrig(); }
}
function drawOrig() {
  const bi = origBar, b = SONG.bars[bi], [p, bx0, bx1, by0, by1] = REG[bi], im = imgs[p], c = $('origCanvas');
  const sys = SONG.systems && SONG.systems[b.system];
  const [x0, x1, y0, y1] = sys && sys.crop && sys.page === p ? sys.crop : [bx0, bx1, by0, by1]; // 整行；没有行信息时只看这一小节
  const warn = (b.warn && b.warn.length) || b.uncertain;
  $('origTitle').textContent = `原图 · 第 ${bi + 1} 小节`;
  $('origHint').textContent = warn ? '可能识别有误' : '';
  $('origPrev').disabled = bi === 0; $('origNext').disabled = bi === SONG.bars.length - 1;
  if (!im || im.bad) { c.hidden = true; $('origHint').textContent = '原谱图片加载失败'; return; }
  c.hidden = false;
  if (!im.ok) { im.addEventListener('load', () => origBar === bi && drawOrig(), { once: true }); return; }
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
  g.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() || '#e07a2f';
  g.lineWidth = 2 * dpr;
  g.strokeRect(hx0 + dpr, dpr, hx1 - hx0 - 2 * dpr, c.height - 2 * dpr);
}
function initOrig() {
  $('origClose').onclick = closeOrig;
  $('origPrev').onclick = () => stepOrig(-1);
  $('origNext').onclick = () => stepOrig(1);
  $('origModal').addEventListener('click', e => { if (e.target === $('origModal')) closeOrig(); });
  document.addEventListener('keydown', e => {
    if ($('origModal').hidden) return;
    const k = { Escape: closeOrig, ArrowLeft: () => stepOrig(-1), ArrowRight: () => stepOrig(1) }[e.key];
    if (k) { e.preventDefault(); k(); }
  });
  addEventListener('resize', () => { if (origBar >= 0) drawOrig(); });
}

function drawCrop(bi) {
  if (!REG[bi]) return;
  const [p, x0, x1, y0, y1] = REG[bi], im = imgs[p], c = $('crop'), msg = $('cropMsg');
  if (!im || im.bad) { c.style.display = 'none'; msg.style.display = ''; msg.textContent = '原谱图片加载失败'; return; }
  c.style.display = ''; msg.style.display = 'none';
  if (!im.ok) return;
  const box = c.parentElement, bw = box.clientWidth - 12, bh = box.clientHeight - 12;
  const sc = Math.min(bw / (x1 - x0), bh / (y1 - y0)), dpr = window.devicePixelRatio || 1;
  c.style.width = (x1 - x0) * sc + 'px'; c.style.height = (y1 - y0) * sc + 'px';
  c.width = (x1 - x0) * sc * dpr; c.height = (y1 - y0) * sc * dpr;
  c.getContext('2d').drawImage(im, x0, y0, x1 - x0, y1 - y0, 0, 0, c.width, c.height);
}
