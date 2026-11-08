// 弹唱谱的和弦：这一小节弹哪些和弦（含从前面延续下来的），以及侧栏里的小和弦图（SVG 文本，纯函数，Node 测试直接用）。

import { STRINGS } from '../vocab.js';
import { esc } from '../lib/html.js';

/** 全曲的和弦指法：和弦名 → 1–6 弦品位（-1 = 不弹）。同名的以第一次出现的为准。 */
export function chordShapes(bars) {
  const out = {};
  for (const b of bars || []) for (const [k, v] of Object.entries(b.shapes || {})) if (!(k in out)) out[k] = v;
  return out;
}

/**
 * 第 bi 小节弹的和弦，按出现顺序：[{ name, carried }]。小节开头没写新和弦、但前面的小节有和弦时，
 * 先列出延续下来的那个（carried = true）。整首都没有和弦名时返回 []。
 * 往前找延续的和弦时按跳房子（bar.endings）走：第二房子前面紧挨着弹的不是第一房子，跳过第一房子；
 * 房子后面的小节接的是最后一个房子（往回走先碰到的那个）。过了反复开始（repeat_start）就是前面另一段，重新算。
 */
export function barChords(bars, bi) {
  const evs = (bars[bi] && bars[bi].events) || [];
  const out = [];
  if (!(evs[0] && evs[0].chord)) {
    const endingsOf = b => (b && b.endings && b.endings.length ? b.endings : null);
    let want = endingsOf(bars[bi]); // 这一遍弹的是第几房子（还不知道时是 null）
    for (let i = bi - 1; i >= 0; i--) {
      const e = endingsOf(bars[i]);
      if (e) {
        if (!want) want = e;
        else if (!e.some(x => want.includes(x))) continue; // 别的房子：这一遍不弹
      }
      const prev = [...((bars[i] && bars[i].events) || [])].reverse().find(e => e.chord);
      if (prev) { out.push({ name: prev.chord, carried: true }); break; }
      if (bars[i] && bars[i].repeat_start) want = null;
    }
  }
  for (const e of evs) if (e.chord && (!out.length || out[out.length - 1].name !== e.chord)) out.push({ name: e.chord, carried: false });
  return out;
}

/** 一个和弦图（SVG）：上面是名字，格子 5 品，左边标起始品位（高把位时），弦上方 × = 不弹、○ = 空弦。 */
export function chordSVG(name, frets) {
  const W = 64, H = 84, x0 = 12, gap = 8, top = 26, fh = 10, nF = 5;
  const played = (frets || []).filter(f => f > 0);
  const base = played.length && Math.max(...played) > nF ? Math.min(...played) : 1;
  const xs = s => x0 + (STRINGS - s) * gap; // 6 弦在最左
  const parts = [`<text x="${W / 2}" y="11" text-anchor="middle" class="cd-name">${esc(name)}</text>`];
  for (let s = 1; s <= STRINGS; s++) parts.push(`<line x1="${xs(s)}" y1="${top}" x2="${xs(s)}" y2="${top + nF * fh}"/>`);
  for (let k = 0; k <= nF; k++) {
    parts.push(`<line x1="${xs(STRINGS)}" y1="${top + k * fh}" x2="${xs(1)}" y2="${top + k * fh}"${k === 0 && base === 1 ? ' class="cd-nut"' : ''}/>`);
  }
  if (base > 1) parts.push(`<text x="${xs(STRINGS) - 4}" y="${top + fh - 2}" text-anchor="end" class="cd-base">${base}</text>`);
  (frets || []).forEach((f, i) => {
    const s = i + 1, x = xs(s);
    if (f < 0) parts.push(`<text x="${x}" y="${top - 4}" text-anchor="middle" class="cd-mark">×</text>`);
    else if (f === 0) parts.push(`<circle cx="${x}" cy="${top - 7}" r="2.6" class="cd-open"/>`);
    else parts.push(`<circle cx="${x}" cy="${top + (f - base + 0.5) * fh}" r="3.4" class="cd-dot"/>`);
  });
  return `<svg class="chord-diagram" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc(name)} 和弦图">${parts.join('')}</svg>`;
}
