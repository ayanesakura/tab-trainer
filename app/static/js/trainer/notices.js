// 主栏顶部的通知区：识别进度 / 出错、接着练、讲解、AI 备注……每条可以单独关掉。
// notice(id, spec) 显示或更新一条，notice(id, null) 去掉。顺序按 ORDER，不按出现先后。

import { $, UI } from './dom.js';

const ORDER = ['progress', 'error', 'warn', 'resume', 'explain', 'metaNote'];
const items = new Map();

/**
 * spec: { kind: 'info' | 'warn' | 'err' | '', lead: 'spinner' | 'dot' | 图标名, html,
 *         actions: [{ label, icon, primary, onClick(btn) }], onClose: 关掉时调用（不给就不能关） }
 */
export function notice(id, spec) {
  if (!spec) { if (items.delete(id)) paint(); return; }
  items.set(id, spec);
  paint();
}

function paint() {
  const host = $('notices');
  host.replaceChildren();
  const ids = [...items.keys()].sort((a, b) => ORDER.indexOf(a) - ORDER.indexOf(b));
  for (const id of ids) {
    const s = items.get(id);
    const el = document.createElement('div');
    el.className = 'notice' + (s.kind ? ' ' + s.kind : '');
    el.dataset.id = id;
    el.setAttribute('role', s.kind === 'err' ? 'alert' : 'status');
    const lead = s.lead === 'spinner' ? '<span class="spinner"></span>'
      : s.lead === 'dot' ? '<span class="dot"></span>'
        : s.lead ? `<span class="lead">${UI.icon(s.lead, 15)}</span>` : '';
    el.innerHTML = `${lead}<div class="msg">${s.html}</div>`;
    for (const a of s.actions || []) {
      const b = document.createElement('button');
      b.className = 'btn sm' + (a.primary ? ' primary' : '');
      b.innerHTML = (a.icon ? UI.icon(a.icon) : '') + a.label;
      b.onclick = () => a.onClick(b);
      el.appendChild(b);
    }
    if (s.onClose) {
      const x = document.createElement('button');
      x.className = 'icon-btn sm';
      x.title = '关闭'; x.setAttribute('aria-label', '关闭这条通知');
      x.innerHTML = UI.icon('x');
      x.onclick = () => { items.delete(id); paint(); s.onClose(); };
      el.appendChild(x);
    }
    host.appendChild(el);
  }
  host.hidden = !ids.length;
}
