// 谱面和播放（alphaTab）：渲染谱面、播放和循环、速度、音色。
// 对外：play / stop / setLoop / toLoopStart / setTempo / setStatus / stepSection / applyAudio / onBeatClick / getApi，
// 以及谱面位置（barBox / scoreRow / rowOfBar / cursorOutOfView / scrollToCursor）和 holdBarWhile / revealLoop。
// 播放状态：play-machine.js 的状态机（这里只把用户操作和 alphaTab 的事件交给它，按它给的 fx 操作 alphaTab）；
// 结果写到 store 的 play 切片（ready、playing、phase、bpm、a..b、loops、指板的 note / next，以及节拍器、音量等设置）。
// alphaTab 的内部结构只通过 at-adapter.js 碰。拆出去的部分：控制条的输入（controls.js）、歌词（lyrics.js）、
// 待核对 / 精修的框（review-marks.js）、导出 GP（gp-export.js）。曲子、改谱、敲节奏通过 hooks.js 挂进来。

import * as alphaTab from '/static/vendor/alphatab/alphaTab.min.mjs';
import { eventIndexAt, scoreNeedsGbk } from '../score.js';
import { get, set, on } from './store.js';
import { $, UI } from './dom.js';
import { stepSection as stepCalc, isScore, beatNumber } from './view.js';
import { initial as machineInitial, step as machineStep, isPlaying } from './play-machine.js';
import * as at from './at-adapter.js';
import { invoke, has as hasHook } from './hooks.js';
import { metronomeGain, countInGain } from './click.js';

export { alphaTab };
// 停着时第二行不写快捷键说明（空格、←→、↑↓、Home、M 写在各个按钮的悬停提示里），只在播放时显示“第几遍”
export const IDLE_HINT = '';

const AT_BASE = '/static/vendor/alphatab/';
// 两个音色库：Sonivox 是完整的通用音色库（节拍器、GP 文件里的贝斯和鼓等都靠它）；
// 木吉他库后加载，alphaTab 查音色时后加载的优先，所以 24 / 25 号用的是真实木吉他采样。
const SF_BASE = '/static/vendor/soundfont/sonivox.sf2', SF_GUITARS = '/static/vendor/soundfont/guitars.sf3';
export const TIMBRES = { steel: 25, nylon: 24 };
export const timbreProgram = () => TIMBRES[get('play.timbre')] ?? 25;

let api = null, baseTempo = 60, soundFontsLoaded = 0, playerReadyOnce = false, warnedUnplayed = '';
// tempoSet：速度已经由用户 / 接着练定下来了（setTempo 调过）。谱子加载好以后只在还没定的时候才用默认速度（谱上标的原速）。
// scoreTempoDone：第一次加载谱子时按原速放宽滑块上限这件事做过了。cursorPending：谱子没加载好时设了循环范围，加载好后把光标放到段首。
let tempoSet = false, scoreTempoDone = false, cursorPending = false, scoreBytes = null;
// 谱面上的点击：pendingBeat 一次点击里先到的拍（见 beatMouseDown）；drag 按着鼠标拖过几个小节；shiftDown 按下时有没有按着 Shift
let pendingBeat = null, drag = null, shiftDown = false;
export const getApi = () => api;

// ---------------------------------------------------------------------
// 播放状态机
// ---------------------------------------------------------------------
let M = machineInitial(true);
/** 唯一改播放状态的地方：事件 → 状态机 → 按 fx 操作 alphaTab → 写回 store */
function dispatch(ev) {
  const was = M.phase, { state, fx } = machineStep(M, ev);
  M = state;
  for (const [k, v] of fx) {
    if (k === 'loop') set('play.loops', get('play.loops') + 1); // 练习记录（practice-log.js）记下这一遍
    else if (k === 'resetLoops') set('play.loops', 0);
    else if (k === 'showBar') set('ui.bar', v);
    else if (!api) continue;
    else if (k === 'seek') api.tickPosition = v;
    else if (k === 'play') { applyAudio(); api.play(); } // 试听时可能临时打开了示范音的音量（见 audition）：开始前按开关设回去
    else if (k === 'pause') api.pause();
    else if (k === 'stop') api.stop();
    else if (k === 'reloadMidi') api.loadMidiForScore();
  }
  // 预备拍开始的时刻（performance.now）：界面要显示“预备 3…2…1”时用它和速度、拍号算
  if (M.phase === 'countIn' && was !== 'countIn') set('play.countInStart', performance.now());
  set({ 'play.playing': isPlaying(M), 'play.phase': M.phase });
}
const playing = () => isPlaying(M);
const nBars = () => (get('view') ? get('view').bars.length : 0);

