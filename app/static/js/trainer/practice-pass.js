// 练习记录的纯逻辑（practice-log.js 用）：一段练习从哪开始、什么时候结束、记几遍、多长时间、干不干净。
// 不碰 DOM、store 和时钟（时刻由调用方传进来），Node 测试直接导入（tests/js/trainer.test.mjs）。
//
// 状态 {cur, held, cleanUsed}：
//   cur   正在记的一段 {ids: 循环的小节 id, t: 开始时刻（毫秒）, tempo: tempoSpan, kind: 'play' | 'rhythm', clean: 标了这遍干净}；没在练是 null
//   held  刚弹完、还没发出去的那一遍 {rec, until}：弹完以后 graceMs 之内按“这遍干净”算这一遍（开着预备拍时，
//         弹完才按的 Enter 不会落到还没弹的下一遍上）；过了宽限（tick）、或者有别的记录要发时先把它发出去
//   cleanUsed  这次打开页面里用过“这遍干净”：之后弹完的每一遍都带 clean（true / false）；没用过就不带
//
// step(状态, 事件) → {state, send: [要发的记录], clean?: play.clean 的新值, msg?: 给用户的提示}。事件：
//   {type: 'start', kind, ids, bpm, now}    开始练（播放 / 敲节奏开始，或上一段结束后接着练同一件事）
//   {type: 'tempo', bpm, now}               换了速度（这一段的速度按时间加权平均）
//   {type: 'end', loopDone, now, next}      这一段结束：loopDone 是刚弹完一遍；不是的话是停下 / 换段 / 从段首重新开始，
//                                           只记零头时间（不到 MIN_SECONDS 的不记）。next：接着练的 {kind, ids, bpm}，没有是 null
//   {type: 'mark', now}                     这遍干净（Enter / 按钮）
//   {type: 'tick', now}                     看看宽限过了没有

import { tempoSpan, tempoChange, tempoAverage } from './view.js';

export const GRACE_MS = 1500; // 弹完以后多久之内按“这遍干净”还算刚弹完的那一遍
/** 宽限：GRACE_MS，但不超过一遍的一半（一遍很短时，比如 240 BPM 循环一小节，还要能标正在弹的这一遍） */
export const graceMs = seconds => Math.min(GRACE_MS, seconds * 500);
export const MIN_SECONDS = 2; // 没弹完一遍的零头时间，不到这么久不记（点一下就停的）

export const initialLog = () => ({ cur: null, held: null, cleanUsed: false });

const segment = ({ kind, ids, bpm, now }) => ({ ids: ids || [], t: now, tempo: tempoSpan(now, bpm), kind, clean: false });

/** 一段 → 记录（pass：弹完的一遍；clean 留到发出去时按 cleanUsed 决定带不带）；不该记的返回 null */
function close(seg, now, loopDone) {
  const seconds = (now - seg.t) / 1000;
  if (!seg.ids.length || (!loopDone && seconds < MIN_SECONDS)) return null;
  const pass = loopDone && seg.kind === 'play';
  return { ids: seg.ids, loops: pass ? 1 : 0, bpm: tempoAverage(seg.tempo, now), seconds, kind: seg.kind, pass, clean: seg.clean };
}
/** 发出去的样子：{ids, loops, bpm, seconds, kind, clean?}（clean 只在用过“这遍干净”以后、弹完的一遍上带） */
function out(rec, cleanUsed) {
  const { pass, clean, ...r } = rec;
  if (pass && cleanUsed) r.clean = !!clean;
  return r;
}

export function step(state, ev) {
  let { cur, held, cleanUsed } = state;
  const send = [], res = {};
  const release = () => { if (held) { send.push(out(held.rec, cleanUsed)); held = null; } };
  switch (ev.type) {
    case 'start':
      if (!cur) cur = segment(ev);
      break;
    case 'tempo':
      if (cur) { const tempo = { ...cur.tempo }; tempoChange(tempo, ev.now, ev.bpm); cur = { ...cur, tempo }; }
      break;
    case 'end': {
      if (!cur) { if (!ev.loopDone) release(); break; }
      const rec = close(cur, ev.now, ev.loopDone);
      const hadClean = cur.clean;
      cur = null;
      release(); // 先发上一遍（同一时间只压着一遍）
      if (rec && rec.pass) held = { rec, until: ev.now + graceMs(rec.seconds) };
      else if (rec) send.push(out(rec, cleanUsed));
      if (ev.next) cur = segment({ ...ev.next, now: ev.now });
      if (hadClean) res.clean = false; // 标记只管那一遍：下一遍（或从段首重新开始的这一遍）从没标开始
      break;
    }
    case 'mark':
      if (held && ev.now < held.until) { // 刚弹完：算刚弹完的那一遍，不会同时标到下一遍上
        if (held.rec.clean) res.msg = 'prevAlready';
        else { held = { ...held, rec: { ...held.rec, clean: true } }; cleanUsed = true; res.msg = 'prev'; }
      } else if (cur && cur.kind === 'play') {
        if (cur.clean) res.msg = 'already';
        else { cur = { ...cur, clean: true }; cleanUsed = true; res.clean = true; res.msg = 'marked'; }
      } else res.msg = 'idle';
      break;
    case 'tick':
      if (held && ev.now >= held.until) release();
      break;
    default:
      throw new Error(`未知的练习记录事件：${ev.type}`);
  }
  return { state: { cur, held, cleanUsed }, send, ...res };
}
