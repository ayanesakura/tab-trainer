// 谱面和播放（alphaTab）：渲染谱面、播放和循环、速度、音色、导出 GP，以及吸顶的控制条。
// 对外：play / stop / setLoop / setTempo / setStatus / stepSection / applyAudio / exportGp / getApi。
// 状态都在 store 的 play 切片里（ready、playing、bpm、a..b、loops、指板的 note / next，以及节拍器、音量等设置）。

import * as alphaTab from '/static/vendor/alphatab/alphaTab.min.mjs';
import { TICKS, CHORD_PAD, eventIndexAt, scoreNeedsGbk } from '../score.js';
import { get, set, on } from './store.js';
import { $, UI } from './dom.js';
import { parseBarInput, stepSection as stepCalc, isScore, beatNumber } from './view.js';
import { PREFS_KEY, PREF_DEFAULTS, parsePrefs, serializePrefs, clampPref } from './prefs.js';

export { alphaTab };
// 停着时第二行不写快捷键说明（空格、←→、↑↓ 写在各个按钮的悬停提示里），只在播放时显示“第几遍”
export const IDLE_HINT = '';

const AT_BASE = '/static/vendor/alphatab/';
// 两个音色库：Sonivox 是完整的通用音色库（节拍器、GP 文件里的贝斯和鼓等都靠它）；
// 木吉他库后加载，alphaTab 查音色时后加载的优先，所以 24 / 25 号用的是真实木吉他采样。
const SF_BASE = '/static/vendor/soundfont/sonivox.sf2', SF_GUITARS = '/static/vendor/soundfont/guitars.sf3';
export const TIMBRES = { steel: 25, nylon: 24 };

let pendingBeat = null; // 谱面上一次点击里先到的拍（见 beatMouseDown）
let api = null, baseTempo = 60, soundFontsLoaded = 0, playerReadyOnce = false, lastTick = -1, shiftDown = false, warnedUnplayed = '';
// tempoSet：速度已经由用户 / 接着练定下来了（setTempo 调过）。谱子加载好以后只在还没定的时候才用默认速度（谱上标的原速）。
// scoreTempoDone：第一次加载谱子时按原速放宽滑块上限这件事做过了。cursorPending：谱子没加载好时设了循环范围，加载好后把光标放到段首。
let tempoSet = false, scoreTempoDone = false, cursorPending = false, scoreBytes = null;
// 预备拍：alphaTab 1.9 在预备拍里停下以后状态清不干净：只 pause() 的话下次 play() 会一直停在“播放中”却不前进
// （图标变了、没有声音），直接 stop() 又会被当成“预备拍弹完了”转入播放。所以预备拍里停就重新生成一次 MIDI 把它清掉，
// 好了以后（midiLoaded）把位置放回去；这期间按了播放就等它好了再开始。状态和转换都在 countIn 里：
//   begin（开始播放）→ from = 开始的位置；advance（位置往前走了）→ 预备拍结束，from = null
//   pause（在预备拍里停）→ reload = {tick: from, play: false}，调用方重新生成 MIDI
//   queuePlay（重新生成期间按播放）→ reload.play = true；reloaded（MIDI 好了）→ 取出 reload 并清掉
const countIn = {
  from: null, // 预备拍进行中：开始播放时的位置；不在预备拍里是 null
  reload: null, // 预备拍里停下、MIDI 正在重新生成：{tick: 好了以后放回的位置, play: 好了以后接着播放}
  begin(tick, on) { this.from = on ? tick : null; },
  advance(tick) { if (this.from !== null && tick > this.from + 5) this.from = null; },
  /** 停下；返回 true 表示停在预备拍里，需要重新生成 MIDI */
  pause() {
    if (this.reload) this.reload.play = false;
    if (this.from === null) return false;
    this.reload = { tick: this.from, play: false };
    this.from = null;
    return true;
  },
  /** 按了播放：MIDI 还在重新生成时记下来、返回 true（等好了再开始） */
  queuePlay() { if (!this.reload) return false; this.reload.play = true; return true; },
  reloaded() { const r = this.reload; this.reload = null; return r; },
  /** 预备拍里（或停下后重新生成期间）用户换了位置（点小节、换段）：之后停下 / 生成好时放回的是新位置，不是开始播放时的位置 */
  seek(tick) { if (this.from !== null) this.from = tick; if (this.reload) this.reload.tick = tick; },
};
// 谱子重新加载（改音、撤销、改曲子信息、识别出新的一行……都会重新生成 alphaTex）：alphaTab 会停下播放、把位置放回 0，
// 停下前后还会发几个往回跳的位置事件（会被 onPosition 当成“弹完一遍”：记练习）。
// 重新加载前记下位置和是否在播放：{tick, play}；MIDI 好了以后（midiLoaded）放回去（不在循环范围里就放到段首），
// 原来在播放就接着播放。这期间的位置事件都不算
let reloading = null;
export const getApi = () => api;

