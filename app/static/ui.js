// tab-trainer 共用的界面工具（ES 模块）：$ / esc、线条图标、主题、轻提示、浮层栈（菜单、确认框、输入框）、通知条、分段开关、滑块填充。
// 用法：import { UI, $, esc } from '/static/ui.js'（也有逐个的具名导出）。
// 暂时还挂一份 window.UI 给没改成模块的老脚本用，练习页改完后删掉。

export const $ = id => document.getElementById(id);
export const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// ---------------------------------------------------------------------
// 图标（lucide 风格的线条图标）
// ---------------------------------------------------------------------
const P = {
  logo: '<path d="M12 21.5c-3.2-3.1-8-9.2-8-13.2C4 5 7.6 2.5 12 2.5s8 2.5 8 5.8c0 4-4.8 10.1-8 13.2Z"/><path d="M9 9.5c.8-1 1.8-1.5 3-1.5s2.2.5 3 1.5"/>',
  play: '<path d="M7 4.5v15a1 1 0 0 0 1.5.86l12.5-7.5a1 1 0 0 0 0-1.72L8.5 3.64A1 1 0 0 0 7 4.5Z" fill="currentColor" stroke="none"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" stroke="none"/>',
  repeat: '<path d="m17 2 4 4-4 4"/><path d="M3 11v-1a4 4 0 0 1 4-4h14"/><path d="m7 22-4-4 4-4"/><path d="M21 13v1a4 4 0 0 1-4 4H3"/>',
  edit: '<path d="M21.17 6.81a1 1 0 0 0-3.99-3.99L3.84 16.17a2 2 0 0 0-.5.83l-1.32 4.35a.5.5 0 0 0 .62.62l4.35-1.32a2 2 0 0 0 .83-.5z"/>',
  refresh: '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
  upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
  trash: '<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
  left: '<path d="m15 18-6-6 6-6"/>',
  right: '<path d="m9 18 6-6-6-6"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
  moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
  monitor: '<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
  mic: '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><path d="M12 19v3"/>',
  ear: '<path d="M3 14h3a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-7a9 9 0 0 1 18 0v7a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3"/>',
  volume: '<path d="M11 5 6 9H2v6h4l5 4V5Z"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>',
  gauge: '<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>',
  settings: '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M2 14h4M10 8h4M18 16h4"/>',
  music: '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4M12 17h.01"/>',
  plus: '<path d="M5 12h14M12 5v14"/>',
  minus: '<path d="M5 12h14"/>',
  more: '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
  key: '<circle cx="7.5" cy="15.5" r="5.5"/><path d="m21 2-9.6 9.6M15.5 7.5l3 3L22 7l-3-3"/>',
  book: '<path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1 0-5H20"/>',
  list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  tuning: '<path d="M4 4v16M9 4v16M14 4v16M19 4v16"/><circle cx="9" cy="9" r="1.6" fill="currentColor"/><circle cx="14" cy="14" r="1.6" fill="currentColor"/>',
  sparkle: '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9Z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8Z"/>',
  back: '<path d="M19 12H5M12 19l-7-7 7-7"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-5-5L5 21"/>',
  locate: '<circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="2"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6"/><path d="M8 13h8M8 17h5"/>',
  score: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6"/><path d="M11 18v-6l4-1"/><circle cx="9.5" cy="18" r="1.5"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
};
export const icon = (name, size) => `<span class="ic" aria-hidden="true"${size ? ` style="width:${size}px;height:${size}px"` : ''}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${P[name] || ''}</svg></span>`;
// 把 data-ic="名字" 的元素补上图标（插在最前面），补完去掉 data-ic
export const hydrate = (root = document) => root.querySelectorAll('[data-ic]').forEach(el => {
  el.insertAdjacentHTML('afterbegin', icon(el.dataset.ic, el.dataset.icSize));
  el.removeAttribute('data-ic');
});

