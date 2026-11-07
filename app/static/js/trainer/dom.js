// 练习页的 DOM 小工具。共用的图标、轻提示、浮层、分段开关等在 /static/ui.js，请求后端用 /static/js/lib/api.js。

import UI, { $, esc } from '../../ui.js';

export { UI, $, esc };
export const SONG_ID = new URLSearchParams(location.search).get('id');
export const songUrl = (path = '') => `/api/songs/${encodeURIComponent(SONG_ID)}${path}`;

/** 按钮内容由代码管理时：去掉 data-ic，免得 UI.hydrate 再补一个图标 */
export function setButton(el, html) { el.removeAttribute('data-ic'); el.innerHTML = html; }

const SVGNS = 'http://www.w3.org/2000/svg';
/** 建 SVG 元素（指板、敲节奏的节奏条）：svgEl('line', {x1: 0, …}, parent) */
export function svgEl(tag, attrs = {}, parent) {
  const n = document.createElementNS(SVGNS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(n);
  return n;
}
