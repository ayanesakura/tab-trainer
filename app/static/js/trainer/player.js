// 谱面和播放（alphaTab）：渲染谱面、播放和循环、速度、音色、导出 GP，以及吸顶的控制条。
// 对外：play / stop / setLoop / setTempo / setStatus / stepSection / applyAudio / exportGp / getApi。
// 状态都在 store 的 play 切片里（ready、playing、bpm、a..b、loops、指板的 note / next，以及节拍器、音量等设置）。

import * as alphaTab from '/static/vendor/alphatab/alphaTab.min.mjs';
import { TICKS, eventIndexAt, scoreNeedsGbk } from '../score.js';
import { get, set, on } from './store.js';
import { $, UI } from './dom.js';
import { loopLabel, stepSection as stepCalc, isScore } from './view.js';

export { alphaTab };
export const IDLE_HINT = '空格播放 · ←→ 换段 · ↑↓ 调速';

const AT_BASE = '/static/vendor/alphatab/';
// 两个音色库：Sonivox 是完整的通用音色库（节拍器、GP 文件里的贝斯和鼓等都靠它）；
// 木吉他库后加载，alphaTab 查音色时后加载的优先，所以 24 / 25 号用的是真实木吉他采样。
const SF_BASE = '/static/vendor/soundfont/sonivox.sf2', SF_GUITARS = '/static/vendor/soundfont/guitars.sf3';
export const TIMBRES = { steel: 25, nylon: 24 };

let pendingBeat = null; // 谱面上一次点击里先到的拍（见 beatMouseDown）
let api = null, baseTempo = 60, soundFontsLoaded = 0, playerReadyOnce = false, lastTick = -1, shiftDown = false, warnedUnplayed = '';
// tempoSet：速度已经由用户 / 接着练定下来了（setTempo 调过）。谱子加载好以后只在还没定的时候才用默认速度（原速六成）。
// scoreTempoDone：第一次加载谱子时按原速放宽滑块上限这件事做过了。cursorPending：谱子没加载好时设了循环范围，加载好后把光标放到段首。
let tempoSet = false, scoreTempoDone = false, cursorPending = false, scoreBytes = null;
// 预备拍：alphaTab 1.9 在预备拍里停下以后状态清不干净：只 pause() 的话下次 play() 会一直停在“播放中”却不前进
// （图标变了、没有声音），直接 stop() 又会被当成“预备拍弹完了”转入播放。所以预备拍里停就重新生成一次 MIDI 把它清掉，
// 好了以后（midiLoaded）把位置放回去；这期间按了播放就等它好了再开始。
let countInFrom = null; // 预备拍进行中：开始播放时的位置；不在预备拍里是 null
let reloadTick = null, playAfterReload = false;
export const getApi = () => api;

// 曲子相关的处理由 song.js / editor.js 注册，敲节奏由 rhythm-drill.js 注册 drill: {start, stop, loopChanged}（播放模块不反过来依赖它们）
const hooks = { scoreLoaded: null, error: null, editClick: null, drill: null };
export function setPlayerHooks(h) { Object.assign(hooks, h); }
// 侧栏正在用的时候（小节编辑器开着、正在输入问 AI 的问题）播放光标不切换侧栏的小节，点谱面照常切换
const holds = [];
export function holdBarWhile(fn) { holds.push(fn); }

const playing = () => get('play.playing');
const nBars = () => (get('view') ? get('view').bars.length : 0);

function guitarsFailed() {
  if (soundFontsLoaded >= 2) return;
  soundFontsLoaded = 2;
  UI.toast('木吉他音色加载失败，先用默认音色播放', 'err', 6000);
  if (playerReadyOnce) setReady();
}
// 播放器可以用了（两个音色库都好了，或木吉他库失败退回默认音色）：节拍器、预备拍、音量在这里一起设上，
// 不然 alphaTab 默认的 0 音量会一直留着（另外 on('play.ready') 也会再设一次，哪条路径变成就绪都不会漏）
function setReady() {
  set('play.ready', true);
  applyAudio();
}

function themeColors() {
  const cs = getComputedStyle(document.documentElement), v = n => cs.getPropertyValue(n).trim() || '#808080';
  return { mainGlyphColor: v('--fg'), secondaryGlyphColor: v('--muted'), staffLineColor: v('--faint'), barSeparatorColor: v('--muted'), barNumberColor: v('--muted'), scoreInfoColor: v('--fg') };
}
const staveProfile = () => (get('ui.notation') === 'tab' ? 'Tab' : 'ScoreTab');

