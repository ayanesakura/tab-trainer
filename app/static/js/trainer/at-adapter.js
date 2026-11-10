// alphaTab 适配层：我们用到的 alphaTab 内部结构（不在它公开文档里的字段和方法）都只在这里碰——
//   tickCache（masterBars / getMasterBar：有反复时每个小节在播放顺序里的时间）、boundsLookup（staffSystems / realBounds /
//   findMasterBarByIndex / onNotesX：谱面上小节、行、拍的位置）、beat.automations（换音色）、
//   api.player.soundFontLoadFailed / api.player.output.context、播放光标的 CSS 类名 .at-cursor-bar、音频导出（节拍器的声音）。
// 我们挂在 alphaTab 模型对象上的字段（_origProgram / _orig：换成木吉他前的原音色，导出 GP 时还原）也只在这里读写。
// 升级 alphaTab 时先看这个文件；tests/e2e/test_player.py 的 test_at_adapter_smoke 对着真实加载的谱子把每个函数都调一遍。

import * as alphaTab from '/static/vendor/alphatab/alphaTab.min.mjs';
import { TICKS } from '../score.js';

const lookup = api => (api && (api.boundsLookup || (api.renderer && api.renderer.boundsLookup))) || null;
const surfaceOf = root => (root ? root.querySelector('.at-surface') : null);

// ---------------------------------------------------------------------
// 谱面上的位置（boundsLookup；相对 root = #score）
// ---------------------------------------------------------------------
/** 第 bi 小节的框 {x, y, w, h}；没画好时 null */
export function barBox(api, root, bi) {
  const look = lookup(api), surface = surfaceOf(root);
  const mb = bi >= 0 && look && surface && look.findMasterBarByIndex(bi);
  if (!mb) return null;
  const r = mb.realBounds;
  return { x: surface.offsetLeft + r.x, y: surface.offsetTop + r.y, w: r.w, h: r.h };
}
/** 第 i 行 {i, y, h, a: 头一个小节, b: 最后一个小节}（包括上面的和弦和下面留给歌词的空）；没有时 null */
export function scoreRow(api, root, i) {
  const look = lookup(api), surface = surfaceOf(root);
  const sys = look && surface && i >= 0 && look.staffSystems[i];
  if (!sys || !sys.bars.length) return null;
  const r = sys.realBounds;
  return { i, y: surface.offsetTop + r.y, h: r.h, a: sys.bars[0].index, b: sys.bars[sys.bars.length - 1].index };
}
/** 第 bi 小节在第几行；没画好时 -1 */
export function rowOfBar(api, bi) {
  const look = lookup(api);
  const mb = look && bi >= 0 && look.findMasterBarByIndex(bi);
  return mb && mb.staffSystemBounds ? look.staffSystems.indexOf(mb.staffSystemBounds) : -1;
}
/**
 * 歌词要写的位置：每行谱表下面留的空（systemPaddingBottom）的顶上，对准各拍。
 * → [{text, x, y, k: 第几段歌词}]（相对 root）；五线谱 + 六线谱都显示时同一拍只出一次
 */
export function lyricSpots(api, root) {
  const look = lookup(api), surface = surfaceOf(root), out = [];
  if (!look || !surface) return out;
  const pad = api.settings.display.systemPaddingBottom, seen = new Set();
  for (const sys of look.staffSystems) {
    const y = surface.offsetTop + sys.realBounds.y + sys.realBounds.h - pad;
    for (const mb of sys.bars) for (const bar of mb.bars) for (const bb of bar.beats) {
      const beat = bb.beat, lyr = beat.lyrics;
      if (!lyr || beat.voice.index !== 0 || seen.has(beat)) continue;
      seen.add(beat);
      lyr.forEach((text, k) => { if (text) out.push({ text, x: surface.offsetLeft + (bb.onNotesX || bb.visualBounds.x), y, k }); });
    }
  }
  return out;
}
/** 播放光标所在小节的高亮框（alphaTab 画的 DOM 元素）；没有时 null */
export const cursorBar = root => (root ? root.querySelector('.at-cursor-bar') : null);

