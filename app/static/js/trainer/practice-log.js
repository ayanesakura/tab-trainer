// 练习记录：每弹完一遍记一次（循环的小节、速度、用时），停下或换段时把零头时间也记上；
// 小节详情里显示这一小节的记录。

import { get, set, on } from './store.js';
import { $, SONG_ID } from './dom.js';
import { practiceText } from './view.js';

const practice = { ids: [], t: 0 };
function start() {
  const { a, b } = get('play');
  practice.ids = get('song').bars.slice(a, b + 1).map(x => x.id);
  practice.t = performance.now();
}
function flush(loopDone) {
  if (!practice.t || !practice.ids.length) return;
  const now = performance.now(), seconds = (now - practice.t) / 1000, ids = practice.ids;
  practice.t = get('play.playing') ? now : 0;
  if (!get('play.playing')) practice.ids = [];
  if (!loopDone && seconds < 2) return; // 点一下就停的不算
  const body = JSON.stringify({ bar_ids: ids, loops: loopDone ? 1 : 0, bpm: get('play.bpm'), seconds: Math.round(seconds) });
  fetch(`/api/songs/${SONG_ID}/practice`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body, keepalive: true })
    .then(r => (r.ok ? r.json() : null)).then(p => { if (p) set('song.practice', p); }).catch(() => {});
}

function renderLine() {
  const bi = get('ui.bar'), s = get('song');
  if (bi < 0 || !s || !s.bars[bi]) return;
  const p = ((s.practice || {}).bars || {})[s.bars[bi].id];
  $('practiceLine').textContent = practiceText(p);
  $('practiceLine').classList.toggle('none', !p);
}

export function initPracticeLog() {
  on('play.playing', (now, was) => { if (now && !was) start(); else if (!now && was) flush(false); });
  // 换段：先把上一段弹了的时间记下来，再从新的一段开始计时
  on(['play.a', 'play.b'], () => { if (get('play.playing')) { flush(false); start(); } });
  on('play.loops', (n, old) => { if (n > old) flush(true); });
  on(['ui.bar', 'song.practice', 'song.bars'], renderLine);
  addEventListener('pagehide', () => { if (get('play.playing')) flush(false); });
}
