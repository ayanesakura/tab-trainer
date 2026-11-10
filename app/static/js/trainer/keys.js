// 练习页的键盘快捷键：只有这一个 keydown 监听，判断条件都在这里，各模块用 addShortcuts 登记自己的一组。
//
// 一组快捷键：{name, order（小的先问）, when()（现在归不归它管）, keys: {按键: fn(e)}, layer（浮层开着时也管——
// 只给浮层自己的那组用，比如原谱弹窗里的 ←→）, mod（要按着 ⌘ / Ctrl：撤销）}。按键先按 e.code 找（'Space'、'KeyM'、
// 'Home'、'ArrowLeft'），找不到再按 e.key（'z'：不同键盘布局下 ⌘Z 都认）。fn 返回 false 表示不处理、让给下一组；
// 处理了的按键都 preventDefault。
//
// 统一的判断（任何一组都先过这些）：
//   - 别的监听已经处理过（e.defaultPrevented）：顶栏标签页的 ←→、起止小节输入框里的 ↑↓、通知里折叠备注的空格……
//   - 输入法正在组字（isComposing）
//   - 焦点在打字的地方（文字输入框、多行文本、下拉框、可编辑区域）：什么都不抢
//   - 焦点在方向键有自己用处的控件上（滑块、标签页、分段开关、菜单）：方向键、Home / End 留给控件
//   - 有浮层开着（UI.hasLayer）：只问标了 layer 的组
//   - 不在练习页（ui.view）：都不管
//   - 修饰键：普通快捷键不能按着 ⌘ / Ctrl / Alt；mod 组要按着 ⌘ 或 Ctrl

import { get } from './store.js';
import { UI } from './dom.js';

const scopes = [];

const TYPING = 'textarea, select, [contenteditable]:not([contenteditable="false"]), input:not([type=range]):not([type=checkbox]):not([type=radio]):not([type=button]):not([type=submit]):not([type=reset])';
const ARROW_OWNER = 'input[type=range], [role=tab], [role=tablist], [role=menu], [role=menuitem], [role=radiogroup], [role=listbox], .seg';
const ARROWS = new Set(['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown']);

/** 焦点在打字的地方 */
export const isTyping = el => !!(el && el.closest && el.closest(TYPING));
/** 方向键（以及 Home / End）这时属于焦点所在的控件 */
export const ownsArrows = el => !!(el && el.closest && el.closest(ARROW_OWNER));

/** 登记一组快捷键；返回取消的函数 */
export function addShortcuts(scope) {
  const s = { order: 50, layer: false, mod: false, when: () => true, ...scope };
  scopes.push(s);
  scopes.sort((x, y) => x.order - y.order);
  return () => { const i = scopes.indexOf(s); if (i >= 0) scopes.splice(i, 1); };
}

const keyFn = (s, e) => s.keys[e.code] || s.keys[e.key];

/** 这一下按键可以交给哪几组（按顺序） */
function candidates(e) {
  if (e.defaultPrevented || e.isComposing || get('ui.view') !== 'practice') return [];
  const t = e.target;
  if (isTyping(t) || (ARROWS.has(e.code) && ownsArrows(t))) return [];
  const layer = UI.hasLayer(), mod = e.metaKey || e.ctrlKey;
  return scopes.filter(s => (!layer || s.layer) && (s.mod ? mod : !mod && !e.altKey) && keyFn(s, e) && s.when());
}

/** 给测试和调试用：这一下按键现在会交给哪一组（不执行）；没人管是 null */
export const route = e => { const c = candidates(e)[0]; return c ? c.name : null; };

function onKey(e) {
  for (const s of candidates(e)) {
    if (keyFn(s, e)(e) === false) continue;
    e.preventDefault();
    return;
  }
}

export function initKeys() {
  document.addEventListener('keydown', onKey);
}
