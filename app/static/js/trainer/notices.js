// 主栏顶部的通知区：识别进度 / 出错、接着练、讲解、AI 备注……一条一个 UI.notice，可以逐条关掉。
// notice(id, spec) 显示或更新一条，notice(id, null) 去掉。顺序按 ORDER，不按出现先后。

import { $, UI } from './dom.js';

const ORDER = ['progress', 'offline', 'error', 'resume', 'explain', 'metaNote'];
const els = new Map();

/** spec：UI.notice 的参数 {kind, html, actions, onDismiss}，另外 spinner: true 时圆点换成转圈 */
export function notice(id, spec) {
  const host = $('notices');
  let el = els.get(id);
  if (!spec) {
    if (el) { el.remove(); els.delete(id); }
    host.hidden = !els.size;
    return;
  }
  if (!el) {
    el = document.createElement('div');
    el.dataset.id = id;
    els.set(id, el);
    const after = [...els.keys()].filter(k => ORDER.indexOf(k) > ORDER.indexOf(id)).map(k => els.get(k)).find(x => x.isConnected);
    host.insertBefore(el, after || null);
  }
  UI.notice(el, { ...spec, onDismiss: spec.onDismiss && (() => { els.delete(id); el.remove(); host.hidden = !els.size; spec.onDismiss(); }) });
  if (spec.spinner) el.querySelector('.dot').outerHTML = '<span class="spinner" aria-hidden="true"></span>';
  host.hidden = false;
}