// ---------------------------------------------------------------------
// 主题：设置是 system / light / dark（记在本机）；<html data-theme> 总是写实际生效的 light / dark，
// 所以 CSS 只需要一个暗色选择器。跟随系统时监听系统外观变化。主题变了发 themechange 事件（detail = 设置值）。
// ---------------------------------------------------------------------
const THEME_KEY = 'tt-theme';
const darkMQ = matchMedia('(prefers-color-scheme: dark)');
export const getTheme = () => {
  let t = null;
  try { t = localStorage.getItem(THEME_KEY); } catch { /* 读不了就跟随系统 */ }
  return t === 'light' || t === 'dark' ? t : 'system';
};
const effective = t => (t === 'system' ? (darkMQ.matches ? 'dark' : 'light') : t);
export const isDark = () => effective(getTheme()) === 'dark';
const applyTheme = () => { document.documentElement.dataset.theme = effective(getTheme()); };
const themeChanged = () => document.dispatchEvent(new CustomEvent('themechange', { detail: getTheme() }));
export const setTheme = t => {
  try { localStorage.setItem(THEME_KEY, t); } catch { /* 只在这次打开时生效 */ }
  applyTheme();
  themeChanged();
};
applyTheme();
darkMQ.addEventListener('change', () => { if (getTheme() === 'system') { applyTheme(); themeChanged(); } });

// 顶栏的外观按钮：跟随系统 → 浅色 → 深色 → 跟随系统，图标显示当前设置（不是当前颜色），点几下都能回到“跟随系统”
const THEME_NEXT = { system: 'light', light: 'dark', dark: 'system' };
const THEME_NAME = { system: '跟随系统', light: '浅色', dark: '深色' };
export function themeButton(btn) {
  const paint = () => {
    const t = getTheme();
    btn.innerHTML = icon({ system: 'monitor', light: 'sun', dark: 'moon' }[t]);
    btn.title = `外观：${THEME_NAME[t]}（点击切换为${THEME_NAME[THEME_NEXT[t]]}）`;
    btn.setAttribute('aria-label', btn.title);
  };
  btn.onclick = () => setTheme(THEME_NEXT[getTheme()]);
  document.addEventListener('themechange', paint);
  paint();
}

// 两个页面共同的顶栏：外观按钮
export function initShell() {
  const btn = $('themeBtn');
  if (btn) themeButton(btn);
}

// ---------------------------------------------------------------------
// 轻提示
// ---------------------------------------------------------------------
let toastTimer;
export function toast(msg, kind = 'ok', ms = 2200) {
  document.querySelectorAll('.toast').forEach(t => t.remove());
  const el = document.createElement('div');
  el.className = 'toast ' + kind; el.textContent = msg;
  el.setAttribute('role', kind === 'err' ? 'alert' : 'status');
  document.body.appendChild(el);
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el.remove(), ms);
}

// ---------------------------------------------------------------------
// 浮层栈：菜单、对话框等都登记在这里，统一处理 Esc（只关最上面一层）、点外面关闭、焦点移入和关闭后还回去。
// layer(el, {onClose(reason), modal, outside, ignore, focus}) → { el, close(reason) }
//   modal：模态（<dialog> 用 showModal；其他元素把 Tab 限制在层内），默认 false
//   outside：点层外面是否关闭，默认 true（模态 <dialog> 点遮罩算外面）
//   ignore：点到这些元素不算“外面”（比如打开菜单的按钮，由调用方自己处理开关）
//   focus：打开后把焦点放到哪个元素；默认层里第一个 [autofocus] 或可聚焦元素
// reason：'esc' | 'outside' | 'api'（调用 close() 不传）| 'replaced'（被同类浮层顶替）等
// ---------------------------------------------------------------------
const stack = [];
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type=hidden]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
const focusables = el => [...el.querySelectorAll(FOCUSABLE)].filter(x => !x.hidden && x.offsetParent !== null);

