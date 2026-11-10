// tab-trainer 共用的界面工具（ES 模块）：$ / esc、线条图标、主题、轻提示、浮层栈（菜单、确认框、输入框）、通知条、分段开关、滑块填充。
// 用法：import { UI, $, esc } from '/static/ui.js'（也有逐个的具名导出）。
// 暂时还挂一份 window.UI 给没改成模块的老脚本用，练习页改完后删掉。

import { esc, icon } from './js/lib/html.js'; // 纯函数（Node 测试也能用），这里原样再导出

export const $ = id => document.getElementById(id);
export { esc, icon };

// ---------------------------------------------------------------------
// 图标（lucide 风格的线条图标，路径和 icon() 在 js/lib/html.js）
// ---------------------------------------------------------------------
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

// 配色：和亮 / 暗无关的另一个设置（记在本机），写在 <html data-palette>；CSS 变量在 ui.css 里按配色覆盖。
// 变了同样发 themechange（谱面要按新的颜色重画）。
const PALETTE_KEY = 'tt-palette';
export const PALETTES = [
  { id: 'indigo', name: '靛蓝', swatch: '#4f46e5' },
  { id: 'walnut', name: '胡桃木', swatch: '#8a5328' },
  { id: 'pine', name: '松绿', swatch: '#2f6a50' },
  { id: 'cinnabar', name: '朱砂', swatch: '#a8412b' },
];
export const getPalette = () => {
  let p = null;
  try { p = localStorage.getItem(PALETTE_KEY); } catch { /* 读不了就用默认 */ }
  return PALETTES.some(x => x.id === p) ? p : 'indigo';
};
const applyPalette = () => { document.documentElement.dataset.palette = getPalette(); };
export const setPalette = p => {
  try { localStorage.setItem(PALETTE_KEY, p); } catch { /* 只在这次打开时生效 */ }
  applyPalette();
  themeChanged();
};
applyPalette();
// 别的标签页（比如开着的练习页）改了外观或配色：这里跟着变
addEventListener('storage', e => {
  if (e.key !== THEME_KEY && e.key !== PALETTE_KEY) return;
  applyTheme(); applyPalette(); themeChanged();
});

// 外观只在设置页（设置 › 外观 › 明暗）改，顶栏不再有切换按钮。下面的 themeButton / initShell 只是兼容：
// 页面上还有 #themeBtn（比如没更新的练习页）时把它接上（跟随系统 → 浅色 → 深色 → 跟随系统），没有就什么都不做。
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

// 两个页面共同的顶栏（现在没有需要初始化的东西；有旧的外观按钮就接上）
export function initShell() {
  const btn = $('themeBtn');
  if (btn) themeButton(btn);
}

// ---------------------------------------------------------------------
// 轻提示
// ---------------------------------------------------------------------
// 有模态对话框开着时，对话框外面的内容被遮罩压暗、对读屏软件也不可见（inert）：提示要放进最上面那个模态对话框里，
// 并用 popover 放进顶层（top layer），盖在对话框和遮罩上面。对话框打开 / 关上时 placeToast() 重新安放还在显示的提示。
let toastTimer;
const topModal = () => {
  for (let i = stack.length - 1; i >= 0; i--) { const { el, modal } = stack[i]; if (modal && el.tagName === 'DIALOG' && el.open) return el; }
  const open = [...document.querySelectorAll('dialog')].filter(d => d.open && d.matches(':modal'));
  return open[open.length - 1] || null; // 不是经 layer() 打开的模态对话框
};
function placeToast(el = document.querySelector('.toast')) {
  if (!el) return;
  const host = topModal() || document.body;
  if (el.parentNode !== host) host.appendChild(el); // 挪动会把已显示的 popover 收起，下面重新显示
  if (el.showPopover && !el.matches(':popover-open')) { try { el.showPopover(); } catch { /* 不支持 popover 时退回普通 fixed 定位 */ } }
}
export function toast(msg, kind = 'ok', ms = 2200) {
  document.querySelectorAll('.toast').forEach(t => t.remove());
  const el = document.createElement('div');
  el.className = 'toast ' + kind; el.textContent = msg;
  el.setAttribute('role', kind === 'err' ? 'alert' : 'status');
  el.popover = 'manual';
  placeToast(el);
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
    if (modal) placeToast(); // 还在显示的提示挪到新对话框里、盖在它上面
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
    if (el.tagName === 'DIALOG' && el.querySelector('.toast')) placeToast(); // 先挪出来（onClose 可能删掉对话框），关上后提示还在
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
  el.innerHTML = `<span class="dot ${kind || 'plain'}"></span><div class="n-body">${opts.html ?? ''}</div>`
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
  PALETTES, getPalette, setPalette,
  layer, hasLayer, topLayer, menu, closeMenu, confirm, prompt, notice, seg, tabs, fillRange, fillAll,
};
export default UI;
window.UI = UI; // 兼容：练习页改成模块后删掉