// 侧栏正在用的时候（小节编辑器开着、正在输入问 AI 的问题）播放光标不切换侧栏的小节，点谱面照常切换
const holds = [];
export function holdBarWhile(fn) { holds.push(fn); }
// 选了新的一段（段落按钮、←→、输入起止小节、继续上次、练习计划）：让 follow.js 把这一段滚到看得见的地方。
// 拖选、点小节、后台刷新不滚：那时用户正看着这里
let revealSeq = 0;
export function revealLoop() { const { a, b } = get('play'); set('ui.reveal', { a, b, seq: ++revealSeq }); }

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

// 谱线（六线谱的弦）有意义：用 --line-strong（卡片底上至少 3:1），不用 --faint
function themeColors() {
  const cs = getComputedStyle(document.documentElement), v = n => cs.getPropertyValue(n).trim() || '#808080';
  return { mainGlyphColor: v('--fg'), secondaryGlyphColor: v('--muted'), staffLineColor: v('--line-strong'), barSeparatorColor: v('--muted'), barNumberColor: v('--muted'), scoreInfoColor: v('--fg') };
}
const staveProfile = () => (get('ui.notation') === 'tab' ? 'Tab' : 'ScoreTab');

function ensureApi() {
  if (api) return api;
  api = new alphaTab.AlphaTabApi($('score'), {
    core: { fontDirectory: AT_BASE + 'font/', includeNoteBounds: true }, // 不开这个 alphaTab 不会触发点击音符事件（试听、改音都靠它）
    display: { scale: 0.9, layoutMode: 'Page', staveProfile: staveProfile(), resources: themeColors() },
    // 曲名、艺人等页面顶栏已经有了，谱面上不再画（省出一大块地方）。alphaTab 署名不受影响。
    // 调弦说明也不画：alphaTab 按降号拼音名（③=Gb ④=Db），和我们全站的升号写法（F♯ C♯，view.js 的 NN）对不上，
    // 又不能改它的拼法；非标准调弦由谱面标题栏的 #tuningLine 用我们的写法显示（song.js），标准调弦什么都不写
    notation: { rhythmMode: 'ShowWithBars', elements: { scoreTitle: false, scoreSubTitle: false, scoreArtist: false, scoreAlbum: false, scoreWords: false, scoreMusic: false, scoreWordsAndMusic: false, scoreCopyright: false, guitarTuning: false, effectLyrics: false } }, // 歌词自己画在六线谱下方（lyrics.js）
    player: {
      playerMode: 'EnabledSynthesizer', soundFont: SF_BASE,
      enableCursor: true, enableAnimatedBeatCursor: true, enableUserInteraction: true,
      enablePlaybackRangeSelection: false, scrollOffsetY: -230,
      scrollMode: 'Off', // 不自动滚动：光标不在视野里时显示“回到播放位置”按钮，点了才滚（见 controls.js）
    },
  });
  api.soundFontLoad.on(e => { if (!get('play.ready')) setStatus('加载音色…', `${Math.round(100 * e.loaded / Math.max(1, e.total))}%`); });
  api.soundFontLoaded.on(() => {
    if (++soundFontsLoaded !== 1) return;
    // 木吉他音色加载失败时退回只用 Sonivox，不能让播放器一直卡在“音色还在加载”
    at.onSoundFontLoadFailed(api, guitarsFailed);
    api.loadSoundFont(SF_GUITARS, true);
  });
  api.playerReady.on(() => {
    playerReadyOnce = true;
    if (soundFontsLoaded < 2) return; // 等木吉他音色也加载完
    setReady();
  });
  api.playerStateChanged.on(e => dispatch({ type: 'state', playing: e.state === 1 }));
  // 弹到段尾 / 曲末（不是 alphaTab 自己循环时）：alphaTab 先发 finished，再停下并把位置放回段首（全曲是开头）
  api.playerFinished.on(() => {
    if (!api || api.isLooping) return; // alphaTab 自己循环：跳回段首时由位置事件计遍数
    const whole = isWhole(), r = at.playbackRange(api);
    dispatch({ type: 'finished', whole, rangeStart: r ? r.start : 0, end: whole ? at.lastPlayedBar(api) : null });
  });
  api.playerPositionChanged.on(e => { const r = at.playbackRange(api); dispatch({ type: 'position', tick: e.currentTick, rangeStart: r ? r.start : 0 }); });
  api.playedBeatChanged.on(beat => onBeat(beat, false));
  // 点音符时 alphaTab 先触发拍、再触发音符：攒到同一轮里，点在音符上就只试听（不改播放位置），点在别处才从这个小节开始播放。
  // 播放中点音符不试听，照常从这个小节开始播放
  api.beatMouseDown.on(b => {
    if (get('ui.editMode') && hasHook('score.editClick')) { invoke('score.editClick', b, null); return; }
    drag = { a: b.voice.bar.index, b: b.voice.bar.index, moved: false };
    if (pendingBeat) return;
    pendingBeat = { beat: b, note: null };
    setTimeout(() => {
      const c = pendingBeat; pendingBeat = null;
      if (c.note && !playing() && get('play.ready')) audition(c.note); else onBeatClick(c.beat);
    }, 0);
  });
  api.noteMouseDown.on(n => {
    if (get('ui.editMode') && hasHook('score.editClick')) invoke('score.editClick', n.beat, n);
    else if (pendingBeat) pendingBeat.note = n;
    else if (!playing() && get('play.ready')) audition(n);
  });
  api.beatMouseMove.on(dragOver); // alphaTab 只在按着鼠标时发这个
  api.beatMouseUp.on(b => { dragOver(b); endDrag(); });
  api.scoreLoaded.on(onScoreLoaded);
  api.renderFinished.on(() => { if (nBars()) applyLoopRange(); });
  api.postRenderFinished.on(() => set('ui.renderGen', (get('ui.renderGen') || 0) + 1)); // 小节位置要等所有部分都画完才有
  api.midiLoaded.on(() => { // 反复的播放时间要等 MIDI 生成后才知道
    applyAudio(); // 音轨音量跟着 MIDI 走：重新生成后再设一次
    const range = nBars() ? applyLoopRange() : null;
    dispatch({ type: 'midiLoaded', range: range && { start: range.start, end: range.end ?? Infinity }, cursorPending });
    if (range) cursorPending = false;
  });
  api.error.on(e => {
    dispatch({ type: 'error' });
    const msg = e && e.message ? e.message : String(e);
    if (hasHook('score.error')) invoke('score.error', msg); else UI.toast('谱面出错：' + msg, 'err', 6000);
  });
  return api;
}