function ensureApi() {
  if (api) return api;
  api = new alphaTab.AlphaTabApi($('score'), {
    core: { fontDirectory: AT_BASE + 'font/', includeNoteBounds: true }, // 不开这个 alphaTab 不会触发点击音符事件（试听、改音都靠它）
    display: { scale: 0.9, layoutMode: 'Page', staveProfile: staveProfile(), resources: themeColors() },
    // 曲名、艺人等页面顶栏已经有了，谱面上不再画（省出一大块地方）；调弦说明保留。alphaTab 署名不受影响
    notation: { rhythmMode: 'ShowWithBars', elements: { scoreTitle: false, scoreSubTitle: false, scoreArtist: false, scoreAlbum: false, scoreWords: false, scoreMusic: false, scoreWordsAndMusic: false, scoreCopyright: false } },
    player: {
      playerMode: 'EnabledSynthesizer', soundFont: SF_BASE,
      enableCursor: true, enableAnimatedBeatCursor: true, enableUserInteraction: true,
      enablePlaybackRangeSelection: false, scrollOffsetY: -230,
      scrollMode: 'Off', // 不自动滚动：光标不在视野里时显示“回到播放位置”按钮，点了才滚（见 updateFollowBtn）
    },
  });
  api.soundFontLoad.on(e => { if (!get('play.ready')) setStatus('加载音色…', `${Math.round(100 * e.loaded / Math.max(1, e.total))}%`); });
  api.soundFontLoaded.on(() => {
    if (++soundFontsLoaded !== 1) return;
    // 木吉他音色加载失败时退回只用 Sonivox，不能让播放器一直卡在“音色还在加载”
    if (api.player && api.player.soundFontLoadFailed) api.player.soundFontLoadFailed.on(guitarsFailed);
    api.loadSoundFont(SF_GUITARS, true);
  });
  api.playerReady.on(() => {
    playerReadyOnce = true;
    if (soundFontsLoaded < 2) return; // 等木吉他音色也加载完
    setReady();
  });
  api.playerStateChanged.on(e => set('play.playing', e.state === 1));
  api.playerPositionChanged.on(e => onPosition(e.currentTick));
  api.playedBeatChanged.on(beat => onBeat(beat, false));
  // 点音符时 alphaTab 先触发拍、再触发音符：攒到同一轮里，点在音符上就只试听（不改循环），点在别处才循环这一小节。
  // 播放中点音符不试听，照常循环这一小节
  api.beatMouseDown.on(b => {
    if (get('ui.editMode') && hooks.editClick) { hooks.editClick(b, null); return; }
    if (pendingBeat) return;
    pendingBeat = { beat: b, note: null };
    setTimeout(() => {
      const c = pendingBeat; pendingBeat = null;
      if (c.note && !playing() && get('play.ready')) api.playNote(c.note); else onBeatClick(c.beat);
    }, 0);
  });
  api.noteMouseDown.on(n => {
    if (get('ui.editMode') && hooks.editClick) hooks.editClick(n.beat, n);
    else if (pendingBeat) pendingBeat.note = n;
    else if (!playing() && get('play.ready')) api.playNote(n);
  });
  api.scoreLoaded.on(onScoreLoaded);
  api.renderFinished.on(() => { if (nBars()) applyLoopRange(); });
  api.postRenderFinished.on(() => set('ui.renderGen', (get('ui.renderGen') || 0) + 1)); // 小节位置要等所有部分都画完才有
  api.midiLoaded.on(() => { // 反复的播放时间要等 MIDI 生成后才知道
    if (!nBars()) return;
    const range = applyLoopRange();
    if (range && cursorPending && !playing()) api.tickPosition = range.startTick;
    if (range) cursorPending = false;
    if (reloadTick !== null) { // 预备拍里停下后的重新生成（见 pausePlayer）：放回原来的位置
      api.tickPosition = reloadTick;
      reloadTick = null;
      if (playAfterReload) { playAfterReload = false; startPlayer(); }
    }
  });
  api.error.on(e => {
    const msg = e && e.message ? e.message : String(e);
    if (hooks.error) hooks.error(msg); else UI.toast('谱面出错：' + msg, 'err', 6000);
  });
  return api;
}