// ---------------------------------------------------------------------
// 播放时间（tickCache：有反复 / D.S. 时播放顺序和谱面顺序不一样）
// ---------------------------------------------------------------------
/** 播放顺序里的每一遍小节（弹不到的不要）：[{bar: 小节下标, start, end}] */
export function playedBars(api) {
  const tc = api && api.tickCache;
  return tc && tc.masterBars ? tc.masterBars.filter(m => m.end > m.start).map(m => ({ bar: m.masterBar.index, start: m.start, end: m.end })) : [];
}
/** 全曲弹完时最后弹到的小节 {tick, bar}（有 D.C. al Fine 时是 Fine 那一小节）；没有时 null */
export function lastPlayedBar(api) {
  const p = playedBars(api), m = p[p.length - 1];
  return m ? { tick: m.start, bar: m.bar } : null;
}
function findLinearRun(played, a, b) {
  const n = b - a + 1;
  for (let i = 0; i + n <= played.length; i++) {
    let ok = true;
    for (let k = 0; k < n && ok; k++) ok = played[i + k].masterBar.index === a + k;
    if (ok) return [played[i], played[i + n - 1]];
  }
  return null;
}
/**
 * 循环第 a..b 小节时的播放时间 {start, end}；按反复记号这几个小节根本弹不到时 null。
 * 有反复时优先找播放顺序里正好依次弹过 a..b 的那一段（|: 1 2 3 4 :| 5 选 3–5，用第二遍的 3 4 5），
 * 这样循环里只有选中的小节；找不到才用各自第一次被弹到的位置
 */
export function sectionTicks(api, a, b) {
  const mbs = api.score.masterBars, tc = api.tickCache;
  const first = mb => (tc && tc.getMasterBar(mb)) || { start: mb.start, end: mb.start + mb.calculateDuration() };
  let la = first(mbs[a]), lb = first(mbs[b]);
  const run = tc && tc.masterBars && findLinearRun(tc.masterBars, a, b);
  if (run) [la, lb] = run;
  if (la.end <= la.start || lb.end <= lb.start) return null;
  return { start: la.start, end: lb.end > la.start ? lb.end : la.end };
}
/** 第 bi 小节开头的播放时间：有反复时取落在 range（{start, end}，可以是 null）里的那一遍 */
export function barStartTick(api, bi, range) {
  const mb = api.score.masterBars[bi];
  const runs = playedBars(api).filter(m => m.bar === bi);
  const inRange = runs.find(m => !range || (m.start >= range.start && m.start < range.end));
  return inRange ? inRange.start : runs.length ? runs[0].start : mb.start;
}
/** 现在的播放范围 {start, end}；全曲（没设范围）是 null */
export function playbackRange(api) {
  const r = api && api.playbackRange;
  return r ? { start: r.startTick, end: r.endTick } : null;
}
/** 设播放范围（null = 全曲）；和现在的一样就不设（设了会把播放位置跳回段首）。返回是不是真的设了 */
export function setPlaybackRange(api, range) {
  const cur = api.playbackRange;
  if (cur && range ? cur.startTick === range.start && cur.endTick === range.end : !cur && !range) return false;
  let r = null;
  if (range) { r = new alphaTab.synth.PlaybackRange(); r.startTick = range.start; r.endTick = range.end; }
  api.playbackRange = r;
  return true;
}
/** 谱面上把第 a..b 小节标成循环范围；all = 全曲时清掉 */
export function highlightBars(api, a, b, all = false) {
  if (all) { api.clearPlaybackRangeHighlight(); return; }
  const bars = api.tracks[0].staves[0].bars, last = bars[b].voices[0].beats;
  api.highlightPlaybackRange(bars[a].voices[0].beats[0], last[last.length - 1]);
}

