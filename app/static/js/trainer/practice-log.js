// 练习记录：每弹完一遍记一次（循环的小节、速度、用时），停下或换段时把零头时间也记上；
// 敲节奏（rhythm.running，校准那一遍除外）的时间也记成练习时间，但不算遍数（不影响“循环几遍”“最快多少 BPM”）。
// 速度是这一段里按时间加权的平均值（tempoAverage）：快弹完时才调快，不会把整遍都记成新的速度。
// “这遍干净”（控制条的按钮 / Enter，markClean）：标记正在弹的这一遍，弹完时这一遍的记录带 clean: true；
// 一遍刚弹完的 1.5 秒里（不超过一遍的一半，见 practice-pass.js 的 graceMs）按算刚弹完的那一遍（所以弹完的一遍晚这么久才发出去）。从段首重新开始（Home、重选这一段）时标记作废。
// 这次打开页面里用过一次以后，没标的遍都带 clean: false（后端据此把“连续干净”清零）；从没用过就不带 clean。
// 遍数、时长、干净标记的逻辑在 practice-pass.js（纯函数）。
// 小节详情里显示这一小节的记录。

import { get, set, on } from './store.js';
import { $, UI, esc, songUrl } from './dom.js';
import { practiceLines } from './view.js';
import { initialLog, step } from './practice-pass.js';

// 练到哪了（practice-pass.js 的状态）：这里只把 store 的变化翻成事件交给它，按它给的结果发记录、改 play.clean
let log = initialLog();
let graceTimer = 0;
// 现在在练什么：alphaTab 在放是 'play'；敲节奏在敲（不是校准）是 'rhythm'；都没有是 null
const activity = () => (get('play.playing') ? 'play' : get('rhythm.running') && !get('rhythm.calib') ? 'rhythm' : null);
/** 接着练的这一段：循环的小节 id 和现在的速度 */
function what(kind) {
  const { a, b } = get('play'), s = get('song');
  return { kind, ids: s ? s.bars.slice(a, b + 1).map(x => x.id) : [], bpm: get('play.bpm') };
}

const TOASTS = {
  marked: ['这一遍标为干净，弹完记下', 'ok', 1400],
  prev: ['刚弹完的那一遍标为干净', 'ok', 1400],
  idle: ['播放时按：标记正在弹的这一遍', 'info', 1800],
};
function dispatch(ev) {
  const r = step(log, { now: performance.now(), ...ev });
  log = r.state;
  for (const rec of r.send) recordPractice(rec);
  if (r.clean !== undefined) set('play.clean', r.clean);
  if (TOASTS[r.msg]) UI.toast(...TOASTS[r.msg]);
  // 刚弹完的一遍压着等宽限：到点发出去
  clearTimeout(graceTimer);
  if (log.held) graceTimer = setTimeout(() => dispatch({ type: 'tick' }), Math.max(0, log.held.until - performance.now()) + 20);
  return r;
}

/** 这遍干净：标记正在弹的这一遍（跟着谱子播放，不是敲节奏）；一遍刚弹完的宽限里按，算刚弹完的那一遍。返回标上了没有 */
export function markClean() {
  const r = dispatch({ type: 'mark' });
  return r.msg !== 'idle';
}

/**
 * 发一条练习记录。ids：小节 id；loops：弹完了几遍；bpm、seconds；kind：'rhythm' 表示敲节奏（后端还不认这一项时会忽略它）；
 * clean：这一遍干净 / 不干净（用过“这遍干净”才传，见 practice-pass.js）。返回请求（Promise）
 */
export function recordPractice({ ids, loops = 0, bpm, seconds, kind, clean }) {
  const body = { bar_ids: ids, loops, bpm, seconds: Math.round(seconds) };
  if (kind && kind !== 'play') body.kind = kind;
  if (clean !== undefined) body.clean = !!clean;
  return fetch(songUrl('/practice'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), keepalive: true })
    .then(r => (r.ok ? r.json() : null)).then(p => { if (p) set('song.practice', p); }).catch(() => {});
}

/** 这一段结束（loopDone：刚弹完一遍）；还在练同一件事就从现在开始下一段 */
function flush(loopDone) {
  const kind = log.cur && log.cur.kind, now = activity();
  dispatch({ type: 'end', loopDone, next: now && now === kind ? what(now) : null });
}

/** 开始 / 停止播放、敲节奏：停了的那一段记下来，开始的开始计时 */
function sync() {
  const now = activity();
  if (log.cur && log.cur.kind !== now) flush(false);
  if (!log.cur && now) dispatch({ type: 'start', ...what(now) });
}

function renderLine() {
  const bi = get('ui.bar'), s = get('song');
  if (bi < 0 || !s || !s.bars[bi]) return;
  const p = ((s.practice || {}).bars || {})[s.bars[bi].id];
  const { lines, title } = practiceLines(p);
  $('practiceLine').innerHTML = `<span class="pl-h">本小节</span><div class="pl-body">${lines.map(l => `<div>${esc(l)}</div>`).join('')}</div>`;
  $('practiceLine').title = title;
  $('practiceLine').classList.toggle('none', !p);
}

export function initPracticeLog() {
  on(['play.playing', 'rhythm.running', 'rhythm.calib'], sync);
  // 换段、按 Home / 重选这一段从段首重新开始（player.js 的 play.restarts）：先把这一段练了的时间记下来
  // （没弹完的不算一遍，标的“这遍干净”也作废），再从段首开始计时
  on(['play.a', 'play.b', 'play.restarts'], () => { if (log.cur) { flush(false); sync(); } });
  on('play.bpm', bpm => { if (log.cur) dispatch({ type: 'tempo', bpm }); });
  on('play.loops', (n, old) => { if (n > old && log.cur && log.cur.kind === 'play') flush(true); });
  on(['ui.bar', 'song.practice', 'song.bars'], renderLine);
  addEventListener('pagehide', () => dispatch({ type: 'end', loopDone: false, next: null })); // 压着的那一遍也马上发出去
}
