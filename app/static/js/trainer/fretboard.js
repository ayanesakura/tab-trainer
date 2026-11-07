// 指板（SVG）：标出正在弹的音和下一个音，品格数字从变调夹算起

const SVGNS = 'http://www.w3.org/2000/svg';
function el(tag, attrs = {}, parent) {
  const n = document.createElementNS(SVGNS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(n);
  return n;
}

const FB = { W: 960, L: 58, R: 14, T: 20, G: 22, MAXF: 17 };
const pfr = r => 1 - Math.pow(2, -r / 12);
const fx = n => FB.L + (pfr(n + CAPO) - pfr(CAPO)) / (pfr(FB.MAXF + CAPO) - pfr(CAPO)) * (FB.W - FB.L - FB.R);
const nx = n => n === 0 ? FB.L - 16 : (fx(n - 1) + fx(n)) / 2;
const sy = s => FB.T + (s - 1) * FB.G;
let fbDyn;
function renderFretboard() {
  const svg = document.getElementById('fb');
  svg.innerHTML = '';
  el('rect', { x: FB.L, y: FB.T - 8, width: FB.W - FB.L - FB.R, height: 5 * FB.G + 16, fill: '#3b2a1e', rx: 3 }, svg);
  for (const r of [3, 5, 7, 9, 15, 17, 19]) { const n = r - CAPO; if (n >= 1 && n <= FB.MAXF) el('circle', { cx: nx(n), cy: sy(3.5), r: 5, fill: '#cdbb9c', opacity: 0.55 }, svg); }
  el('circle', { cx: nx(12 - CAPO), cy: sy(2), r: 5, fill: '#cdbb9c', opacity: 0.55 }, svg);
  el('circle', { cx: nx(12 - CAPO), cy: sy(5), r: 5, fill: '#cdbb9c', opacity: 0.55 }, svg);
  for (let n = 1; n <= FB.MAXF; n++) {
    el('line', { x1: fx(n), x2: fx(n), y1: FB.T - 8, y2: sy(6) + 8, stroke: '#b9b9b9', 'stroke-width': 2 }, svg);
    el('text', { x: nx(n), y: sy(6) + 26, 'text-anchor': 'middle', fill: '#9aa0a8', 'font-size': 11 }, svg).textContent = n;
  }
  el('rect', { x: fx(0) - 4, y: FB.T - 12, width: 8, height: 5 * FB.G + 24, fill: '#222', stroke: '#888', rx: 2 }, svg);
  el('text', { x: fx(0), y: sy(6) + 26, 'text-anchor': 'middle', fill: '#9aa0a8', 'font-size': 10 }, svg).textContent = `夹${CAPO}品`;
  for (let s = 1; s <= 6; s++) {
    el('line', { x1: FB.L - 26, x2: FB.W - FB.R, y1: sy(s), y2: sy(s), stroke: '#ddd', 'stroke-width': 0.6 + s * 0.28, opacity: 0.85 }, svg);
    el('text', { x: 4, y: sy(s) + 4, fill: '#9aa0a8', 'font-size': 11 }, svg).textContent = `${s}弦 ${OPEN_NAME[s]}`;
  }
  fbDyn = el('g', {}, svg);
}
function diamond(x, y, r, attrs, parent) {
  return el('polygon', Object.assign({ points: `${x},${y - r} ${x + r},${y} ${x},${y + r} ${x - r},${y}` }, attrs), parent);
}
function drawEventOnFb(e, ghost) {
  const col = ghost ? '#8fc6e0' : '#e8b04f';
  for (const n of e.notes) {
    const y = sy(n.s);
    if (n.tech.has('nh')) { diamond(fx(n.f), y, 9, { fill: ghost ? 'none' : col, stroke: col, 'stroke-width': 2 }, fbDyn); continue; }
    const x = nx(n.f);
    if (ghost) el('circle', { cx: x, cy: y, r: 8, fill: 'none', stroke: col, 'stroke-width': 2 }, fbDyn);
    else {
      el('circle', { cx: x, cy: y, r: 9, fill: n.f === 0 ? 'none' : col, stroke: col, 'stroke-width': 2 }, fbDyn);
      el('text', { x, y: y + 4, 'text-anchor': 'middle', 'font-size': 11, 'font-weight': 700, fill: n.f === 0 ? col : '#1a1a1a' }, fbDyn).textContent = n.f;
    }
    if (n.tech.has('ah') && n.f + 12 <= FB.MAXF) diamond(fx(n.f + 12), y, 9, { fill: 'none', stroke: col, 'stroke-width': 2 }, fbDyn);
  }
}
let fbKey = '';
function showFb(cur, next) {
  const key = (cur ? cur.idx : -1) + ':' + (next ? next.idx : -1);
  if (key === fbKey) return; fbKey = key;
  fbDyn.innerHTML = '';
  if (next) drawEventOnFb(next, true);
  if (cur) drawEventOnFb(cur, false);
}
