// 谱面和播放（alphaTab）：渲染谱面、播放和循环、速度、音色、导出 GP，以及吸顶的播放栏。

const AT_BASE = '/static/vendor/alphatab/';
// 两个音色库：Sonivox 是完整的通用音色库（节拍器、GP 文件里的贝斯和鼓等都靠它）；
// 木吉他库后加载，alphaTab 查音色时后加载的优先，所以 24 / 25 号用的是真实木吉他采样。
const SF_BASE = '/static/vendor/soundfont/sonivox.sf2', SF_GUITARS = '/static/vendor/soundfont/guitars.sf3';
const TIMBRES = { steel: 25, nylon: 24 };
let soundFontsLoaded = 0, playerReadyOnce = false;
function guitarsFailed() {
  if (soundFontsLoaded >= 2) return;
  soundFontsLoaded = 2;
  UI.toast('木吉他音色加载失败，先用默认音色播放', 'err', 6000);
  if (playerReadyOnce && !state.ready) { state.ready = true; applyAudio(); if (!state.playing) setStatus('已停止', '空格播放 · ←→ 换段 · ↑↓ 调速'); }
}
// alphaTab 和 score.js 由 trainer.html 里的模块脚本加载（模块脚本总在这些普通脚本之后执行），就绪后才能建谱面、转换曲子数据
const atReady = new Promise(r => (window.alphaTab ? r() : addEventListener('alphatab-ready', r, { once: true })));
let api = null, baseTempo = 60, beatMap = [], scoreKey = '', shiftDown = false;

function themeColors() {
  const cs = getComputedStyle(document.documentElement), v = n => cs.getPropertyValue(n).trim();
  return { mainGlyphColor: v('--fg'), secondaryGlyphColor: v('--muted'), staffLineColor: v('--tab-line'), barSeparatorColor: v('--tab-lab'), barNumberColor: v('--muted'), scoreInfoColor: v('--fg') };
}
const staveProfile = () => ($('notationSeg').dataset.value === 'tab' ? 'Tab' : 'ScoreTab');

async function ensureApi() {
  if (api) return api;
  await atReady;
  api = new alphaTab.AlphaTabApi($('score'), {
    core: { fontDirectory: AT_BASE + 'font/', includeNoteBounds: true }, // 不开这个 alphaTab 不会触发点击音符事件（试听、改音都靠它）
    display: { scale: 0.9, layoutMode: 'Page', staveProfile: staveProfile(), resources: themeColors() },
    notation: { rhythmMode: 'ShowWithBars' },
    player: {
      playerMode: 'EnabledSynthesizer', soundFont: SF_BASE,
      enableCursor: true, enableAnimatedBeatCursor: true, enableUserInteraction: true,
      enablePlaybackRangeSelection: false, scrollOffsetY: -230,
      scrollMode: 'Off', // 只在播放时跟着光标滚动（见 playerStateChanged）；否则一打开页面就会跳到谱面
    },
  });
  api.soundFontLoad.on(e => { if (!state.ready) setStatus('加载音色…', `${Math.round(100 * e.loaded / Math.max(1, e.total))}%`); });
  api.soundFontLoaded.on(() => {
    if (++soundFontsLoaded !== 1) return;
    // 木吉他音色加载失败时退回只用 Sonivox，不能让播放器一直卡在“音色还在加载”
    if (api.player && api.player.soundFontLoadFailed) api.player.soundFontLoadFailed.on(guitarsFailed);
    api.loadSoundFont(SF_GUITARS, true);
  });
  api.playerReady.on(() => {
    playerReadyOnce = true;
    if (soundFontsLoaded < 2) return; // 等木吉他音色也加载完
    state.ready = true; applyAudio(); if (!state.playing) setStatus('已停止', '空格播放 · ←→ 换段 · ↑↓ 调速');
  });
  api.playerStateChanged.on(e => {
    const was = state.playing;
    state.playing = e.state === 1;
    if (state.playing !== was) { api.settings.player.scrollMode = state.playing ? alphaTab.ScrollMode.Continuous : alphaTab.ScrollMode.Off; api.updateSettings(); }
    if (state.playing && !was) practiceStart();
    if (!state.playing && was) practiceFlush(false);
    setPlayBtn(state.playing);
    if (!state.playing) setStatus('已暂停', '空格继续 · ←→ 换段 · ↑↓ 调速');
  });
  api.playerPositionChanged.on(e => onPosition(e.currentTick));
  api.playedBeatChanged.on(beat => onBeat(beat, false));
  api.beatMouseDown.on(b => (editMode ? queueEditClick(b, null) : onBeatClick(b)));
  api.noteMouseDown.on(n => {
    if (editMode) queueEditClick(n.beat, n);
    else if (!state.playing && state.ready) api.playNote(n);
  });
  api.scoreLoaded.on(onScoreLoaded);
  api.renderFinished.on(() => { if (BARS.length) applyLoopRange(); });
  api.postRenderFinished.on(() => markWarnBars()); // 小节位置要等所有部分都画完才有
  api.midiLoaded.on(() => { if (BARS.length) applyLoopRange(); }); // 反复的播放时间要等 MIDI 生成后才知道
  api.error.on(e => {
    const msg = e && e.message ? e.message : String(e);
    if (SONG && SONG.kind === 'score' && !SONG.bars.length) reportImportError(msg);
    else UI.toast('谱面出错：' + msg, 'err', 6000);
  });
  return api;
}

