// 练习记录：每弹完一遍记一次（循环的小节、速度、用时），停下或换段时把零头时间也记上；
// 敲节奏（rhythm.running，校准那一遍除外）的时间也记成练习时间，但不算遍数（不影响“循环几遍”“最快多少 BPM”）。
// 速度是这一段里按时间加权的平均值（tempoAverage）：快弹完时才调快，不会把整遍都记成新的速度。
// 小节详情里显示这一小节的记录。

import { get, set, on } from './store.js';
import { $, songUrl } from './dom.js';
import { practiceText, tempoSpan, tempoChange, tempoAverage } from './view.js';

// 正在记的一段：{ids: 循环的小节 id, t: 开始的 performance.now(), tempo: tempoSpan, kind: 'play' | 'rhythm'}；没在练是 null
let cur = null;
// 现在在练什么：alphaTab 在放是 'play'；敲节奏在敲（不是校准）是 'rhythm'；都没有是 null
const activity = () => (get('play.playing') ? 'play' : get('rhythm.running') && !get('rhythm.calib') ? 'rhythm' : null);

function start(kind) {
  const { a, b } = get('play'), s = get('song'), now = performance.now();
  cur = { ids: s ? s.bars.slice(a, b + 1).map(x => x.id) : [], t: now, tempo: tempoSpan(now, get('play.bpm')), kind };
}

/**
 * 发一条练习记录。ids：小节 id；loops：弹完了几遍；bpm、seconds；kind：'rhythm' 表示敲节奏（后端还不认这一项时会忽略它）；
 * clean：这一遍弹得干净（以后的“干净的一遍”按键用，后端支持之前不传）。返回请求（Promise）
 */
export function recordPractice({ ids, loops = 0, bpm, seconds, kind, clean }) {
  const body = { bar_ids: ids, loops, bpm, seconds: Math.round(seconds) };
  if (kind && kind !== 'play') body.kind = kind;
  if (clean !== undefined) body.clean = !!clean;
  return fetch(songUrl('/practice'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), keepalive: true })
    .then(r => (r.ok ? r.json() : null)).then(p => { if (p) set('song.practice', p); }).catch(() => {});
}

/** 记下 cur 这一段（loopDone：刚弹完一遍）；还在练同一件事就从现在开始下一段，否则清掉 */
function flush(loopDone) {
  if (!cur) return;
  const c = cur, now = performance.now(), seconds = (now - c.t) / 1000, bpm = tempoAverage(c.tempo, now);
  cur = null;
  if (activity() === c.kind) start(c.kind);
  if (!c.ids.length || (!loopDone && seconds < 2)) return; // 点一下就停的不算
  recordPractice({ ids: c.ids, loops: loopDone && c.kind === 'play' ? 1 : 0, bpm, seconds, kind: c.kind });
}

/** 开始 / 停止播放、敲节奏：停了的那一段记下来，开始的开始计时 */
function sync() {
  const now = activity();
  if (cur && cur.kind !== now) flush(false);
  if (!cur && now) start(now);
}

function renderLine() {
  const bi = get('ui.bar'), s = get('song');
  if (bi < 0 || !s || !s.bars[bi]) return;
  const p = ((s.practice || {}).bars || {})[s.bars[bi].id];
  $('practiceLine').textContent = practiceText(p);
  $('practiceLine').classList.toggle('none', !p);
}

export function initPracticeLog() {
  on(['play.playing', 'rhythm.running', 'rhythm.calib'], sync);
  // 换段：先把上一段练了的时间记下来，再从新的一段开始计时
  on(['play.a', 'play.b'], () => { if (cur) { flush(false); sync(); } });
  on('play.bpm', bpm => { if (cur) tempoChange(cur.tempo, performance.now(), bpm); });
  on('play.loops', (n, old) => { if (n > old && cur && cur.kind === 'play') flush(true); });
  on(['ui.bar', 'song.practice', 'song.bars'], renderLine);
  addEventListener('pagehide', () => flush(false));
}
