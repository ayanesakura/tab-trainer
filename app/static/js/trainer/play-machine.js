// 播放的状态机（纯函数，Node 测试直接用）。player.js 把用户操作（播放、停止、换段、点小节、换音色、谱子重新加载）
// 和 alphaTab 的事件（播放状态、位置、弹完、MIDI 生成好）都变成 event 交给 step(state, event)，
// 按返回的 fx 去操作 alphaTab；播放状态只在这里变，store 的 play.playing / play.phase 由它算出来。
//
// 阶段 phase：
//   idle            停着。atEnd：全曲弹完、停在曲末的位置（下次播放从头开始）；否则 null
//   countIn         正在数预备拍：from = 开始的位置（预备拍期间位置不动，位置往前走了才算数完）
//   playing         正在放
//   restarting      等 alphaTab 真的停下（state 事件），再从 resume.tick 重新开始（开着预备拍就重新数）：
//                   段落循环开着预备拍时每一遍弹完、播放中换段 / 重选这一段
//   finishing       全曲弹完、alphaTab 正在停：停下以后把光标放到最后弹到的那一小节（end: {tick, bar}）
//   reloadingMidi   正在重新生成 MIDI：预备拍里停下（alphaTab 1.9 在预备拍里 pause() 以后下次 play() 会卡住不走，
//                   stop() 又会被当成预备拍数完转入播放，只有重新生成 MIDI 能清干净）或者换了音色。
//                   生成好了（midiLoaded）放回 resume.tick（不在循环范围里就放到段首），resume.play 就接着放
//   reloadingScore  谱子重新加载（改音、撤销、识别出新的一行……）：alphaTab 会停下、把位置放回 0，期间还会发几个往回跳的
//                   位置事件（不能当成弹完一遍）。同样等 midiLoaded 放回 resume.tick
//
// 位置：pos 是最后确认的位置——alphaTab 的位置事件给的，或者我们自己设的（seek）。不在 stop() / 设位置之后马上读
// api.tickPosition：播放器在 worker 里，那时还没处理完，读到的是旧位置（以前播放中换到前面的段，预备拍的起点记成了旧位置，
// 预备拍一直“数不完”，停下后又放回旧位置）。
// last：判断“从段尾跳回段首 = 弹完一遍”用的上一个位置；-1 表示下一个位置事件不算（刚跳过、刚开始）。
// started：开始播放以后 alphaTab 已经报了“在放”（state 事件）。在这之前的位置事件可能是停下 / 重新开始时的抖动
// （停在 1，又报一个 11，再回到 1）：不拿它们判断预备拍数完、弹完一遍。
//
// fx（按顺序做）：['seek', tick] 设位置；['play'] 开始播放；['pause']；['stop']；['reloadMidi'] 重新生成 MIDI；
//   ['loop'] 弹完了一遍（遍数 +1）；['resetLoops'] 遍数清零；['showBar', i] 侧栏切到第 i 小节

import { TICKS } from '../score.js';

export const PHASES = ['idle', 'countIn', 'playing', 'restarting', 'finishing', 'reloadingMidi', 'reloadingScore'];

/** countIn：开始播放时会不会数预备拍（alphaTab 的预备拍音量 > 0） */
export const initial = (countIn = true) => ({ phase: 'idle', pos: 0, from: null, resume: null, atEnd: null, end: null, last: -1, started: false, countIn });

const reloading = s => s.phase === 'reloadingMidi' || s.phase === 'reloadingScore';

/** 对用户来说是不是“在放”（播放键显示停止、练习记录在计时）：数预备拍、两遍之间重新开始、重新加载完要接着放的都算 */
export const isPlaying = s => s.phase === 'countIn' || s.phase === 'playing' || s.phase === 'restarting' || (reloading(s) && !!s.resume && s.resume.play);