/** 谱面来源（view.source）变了才重新加载：GP 原文件的某个音轨，或我们生成的 alphaTex */
async function loadSource(src) {
  if (!src) return;
  ensureApi();
  if (src.kind === 'score') {
    let r;
    try { r = await fetch(`/files/${src.id}/source`); } catch { r = { ok: false, status: '连不上服务' }; }
    if (!r.ok) {
      const msg = '曲谱文件加载失败（' + r.status + '）';
      if (hooks.error) hooks.error(msg); else UI.toast(msg, 'err', 6000);
      return;
    }
    scoreBytes = new Uint8Array(await r.arrayBuffer());
    api.settings.importer.encoding = 'utf-8';
    api.load(scoreBytes, [src.track]);
  } else {
    scoreBytes = null;
    api.settings.notation.elements.set(alphaTab.NotationElement.EffectTempo, src.showTempo);
    api.tex(src.tex);
  }
}

// 所有音轨都用选中的木吉他音色：GP 文件里可能是电吉他等音色，而音色库里只有木吉他。
// scoreLoaded 在生成 MIDI 之前同步触发，所以在这里改就会生效；中途切换时再重新生成 MIDI。
// 只换吉他音轨（GM 24–31，包括电吉他）：贝斯、鼓、钢琴等保持原音色。原来的音色记在 _orig 里，导出 GP 时还原。
const isGuitarProgram = p => p >= 24 && p <= 31;
function applyTimbre(score, restore = false) {
  const prog = TIMBRES[get('play.timbre')] ?? 25;
  for (const t of score.tracks) {
    if (t.isPercussion) continue;
    if (t._origProgram === undefined) t._origProgram = t.playbackInfo.program;
    if (!isGuitarProgram(t._origProgram)) continue;
    t.playbackInfo.program = restore ? t._origProgram : prog;
    for (const st of t.staves) for (const bar of st.bars) for (const v of bar.voices) for (const b of v.beats)
      for (const a of b.automations) if (a.type === alphaTab.model.AutomationType.Instrument) {
        if (a._orig === undefined) a._orig = a.value;
        if (isGuitarProgram(a._orig)) a.value = restore ? a._orig : prog;
      }
  }
}
function switchTimbre() {
  if (!api || !api.score) return;
  if (playing()) pausePlayer();
  applyTimbre(api.score);
  api.loadMidiForScore();
  applyLoopRange();
}

async function onScoreLoaded(score) {
  const song = get('song');
  if (isScore(song) && scoreBytes && api.settings.importer.encoding !== 'gbk' && scoreNeedsGbk(score, scoreBytes)) {
    // 中文 GP3–5 / GPX 文件：按 GBK 重新导入（曲名、艺人、音轨名才不是乱码）
    api.settings.importer.encoding = 'gbk';
    const bytes = scoreBytes, track = (get('view.source') || {}).track || 0;
    setTimeout(() => api.load(bytes, [track]), 0);
    return;
  }
  applyTimbre(score);
  baseTempo = score.tempo || 60;
  api.playbackSpeed = get('play.bpm') / baseTempo; // 先设好：下面的 hook 失败提前返回时，实际速度也要和显示的 BPM 一致
  if (isScore(song) && hooks.scoreLoaded && !(await hooks.scoreLoaded(score))) return;
  if (!scoreTempoDone) {
    scoreTempoDone = true;
    const s = get('song'), t = (s.meta && s.meta.tempo) || (isScore(s) ? baseTempo : 0);
    if (t) { // 原速比滑块上限快时把上限放宽，原速也能拖得到（已经定下的速度更快时不收窄）
      const bpm = get('play.bpm');
      set({ 'play.bpmMax': Math.min(240, Math.max(140, Math.ceil(t * 1.25 / 10) * 10, Math.ceil(bpm / 10) * 10)), 'play.autoTarget': t });
    }
    // 默认用原速的六成练习；没有标速度就 50。接着练 / 用户在谱子加载好之前已经定了速度的，不覆盖
    if (!tempoSet) setTempo(t ? Math.round(t * 0.6) : 50);
  }
  if (api) api.playbackSpeed = get('play.bpm') / baseTempo;
}

