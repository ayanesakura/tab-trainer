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
