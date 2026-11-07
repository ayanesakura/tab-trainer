// 练习页的 DOM 小工具。共用的图标、轻提示、分段开关等在 /static/ui.js（window.UI）。

export const UI = window.UI;
export const $ = id => document.getElementById(id);
export const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
export const SONG_ID = new URLSearchParams(location.search).get('id');

/** 按钮内容由代码管理时：去掉 data-ic，免得页面加载时 UI.hydrate 再补一个图标 */
export function setButton(el, html) { el.removeAttribute('data-ic'); el.innerHTML = html; }

/** 请求后端：出错时抛出带中文说明的 Error（后端的 detail） */
export async function request(method, url, body) {
  const opt = { method };
  if (body !== undefined) { opt.headers = { 'Content-Type': 'application/json' }; opt.body = JSON.stringify(body); }
  let r;
  try { r = await fetch(url, opt); } catch { throw new Error('连不上本机服务，确认它还在运行'); }
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(d.detail || `请求失败（${r.status}）`); e.status = r.status; throw e; }
  return d;
}

/** 按钮点下去到请求结束一直禁用（出错也会恢复），防止重复提交 */
export async function withBusy(btn, fn) {
  if (btn.disabled) return undefined;
  btn.disabled = true;
  try { return await fn(); } finally { btn.disabled = false; }
}
