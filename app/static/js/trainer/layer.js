// 浮层栈（改音面板、原图弹窗、“更多”弹层、窄屏的小节抽屉）：Esc 关最上面一层，点外面关闭，
// 打开时焦点移进去，关掉时还给原来的元素。键盘快捷键看 hasLayer()，有浮层时不响应。

const stack = [];

/** 打开一个浮层。opts: { onClose, outside: 点外面是否关闭（默认 true）, anchor: 点它不算“外面”, focus: 打开后聚焦的元素 } */
export function openLayer(el, opts = {}) {
  closeLayer(el, true);
  const entry = { el, opts, back: document.activeElement };
  stack.push(entry);
  el.hidden = false;
  const f = opts.focus || el.querySelector('[autofocus], input, button, [tabindex]');
  if (f) setTimeout(() => f.focus(), 0);
}

/** 关闭；silent 时不调 onClose（重复打开同一层时用） */
export function closeLayer(el, silent = false) {
  const i = stack.findIndex(x => x.el === el);
  if (i < 0) return;
  const [entry] = stack.splice(i, 1);
  el.hidden = true;
  if (!silent) {
    if (entry.opts.onClose) entry.opts.onClose();
    if (entry.back && document.contains(entry.back) && el.contains(document.activeElement)) entry.back.focus();
  }
}

export const hasLayer = () => stack.length > 0;
export const isOpen = el => stack.some(x => x.el === el);

document.addEventListener('keydown', e => {
  if (e.key !== 'Escape' || !stack.length) return;
  e.preventDefault(); e.stopPropagation();
  closeLayer(stack[stack.length - 1].el);
}, true);
document.addEventListener('mousedown', e => {
  const top = stack[stack.length - 1];
  if (!top || top.opts.outside === false) return;
  if (top.el.contains(e.target) || (top.opts.anchor && top.opts.anchor.contains(e.target))) return;
  if (top.opts.inside && top.opts.inside(e.target)) return;
  closeLayer(top.el);
}, true);