async function loadScore() {
  await ensureApi();
  if (SONG.kind === 'score') {
    const key = 'score:' + (SONG.track || 0);
    if (key === scoreKey) return;
    scoreKey = key;
    const r = await fetch(`/files/${SONG.id}/source`);
    if (!r.ok) {
      const msg = '曲谱文件加载失败（' + r.status + '）';
      if (!SONG.bars.length) reportImportError(msg); else UI.toast(msg + '，刷新页面再试', 'err', 6000);
      return;
    }
    api.load(new Uint8Array(await r.arrayBuffer()), [SONG.track || 0]);
  } else {
    if (!BARS.length) return;
    const known = (SONG.meta && SONG.meta.tempo) || 0; // 谱上没标速度时不显示速度记号（否则总是写着 ♩ = 60）
    api.settings.notation.elements.set(alphaTab.NotationElement.EffectTempo, !!known);
    const { tex, beatMap: bm } = scoreConv.toAlphaTex(SONG, { tempo: known || 60 });
    beatMap = bm;
    if ('tex:' + tex === scoreKey) return;
    scoreKey = 'tex:' + tex;
    api.tex(tex);
  }
}

// 所有音轨都用选中的木吉他音色：GP 文件里可能是电吉他等音色，而音色库里只有木吉他。
// scoreLoaded 在生成 MIDI 之前同步触发，所以在这里改就会生效；中途切换时再重新生成 MIDI。
// 只换吉他音轨（GM 24–31，包括电吉他）：贝斯、鼓、钢琴等保持原音色。原来的音色记在 _orig 里，导出 GP 时还原。
const isGuitarProgram = p => p >= 24 && p <= 31;
function applyTimbre(score, restore = false) {
  const prog = TIMBRES[$('timbreSeg').dataset.value] ?? 25;
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
function switchTimbre(v) {
  localStorage.setItem('tt.timbre', v);
  if (!api || !api.score) return;
  if (state.playing) api.pause();
  applyTimbre(api.score);
  api.loadMidiForScore();
  applyLoopRange();
}

async function onScoreLoaded(score) {
  applyTimbre(score);
  baseTempo = score.tempo || 60;
  if (SONG.kind === 'score') {
    // 第一次打开（或换了音轨）：把谱子转成我们的小节数据存起来，指板、提示和 AI 讲解都靠它
    const six = score.tracks.findIndex(t => t.staves[0] && t.staves[0].tuning.length === 6);
    const track = SONG.bars.length ? (SONG.track || 0) : Math.max(0, six);
    $('trackName').textContent = score.tracks[track] ? `音轨：${score.tracks[track].name}` : '';
    // 转换规则或 alphaTab 升级过：重新转换一次（内容没变的小节会保留讲解和练习记录）
    const derived = { version: scoreConv.DERIVE_VERSION, alphatab: (alphaTab.meta && alphaTab.meta.version) || '' };
    const outdated = !SONG.derived || SONG.derived.version !== derived.version || SONG.derived.alphatab !== derived.alphatab;
    if (!SONG.bars.length || track !== (SONG.track || 0) || outdated) {
      try {
        const d = scoreConv.deriveSong(score, track);
        const r = await fetch(`/api/songs/${SONG_ID}/derived`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ meta: d.meta, bars: d.bars, track, derived }) });
        if (!r.ok) throw new Error((await r.json()).detail || r.status);
        await refresh(true);
      } catch (e) {
        if (!SONG.bars.length) reportImportError(e.message); else UI.toast('读取曲谱失败：' + e.message, 'err', 6000);
        return;
      }
    }
  }
  if (!state.tempoSet) { // 默认用原速的六成练习；没有标速度就 50
    state.tempoSet = true;
    const t = (SONG.meta && SONG.meta.tempo) || (SONG.kind === 'score' ? baseTempo : 0);
    setTempo(t ? Math.round(t * 0.6) : 50);
    if (t) $('autoTarget').value = t;
  }
  setTempo(state.bpm);
}

