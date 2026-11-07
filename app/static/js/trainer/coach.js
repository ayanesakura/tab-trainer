// 小节 AI 教练：针对正在显示的小节向 AI 提问，问答记在小节上

let asking = null; // 正在等回答的小节 id
function renderCoach() {
  if (shownBar < 0 || !SONG || !SONG.bars[shownBar]) return;
  const bar = SONG.bars[shownBar], qa = bar.qa || [];
  $('qaList').innerHTML = qa.map(x => `<div class="qa${x.stale ? ' stale' : ''}"><div class="q">${UI.icon('sparkle', 13)}<span>${esc(x.q)}</span>${x.stale ? '<span class="old" title="这一小节后来改过音，回答说的可能是改之前的谱">改谱前</span>' : ''}<span class="t">${esc(x.at || '')}</span></div><div class="a">${esc(x.a)}</div></div>`).join('');
  $('qaList').hidden = !qa.length;
  $('qaList').scrollTop = $('qaList').scrollHeight;
  const busy = !!asking, here = asking === bar.id;
  $('askBtn').disabled = busy;
  const otherN = busy && !here ? (SONG.bars.find(b => b.id === asking) || {}).n || '' : '';
  $('askBtn').title = otherN ? `正在回答第 ${otherN} 小节的问题，回答完再问` : '';
  setButton($('askBtn'), here ? '<span class="spinner"></span>思考中…' : busy ? `<span class="spinner"></span>等第 ${otherN} 小节的回答` : UI.icon('sparkle') + '问 AI');
}
async function askBar() {
  if (shownBar < 0 || asking) return;
  const q = $('askInput').value.trim();
  if (!q) { $('askInput').focus(); return; }
  const bar = SONG.bars[shownBar];
  asking = bar.id; renderCoach();
  try {
    const r = await fetch(`/api/songs/${SONG_ID}/bars/${encodeURIComponent(bar.id)}/ask`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ question: q }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { UI.toast(d.detail || '提问失败', 'err', 6000); return; }
    const b = SONG.bars.find(x => x.id === bar.id);
    if (b) b.qa = d.qa;
    // 还停在这个小节、输入框里也还是刚才的问题时才清空（换到别的小节新打的字不能丢）
    if (SONG.bars[shownBar] && SONG.bars[shownBar].id === bar.id && $('askInput').value.trim() === q) $('askInput').value = '';
  } finally {
    asking = null; renderCoach();
  }
}
function initCoach() {
  $('askBtn').onclick = askBar;
  $('askInput').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); askBar(); } });
}