/** 谱面来源（view.source）变了才重新加载：GP 原文件的某个音轨，或我们生成的 alphaTex */
async function loadSource(src) {
  if (!src) return;
  ensureApi();
  if (api.score) dispatch({ type: 'reloadScore' }); // 换掉已经加载好的谱子：记下位置和在不在放，MIDI 好了放回去
  if (src.kind === 'score') {
    let r;
    try { r = await fetch(`/files/${src.id}/source`); } catch { r = { ok: false, status: '连不上服务' }; }
    if (!r.ok) {
      dispatch({ type: 'error' });
      const msg = '曲谱文件加载失败（' + r.status + '）';
      if (hasHook('score.error')) invoke('score.error', msg); else UI.toast(msg, 'err', 6000);
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

// 所有吉他音轨都用选中的木吉他音色（at.applyTimbre）。scoreLoaded 在生成 MIDI 之前同步触发，所以在这里改就会生效；
// 中途切换时重新生成 MIDI（在放的话好了接着放）
function switchTimbre() {
  if (!api || !api.score) return;
  at.applyTimbre(api.score, timbreProgram());
  dispatch({ type: 'timbre' });
}

// 歌词每行多少高（lyrics.js 画在每行六线谱下方留的空里）
export const LYRIC_LINE = 20;
/** 正在显示的音轨（GP 文件可能有好几个音轨，api.load(bytes, [track]) 只画练的那一个） */
const shownTrack = score => (api && api.score === score && api.tracks[0]) || score.tracks[(get('view.source') || {}).track || 0] || score.tracks[0];
/** 这首有几段歌词（最多 4 段） */
export function lyricLines(score) {
  let n = 0;
  for (const b of at.trackBeats(shownTrack(score))) if (b.lyrics) n = Math.max(n, b.lyrics.filter(Boolean).length && b.lyrics.length);
  return Math.min(n, 4);
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
  at.applyTimbre(score, timbreProgram());
  // 和弦图画在和弦变化的拍上方（像弹唱谱原谱那样），不在曲子开头列一排
  score.stylesheet.globalDisplayChordDiagramsInScore = true;
  score.stylesheet.globalDisplayChordDiagramsOnTop = false;
  // 有歌词：每行下面留出几行字的地方（lyrics.js 画在那里）
  // （设置要在渲染前生效：这里已经晚了，变了的话就按新设置再画一次，只在歌词行数变了的时候发生）
  const nl = lyricLines(score), pad = 10 + nl * LYRIC_LINE + (nl ? 8 : 0);
  if (api.settings.display.systemPaddingBottom !== pad) {
    api.settings.display.systemPaddingBottom = api.settings.display.lastSystemPaddingBottom = pad;
    api.updateSettings();
    setTimeout(() => api.render(), 0);
  }
  baseTempo = score.tempo || 60;
  api.playbackSpeed = get('play.bpm') / baseTempo; // 先设好：下面的 hook 失败提前返回时，实际速度也要和显示的 BPM 一致
  if (isScore(song) && hasHook('score.loaded') && !(await invoke('score.loaded', score))) return;
  if (!scoreTempoDone) {
    scoreTempoDone = true;
    const s = get('song'), t = origTempo(s);
    if (t) { // 原速比滑块上限快时把上限放宽，原速也能拖得到（已经定下的速度更快时不收窄）
      const bpm = get('play.bpm');
      set({ 'play.bpmMax': Math.min(240, Math.max(140, Math.ceil(t * 1.25 / 10) * 10, Math.ceil(bpm / 10) * 10)) });
    }
    // 默认就用谱上标的速度（识别出来的 / GP 文件里的）；没有标速度就 50。接着练 / 用户在谱子加载好之前已经定了速度的，不覆盖
    if (!tempoSet) setTempo(t || 50);
  }
  set('play.origBpm', origTempo(get('song'))); // 控制条上的原速刻度和百分比（controls.js）
  if (api) api.playbackSpeed = get('play.bpm') / baseTempo;
}
/** 谱上标的速度（识别出来的 / GP 文件里的）；不知道是 0 */
const origTempo = s => Math.round((s && s.meta && s.meta.tempo) || (isScore(s) ? baseTempo : 0)) || 0;

export function applyAudio() {
  if (!api) return;
  const p = get('play');
  // 示范音（吉他音轨）和节拍器分开调：只开节拍器、关掉示范音也行。不用 masterVolume——它连节拍器一起调小。
  // 节拍器、预备拍的音量和敲节奏共用一套换算（click.js）
  api.masterVolume = 1;
  if (api.score) api.changeTrackVolume(api.score.tracks, p.demo ? p.gtVol / 100 : 0);
  api.metronomeVolume = metronomeGain(p);
  api.countInVolume = countInGain(p);
  if (M.countIn !== api.countInVolume > 0) dispatch({ type: 'countIn', on: api.countInVolume > 0 });
}

// 点音符试听：alphaTab 的 playNote 走的是这个音轨自己的 MIDI 通道，示范音关掉时（applyAudio 把音轨音量设成 0）
// 试听也没声音了。试听前把音轨音量放回吉他音量；开始播放时再按示范音开关设回去
function audition(note) {
  if (!get('play.demo') && api.score) api.changeTrackVolume(api.score.tracks, get('play.gtVol') / 100);
  api.playNote(note);
}

const isWhole = () => { const { a, b } = get('play'); return !!api && !!api.score && a === 0 && b === api.score.masterBars.length - 1; };
/**
 * 按 play.a..b 设 alphaTab 的播放范围、循环方式和谱面高亮。→ {start, end}（全曲 end 是 undefined），谱子没好时 null。
 * 循环全曲：不设范围，反复和 D.S. / D.C. 跳转都照常播放。循环一段：有反复时播放时间和谱面位置不再一一对应，用 tickCache 查
 */
function applyLoopRange() {
  if (!api || !api.score || !api.tracks.length) return null;
  const mbs = api.score.masterBars, { a, b } = get('play');
  if (!mbs[a] || !mbs[b]) return null;
  let range = null;
  if (!isWhole()) {
    range = at.sectionTicks(api, a, b);
    if (!range) { // 按反复 / 跳转记号走，这个小节根本不会被弹到（比如 Fine 之后的小节）
      if (warnedUnplayed !== `${a}-${b}`) UI.toast('按反复记号，选中的小节弹不到，改为循环全曲', 'info', 5000);
      warnedUnplayed = `${a}-${b}`;
    }
  }
  // 设置范围会把播放位置跳回段首：谱面重画（拖窗口、切主题、通知出现）时范围没变，就别再设，否则播放中会跳回段首
  if (at.setPlaybackRange(api, range)) dispatch({ type: 'jumped' }); // 跳回起点不能当成弹完了一遍
  syncLooping();
  at.highlightBars(api, a, b, b - a + 1 >= mbs.length);
  return range || { start: 0 };
}

// 什么时候让 alphaTab 自己循环（段尾无缝跳回段首）：只有循环一段、又没开预备拍时。
// 全曲：弹到曲末停在最后一小节。开着预备拍的段落循环：每一遍前都要数一小节预备拍，alphaTab 只在 play() 时数，
// 所以不让它循环，段尾停下后马上从段首重新开始（状态机的 restarting）
function syncLooping() {
  if (!api || !api.score) return;
  api.isLooping = !isWhole() && !get('play.countIn');
}

/** 谱面上某一小节的位置（相对 #score），没画好时返回 null */
export const barBox = bi => at.barBox(api, $('score'), bi);
/**
 * 谱面上的第 i 行（相对 #score 的上下沿）：包括上面的和弦名 / 和弦图和下面留给歌词的空；
 * 另外给出这一行的头尾小节。没画好或没有这一行时返回 null
 */
export const scoreRow = i => at.scoreRow(api, $('score'), i);
/** 第 bi 小节在第几行；没画好时返回 -1 */
export const rowOfBar = bi => at.rowOfBar(api, bi);

export function play() {
  if (get('rhythm.on') && hasHook('drill.start')) { invoke('drill.start'); return; } // 敲节奏打开时：开始的是敲节奏，不是 alphaTab
  if (!api || !get('play.ready')) { UI.toast('音色加载中', 'info'); return; }
  dispatch({ type: 'play' });
}
export function stop() {
  if (get('rhythm.running')) invoke('drill.stop');
  dispatch({ type: 'stop' });
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
  else if (restart && playing()) dispatch({ type: 'restart', tick: range.start }); // 等播放器真的停下再从段首开始（见 play-machine.js）
  else if (!playing()) dispatch({ type: 'seek', tick: range.start });
  if (get('rhythm.on')) invoke('drill.loopChanged'); // 敲节奏：换成新的段（敲着的话从头再来）
}
/** 回到循环范围的开头（Home 键）：播放中就从段首重新开始（开着预备拍就重新数）；敲节奏从头再敲 */
export function toLoopStart() {
  if (!nBars()) return;
  set('ui.bar', get('play.a'));
  if (get('rhythm.on')) { invoke('drill.loopChanged'); return; }
  const range = applyLoopRange();
  if (range) dispatch({ type: playing() ? 'restart' : 'seek', tick: range.start });
}
/** 上一段 / 下一段 */
export function stepSection(dir) {
  const v = get('view'), { a, b } = get('play');
  if (!v) return;
  const r = stepCalc({ a, b, n: v.bars.length, shown: get('ui.bar'), secs: v.sections }, dir);
  if (!r) return;
  if (r.msg) UI.toast(r.msg, 'info'); else { setLoop(r.a, r.b); revealLoop(); }
}

function onBeat(beat, clicked) {
  const v = get('view');
  if (!v) return;
  const bi = beat.voice.bar.index, evs = v.bars[bi];
  if (!clicked) setStatus(`第 ${bi + 1} 小节 · 第 ${beatNumber(beat.playbackStart, beat.voice.bar.masterBar.timeSignatureDenominator)} 拍`, `第 ${get('play.loops') + 1} 遍`);
  if (bi < v.bars.length && (clicked || !holds.some(f => f()))) set('ui.bar', bi);
  if (!evs || !evs.length) return;
  const cur = evs[eventIndexAt(v.beatMap[bi] || [0], beat.playbackStart)];
  if (!cur) return;
  let next = v.all[cur.idx + 1];
  const { a, b } = get('play');
  if (!next || next.bar > b) next = v.bars[a] && v.bars[a][0];
  set({ 'play.note': cur.idx, 'play.next': next ? next.idx : -1 });
}
// 播放光标所在的小节（alphaTab 画的高亮框）不在视野里（被吸顶的顶栏、控制条、底部指板挡住也算）
export function cursorOutOfView() {
  const el = at.cursorBar($('score'));
  if (!el) return false;
  const r = el.getBoundingClientRect();
  if (!r.height) return false;
  const top = Math.max(0, $('transport').getBoundingClientRect().bottom);
  const d = $('fbDock'), bottom = innerHeight - (d && !d.hidden ? d.offsetHeight : 0);
  return r.bottom < top + 8 || r.top > bottom - 8;
}
/** 滚到播放光标那里（“回到播放位置”按钮） */
export function scrollToCursor() {
  const el = at.cursorBar($('score'));
  if (!el) return;
  const t = $('transport');
  const stuck = parseFloat(getComputedStyle(t).top) + t.offsetHeight; // 滚过去以后控制条吸顶时的下沿
  scrollTo({ top: scrollY + el.getBoundingClientRect().top - stuck - 16, behavior: 'smooth' });
}

// 按住鼠标拖过几个小节：循环这几个小节（像拖选文字，往前往后拖都行；拖回起点就是只练起点这一小节）。
// 按下时照常（从这个小节开始播放 / 试听），拖到别的小节才算拖选，拖的过程中高亮范围、控制条上的起止小节跟着变
// （ui.loopPreview，controls.js 显示），松开时定下。改音模式里按下是改音，不拖选
function dragOver(beat) {
  if (!drag || !beat || !api) return;
  const bi = beat.voice.bar.index;
  if (bi >= nBars() || bi === drag.b) return;
  drag.b = bi;
  drag.moved = true;
  $('score').classList.add('dragging');
  const lo = Math.min(drag.a, bi), hi = Math.max(drag.a, bi);
  at.highlightBars(api, lo, hi);
  set('ui.loopPreview', { a: lo, b: hi });
}
function endDrag() {
  const d = drag;
  drag = null;
  if (!d || !d.moved) return;
  $('score').classList.remove('dragging');
  set('ui.loopPreview', null);
  setLoop(Math.min(d.a, d.b), Math.max(d.a, d.b));
}

// 点谱面上的小节：从这个小节开始播放（不改循环范围；点到范围外面就改成全曲，好从这里一直往下放）。
// 要循环就点“只练这一小节”，或 Shift + 点把范围扩到这个小节。敲节奏练的是一段，点小节仍然是选这一小节。
export function onBeatClick(beat) {
  const bi = beat.voice.bar.index;
  if (bi >= nBars()) return;
  const { a, b } = get('play');
  if (shiftDown) setLoop(Math.min(a, bi), Math.max(b, bi));
  else if (get('rhythm.on')) setLoop(bi, bi);
  else {
    if (bi < a || bi > b) setLoop(0, nBars() - 1, false, false);
    seekBar(bi);
    set('ui.bar', bi);
  }
  onBeat(beat, true);
}

/** 播放位置跳到第 bi 小节开头（播放中就从那里接着放）。有反复时取落在当前循环范围里的那一遍。 */
function seekBar(bi) {
  if (!api || !api.score || !api.score.masterBars[bi]) return;
  dispatch({ type: 'seek', tick: at.barStartTick(api, bi, at.playbackRange(api)) });
}

export function refreshScoreTheme() {
  if (!api) return;
  const res = api.settings.display.resources, c = themeColors();
  for (const k in c) res[k] = alphaTab.model.Color.fromJson(c[k]);
  api.updateSettings(); api.render();
}

// ---------------------------------------------------------------------
// 预备拍：状态栏倒数“预备 3…2…1”，每一拍谱面上的光标和底部指板轻轻闪一下（第一拍重一点）。
// 按 play.countInStart（状态机进入预备拍的时刻）、速度和开始那一小节的拍号算（alphaTab 数一小节：分子个拍，每拍一个分母音符）
// ---------------------------------------------------------------------
let ciRaf = 0;
function meterAt(bi) {
  const s = get('song'), b = s && s.bars[bi];
  return (b && b.time_signature) || (s && s.meta && s.meta.time_signature) || [4, 4];
}
function pulse(el, strong) {
  if (!el || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  el.classList.remove('ci-beat', 'ci-strong');
  void el.offsetWidth; // 重新开始动画
  el.classList.add('ci-beat');
  if (strong) el.classList.add('ci-strong');
}
function countIn() {
  cancelAnimationFrame(ciRaf);
  const shown = get('ui.bar'), { a, b } = get('play');
  const [num, den] = meterAt(shown >= a && shown <= b ? shown : a);
  let last = -1;
  const frame = () => {
    if (get('play.phase') !== 'countIn') { ['score', 'fbDock'].forEach(id => $(id).classList.remove('ci-beat', 'ci-strong')); return; }
    const beat = 60 / (get('play.bpm') || 60) * 4 / (den || 4) * 1000;
    const k = Math.max(0, Math.floor((performance.now() - (get('play.countInStart') || performance.now())) / beat));
    if (k !== last && k < num) {
      last = k;
      setStatus(`预备 ${num - k}…`, `${get('play.bpm')} BPM · 第 ${get('play.loops') + 1} 遍`);
      pulse($('score'), k === 0); pulse($('fbDock'), k === 0);
    }
    ciRaf = requestAnimationFrame(frame);
  };
  frame();
}

/** 状态栏两行（窄的时候会截断：悬停看全文） */
export function setStatus(l1, l2) { $('statusL1').textContent = l1; $('statusL2').textContent = l2; $('statusL1').parentElement.title = l2 ? `${l1}\n${l2}` : l1; }

/** 给 e2e 测试和调试用：状态机现在的状态 */
export const machineState = () => M;

export function initPlayer() {
  // Shift + 点：alphaTab 的点击事件里没有按键状态，在谱面的 mousedown（捕获阶段，比 alphaTab 先）里记下。
  // 不用全局 keydown / keyup：按着 Shift 切到别的应用时 keyup 会丢，之后每次点小节都成了扩展循环
  $('score').addEventListener('mousedown', e => { shiftDown = e.shiftKey; }, true);
  document.addEventListener('mouseup', endDrag); // 在谱面外面松开（alphaTab 收不到）也定下范围
  on(['play.metro', 'play.metroVol', 'play.countIn', 'play.gtVol', 'play.demo'], applyAudio);
  on('play.countIn', syncLooping); // 预备拍开关决定段落循环是 alphaTab 自己循环，还是每遍停下重新开始
  on('play.timbre', switchTimbre);
  on('play.bpm', bpm => { if (api) api.playbackSpeed = bpm / baseTempo; });
  on('play.playing', (now, was) => { if (!now && was) setStatus('已暂停', IDLE_HINT); });
  on('play.phase', ph => { if (ph === 'countIn') countIn(); });
  on('play.ready', r => { if (!r) return; applyAudio(); if (!playing() && !get('rhythm.on')) setStatus('已停止', IDLE_HINT); });
  // 离开练习页（调弦、练习建议…）时停下：播放键和快捷键都只在练习页，切走就停不了了；调弦的麦克风也会听到它。
  // 预备拍、节拍器是同一个播放器，一起停；谱子正在重新加载、本来要接着放的也不放了（状态机的 stop 都管）
  on('ui.view', v => { if (v !== 'practice') stop(); });
  on('view.source', loadSource);
  on('ui.notation', () => { if (!api) return; api.settings.display.staveProfile = alphaTab.StaveProfile[staveProfile()]; api.updateSettings(); api.render(); });
  setBpm(50); // 加载前的占位，不算“定下了速度”
  setStatus('加载中…', IDLE_HINT);
}
