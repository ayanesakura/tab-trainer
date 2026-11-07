// 主栏顶部的通知区：识别进度 / 出错、接着练、讲解、AI 备注……一条一个 UI.notice，可以逐条关掉。
// notice(id, spec) 显示或更新一条，notice(id, null) 去掉。顺序按 ORDER，不按出现先后。
//
// 曲子每次更新（识别中每 2.5 秒轮询、每弹完一遍练习记录回来）都会重新调一遍 notice()：内容没变就什么都不动，
// 只有文字变了就只换文字，按钮留着原来的 DOM（否则按下和松开之间按钮被换掉，点击会丢、焦点会跳、withBusy 的“忙”状态也没了）。

import { $, UI } from './dom.js';

const ORDER = ['progress', 'offline', 'error', 'resume', 'explain', 'metaNote'];
const els = new Map(); // id → { el, sig, html, spec }

// 决定要不要重画按钮的部分（回调函数每次都是新的，不算；按钮上调用的总是最新一次的回调）
const shape = spec => JSON.stringify({
  kind: spec.kind || '', spinner: !!spec.spinner, dismiss: !!spec.onDismiss,
  actions: (spec.actions || []).map(a => [a.label, a.icon || '', !!a.primary]),
});

function remove(id) {
  const cur = els.get(id);
  if (cur) { cur.el.remove(); els.delete(id); }
  $('notices').hidden = !els.size;
}

/** spec：UI.notice 的参数 {kind, html, actions, onDismiss}，另外 spinner: true 时圆点换成转圈 */
export function notice(id, spec) {
  const host = $('notices');
  if (!spec) { remove(id); return; }
  let cur = els.get(id);
  if (!cur) {
    const el = document.createElement('div');
    el.dataset.id = id;
    cur = { el, sig: null, html: null, spec };
    els.set(id, cur);
    // 插在排在它后面的通知里最靠前的那一条前面（els 是按出现先后排的，不能直接取第一个）
    const rank = k => ORDER.indexOf(k);
    const next = [...els.keys()].filter(k => rank(k) > rank(id) && els.get(k).el.isConnected).sort((a, b) => rank(a) - rank(b))[0];
    host.insertBefore(el, next ? els.get(next).el : null);
  }
  cur.spec = spec; // 按钮点下去时用最新的回调
  const sig = shape(spec), html = spec.html ?? '';
  if (sig === cur.sig) {
    if (html !== cur.html) { cur.el.querySelector('.n-body').innerHTML = html; cur.html = html; }
    host.hidden = false;
    return;
  }
  const { el } = cur;
  UI.notice(el, {
    ...spec,
    actions: (spec.actions || []).map((a, i) => ({ ...a, onClick: btn => cur.spec.actions[i].onClick(btn) })),
    onDismiss: spec.onDismiss && (() => { remove(id); cur.spec.onDismiss(); }),
  });
  if (spec.spinner) el.querySelector('.dot').outerHTML = '<span class="spinner" aria-hidden="true"></span>';
  cur.sig = sig; cur.html = html;
  host.hidden = false;
}