function applyAudio() {
  if (!api) return;
  api.masterVolume = $('gtVol').value / 100;
  api.metronomeVolume = $('metro').checked ? $('metroVol').value / 100 * 1.5 : 0;
  api.countInVolume = $('countIn').checked ? 1 : 0;
}

let warnedUnplayed = '';
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
  if (!api || !api.score || !api.tracks.length) return;
  const mbs = api.score.masterBars;
  if (!mbs[state.a] || !mbs[state.b]) return;
  // 循环全曲：不设范围，反复和 D.S. / D.C. 跳转都照常播放。
  // 循环一段：有反复时播放时间和谱面位置不再一一对应，用 tickCache 查每个小节第一次被弹到的时间。
  let range = null;
  if (!(state.a === 0 && state.b === mbs.length - 1)) {
    const tc = api.tickCache;
    const first = mb => (tc && tc.getMasterBar(mb)) || { start: mb.start, end: mb.start + mb.calculateDuration() };
    let la = first(mbs[state.a]), lb = first(mbs[state.b]);
    // 有反复时，优先找播放顺序里正好依次弹过 a..b 的那一段（比如 |: 1 2 3 4 :| 5 选 3–5，用第二遍的 3 4 5），
    // 这样循环里只有选中的小节；找不到才用各自第一次被弹到的位置
    const run = tc && findLinearRun(tc.masterBars, state.a, state.b);
    if (run) { la = run[0]; lb = run[1]; }
    if (la.end <= la.start || lb.end <= lb.start) {
      // 按反复 / 跳转记号走，这个小节根本不会被弹到（比如 Fine 之后的小节）
      if (warnedUnplayed !== `${state.a}-${state.b}`) UI.toast('按反复记号，选中的小节弹不到，改为循环全曲', 'info', 5000);
      warnedUnplayed = `${state.a}-${state.b}`;
    } else {
      range = new alphaTab.synth.PlaybackRange();
      range.startTick = la.start;
      range.endTick = lb.end > la.start ? lb.end : la.end;
    }
  }
  state.lastTick = -1; // 设置范围会把播放位置跳回起点：不能当成弹完了一遍
  api.playbackRange = range;
  api.isLooping = true;
  const bars = api.tracks[0].staves[0].bars;
  const first = bars[state.a].voices[0].beats[0];
  const lastBeats = bars[state.b].voices[0].beats, last = lastBeats[lastBeats.length - 1];
  if (state.b - state.a + 1 < mbs.length) api.highlightPlaybackRange(first, last);
  else api.clearPlaybackRangeHighlight();
  return range || { startTick: 0 };
}

function markWarnBars() {
  // 可能识别有误的小节：在谱面上这一小节的位置画一层淡橙色（alphaTab 只给每行第一个小节标号，染小节号不够用）
  let layer = $('warnLayer');
  if (!layer) { layer = document.createElement('div'); layer.id = 'warnLayer'; $('score').appendChild(layer); }
  layer.innerHTML = '';
  const look = api && (api.boundsLookup || (api.renderer && api.renderer.boundsLookup)), surface = $('score').querySelector('.at-surface');
  if (!look || !surface) return;
  const dx = surface.offsetLeft, dy = surface.offsetTop;
  SONG.bars.forEach((b, i) => {
    if (!((b.warn && b.warn.length) || b.uncertain)) return;
    const mb = look.findMasterBarByIndex(i);
    if (!mb) return;
    const r = mb.realBounds, el = document.createElement('div');
    el.className = 'warn-mark';
    Object.assign(el.style, { left: dx + r.x + 'px', top: dy + r.y + 'px', width: r.w + 'px', height: r.h + 'px' });
    el.title = `第 ${b.n} 小节可能识别有误`;
    layer.appendChild(el);
  });
}

