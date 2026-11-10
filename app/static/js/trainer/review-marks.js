// 谱面上的小节标记：在这一小节的位置画一层框（alphaTab 只给每行第一个小节标号，染小节号不够用）。
// 待核对的淡橙色；精修改过的淡绿色（两样都有时显示待核对）；正在精修某个小节 / 某一行时，还没核对完的小节加一圈闪动的框和转圈。

import { get, on } from './store.js';
import { $ } from './dom.js';
import { barBox } from './player.js';

function markReviewBars() {
  let layer = $('warnLayer');
  if (!layer) { layer = document.createElement('div'); layer.id = 'warnLayer'; $('score').appendChild(layer); }
  layer.replaceChildren();
  const v = get('view'), s = get('song');
  if (!v || !s) return;
  const a = s.activity, pending = new Set(a && a.kind === 'refine' && a.scope !== 'song' ? a.pending || [] : []);
  const box = (i, cls, title) => {
    const b = barBox(i);
    if (!b) return null;
    const el = document.createElement('div');
    el.className = cls;
    Object.assign(el.style, { left: b.x + 'px', top: b.y + 'px', width: b.w + 'px', height: b.h + 'px' });
    el.title = title;
    layer.appendChild(el);
    return el;
  };
  v.review.forEach((need, i) => {
    const sb = s.bars[i];
    if (need) box(i, 'warn-mark', `第 ${i + 1} 小节待核对`);
    else if (sb && sb.refine) box(i, 'refine-mark', `第 ${i + 1} 小节精修改过`);
    if (sb && pending.has(sb.id)) {
      const el = box(i, 'refining-mark', `第 ${i + 1} 小节精修中`);
      if (el) el.innerHTML = '<span class="spinner"></span>';
    }
  });
}

export function initReviewMarks() {
  on('ui.renderGen', markReviewBars);
  on('view', markReviewBars); // 确认没问题只改了 review，谱面不重画：这里也要重画一次，不然黄框留着
  on('song.activity', markReviewBars); // 精修开始、核对完一个小节
}