// ---------------------------------------------------------------------
// 音色、音频
// ---------------------------------------------------------------------
const isGuitarProgram = p => p >= 24 && p <= 31;
/**
 * 所有吉他音轨（GM 24–31，包括电吉他）都换成 program（音色库里只有木吉他）；贝斯、鼓、钢琴等不动。
 * 原来的音色记在模型对象的 _origProgram / _orig 上，restore = true 时换回去（导出 GP 用）
 */
export function applyTimbre(score, program, restore = false) {
  for (const t of score.tracks) {
    if (t.isPercussion) continue;
    if (t._origProgram === undefined) t._origProgram = t.playbackInfo.program;
    if (!isGuitarProgram(t._origProgram)) continue;
    t.playbackInfo.program = restore ? t._origProgram : program;
    for (const st of t.staves) for (const bar of st.bars) for (const v of bar.voices) for (const b of v.beats)
      for (const a of b.automations) if (a.type === alphaTab.model.AutomationType.Instrument) {
        if (a._orig === undefined) a._orig = a.value;
        if (isGuitarProgram(a._orig)) a.value = restore ? a._orig : program;
      }
  }
}
/** 现在吉他音轨用的音色（GM 号）：看第一个吉他音轨 */
export function currentProgram(score) {
  const t = score.tracks.find(x => !x.isPercussion && isGuitarProgram(x._origProgram ?? x.playbackInfo.program));
  return t ? t.playbackInfo.program : null;
}
/** 木吉他音色库加载失败时调 fn（alphaTab 1.9 的 api.player 才有这个事件；没有就什么都不做） */
export function onSoundFontLoadFailed(api, fn) {
  const ev = api && api.player && api.player.soundFontLoadFailed;
  if (ev) ev.on(fn);
  return !!ev;
}
/** alphaTab 播放用的 AudioContext（WebAudio 输出打开以后才有）；拿不到时 null */
export const audioContext = api => (api && api.player && api.player.output && api.player.output.context) || null;

/**
 * 用 alphaTab 自己的合成器（同一个音色库、同一个音符：GM 33 号 Metronome Click，力度 95）渲染一下节拍器的声音，
 * 敲节奏的节拍器用它，听起来和播放时的节拍器一样。→ Promise<Float32Array 单声道 | null>（谱子没加载好、导出失败时 null）
 */
export async function renderMetronomeClick(api, sampleRate, ms = 160) {
  if (!api || !api.score || typeof api.exportAudio !== 'function') return null;
  const o = new alphaTab.synth.AudioExportOptions();
  o.sampleRate = sampleRate; o.masterVolume = 1; o.metronomeVolume = 1;
  api.score.tracks.forEach((_, i) => o.trackVolume.set(i, 0)); // 只要节拍器，不要吉他
  const r = new alphaTab.synth.PlaybackRange();
  r.startTick = 0; r.endTick = TICKS;
  o.playbackRange = r;
  let ex = null;
  try {
    ex = await api.exportAudio(o);
    const chunk = await ex.render(ms);
    const st = chunk && chunk.samples;
    if (!st || !st.length) return null;
    const mono = new Float32Array(st.length >> 1);
    for (let i = 0; i < mono.length; i++) mono[i] = (st[2 * i] + st[2 * i + 1]) / 2;
    // 去掉开头的静音（合成器的缓冲）：咔哒要正好在排好的时刻响，敲击是按它判早晚的
    let peak = 0;
    for (const x of mono) peak = Math.max(peak, Math.abs(x));
    if (peak < 1e-4) return null;
    const at0 = mono.findIndex(x => Math.abs(x) > peak * 0.02);
    return mono.slice(Math.max(0, at0 - 1));
  } catch {
    return null;
  } finally {
    try { if (ex) ex.destroy(); } catch { /* 已经没了 */ }
  }
}

/** 谱子里所有和弦（导出 GP 前要临时改名字） */
export const scoreChords = score => score.tracks.flatMap(t => t.staves.flatMap(st => [...(st.chords ? st.chords.values() : [])]));
/** 某个音轨的所有拍（第一个谱表） */
export const trackBeats = track => track.staves[0].bars.flatMap(b => b.voices.flatMap(v => v.beats));