export function layer(el, { onClose, modal = false, outside = true, ignore = [], focus } = {}) {
  const prev = document.activeElement;
  const entry = { el, modal, outside, ignore, onClose, close: reason => closeLayer(entry, reason) };
  stack.push(entry);
  if (el.tagName === 'DIALOG') {
    if (!el.open) (modal ? el.showModal() : el.show());
    // Esc 在模态对话框上先触发 cancel：交给栈处理（可能还要 onClose 收尾）
    entry.onCancel = e => { e.preventDefault(); entry.close('esc'); };
    entry.onNativeClose = () => entry.close('native');
    el.addEventListener('cancel', entry.onCancel);
    el.addEventListener('close', entry.onNativeClose);
  }
  entry.prev = prev;
  const target = focus || el.querySelector('[autofocus]') || focusables(el)[0];
  if (target) target.focus({ preventScroll: true });
  else { if (!el.hasAttribute('tabindex')) el.tabIndex = -1; el.focus({ preventScroll: true }); }
  return { el, close: entry.close };
}
function closeLayer(entry, reason = 'api') {
  const i = stack.indexOf(entry);
  if (i < 0) return;
  // 上面压着的层一起关掉（从上往下）
  for (const e of stack.splice(i).reverse()) {
    const { el } = e;
    if (e.onCancel) { el.removeEventListener('cancel', e.onCancel); el.removeEventListener('close', e.onNativeClose); }
    if (el.tagName === 'DIALOG' && el.open) el.close();
    const focusInside = el.contains(document.activeElement) || document.activeElement === document.body || !document.activeElement;
    try { e.onClose && e.onClose(e === entry ? reason : 'parent'); } catch (err) { console.error(err); }
    if (focusInside && e.prev && e.prev.isConnected && e.prev.focus) e.prev.focus({ preventScroll: true });
  }
}
export const hasLayer = () => stack.length > 0;
export const topLayer = () => stack[stack.length - 1] || null;

document.addEventListener('keydown', e => {
  const top = topLayer();
  if (!top) return;
  if (e.key === 'Escape') {
    if (top.el.tagName === 'DIALOG' && top.modal) return; // 走 cancel 事件
    e.preventDefault(); e.stopPropagation();
    top.close('esc');
  } else if (e.key === 'Tab' && top.modal && top.el.tagName !== 'DIALOG') {
    const f = focusables(top.el);
    if (!f.length) { e.preventDefault(); return; }
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
}, true);
document.addEventListener('pointerdown', e => {
  const top = topLayer();
  if (!top || !top.outside) return;
  const { el } = top;
  let inside = el.contains(e.target) || top.ignore.some(x => x && x.contains(e.target));
  if (inside && el.tagName === 'DIALOG' && e.target === el) { // 点到模态对话框的遮罩：目标是 dialog 自己
    const r = el.getBoundingClientRect();
    inside = e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
  }
  if (!inside) top.close('outside');
}, true);

// ---------------------------------------------------------------------
// 弹出菜单：items = [{label, icon, danger, onClick} | 'sep' | false]。再点一次同一个按钮就收起。
// ↑ ↓ 在菜单项之间移动，Esc / 点外面 / 滚动关闭。
// ---------------------------------------------------------------------
let openMenu = null; // { layer, anchor }
export function closeMenu() { if (openMenu) openMenu.layer.close('api'); }
export function menu(anchor, items) {
  if (openMenu && openMenu.anchor === anchor) { closeMenu(); return; }
  closeMenu();
  const m = document.createElement('div');
  m.className = 'menu'; m.setAttribute('role', 'menu');
  for (const it of items) {
    if (!it) continue;
    if (it === 'sep') { m.appendChild(document.createElement('hr')); continue; }
    const b = document.createElement('button');
    b.setAttribute('role', 'menuitem');
    if (it.danger) b.className = 'danger';
    b.innerHTML = (it.icon ? icon(it.icon) : '') + `<span>${esc(it.label)}</span>`;
    b.onclick = e => { e.stopPropagation(); closeMenu(); it.onClick(); };
    m.appendChild(b);
  }
  m.addEventListener('keydown', e => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    const bs = [...m.querySelectorAll('button')], i = bs.indexOf(document.activeElement);
    bs[(i + (e.key === 'ArrowDown' ? 1 : -1) + bs.length) % bs.length].focus();
  });
  document.body.appendChild(m);
  const r = anchor.getBoundingClientRect(), w = m.offsetWidth;
  m.style.top = Math.max(8, Math.min(innerHeight - m.offsetHeight - 8, r.bottom + 4)) + 'px';
  m.style.left = Math.max(8, Math.min(innerWidth - w - 8, r.right - w)) + 'px';
  anchor.setAttribute('aria-expanded', 'true');
  const onScroll = e => { if (!m.contains(e.target)) closeMenu(); };
  const state = { anchor };
  state.layer = layer(m, {
    ignore: [anchor],
    onClose: () => {
      m.remove(); removeEventListener('scroll', onScroll, true);
      anchor.setAttribute('aria-expanded', 'false');
      if (openMenu === state) openMenu = null;
    },
  });
  addEventListener('scroll', onScroll, true);
  openMenu = state;
}

