// 小节 AI 教练：针对正在看的小节向 AI 提问，问答记在小节上。

import { get, set, on } from './store.js';
import { $, UI, esc, setButton, songUrl } from './dom.js';
import { post } from '../lib/api.js';

let asking = null; // 正在等回答的小节 id

function render() {
  const bi = get('ui.bar'), s = get('song');
  if (bi < 0 || !s || !s.bars[bi]) return;
  const bar = s.bars[bi], qa = bar.qa || [];
  $('qaList').innerHTML = qa.map(x => `<div class="qa${x.stale ? ' stale' : ''}"><div class="q">${UI.icon('sparkle', 13)}<span>${esc(x.q)}</span>${x.stale ? '<span class="old" title="这一小节后来改过音，回答说的可能是改之前的谱">改谱前</span>' : ''}<span class="t">${esc(x.at || '')}</span></div><div class="a">${esc(x.a)}</div></div>`).join('');
  $('qaList').hidden = !qa.length;
  $('qaList').scrollTop = $('qaList').scrollHeight;
  const busy = !!asking, here = asking === bar.id;
  $('askBtn').disabled = busy;
  const otherN = busy && !here ? (s.bars.find(b => b.id === asking) || {}).n || '' : '';
  $('askBtn').title = otherN ? `正在回答第 ${otherN} 小节的问题，回答完再问` : '';
  setButton($('askBtn'), here ? '<span class="spinner"></span>思考中…' : busy ? `<span class="spinner"></span>等第 ${otherN} 小节` : UI.icon('sparkle') + '问 AI');
}

async function ask() {
  const bi = get('ui.bar');
  if (bi < 0 || asking) return;
  const q = $('askInput').value.trim();
  if (!q) { $('askInput').focus(); return; }
  const bar = get('song').bars[bi];
  asking = bar.id; render();
  try {
    const d = await post(songUrl(`/bars/${encodeURIComponent(bar.id)}/ask`), { question: q });
    // 回答写进了这个小节的 qa：并到 store 里（不重新取整首曲子）。小节的 rev / 曲子的 meta_rev 不因问答变化，
    // 后端如果顺带给了新值也一起并进来，下一次改谱带的版本号才是最新的
    const k = get('song').bars.findIndex(x => x.id === bar.id);
    if (k >= 0) {
      const patch = { [`song.bars.${k}.qa`]: d.qa };
      if (Number.isInteger(d.bar_rev)) patch[`song.bars.${k}.rev`] = d.bar_rev;
      if (Number.isInteger(d.rev)) patch['song.rev'] = d.rev;
      set(patch);
    }
    // 还停在这个小节、输入框里也还是刚才的问题时才清空（换到别的小节新打的字不能丢）
    const cur = get('song').bars[get('ui.bar')];
    if (cur && cur.id === bar.id && $('askInput').value.trim() === q) $('askInput').value = '';
  } catch (e) {
    UI.toast(e.message || '提问失败', 'err', 6000);
  } finally {
    asking = null; render();
  }
}

export function initCoach() {
  $('askBtn').onclick = ask;
  $('askInput').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); ask(); } });
  on(['ui.bar', 'song.bars'], render);
}
