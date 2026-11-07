// 练习记录：每弹完一遍记一次（循环的小节、速度、用时），停下或换段时把零头时间也记上；
// 小节说明下面显示这一小节的记录，打开页面时提示上次练到哪

const practice = { ids: [], t: 0 };
function practiceStart() {
  practice.ids = SONG.bars.slice(state.a, state.b + 1).map(b => b.id);
  practice.t = performance.now();
}
function practiceFlush(loopDone) {
  if (!practice.t || !practice.ids.length) return;
  const now = performance.now(), seconds = (now - practice.t) / 1000;
  practice.t = state.playing ? now : 0;
  if (!loopDone && seconds < 2) return; // 点一下就停的不算
  const body = JSON.stringify({ bar_ids: practice.ids, loops: loopDone ? 1 : 0, bpm: state.bpm, seconds: Math.round(seconds) });
  if (!state.playing) practice.ids = [];
  fetch(`/api/songs/${SONG_ID}/practice`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body, keepalive: true })
    .then(r => (r.ok ? r.json() : null)).then(p => { if (p) { SONG.practice = p; renderPracticeLine(); } }).catch(() => {});
}
function initPracticeLog() {
  addEventListener('pagehide', () => { if (state.playing) practiceFlush(false); });
}

function renderPracticeLine() {
  if (shownBar < 0 || !SONG || !SONG.bars[shownBar]) return;
  const p = ((SONG.practice || {}).bars || {})[SONG.bars[shownBar].id];
  const mins = s => (s < 60 ? '不到 1 分钟' : `${Math.round(s / 60)} 分钟`);
  $('practiceLine').textContent = !p
    ? '还没练过这一小节'
    : p.loops
      ? `练习记录：循环 ${p.loops} 遍 · 累计 ${mins(p.seconds)}${p.best_bpm ? ` · 最快 ${p.best_bpm} BPM` : ''} · 上次 ${p.last}`
      : `练习记录：听过 ${mins(p.seconds)} · 上次 ${p.last}`;
}
let resumeShown = false;
function renderResume() {
  if (resumeShown) return;
  resumeShown = true;
  const last = (SONG.practice || {}).last;
  if (!last) return;
  const a = SONG.bars.findIndex(b => b.id === last.a), z = SONG.bars.findIndex(b => b.id === last.b);
  if (a < 0 || z < 0) return;
  const label = a === 0 && z === SONG.bars.length - 1 ? '全曲' : a === z ? `第 ${a + 1} 小节` : `第 ${a + 1}–${z + 1} 小节`;
  $('resumeNote').innerHTML = `${UI.icon('repeat', 14)}<div style="flex:1">上次（${esc(last.at)}）练到 <b>${label}</b>，${last.bpm} BPM。</div><button class="btn sm primary" id="resumeBtn">接着练</button><button class="icon-btn sm" id="resumeClose" data-ic="x" title="关闭"></button>`;
  UI.hydrate($('resumeNote'));
  $('resumeNote').style.display = '';
  $('resumeBtn').onclick = () => { setLoop(a, z); setTempo(last.bpm); $('resumeNote').style.display = 'none'; };
  $('resumeClose').onclick = () => { $('resumeNote').style.display = 'none'; };
}