// ---------------------------------------------------------------------
// 对话框：确认 / 输入一行字，替代原生 confirm() / prompt()，样式跟界面一致
// confirm(msg, {title, ok, cancel, danger}) → Promise<boolean>
// prompt(label, value, {title, ok, placeholder, hint}) → Promise<string | null>（取消为 null）
// ---------------------------------------------------------------------
let dlgN = 0;
function dialog({ title, text, input, ok = '确定', cancel = '取消', danger = false }) {
  return new Promise(resolve => {
    const d = document.createElement('dialog'), tid = 'dlgT' + ++dlgN;
    d.className = 'dlg dlg-msg';
    d.setAttribute('aria-labelledby', tid);
    d.innerHTML = `<form method="dialog">
      <div class="dlg-b">${title ? `<div class="dlg-t" id="${tid}">${esc(title)}</div>` : ''}
        ${text ? `<div class="dlg-text"${title ? '' : ` id="${tid}"`}>${esc(text)}</div>` : ''}
        ${input ? `<input class="input" name="v" autocomplete="off" aria-label="${esc(input.label || title || text)}" placeholder="${esc(input.placeholder || '')}">${input.hint ? `<div class="hint">${esc(input.hint)}</div>` : ''}` : ''}
      </div>
      <div class="dlg-f"><button type="button" class="btn ghost" data-cancel>${esc(cancel)}</button><button class="btn primary${danger ? ' danger' : ''}" data-ok>${esc(ok)}</button></div>
    </form>`;
    const field = d.querySelector('input');
    if (field) field.value = input.value ?? '';
    let result = input ? null : false;
    document.body.appendChild(d);
    const lay = layer(d, {
      modal: true,
      focus: field || d.querySelector(danger ? '[data-cancel]' : '[data-ok]'),
      onClose: () => { d.remove(); resolve(result); },
    });
    if (field) field.select();
    d.querySelector('form').onsubmit = e => { e.preventDefault(); result = field ? field.value : true; lay.close('ok'); };
    d.querySelector('[data-cancel]').onclick = () => lay.close('cancel');
  });
}
export const confirm = (msg, { title, ok, cancel, danger } = {}) => dialog({ title, text: msg, ok, cancel, danger });
export const prompt = (label, value = '', { title, ok, placeholder, hint } = {}) =>
  dialog({ title: title || label, text: title ? label : '', input: { label, value, placeholder, hint }, ok });