// 曲子相关的处理由 song.js / editor.js 注册，敲节奏由 rhythm-drill.js 注册 drill: {start, stop, loopChanged}（播放模块不反过来依赖它们）
const hooks = { scoreLoaded: null, error: null, editClick: null, drill: null };
export function setPlayerHooks(h) { Object.assign(hooks, h); }
// 侧栏正在用的时候（小节编辑器开着、正在输入问 AI 的问题）播放光标不切换侧栏的小节，点谱面照常切换
const holds = [];
// 选了新的一段（段落按钮、←→、输入起止小节、继续上次、练习计划）：让 follow.js 把这一段滚到看得见的地方。
// 拖选、点小节、后台刷新不滚：那时用户正看着这里
let revealSeq = 0;
export function revealLoop() { const { a, b } = get('play'); set('ui.reveal', { a, b, seq: ++revealSeq }); }
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

// 谱线（六线谱的弦）有意义：用 --line-strong（卡片底上至少 3:1），不用 --faint
function themeColors() {
  const cs = getComputedStyle(document.documentElement), v = n => cs.getPropertyValue(n).trim() || '#808080';
  return { mainGlyphColor: v('--fg'), secondaryGlyphColor: v('--muted'), staffLineColor: v('--line-strong'), barSeparatorColor: v('--muted'), barNumberColor: v('--muted'), scoreInfoColor: v('--fg') };
}
const CHORD_NOTATION_KEY = 'tt.notation.chordSheet';
const hasChords = song => !!song && (song.bars || []).some(b => (b.events || []).some(e => e.chord));
const staveProfile = () => (get('ui.notation') === 'tab' ? 'Tab' : 'ScoreTab');

