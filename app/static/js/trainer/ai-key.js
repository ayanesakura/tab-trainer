// 没有 API key（GET /api/settings 的 has_key 是 false）时，练习页上调用 AI 的操作都灰掉：精修整首、核对本小节 / 本行、
// 重新识别整行、问 AI、让 AI 讲解 / 补写、生成练习建议（“重试失败的 N 行”不在其中）。这些按钮都标了 data-ai（通知里的按钮用 notices.js 的 ai: true）。
// 灰掉的按钮还能点：点了说明要先填 key，可以直接去设置（曲库页的 /#settings）；悬停提示换成“先在设置里填 API key”。
// 不用 disabled：那样点了没有任何反应，也看不到为什么。key 在别的标签页填好了，回到这个页面时重新查一次。

import { get, set, on } from './store.js';
import { $, UI } from './dom.js';
import { get as apiGet } from '../lib/api.js';

export const NO_KEY = '先在设置里填 API key';
/** 能不能用 AI：还没查到时当作能（不先把按钮灰掉再变回来） */
export const hasKey = () => get('ui.hasKey') !== false;

async function check() {
  try { set('ui.hasKey', !!(await apiGet('/api/settings')).has_key); } catch { /* 查不到就保持原样 */ }
}

/** 没有 key：说明一下，可以去设置 */
export async function askForKey() {
  if (await UI.confirm('AI 的功能（识别、核对、讲解、问答）要先在设置里填 Anthropic API key。', { title: '还没有 API key', ok: '去设置' })) location.href = '/#settings';
}

function paint(v) {
  document.documentElement.classList.toggle('no-ai-key', v === false);
  $('askInput').placeholder = v === false ? NO_KEY : '如“左手指法怎么安排”';
}

export function initAiKey() {
  on('ui.hasKey', paint);
  // 点灰掉的 AI 按钮：在捕获阶段拦下（按钮自己的处理不执行），改成说明
  document.addEventListener('click', e => {
    const b = !hasKey() && e.target.closest && e.target.closest('[data-ai]');
    if (!b) return;
    e.preventDefault(); e.stopImmediatePropagation();
    askForKey();
  }, true);
  // 悬停 / 聚焦时悬停提示换成说明，离开时换回原来的（原来的提示会被别的代码随时更新，所以不长期替换）
  const swap = e => {
    const b = e.target.closest && e.target.closest('[data-ai]');
    if (!b || hasKey() || b.dataset.aiTitle !== undefined) return;
    b.dataset.aiTitle = b.title; b.title = NO_KEY;
  };
  const back = e => {
    const b = e.target.closest && e.target.closest('[data-ai]');
    if (!b || b.dataset.aiTitle === undefined || (e.relatedTarget && b.contains(e.relatedTarget))) return;
    b.title = b.dataset.aiTitle; delete b.dataset.aiTitle;
  };
  document.addEventListener('pointerover', swap); document.addEventListener('focusin', swap);
  document.addEventListener('pointerout', back); document.addEventListener('focusout', back);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) check(); });
  check();
}