export function applyAudio() {
  if (!api) return;
  const p = get('play');
  api.masterVolume = p.gtVol / 100;
  api.metronomeVolume = p.metro ? p.metroVol / 100 * 1.5 : 0;
  api.countInVolume = p.countIn ? 1 : 0;
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
function applyLoopRange() {
  if (!api || !api.score || !api.tracks.length) return null;
  const mbs = api.score.masterBars, { a, b } = get('play');
  if (!mbs[a] || !mbs[b]) return null;
  // 循环全曲：不设范围，反复和 D.S. / D.C. 跳转都照常播放。
  // 循环一段：有反复时播放时间和谱面位置不再一一对应，用 tickCache 查每个小节第一次被弹到的时间。
  let range = null;
  if (!(a === 0 && b === mbs.length - 1)) {
    const tc = api.tickCache;
    const first = mb => (tc && tc.getMasterBar(mb)) || { start: mb.start, end: mb.start + mb.calculateDuration() };
    let la = first(mbs[a]), lb = first(mbs[b]);
    // 有反复时，优先找播放顺序里正好依次弹过 a..b 的那一段（比如 |: 1 2 3 4 :| 5 选 3–5，用第二遍的 3 4 5），
    // 这样循环里只有选中的小节；找不到才用各自第一次被弹到的位置
    const run = tc && findLinearRun(tc.masterBars, a, b);
    if (run) { la = run[0]; lb = run[1]; }
    if (la.end <= la.start || lb.end <= lb.start) {
      // 按反复 / 跳转记号走，这个小节根本不会被弹到（比如 Fine 之后的小节）
      if (warnedUnplayed !== `${a}-${b}`) UI.toast('按反复记号，选中的小节弹不到，改为循环全曲', 'info', 5000);
      warnedUnplayed = `${a}-${b}`;
    } else {
      range = new alphaTab.synth.PlaybackRange();
      range.startTick = la.start;
      range.endTick = lb.end > la.start ? lb.end : la.end;
    }
  }
  // 设置范围会把播放位置跳回段首：谱面重画（拖窗口、切主题、通知出现）时范围没变，就别再设，否则播放中会跳回段首
  const cur = api.playbackRange;
  if (!(cur && range ? cur.startTick === range.startTick && cur.endTick === range.endTick : !cur && !range)) {
    lastTick = -1; // 跳回起点不能当成弹完了一遍
    api.playbackRange = range;
  }
  api.isLooping = true;
  const bars = api.tracks[0].staves[0].bars;
  const first = bars[a].voices[0].beats[0];
  const lastBeats = bars[b].voices[0].beats, last = lastBeats[lastBeats.length - 1];
  if (b - a + 1 < mbs.length) api.highlightPlaybackRange(first, last);
  else api.clearPlaybackRangeHighlight();
  return range || { startTick: 0 };
}

/** 谱面上某一小节的位置（相对 #score），没画好时返回 null */
export function barBox(bi) {
  const look = api && (api.boundsLookup || (api.renderer && api.renderer.boundsLookup)), surface = $('score').querySelector('.at-surface');
  const mb = bi >= 0 && look && surface && look.findMasterBarByIndex(bi);
  if (!mb) return null;
  const r = mb.realBounds;
  return { x: surface.offsetLeft + r.x, y: surface.offsetTop + r.y, w: r.w, h: r.h };
}

function markReviewBars() {
  // 待核对的小节：在谱面上这一小节的位置画一层淡橙色（alphaTab 只给每行第一个小节标号，染小节号不够用）
  let layer = $('warnLayer');
  if (!layer) { layer = document.createElement('div'); layer.id = 'warnLayer'; $('score').appendChild(layer); }
  layer.replaceChildren();
  const v = get('view');
  if (!v) return;
  v.review.forEach((need, i) => {
    const box = need && barBox(i);
    if (!box) return;
    const el = document.createElement('div');
    el.className = 'warn-mark';
    Object.assign(el.style, { left: box.x + 'px', top: box.y + 'px', width: box.w + 'px', height: box.h + 'px' });
    el.title = `第 ${i + 1} 小节待核对`;
    layer.appendChild(el);
  });
}

export function play() {
  if (get('rhythm.on') && hooks.drill) { hooks.drill.start(); return; } // 敲节奏打开时：开始的是敲节奏，不是 alphaTab
  if (!api || !get('play.ready')) { UI.toast('音色加载中', 'info'); return; }
  set('play.loops', 0); lastTick = -1;
  if (reloadTick !== null) { playAfterReload = true; return; } // 预备拍里停过、MIDI 还在重新生成：好了再开始
  startPlayer();
}
export function stop() {
  if (get('rhythm.running') && hooks.drill) hooks.drill.stop();
  pausePlayer();
}
function startPlayer() {
  countInFrom = api.countInVolume > 0 ? api.tickPosition : null;
  api.play();
}
function pausePlayer() {
  playAfterReload = false;
  if (!api) return;
  if (countInFrom === null) { api.pause(); return; }
  reloadTick = countInFrom;
  countInFrom = null;
  api.pause();
  api.loadMidiForScore();
}
export function setTempo(bpm) {
  tempoSet = true;
  setBpm(bpm);
}
function setBpm(bpm) {
  bpm = Math.max(20, Math.min(240, Math.round(bpm)));
  const max = get('play.bpmMax');
  set({ 'play.bpm': bpm, 'play.bpmMax': bpm > max ? Math.min(240, Math.ceil(bpm / 10) * 10) : max }); // ↑ 键 / + 按钮超过了滑块上限
}
/**
 * 设循环范围 a..b（小节下标）。restart：播放中就从段首重新开始；暂停时把光标放到段首。
 * restart = false 并且范围没变（比如后台刷新后重新对一遍范围）：什么都不动，暂停的位置不会跳回段首。
 * show：侧栏切到段首小节。
 */
export function setLoop(a, b, restart = true, show = true) {
  const n = nBars();
  if (!n) return;
  const na = Math.max(0, Math.min(a, b, n - 1)), nb = Math.min(n - 1, Math.max(a, b));
  const { a: oa, b: ob } = get('play');
  if (!restart && na === oa && nb === ob) { if (show) set('ui.bar', na); return; }
  set({ 'play.a': na, 'play.b': nb }); // 练习记录（practice-log.js）跟着它记上一段、开始下一段
  const range = applyLoopRange();
  if (show) set('ui.bar', na);
  if (!range) cursorPending = true; // 谱子还没加载好：加载好以后（midiLoaded）再把光标放到段首
  if (range && api) {
    if (playing() && restart) { api.stop(); set('play.loops', 0); startPlayer(); }
    else if (!playing()) api.tickPosition = range.startTick;
  }
  if (get('rhythm.on') && hooks.drill) hooks.drill.loopChanged(); // 敲节奏：换成新的段（敲着的话从头再来）
}
/** 上一段 / 下一段 */
export function stepSection(dir) {
  const v = get('view'), { a, b } = get('play');
  if (!v) return;
  const r = stepCalc({ a, b, n: v.bars.length, shown: get('ui.bar'), secs: v.sections }, dir);
  if (!r) return;
  if (r.msg) UI.toast(r.msg, 'info'); else setLoop(r.a, r.b);
}

function onPosition(tick) {
  // 预备拍期间位置停在开始处不动（拍子事件倒可能先来，不能拿它判断），位置往前走了才算预备拍结束
  if (countInFrom !== null && tick > countInFrom + 5) countInFrom = null;
  // 只有从段尾跳回段首才算弹完一遍；重新设置循环范围、停止时也会跳回起点，那些不算（applyLoopRange 会重置 lastTick）
  const r = api && api.playbackRange, start = r ? r.startTick : 0;
  if (playing() && lastTick >= 0 && tick < lastTick - 10 && tick <= start + TICKS) {
    const loops = get('play.loops') + 1;
    set('play.loops', loops); // 练习记录先按提速前的速度记下刚弹完的这一遍
    const p = get('play');
    if (p.auto) {
      const every = Math.max(1, p.autoEvery || 1), step = p.autoStep || 0, target = p.autoTarget || p.bpm;
      if (loops % every === 0 && p.bpm < target) setTempo(Math.min(target, p.bpm + step));
    }
  }
  lastTick = tick;
}

function onBeat(beat, clicked) {
  const v = get('view');
  if (!v) return;
  const bi = beat.voice.bar.index, evs = v.bars[bi];
  if (!clicked) updateFollowBtn();
  if (!clicked) setStatus(`第 ${bi + 1} 小节 · 第 ${Math.floor(beat.playbackStart / TICKS) + 1} 拍`, `第 ${get('play.loops') + 1} 遍`);
  if (bi < v.bars.length && (clicked || !holds.some(f => f()))) set('ui.bar', bi);
  if (!evs || !evs.length) return;
  const cur = evs[eventIndexAt(v.beatMap[bi] || [0], beat.playbackStart)];
  if (!cur) return;
  let next = v.all[cur.idx + 1];
  const { a, b } = get('play');
  if (!next || next.bar > b) next = v.bars[a] && v.bars[a][0];
  set({ 'play.note': cur.idx, 'play.next': next ? next.idx : -1 });
}
// 播放光标所在的小节（alphaTab 画的高亮框）；不在视野里时显示“回到播放位置”
const cursorBar = () => document.querySelector('#score .at-cursor-bar');
export function cursorOutOfView() {
  const el = cursorBar();
  if (!el) return false;
  const r = el.getBoundingClientRect();
  if (!r.height) return false;
  const top = Math.max(0, $('transport').getBoundingClientRect().bottom); // 吸顶的顶栏和控制条挡住的部分不算看得见
  const bottom = innerHeight - dockHeight();
  return r.bottom < top + 8 || r.top > bottom - 8;
}
const dockHeight = () => { const d = $('fbDock'); return d && !d.hidden ? d.offsetHeight : 0; };
function updateFollowBtn() {
  $('followBtn').hidden = !(playing() && cursorOutOfView());
}
function scrollToCursor() {
  const el = cursorBar();
  if (!el) return;
  const t = $('transport');
  const stuck = parseFloat(getComputedStyle(t).top) + t.offsetHeight; // 滚过去以后控制条吸顶时的下沿
  scrollTo({ top: scrollY + el.getBoundingClientRect().top - stuck - 16, behavior: 'smooth' });
}

export function onBeatClick(beat) {
  const bi = beat.voice.bar.index;
  if (bi >= nBars()) return;
  const { a, b } = get('play');
  if (shiftDown) setLoop(Math.min(a, bi), Math.max(b, bi));
  else setLoop(bi, bi);
  onBeat(beat, true);
}

export function exportGp() {
  if (!api || !api.score) return;
  applyTimbre(api.score, true); // 导出原来的音色，不是练习时临时换的木吉他
  let bytes;
  try { bytes = new alphaTab.exporter.Gp7Exporter().export(api.score, api.settings); } finally { applyTimbre(api.score); }
  const s = get('song'), a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([bytes], { type: 'application/octet-stream' }));
  a.download = ((s.title || (s.meta && s.meta.title) || 'tab').replace(/[\\/:*?"<>|]/g, '_')) + '.gp';
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}
export function refreshScoreTheme() {
  if (!api) return;
  const res = api.settings.display.resources, c = themeColors();
  for (const k in c) res[k] = alphaTab.model.Color.fromJson(c[k]);
  api.updateSettings(); api.render();
}

// ---------------------------------------------------------------------
// 控制条
// ---------------------------------------------------------------------
export function setStatus(l1, l2) { $('statusL1').textContent = l1; $('statusL2').textContent = l2; $('statusL1').parentElement.title = l2 ? `${l1}\n${l2}` : l1; } // 窄的时候会截断：悬停看全文

function renderSections(v) {
  const host = $('secBtns');
  host.replaceChildren();
  if (!v || !v.bars.length) return;
  const add = (label, a, z, all) => {
    const btn = document.createElement('button');
    btn.textContent = label; btn.dataset.a = a; btn.dataset.z = z;
    btn.onclick = () => setLoop(a, z);
    if (!all && v.review.slice(a, z + 1).some(Boolean)) { btn.classList.add('warn'); btn.title = '这段有待核对的小节'; }
    host.appendChild(btn);
  };
  add('全曲', 0, v.bars.length - 1, true); // 放最前面：窄屏时也不会被挤出可见范围
  v.sections.forEach(g => add(g.a === g.z ? `${g.a + 1}` : `${g.a + 1}–${g.z + 1}`, g.a, g.z));
  paintLoop();
}
function paintLoop() {
  const { a, b } = get('play');
  $('loopLabel').textContent = loopLabel(a, b, nBars());
  $('secBtns').querySelectorAll('button').forEach(x => {
    const on_ = +x.dataset.a === a && +x.dataset.z === b;
    x.classList.toggle('on', on_);
    x.setAttribute('aria-pressed', on_);
    if (on_) x.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  });
}

const PREFS = { metro: true, metroVol: 70, countIn: true, gtVol: 80, auto: false, autoEvery: 2, autoStep: 4, autoTarget: 72, bpmMax: 140 };

/** 控制条和“更多”弹层里的输入框 ⇄ store 的 play 切片 */
function bindInputs() {
  const bind = (id, key, kind) => {
    const el = $(id);
    const read = () => (kind === 'check' ? el.checked : +el.value);
    el.addEventListener(kind === 'check' ? 'change' : 'input', () => set('play.' + key, read()));
    const paint = v => { if (kind === 'check') el.checked = v; else if (+el.value !== v) el.value = v; if (el.type === 'range') UI.fillRange(el); };
    on('play.' + key, paint);
    paint(get('play.' + key));
  };
  bind('metro', 'metro', 'check'); bind('metroVol', 'metroVol'); bind('countIn', 'countIn', 'check'); bind('gtVol', 'gtVol');
  bind('auto', 'auto', 'check'); bind('autoEvery', 'autoEvery'); bind('autoStep', 'autoStep'); bind('autoTarget', 'autoTarget');
}

export function initPlayer() {
  set('play', { ...get('play'), ...PREFS, timbre: TIMBRES[localStorage.getItem('tt.timbre')] ? localStorage.getItem('tt.timbre') : 'steel' });
  // Shift + 点：alphaTab 的点击事件里没有按键状态，在谱面的 mousedown（捕获阶段，比 alphaTab 先）里记下。
  // 不用全局 keydown / keyup：按着 Shift 切到别的应用时 keyup 会丢，之后每次点小节都成了扩展循环
  $('score').addEventListener('mousedown', e => { shiftDown = e.shiftKey; }, true);
  bindInputs();
  on(['play.metro', 'play.metroVol', 'play.countIn', 'play.gtVol'], applyAudio);
  on('play.timbre', v => { localStorage.setItem('tt.timbre', v); switchTimbre(); });
  on('play.bpm', bpm => {
    $('bpm').value = bpm; $('bpmVal').textContent = bpm; UI.fillRange($('bpm'));
    if (api) api.playbackSpeed = bpm / baseTempo;
  });
  on('play.bpmMax', m => { $('bpm').max = m; $('autoTarget').max = m; UI.fillRange($('bpm')); });
  // 播放键：alphaTab 在放，或者敲节奏在进行，都显示成“停止”
  on(['play.playing', 'rhythm.running'], () => {
    const now = playing() || !!get('rhythm.running');
    $('playBtn').classList.toggle('on', now);
    $('playBtn').innerHTML = UI.icon(now ? 'stop' : 'play');
    $('playBtn').setAttribute('aria-label', now ? '停止（空格）' : '播放（空格）');
  });
  on('play.playing', (now, was) => {
    updateFollowBtn();
    if (!now && was) setStatus('已暂停', '空格继续 · ←→ 换段 · ↑↓ 调速');
  });
  on('play.ready', r => { if (!r) return; applyAudio(); if (!playing() && !get('rhythm.on')) setStatus('已停止', IDLE_HINT); });
  on(['play.a', 'play.b'], paintLoop);
  on('view', renderSections);
  on('view.source', loadSource);
  on('ui.renderGen', markReviewBars);
  on('ui.notation', () => { if (!api) return; api.settings.display.staveProfile = alphaTab.StaveProfile[staveProfile()]; api.updateSettings(); api.render(); });

  setBpm(50); // 加载前的占位，不算“定下了速度”
  $('playBtn').onclick = () => (playing() || get('rhythm.running') ? stop() : play());
  $('demoBarBtn').onclick = () => { const bi = get('ui.bar') >= 0 ? get('ui.bar') : get('play.a'); setLoop(bi, bi, !playing()); if (!playing()) play(); };
  UI.seg($('timbreSeg'), get('play.timbre'), v => set('play.timbre', v));
  UI.seg($('notationSeg'), 'both', v => set('ui.notation', v));
  $('exportBtn').onclick = exportGp;
  $('bpm').oninput = e => setTempo(+e.target.value);
  $('bpmDown').onclick = () => setTempo(get('play.bpm') - 4);
  $('bpmUp').onclick = () => setTempo(get('play.bpm') + 4);
  $('loopPrev').onclick = () => stepSection(-1);
  $('loopNext').onclick = () => stepSection(1);
  $('followBtn').onclick = scrollToCursor;
  let followTick = 0;
  addEventListener('scroll', () => { if (!followTick) followTick = requestAnimationFrame(() => { followTick = 0; updateFollowBtn(); }); }, { passive: true });
  $('playBtn').innerHTML = UI.icon('play');
  setStatus('加载中…', IDLE_HINT);
}