function ensureApi() {
  if (api) return api;
  api = new alphaTab.AlphaTabApi($('score'), {
    core: { fontDirectory: AT_BASE + 'font/', includeNoteBounds: true }, // 不开这个 alphaTab 不会触发点击音符事件（试听、改音都靠它）
    display: { scale: 0.9, layoutMode: 'Page', staveProfile: staveProfile(), resources: themeColors() },
    // 曲名、艺人等页面顶栏已经有了，谱面上不再画（省出一大块地方）；调弦说明保留。alphaTab 署名不受影响
    notation: { rhythmMode: 'ShowWithBars', elements: { scoreTitle: false, scoreSubTitle: false, scoreArtist: false, scoreAlbum: false, scoreWords: false, scoreMusic: false, scoreWordsAndMusic: false, scoreCopyright: false, effectLyrics: false } }, // 歌词自己画在六线谱下方（drawLyrics）
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
  api.playerStateChanged.on(e => {
    // 段尾停下、马上带着预备拍重新开始（见 onFinished）：不当成停下，播放键、练习记录都不动
    if (e.state !== 1 && again) { again = false; startPlayer(); return; }
    set('play.playing', e.state === 1);
    if (e.state !== 1 && endBar) placeAtEnd();
  });
  api.playerFinished.on(onFinished);
  api.playerPositionChanged.on(e => onPosition(e.currentTick));
  api.playedBeatChanged.on(beat => onBeat(beat, false));
  // 点音符时 alphaTab 先触发拍、再触发音符：攒到同一轮里，点在音符上就只试听（不改播放位置），点在别处才从这个小节开始播放。
  // 播放中点音符不试听，照常从这个小节开始播放
  api.beatMouseDown.on(b => {
    if (get('ui.editMode') && hooks.editClick) { hooks.editClick(b, null); return; }
    drag = { a: b.voice.bar.index, b: b.voice.bar.index, moved: false };
    if (pendingBeat) return;
    pendingBeat = { beat: b, note: null };
    setTimeout(() => {
      const c = pendingBeat; pendingBeat = null;
      if (c.note && !playing() && get('play.ready')) audition(c.note); else onBeatClick(c.beat);
    }, 0);
  });
  api.noteMouseDown.on(n => {
    if (get('ui.editMode') && hooks.editClick) hooks.editClick(n.beat, n);
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
    const back = reloading;
    reloading = null; lastTick = -1;
    if (!nBars()) return;
    const range = applyLoopRange();
    if (range && cursorPending && !playing()) api.tickPosition = range.startTick;
    if (range) cursorPending = false;
    const r = countIn.reloaded(); // 预备拍里停下后的重新生成（见 pausePlayer）：放回原来的位置
    if (back && range) { // 谱子重新加载：放回原来的位置（落在循环范围外面就放到段首）
      const end = range.endTick ?? Infinity;
      api.tickPosition = back.tick >= range.startTick && back.tick < end ? back.tick : range.startTick;
      if (back.play || (r && r.play)) startPlayer();
    } else if (r) { api.tickPosition = r.tick; if (r.play) startPlayer(); }
  });
  api.error.on(e => {
    reloading = null;
    const msg = e && e.message ? e.message : String(e);
    if (hooks.error) hooks.error(msg); else UI.toast('谱面出错：' + msg, 'err', 6000);
  });
  return api;
}

/** 谱面来源（view.source）变了才重新加载：GP 原文件的某个音轨，或我们生成的 alphaTex */
async function loadSource(src) {
  if (!src) return;
  ensureApi();
  if (api.score && !reloading) { // 换掉已经加载好的谱子（见 reloading）；连着重新加载两次时留着第一次记下的
    reloading = { tick: countIn.from ?? api.tickPosition, play: playing() || !!(countIn.reload && countIn.reload.play) };
    countIn.from = null;
    lastTick = -1;
  }
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
  // 和弦图画在和弦变化的拍上方（像弹唱谱原谱那样），不在曲子开头列一排
  score.stylesheet.globalDisplayChordDiagramsInScore = true;
  score.stylesheet.globalDisplayChordDiagramsOnTop = false;
  // 有歌词：每行下面留出几行字的地方（drawLyrics 画在那里）
  // （设置要在渲染前生效：这里已经晚了，变了的话就按新设置再画一次，只在歌词行数变了的时候发生）
  const pad = 10 + lyricLines(score) * LYRIC_LINE + (lyricLines(score) ? 8 : 0);
  if (api.settings.display.systemPaddingBottom !== pad) {
    api.settings.display.systemPaddingBottom = api.settings.display.lastSystemPaddingBottom = pad;
    api.updateSettings();
    setTimeout(() => api.render(), 0);
  }
  baseTempo = score.tempo || 60;
  api.playbackSpeed = get('play.bpm') / baseTempo; // 先设好：下面的 hook 失败提前返回时，实际速度也要和显示的 BPM 一致
  if (isScore(song) && hooks.scoreLoaded && !(await hooks.scoreLoaded(score))) return;
  if (!scoreTempoDone) {
    scoreTempoDone = true;
    const s = get('song'), t = (s.meta && s.meta.tempo) || (isScore(s) ? baseTempo : 0);
    if (t) { // 原速比滑块上限快时把上限放宽，原速也能拖得到（已经定下的速度更快时不收窄）
      const bpm = get('play.bpm');
      set({ 'play.bpmMax': Math.min(240, Math.max(140, Math.ceil(t * 1.25 / 10) * 10, Math.ceil(bpm / 10) * 10)) });
    }
    // 默认就用谱上标的速度（识别出来的 / GP 文件里的）；没有标速度就 50。接着练 / 用户在谱子加载好之前已经定了速度的，不覆盖
    if (!tempoSet) setTempo(t || 50);
  }
  if (api) api.playbackSpeed = get('play.bpm') / baseTempo;
}

export function applyAudio() {
  if (!api) return;
  const p = get('play');
  // 示范音（吉他音轨）和节拍器分开调：只开节拍器、关掉示范音也行。不用 masterVolume——它连节拍器一起调小
  api.masterVolume = 1;
  if (api.score) api.changeTrackVolume(api.score.tracks, p.demo ? p.gtVol / 100 : 0);
  api.metronomeVolume = p.metro ? p.metroVol / 100 * 1.2 : 0;
  api.countInVolume = p.countIn ? 1 : 0;
}

// 点音符试听：alphaTab 的 playNote 走的是这个音轨自己的 MIDI 通道，示范音关掉时（applyAudio 把音轨音量设成 0）
// 试听也没声音了。试听前把音轨音量放回吉他音量；开始播放时 startPlayer 再按示范音开关设回去
function audition(note) {
  if (!get('play.demo') && api.score) api.changeTrackVolume(api.score.tracks, get('play.gtVol') / 100);
  api.playNote(note);
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
  syncLooping();
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

/**
 * 谱面上的第 i 行（相对 #score 的上下沿）：包括上面的和弦名 / 和弦图和下面留给歌词的空；
 * 另外给出这一行的头尾小节。没画好或没有这一行时返回 null
 */
export function scoreRow(i) {
  const look = api && (api.boundsLookup || (api.renderer && api.renderer.boundsLookup)), surface = $('score').querySelector('.at-surface');
  const sys = look && surface && i >= 0 && look.staffSystems[i];
  if (!sys || !sys.bars.length) return null;
  const r = sys.realBounds;
  return { i, y: surface.offsetTop + r.y, h: r.h, a: sys.bars[0].index, b: sys.bars[sys.bars.length - 1].index };
}
/** 第 bi 小节在第几行；没画好时返回 -1 */
export function rowOfBar(bi) {
  const look = api && (api.boundsLookup || (api.renderer && api.renderer.boundsLookup));
  const mb = look && bi >= 0 && look.findMasterBarByIndex(bi);
  return mb && mb.staffSystemBounds ? look.staffSystems.indexOf(mb.staffSystemBounds) : -1;
}

// 歌词：alphaTab 只看六线谱时把歌词画在谱表上方（和弦图上面），看着像是上一行的；这里自己画在每行六线谱的下方，
// 对准各拍的位置，多段歌词一段一行
const LYRIC_LINE = 20;
// 画出来的音轨：GP 文件可能有好几个音轨，api.load(bytes, [track]) 只画练的那一个（不一定是第 0 个）
const shownTrack = score => (api && api.score === score && api.tracks[0]) || score.tracks[(get('view.source') || {}).track || 0] || score.tracks[0];
const beatsOf = score => shownTrack(score).staves[0].bars.flatMap(b => b.voices.flatMap(v => v.beats));
function lyricLines(score) {
  let n = 0;
  for (const b of beatsOf(score)) if (b.lyrics) n = Math.max(n, b.lyrics.filter(Boolean).length && b.lyrics.length);
  return Math.min(n, 4);
}
function drawLyrics() {
  let layer = $('lyricLayer');
  if (!layer) { layer = document.createElement('div'); layer.id = 'lyricLayer'; $('score').appendChild(layer); }
  layer.replaceChildren();
  const look = api && (api.boundsLookup || (api.renderer && api.renderer.boundsLookup)), surface = $('score').querySelector('.at-surface');
  if (!look || !surface || !api.score || !lyricLines(api.score)) return;
  const seen = new Set(); // 五线谱 + 六线谱都显示时同一个拍在两行谱表里各有一份位置：歌词只写一次
  for (const sys of look.staffSystems) {
    // 一行的范围包括下面留的空（systemPaddingBottom）：歌词从这段空白的顶上开始写
    const bottom = surface.offsetTop + sys.realBounds.y + sys.realBounds.h - api.settings.display.systemPaddingBottom;
    for (const mb of sys.bars) for (const bar of mb.bars) for (const bb of bar.beats) {
      const lyr = bb.beat.lyrics;
      if (!lyr || bb.beat.voice.index !== 0 || seen.has(bb.beat)) continue;
      seen.add(bb.beat);
      lyr.forEach((text, k) => {
        if (!text) return;
        const el = document.createElement('span');
        el.className = 'lyric';
        el.textContent = text;
        Object.assign(el.style, { left: surface.offsetLeft + (bb.onNotesX || bb.visualBounds.x) + 'px', top: bottom + 2 + k * LYRIC_LINE + 'px' });
        layer.appendChild(el);
      });
    }
  }
}

function markReviewBars() {
  // 在谱面上这一小节的位置画一层框（alphaTab 只给每行第一个小节标号，染小节号不够用）：
  // 待核对的淡橙色；精修改过的淡绿色（两样都有时显示待核对）；正在精修某个小节 / 某一行时，还没核对完的小节加一圈闪动的框和转圈
  let layer = $('warnLayer');
  if (!layer) { layer = document.createElement('div'); layer.id = 'warnLayer'; $('score').appendChild(layer); }
  layer.replaceChildren();
  const v = get('view'), s = get('song');
  if (!v || !s) return;
  const a = s.activity, pending = new Set(a && a.kind === 'refine' && a.scope !== 'song' ? a.pending || [] : []);
  const box = (i, cls, title) => {
    const b = barBox(i);
    if (!b) return null;
    const el = document.createElement('div');
    el.className = cls;
    Object.assign(el.style, { left: b.x + 'px', top: b.y + 'px', width: b.w + 'px', height: b.h + 'px' });
    el.title = title;
    layer.appendChild(el);
    return el;
  };
  v.review.forEach((need, i) => {
    const sb = s.bars[i];
    if (need) box(i, 'warn-mark', `第 ${i + 1} 小节待核对`);
    else if (sb && sb.refine) box(i, 'refine-mark', `第 ${i + 1} 小节精修改过`);
    if (sb && pending.has(sb.id)) {
      const el = box(i, 'refining-mark', `第 ${i + 1} 小节精修中`);
      if (el) el.innerHTML = '<span class="spinner"></span>';
    }
  });
}

export function play() {
  if (get('rhythm.on') && hooks.drill) { hooks.drill.start(); return; } // 敲节奏打开时：开始的是敲节奏，不是 alphaTab
  if (!api || !get('play.ready')) { UI.toast('音色加载中', 'info'); return; }
  if (endTick !== null) { api.tickPosition = 0; endTick = null; } // 全曲弹完停在最后：再播放从头开始
  set('play.loops', 0); lastTick = -1;
  if (countIn.queuePlay()) return; // 预备拍里停过、MIDI 还在重新生成：好了再开始
  startPlayer();
}
export function stop() {
  again = false; // 段尾停下、正要带着预备拍重新开始时按了停止：就停下
  if (get('rhythm.running') && hooks.drill) hooks.drill.stop();
  pausePlayer();
}
function startPlayer() {
  applyAudio(); // 试听时可能临时打开了示范音的音量（见 audition）：开始播放前按开关设回去
  countIn.begin(api.tickPosition, api.countInVolume > 0);
  api.play();
}
function pausePlayer() {
  const reload = countIn.pause();
  if (!api) return;
  api.pause();
  if (reload) api.loadMidiForScore();
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
  endTick = null;
  const range = applyLoopRange();
  if (show) set('ui.bar', na);
  if (!range) cursorPending = true; // 谱子还没加载好：加载好以后（midiLoaded）再把光标放到段首
  if (range && api) {
    if (playing() && restart) { lastTick = -1; api.stop(); set('play.loops', 0); startPlayer(); } // stop() 跳回段首：不是弹完了一遍
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
  if (r.msg) UI.toast(r.msg, 'info'); else { setLoop(r.a, r.b); revealLoop(); }
}

// 什么时候让 alphaTab 自己循环（段尾无缝跳回段首）：只有循环一段、又没开预备拍时。
// 全曲：弹到曲末停在最后一小节。开着预备拍的段落循环：每一遍前都要数一小节预备拍，alphaTab 只在 play() 时数，
// 所以不让它循环，段尾停下后马上从段首重新 play()（见 onFinished / again）
function syncLooping() {
  if (!api || !api.score) return;
  const { a, b } = get('play'), whole = a === 0 && b === api.score.masterBars.length - 1;
  api.isLooping = !whole && !get('play.countIn');
}
// 弹到段尾 / 曲末（isLooping = false）：alphaTab 先发 finished，再停下并把位置放回段首（全曲是开头）。这里先算弹完一遍；
// 循环一段：停下（playerStateChanged）后马上从段首重新开始，开始时数预备拍（again）。
// 全曲：等停下以后把光标放到最后弹到的那一小节（有 D.C. al Fine 时是 Fine 那一小节）。
// endTick：停在曲末的位置，下次播放从头开始；停下后点了别的小节、换了循环范围就不算了（seekBar / setLoop 清掉）
let endBar = null, endTick = null, again = false;
function onFinished() {
  if (!api || api.isLooping) return; // alphaTab 自己循环：跳回段首时 onPosition 计遍数
  set('play.loops', get('play.loops') + 1); // 练习记录（practice-log.js）记下这一遍
  lastTick = -1; // 停下时跳回段首的那次位置变化不是又弹完一遍
  const { a, b } = get('play'), whole = a === 0 && b === api.score.masterBars.length - 1;
  if (!whole) { again = true; return; }
  const tc = api.tickCache, played = tc && tc.masterBars ? tc.masterBars.filter(m => m.end > m.start) : [];
  endBar = played.length ? played[played.length - 1] : null;
}
function placeAtEnd() {
  const m = endBar;
  endBar = null;
  endTick = m.start;
  lastTick = -1;
  api.tickPosition = m.start;
  set('ui.bar', m.masterBar.index);
}

function onPosition(tick) {
  if (reloading) return; // 谱子重新加载中：往回跳的位置不是弹完了一遍
  // 预备拍期间位置停在开始处不动（拍子事件倒可能先来，不能拿它判断），位置往前走了才算预备拍结束
  countIn.advance(tick);
  // 只有从段尾跳回段首才算弹完一遍；重新设置循环范围、停止时也会跳回起点，那些不算（applyLoopRange 会重置 lastTick）
  const r = api && api.playbackRange, start = r ? r.startTick : 0;
  if (playing() && lastTick >= 0 && tick < lastTick - 10 && tick <= start + TICKS) {
    set('play.loops', get('play.loops') + 1); // 练习记录（practice-log.js）记下刚弹完的这一遍
  }
  lastTick = tick;
}

function onBeat(beat, clicked) {
  const v = get('view');
  if (!v) return;
  const bi = beat.voice.bar.index, evs = v.bars[bi];
  if (!clicked) updateFollowBtn();
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

// 按住鼠标拖过几个小节：循环这几个小节（像拖选文字，往前往后拖都行；拖回起点就是只练起点这一小节）。
// 按下时照常（从这个小节开始播放 / 试听），拖到别的小节才算拖选，拖的过程中高亮范围、控制条上的起止小节跟着变，松开时定下。
// 改音模式里按下是改音，不拖选
let drag = null; // { a: 按下的小节, b: 现在拖到的小节, moved: 拖到过别的小节 }
function dragOver(beat) {
  if (!drag || !beat || !api) return;
  const bi = beat.voice.bar.index;
  if (bi >= nBars() || bi === drag.b) return;
  drag.b = bi;
  drag.moved = true;
  $('score').classList.add('dragging');
  const lo = Math.min(drag.a, bi), hi = Math.max(drag.a, bi), bars = api.tracks[0].staves[0].bars;
  const last = bars[hi].voices[0].beats;
  api.highlightPlaybackRange(bars[lo].voices[0].beats[0], last[last.length - 1]);
  paintLoop({ a: lo, b: hi });
}
function endDrag() {
  const d = drag;
  drag = null;
  if (!d || !d.moved) return;
  $('score').classList.remove('dragging');
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
  const mb = api.score.masterBars[bi], tc = api.tickCache, r = api.playbackRange;
  const runs = (tc && tc.masterBars ? tc.masterBars.filter(m => m.masterBar === mb) : []).filter(m => m.end > m.start);
  const inRange = runs.find(m => !r || (m.start >= r.startTick && m.start < r.endTick));
  const t = inRange ? inRange.start : runs.length ? runs[0].start : mb.start;
  lastTick = -1; // 往回跳不能当成弹完了一遍
  endTick = null;
  api.tickPosition = t;
  countIn.seek(t); // 预备拍里点的：之后停下时放回这里，不是开始播放的位置
}

export function exportGp() {
  if (!api || !api.score) return;
  applyTimbre(api.score, true); // 导出原来的音色，不是练习时临时换的木吉他
  // 和弦名去掉为了区分大小写加的零宽空格（见 score.js 的 CHORD_PAD）：GP 文件里和弦按编号引用，不会再撞车
  const chords = api.score.tracks.flatMap(t => t.staves.flatMap(st => [...(st.chords ? st.chords.values() : [])]));
  const names = chords.map(c => c.name);
  chords.forEach(c => { c.name = String(c.name || '').replaceAll(CHORD_PAD, ''); });
  let bytes;
  try { bytes = new alphaTab.exporter.Gp7Exporter().export(api.score, api.settings); } finally {
    applyTimbre(api.score);
    chords.forEach((c, i) => { c.name = names[i]; });
  }
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
    btn.onclick = () => { setLoop(a, z); revealLoop(); };
    if (!all && v.review.slice(a, z + 1).some(Boolean)) { btn.classList.add('warn'); btn.title = '这段有待核对的小节'; }
    host.appendChild(btn);
  };
  add('全曲', 0, v.bars.length - 1, true); // 放最前面：窄屏时也不会被挤出可见范围
  v.sections.forEach(g => add(g.a === g.z ? `${g.a + 1}` : `${g.a + 1}–${g.z + 1}`, g.a, g.z));
  paintLoop();
}
/** 控制条上的起止小节（输入框）和段落按钮；preview：拖选过程中先显示拖到的范围 */
function paintLoop(preview) {
  const n = nBars(), { a, b } = preview || get('play');
  for (const [id, v] of [['loopA', a], ['loopB', b]]) {
    const el = $(id);
    el.disabled = !n;
    if (document.activeElement !== el) el.value = n ? v + 1 : '';
  }
  if (preview) return;
  $('secBtns').querySelectorAll('button').forEach(x => {
    const on_ = +x.dataset.a === a && +x.dataset.z === b;
    x.classList.toggle('on', on_);
    x.setAttribute('aria-pressed', on_);
    if (on_) x.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  });
}

// 跟着曲子走的默认值（加载谱子后按原速调整）；其余播放设置记在本机，见 prefs.js
const SONG_DEFAULTS = { bpmMax: 140 };
const stored = k => { try { return localStorage.getItem(k); } catch { return null; } };
const remember = (k, v) => { try { localStorage.setItem(k, v); } catch { /* 只在这次打开时生效 */ } };

/** 控制条和“更多”弹层里的输入框 ⇄ store 的 play 切片 */
function bindInputs() {
  const bind = (id, key, kind) => {
    const el = $(id);
    if (kind === 'check') el.addEventListener('change', () => set('play.' + key, el.checked));
    else {
      // 数字：打字过程中（清空了、只打了负号、超出范围）先不改设置；输完（change：回车、离开输入框）夹到范围里并写回输入框
      const num = () => clampPref(key, el.value === '' ? NaN : +el.value, el.max === '' ? Infinity : +el.max);
      el.addEventListener('input', () => { const v = num(); if (v !== null && v === +el.value) set('play.' + key, v); });
      el.addEventListener('change', () => { const v = num() ?? get('play.' + key); set('play.' + key, v); el.value = v; if (el.type === 'range') UI.fillRange(el); });
    }
    const paint = v => { if (kind === 'check') el.checked = v; else if (+el.value !== v) el.value = v; if (el.type === 'range') UI.fillRange(el); };
    on('play.' + key, paint);
    paint(get('play.' + key));
  };
  bind('metro', 'metro', 'check'); bind('metroVol', 'metroVol'); bind('countIn', 'countIn', 'check'); bind('gtVol', 'gtVol'); bind('demo', 'demo', 'check');
  bind('follow', 'follow', 'check');
}

// 起止小节输入框：回车或离开输入框时生效，Esc 放弃，↑↓ 加减一小节（马上生效）。
// 起点改到终点后面时终点跟过去（反过来也一样），这样先改起点再改终点不会被对调
function bindLoopInput(id, isStart) {
  const el = $(id);
  const apply = bi => {
    const { a, b } = get('play');
    const na = isStart ? bi : Math.min(a, bi), nb = isStart ? Math.max(b, bi) : bi;
    if (na !== a || nb !== b) { setLoop(na, nb); revealLoop(); }
  };
  // 点进去就全选，直接打新的数（鼠标点进来时松开鼠标会把全选取消掉：这一次的 mouseup 不让它动）
  let justFocused = false;
  el.addEventListener('focus', () => { el.select(); justFocused = true; });
  el.addEventListener('mouseup', e => { if (justFocused) e.preventDefault(); justFocused = false; });
  el.addEventListener('blur', () => { justFocused = false; });
  el.addEventListener('change', () => {
    const bi = parseBarInput(el.value, nBars());
    if (bi === null) { if (el.value.trim()) UI.toast(`请输入 1–${nBars()} 之间的小节号`, 'info'); } // 清空了就是不改
    else apply(bi);
    el.value = get('play')[isStart ? 'a' : 'b'] + 1; // 夹到范围里、或改回原来的值
  });
  el.addEventListener('keydown', e => {
    if (e.isComposing) return;
    if (e.key === 'Enter') { e.preventDefault(); el.blur(); }
    else if (e.key === 'Escape') { e.preventDefault(); el.value = get('play')[isStart ? 'a' : 'b'] + 1; el.blur(); }
    else if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
      e.preventDefault();
      const cur = get('play')[isStart ? 'a' : 'b'], bi = Math.max(0, Math.min(nBars() - 1, cur + (e.key === 'ArrowUp' ? 1 : -1)));
      apply(bi);
      el.value = get('play')[isStart ? 'a' : 'b'] + 1;
      el.select();
    }
  });
}

export function initPlayer() {
  set('play', { ...get('play'), ...SONG_DEFAULTS, ...parsePrefs(stored(PREFS_KEY)), timbre: TIMBRES[stored('tt.timbre')] ? stored('tt.timbre') : 'steel' });
  on(Object.keys(PREF_DEFAULTS).map(k => 'play.' + k), () => remember(PREFS_KEY, serializePrefs(get('play'))));
  // Shift + 点：alphaTab 的点击事件里没有按键状态，在谱面的 mousedown（捕获阶段，比 alphaTab 先）里记下。
  // 不用全局 keydown / keyup：按着 Shift 切到别的应用时 keyup 会丢，之后每次点小节都成了扩展循环
  $('score').addEventListener('mousedown', e => { shiftDown = e.shiftKey; }, true);
  bindInputs();
  on(['play.metro', 'play.metroVol', 'play.countIn', 'play.gtVol', 'play.demo'], applyAudio);
  on('play.countIn', syncLooping); // 预备拍开关决定段落循环是 alphaTab 自己循环，还是每遍停下重新开始
  on('play.timbre', v => { remember('tt.timbre', v); switchTimbre(); });
  on('play.bpm', bpm => {
    $('bpm').value = bpm; $('bpmVal').textContent = bpm; UI.fillRange($('bpm'));
    if (api) api.playbackSpeed = bpm / baseTempo;
  });
  on('play.bpmMax', m => { $('bpm').max = m; UI.fillRange($('bpm')); });
  // 播放键：alphaTab 在放，或者敲节奏在进行，都显示成“停止”
  on(['play.playing', 'rhythm.running'], () => {
    const now = playing() || !!get('rhythm.running');
    $('playBtn').classList.toggle('on', now);
    $('playBtn').innerHTML = UI.icon(now ? 'stop' : 'play');
    $('playBtn').setAttribute('aria-label', now ? '停止（空格）' : '播放（空格）');
  });
  on('play.playing', (now, was) => {
    updateFollowBtn();
    if (!now && was) setStatus('已暂停', IDLE_HINT);
  });
  on('play.ready', r => { if (!r) return; applyAudio(); if (!playing() && !get('rhythm.on')) setStatus('已停止', IDLE_HINT); });
  // 离开练习页（调弦、练习建议…）时停下：播放键和快捷键都只在练习页，切走就停不了了；调弦的麦克风也会听到它。
  // 预备拍、节拍器是同一个播放器，一起停；谱子正在重新加载、本来要接着放的也不放了
  on('ui.view', v => {
    if (v === 'practice') return;
    if (reloading) reloading.play = false;
    if (playing() || countIn.from !== null || countIn.reload) stop();
  });
  on(['play.a', 'play.b'], () => paintLoop());
  on('view', renderSections);
  on('view.source', loadSource);
  on('ui.renderGen', markReviewBars);
  on('view', markReviewBars); // 确认没问题只改了 review，谱面不重画：这里也要重画一次，不然黄框留着
  on('song.activity', markReviewBars); // 精修开始、核对完一个小节
  on('ui.renderGen', drawLyrics);
  on('ui.notation', () => { if (!api) return; api.settings.display.staveProfile = alphaTab.StaveProfile[staveProfile()]; api.updateSettings(); api.render(); });

  setBpm(50); // 加载前的占位，不算“定下了速度”
  $('playBtn').onclick = () => (playing() || get('rhythm.running') ? stop() : play());
  $('demoBarBtn').onclick = () => { const bi = get('ui.bar') >= 0 ? get('ui.bar') : get('play.a'); setLoop(bi, bi, !playing()); if (!playing()) play(); };
  UI.seg($('timbreSeg'), get('play.timbre'), v => set('play.timbre', v));
  // 谱面样式：弹唱谱（有和弦名的曲子）默认只看六线谱（五线谱对弹唱没什么用，还占地方），换了就记住；其他曲子默认两种都显示
  const setNotationSeg = UI.seg($('notationSeg'), 'both', v => {
    set('ui.notation', v);
    if (hasChords(get('song'))) try { localStorage.setItem(CHORD_NOTATION_KEY, v); } catch { /* 隐私模式 */ }
  });
  on('view', v => {
    if (!v || get('ui.notation') || !hasChords(get('song'))) return;
    let n = 'tab';
    try { n = localStorage.getItem(CHORD_NOTATION_KEY) === 'both' ? 'both' : 'tab'; } catch { /* 隐私模式 */ }
    setNotationSeg(n);
    set('ui.notation', n);
  });
  $('exportBtn').onclick = exportGp;
  $('bpm').oninput = e => setTempo(+e.target.value);
  $('bpmDown').onclick = () => setTempo(get('play.bpm') - 4);
  $('bpmUp').onclick = () => setTempo(get('play.bpm') + 4);
  $('loopPrev').onclick = () => stepSection(-1);
  bindLoopInput('loopA', true); bindLoopInput('loopB', false);
  document.addEventListener('mouseup', endDrag); // 在谱面外面松开（alphaTab 收不到）也定下范围
  $('loopNext').onclick = () => stepSection(1);
  $('followBtn').onclick = scrollToCursor;
  let followTick = 0;
  addEventListener('scroll', () => { if (!followTick) followTick = requestAnimationFrame(() => { followTick = 0; updateFollowBtn(); }); }, { passive: true });
  $('playBtn').innerHTML = UI.icon('play');
  setStatus('加载中…', IDLE_HINT);
}