export function step(s0, ev) {
  const s = { ...s0, resume: s0.resume && { ...s0.resume } }, fx = [];
  // 从 tick 开始放（开着预备拍先数一小节）
  const start = tick => {
    fx.push(['seek', tick], ['play']);
    Object.assign(s, { pos: tick, last: -1, atEnd: null, end: null, resume: null, started: false, phase: s.countIn ? 'countIn' : 'playing', from: s.countIn ? tick : null });
  };
  // 停在预备拍里（或者要换音色）：重新生成 MIDI，好了放回 tick
  const reloadMidi = (tick, play, pause = true) => {
    if (pause) fx.push(['pause']);
    fx.push(['reloadMidi']);
    Object.assign(s, { phase: 'reloadingMidi', resume: { tick, play }, from: null, last: -1 });
  };

  switch (ev.type) {
    case 'countIn': // 预备拍开关变了（之后开始播放时才生效）
      s.countIn = !!ev.on;
      break;

    case 'play':
      fx.push(['resetLoops']);
      if (s.phase === 'idle') start(s.atEnd !== null ? 0 : s.pos); // 全曲弹完停在最后：再播放从头开始
      else if (reloading(s)) s.resume.play = true; // MIDI 还在生成：好了再开始
      else if (s.phase === 'finishing') Object.assign(s, { phase: 'restarting', resume: { tick: 0 }, end: null });
      break;

    case 'stop':
      if (s.phase === 'countIn') reloadMidi(s.from, false);
      else if (s.phase === 'playing') { fx.push(['pause']); s.phase = 'idle'; }
      else if (s.phase === 'restarting') { // 正要重新开始：就停在要开始的地方
        const t = s.resume.tick;
        fx.push(['pause'], ['seek', t]);
        Object.assign(s, { phase: 'idle', pos: t, resume: null, last: -1 });
      } else if (reloading(s)) s.resume.play = false;
      break;

    case 'restart': // 播放中换段（或重选正在循环的段、按 Home）：从 tick 重新开始，开着预备拍就重新数
      fx.push(['resetLoops']);
      if (s.phase === 'countIn') reloadMidi(ev.tick, true);
      else if (s.phase === 'playing') { fx.push(['stop']); Object.assign(s, { phase: 'restarting', resume: { tick: ev.tick }, last: -1 }); }
      else if (s.phase === 'restarting' || reloading(s)) s.resume.tick = ev.tick;
      else if (s.phase === 'finishing') Object.assign(s, { phase: 'restarting', resume: { tick: ev.tick }, end: null });
      else { fx.push(['seek', ev.tick]); Object.assign(s, { pos: ev.tick, atEnd: null, last: -1 }); }
      break;

    case 'seek': // 点小节、停着时换段：位置跳到 tick（播放中就从那里接着放）
      fx.push(['seek', ev.tick]);
      Object.assign(s, { pos: ev.tick, last: -1, atEnd: null });
      if (s.phase === 'countIn') s.from = ev.tick; // 之后在预备拍里停下，放回的是这里，不是开始播放的地方
      else if (s.phase === 'restarting' || reloading(s)) s.resume.tick = ev.tick;
      break;

    case 'jumped': // 设了新的循环范围，alphaTab 把位置跳回了段首：下一个位置事件不算弹完一遍
      s.last = -1;
      break;

    case 'state': // alphaTab 的播放状态：playing = 在放
      if (ev.playing) {
        if (s.phase === 'idle') s.phase = 'playing';
        if (s.phase === 'countIn' || s.phase === 'playing') s.started = true;
        break;
      }
      if (s.phase === 'restarting') start(s.resume.tick);
      else if (s.phase === 'finishing') {
        const e = s.end;
        if (e) fx.push(['seek', e.tick], ['showBar', e.bar]);
        Object.assign(s, { phase: 'idle', end: null, last: -1, atEnd: e ? e.tick : null, pos: e ? e.tick : s.pos });
      } else if (s.phase === 'countIn' || s.phase === 'playing') Object.assign(s, { phase: 'idle', from: null });
      break;

    case 'position': { // alphaTab 的位置事件；rangeStart：循环范围的起点（全曲是 0）
      if (s.phase === 'reloadingScore') break; // 重新加载时往回跳的位置不算
      if (!s.started) { s.pos = ev.tick; break; } // 还没真的开始：开始前的抖动不算
      if (s.phase === 'countIn' && ev.tick > s.from + 5) { s.phase = 'playing'; s.from = null; }
      // alphaTab 自己循环（关着预备拍的段落循环）：从段尾跳回段首才算弹完一遍
      if (s.phase === 'playing' && s.last >= 0 && ev.tick < s.last - 10 && ev.tick <= (ev.rangeStart || 0) + TICKS) fx.push(['loop']);
      s.pos = ev.tick; s.last = ev.tick;
      break;
    }

    case 'finished': // 弹到段尾 / 曲末（alphaTab 不自己循环时）。whole：循环的是全曲；end：最后弹到的小节 {tick, bar}；rangeStart：段首
      if (s.phase !== 'playing' && s.phase !== 'countIn') break;
      fx.push(['loop']);
      if (ev.whole) Object.assign(s, { phase: 'finishing', end: ev.end || null, from: null, last: -1 });
      else Object.assign(s, { phase: 'restarting', resume: { tick: ev.rangeStart || 0 }, from: null, last: -1 });
      break;

    case 'reloadScore': { // 谱子要重新加载；连着重新加载两次时留着第一次记下的位置
      if (s.phase === 'reloadingScore') break;
      const play = isPlaying(s);
      const tick = s.phase === 'countIn' ? s.from : s.resume ? s.resume.tick : s.atEnd ?? s.pos;
      Object.assign(s, { phase: 'reloadingScore', resume: { tick, play }, from: null, end: null, last: -1 });
      break;
    }

    case 'timbre': // 换音色要重新生成 MIDI：在放的话好了接着放（以前会停下不再继续）
      if (s.phase === 'countIn') reloadMidi(s.from, true);
      else if (s.phase === 'playing') reloadMidi(s.pos, true);
      else if (s.phase === 'restarting') reloadMidi(s.resume.tick, true, false);
      else if (s.phase === 'idle') { const atEnd = s.atEnd; reloadMidi(atEnd ?? s.pos, false, false); s.atEnd = atEnd; }
      else fx.push(['reloadMidi']); // finishing / 正在重新加载：再生成一次，位置照旧由它们放回
      break;

    case 'midiLoaded': { // MIDI 生成好了。range：{start, end} 现在的循环范围（全曲 end 是 Infinity），还没有小节时是 null；
      // cursorPending：谱子加载好之前设过循环范围，光标要放到段首
      s.last = -1;
      const r = ev.range;
      if (reloading(s)) {
        const back = s.resume;
        s.resume = null;
        if (!r) { s.phase = 'idle'; break; }
        const tick = back.tick >= r.start && back.tick < r.end ? back.tick : r.start;
        if (back.play) start(tick);
        else { fx.push(['seek', tick]); Object.assign(s, { phase: 'idle', pos: tick }); }
      } else if (ev.cursorPending && r && s.phase === 'idle') { fx.push(['seek', r.start]); s.pos = r.start; }
      break;
    }

    case 'error': // alphaTab 出错：什么都不等了
      Object.assign(s, { phase: 'idle', resume: null, from: null, end: null, last: -1 });
      break;

    default:
      throw new Error(`未知的播放事件：${ev.type}`);
  }
  return { state: s, fx };
}