function play() {
  if (!api || !state.ready) { UI.toast('音色加载中', 'info'); return; }
  state.loops = 0; state.lastTick = -1;
  api.play();
}
function stop() { if (api) api.pause(); }
function setTempo(bpm) {
  bpm = Math.max(20, Math.min(240, Math.round(bpm)));
  state.bpm = bpm;
  $('bpm').value = Math.min(bpm, +$('bpm').max); $('bpmVal').textContent = bpm + ' BPM'; UI.fillRange($('bpm'));
  if (api) api.playbackSpeed = bpm / baseTempo;
}
function setLoop(a, b, restart = true, show = true) {
  if (state.playing) practiceFlush(false); // 换段之前，把上一段弹了的时间记下来
  state.a = Math.max(0, Math.min(a, b)); state.b = Math.min(BARS.length - 1, Math.max(a, b));
  if (state.playing) practiceStart();
  updateLoopUI();
  const range = applyLoopRange();
  if (show) showBar(state.a);
  if (range && api) {
    if (state.playing && restart) { api.stop(); state.loops = 0; api.play(); }
    else if (!state.playing) api.tickPosition = range.startTick;
  }
}

function onPosition(tick) {
  // 只有从段尾跳回段首才算弹完一遍；重新设置循环范围、停止时也会跳回起点，那些不算（applyLoopRange 会重置 lastTick）
  const r = api && api.playbackRange, start = r ? r.startTick : 0;
  if (state.playing && state.lastTick >= 0 && tick < state.lastTick - 10 && tick <= start + scoreConv.TICKS) {
    state.loops++;
    practiceFlush(true); // 先按提速前的速度记下刚弹完的这一遍
    if ($('auto').checked) {
      const every = Math.max(1, +$('autoEvery').value || 1), step = +$('autoStep').value || 0, target = +$('autoTarget').value || state.bpm;
      if (state.loops % every === 0 && state.bpm < target) setTempo(Math.min(target, state.bpm + step));
    }
  }
  state.lastTick = tick;
}

function eventAt(beat) {
  const bi = beat.voice.bar.index, evs = BARS[bi];
  if (!evs || !evs.length) return { bi, cur: null };
  return { bi, cur: evs[scoreConv.eventIndexAt(beatMap[bi] || [0], beat.playbackStart)] };
}
function onBeat(beat, clicked) {
  const { bi, cur } = eventAt(beat);
  if (!clicked) setStatus(`第 ${bi + 1} 小节 · 第 ${Math.floor(beat.playbackStart / scoreConv.TICKS) + 1} 拍`, `第 ${state.loops + 1} 遍`);
  if (bi < BARS.length) showBar(bi);
  if (!cur) return;
  let next = ALL[cur.idx + 1];
  if (!next || next.bar > state.b) next = BARS[state.a] && BARS[state.a][0];
  showFb(cur, next);
}
function onBeatClick(beat) {
  const bi = beat.voice.bar.index;
  if (bi >= BARS.length) return;
  if (shiftDown) setLoop(Math.min(state.a, bi), Math.max(state.b, bi));
  else setLoop(bi, bi);
  onBeat(beat, true);
}