// ---------------------------------------------------------------------
// 通知条：notice(el, {kind, html, actions, onDismiss}) 把 el 填成一条通知；notice(el, null) 隐藏。
//   kind：'info' | 'warn' | 'err'（'error' 同 'err'）| 'ok' | ''（普通）
//   html：正文（调用方负责转义）
//   actions：[{label, icon, primary, onClick}]，按钮放在右边
//   onDismiss：给了就加一个关闭按钮，点了先隐藏再调用
// ---------------------------------------------------------------------
export function notice(el, opts) {
  if (!opts) { el.hidden = true; el.innerHTML = ''; return el; }
  const kind = opts.kind === 'error' ? 'err' : opts.kind || '';
  el.className = `notice ${kind}`.trim();
  el.setAttribute('role', kind === 'err' ? 'alert' : 'status');
  const acts = opts.actions || [];
  el.innerHTML = `<span class="dot ${kind || 'info'}"></span><div class="n-body">${opts.html ?? ''}</div>`
    + (acts.length || opts.onDismiss ? `<div class="n-acts">${acts.map((a, i) => `<button class="btn sm${a.primary ? ' primary' : ' ghost'}" data-act="${i}">${a.icon ? icon(a.icon, 13) : ''}${esc(a.label)}</button>`).join('')}${opts.onDismiss ? `<button class="icon-btn sm" data-dismiss title="关闭" aria-label="关闭">${icon('x')}</button>` : ''}</div>` : '');
  el.querySelectorAll('[data-act]').forEach(b => { b.onclick = () => acts[+b.dataset.act].onClick(b); });
  const x = el.querySelector('[data-dismiss]');
  if (x) x.onclick = () => { el.hidden = true; opts.onDismiss(); };
  el.hidden = false;
  return el;
}

// ---------------------------------------------------------------------
// 分段开关：<div class="seg"><button data-v="x">…</button></div>
// seg(el, value, onChange) → set(value)。按钮带 aria-pressed，容器是 role=group（给容器加 aria-label 说明是什么选项）。
// tabs(el, value, onChange) → set(value)：同样的样子，但用 tablist / tab / aria-selected 语义，←→ 切换（顶栏导航用）。
// ---------------------------------------------------------------------
export function seg(el, value, onChange) {
  if (!el.hasAttribute('role')) el.setAttribute('role', 'group');
  const set = v => {
    el.dataset.value = v;
    el.querySelectorAll('button[data-v]').forEach(b => { const on = b.dataset.v === v; b.classList.toggle('on', on); b.setAttribute('aria-pressed', String(on)); });
  };
  set(value);
  el.onclick = e => { const b = e.target.closest('button[data-v]'); if (!b || b.dataset.v === el.dataset.value) return; set(b.dataset.v); onChange && onChange(b.dataset.v); };
  return set;
}
export function tabs(el, value, onChange) {
  el.setAttribute('role', 'tablist');
  const btns = () => [...el.querySelectorAll('button[data-v]')];
  const set = v => {
    el.dataset.value = v;
    btns().forEach(b => { const on = b.dataset.v === v; b.setAttribute('role', 'tab'); b.classList.toggle('on', on); b.setAttribute('aria-selected', String(on)); b.tabIndex = on ? 0 : -1; });
  };
  set(value);
  const pick = b => { if (!b || b.dataset.v === el.dataset.value) return; set(b.dataset.v); onChange && onChange(b.dataset.v); };
  el.onclick = e => pick(e.target.closest('button[data-v]'));
  el.onkeydown = e => {
    const bs = btns(), i = bs.findIndex(b => b.dataset.v === el.dataset.value);
    const j = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: bs.length - 1 }[e.key];
    if (j === undefined) return;
    e.preventDefault();
    const b = bs[(j + bs.length) % bs.length];
    pick(b); b.focus();
  };
  return set;
}

// ---------------------------------------------------------------------
// 滑块已填充部分的颜色（WebKit 没有 ::range-progress）
// ---------------------------------------------------------------------
export const fillRange = el => { const p = (el.value - el.min) / (el.max - el.min) * 100; el.style.setProperty('--p', p + '%'); };
export const fillAll = (root = document) => root.querySelectorAll('input[type=range]').forEach(fillRange);
document.addEventListener('input', e => { if (e.target.matches && e.target.matches('input[type=range]')) fillRange(e.target); });

// 模块在文档解析完后才执行：这时直接补图标；万一被同步加载，就等 DOMContentLoaded
const ready = () => { hydrate(); fillAll(); };
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready); else ready();

export const UI = {
  $, esc, icon, hydrate, getTheme, setTheme, isDark, themeButton, initShell, toast,
  layer, hasLayer, topLayer, menu, closeMenu, confirm, prompt, notice, seg, tabs, fillRange, fillAll,
};
export default UI;
window.UI = UI; // 兼容：练习页改成模块后删掉