function exportGp() {
  if (!api || !api.score) return;
  applyTimbre(api.score, true); // 导出原来的音色，不是练习时临时换的木吉他
  let bytes;
  try { bytes = new alphaTab.exporter.Gp7Exporter().export(api.score, api.settings); } finally { applyTimbre(api.score); }
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([bytes], { type: 'application/octet-stream' }));
  a.download = ((SONG.title || (SONG.meta && SONG.meta.title) || 'tab').replace(/[\\/:*?"<>|]/g, '_')) + '.gp';
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}
function refreshScoreTheme() {
  if (!api) return;
  const res = api.settings.display.resources, c = themeColors();
  for (const k in c) res[k] = alphaTab.model.Color.fromJson(c[k]);
  api.updateSettings(); api.render();
}

// 段落：原谱的一行是一段（段落按钮和“上一段 / 下一段”都按它来）
function sections() {
  const out = [];
  SONG.bars.forEach((b, i) => { const g = out[out.length - 1]; if (g && g.sys === b.system) g.z = i; else out.push({ sys: b.system, a: i, z: i }); });
  return out;
}
// 上一段 / 下一段：正在循环全曲时，从当前显示的小节所在的那一段开始；
// 自己选的任意范围（不是某一段）就按范围长度平移
function stepSection(dir) {
  if (!BARS.length) return;
  const secs = sections(), all = state.a === 0 && state.b === BARS.length - 1;
  const k = secs.findIndex(g => g.a === state.a && g.z === state.b);
  if (all && secs.length > 1) {
    const here = Math.max(0, secs.findIndex(g => g.a <= shownBar && shownBar <= g.z));
    const g = secs[dir > 0 ? here : Math.max(0, here - 1)];
    setLoop(g.a, g.z);
  } else if (k >= 0) {
    const g = secs[Math.max(0, Math.min(secs.length - 1, k + dir))];
    if (g !== secs[k]) setLoop(g.a, g.z); else UI.toast(dir > 0 ? '已经是最后一段了' : '已经是第一段了', 'info');
  } else {
    const n = state.b - state.a + 1, a = Math.max(0, Math.min(BARS.length - n, state.a + dir * n));
    setLoop(a, a + n - 1);
  }
}

// ---------------------------------------------------------------------
// 播放栏
// ---------------------------------------------------------------------
function setPlayBtn(on) { $('playBtn').classList.toggle('on', on); $('playBtn').innerHTML = UI.icon(on ? 'stop' : 'play'); }
function setStatus(l1, l2) { $('status').innerHTML = `<div class="l1">${l1}</div><div class="l2">${l2}</div>`; }
function updateLoopUI() {
  $('loopLabel').textContent = !BARS.length ? '—' : state.a === state.b ? `第 ${state.a + 1} 小节` : `第 ${state.a + 1}–${state.b + 1} 小节`;
  document.querySelectorAll('#secBtns button').forEach(b => b.classList.toggle('on', +b.dataset.a === state.a && +b.dataset.z === state.b));
}
// 播放设置（节拍器、音色、自动提速）可以收起来：笔记本、平板上吸顶的播放栏别占太多地方
function setOpts(open) { $('tOpts').hidden = !open; $('optsBtn').classList.toggle('on', open); localStorage.setItem('tt.opts', open ? '1' : '0'); }

function initPlayer() {
  addEventListener('keydown', e => { if (e.key === 'Shift') shiftDown = true; });
  addEventListener('keyup', e => { if (e.key === 'Shift') shiftDown = false; });
  setTempo(50);
  $('playBtn').onclick = () => (state.playing ? stop() : play());
  $('demoBarBtn').onclick = () => { const b = shownBar >= 0 ? shownBar : state.a; setLoop(b, b, false); if (!state.playing) play(); };
  UI.seg($('timbreSeg'), TIMBRES[localStorage.getItem('tt.timbre')] ? localStorage.getItem('tt.timbre') : 'steel', switchTimbre);
  UI.seg($('notationSeg'), 'both', () => { if (!api) return; api.settings.display.staveProfile = alphaTab.StaveProfile[staveProfile()]; api.updateSettings(); api.render(); });
  $('exportBtn').onclick = exportGp;
  ['metro', 'countIn'].forEach(id => $(id).addEventListener('change', applyAudio));
  $('bpm').oninput = e => setTempo(+e.target.value);
  $('bpmDown').onclick = () => setTempo(state.bpm - 4);
  $('bpmUp').onclick = () => setTempo(state.bpm + 4);
  $('loopPrev').onclick = () => stepSection(-1);
  $('loopNext').onclick = () => stepSection(1);
  $('gtVol').oninput = applyAudio;
  $('metroVol').oninput = applyAudio;
  $('optsBtn').onclick = () => setOpts($('tOpts').hidden);
  setOpts(localStorage.getItem('tt.opts') ? localStorage.getItem('tt.opts') === '1' : innerWidth >= 1280);
  setPlayBtn(false);
}
