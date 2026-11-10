// 敲节奏：不拿琴，跟着示范（或只有节拍器）在键盘上敲出循环段的节奏；每个音判早晚，每遍打分，指出常错的位置。
// 入口是控制条的“敲节奏”按钮（store 的 rhythm.on）。打开后侧栏的小节卡片和底部指板收起来，位置给节奏卡片；
// 播放键 / 空格开始的是敲节奏而不是 alphaTab 播放。循环哪几个小节、速度、节拍器、预备拍和示范音量
// 都和平时共用（store 的 play 切片）。
// 计时不走 alphaTab：这里用 WebAudio 自己排节拍器和示范音（audio.js 的拨弦合成），敲击时间换算到同一个音频时钟上比较，
// 误差在几毫秒以内。蓝牙耳机等输出延迟报得不准时，用“校准”测一个补偿值。
// 纯计算（要敲的点、配对、打分、诊断）在 ../rhythm.js，有单元测试。
//
// 和别的模块怎么接：
//   - 状态：rhythm.on / rhythm.running 在 store 里（播放键、小节卡片、指板停靠条各自订阅）；一次敲节奏的细节（run）、
//     成绩、练法、延迟补偿是这个模块自己的状态。
//   - player.js 不导入这里：initRhythm 在 hooks.js 上挂 drill.start / stop / loopChanged，
//     player 的 play / stop / setLoop 在 rhythm.on / rhythm.running 时交给它。
//   - 键盘：在 keys.js 登记一组（敲着的时候才管），排在播放快捷键前面。

import * as core from '../rhythm.js';
import { get, set, on } from './store.js';
import { $, UI, esc, setButton, svgEl } from './dom.js';
import { stop as stopPlayback, setLoop, revealLoop, setStatus, IDLE_HINT } from './player.js';
import { provide } from './hooks.js';
import { addShortcuts } from './keys.js';
import { ac, initAudio, playEvent, stopAllVoices, setGuitarVolume } from './audio.js';
import { getApi } from './player.js';
import { renderMetronomeClick } from './at-adapter.js';
import { clickGain, metronomeLevel, countInLevel, audibleLevel, FALLBACK } from './click.js';
import { loopLabel, isScore } from './view.js';
import { stored, remember } from './prefs.js';

const MODES = ['follow', 'listen', 'blind'];
const RH = {
  run: null, prog: null, key: '', hist: [], errs: [], last: null, shown: null,
  mode: MODES.includes(stored('tt.rhythmMode')) ? stored('tt.rhythmMode') : 'follow',
  latency: Math.max(-300, Math.min(300, Math.round(+stored('tt.tapLatency') || 0))), // 毫秒，正数表示声音实际听到得更晚
};
const TAP_KEYS = new Set(['Space', 'KeyF', 'KeyJ']);
const LOOKAHEAD = 0.12, PREP = 0.3; // 声音提前多久交给 WebAudio；下一遍提前多久定下来（换速度从下一遍生效）
const ROW = 86, PAD = 14;
let clickBus = null, geom = null;
// 节拍器的声音：用 alphaTab 的合成器渲染一次（和播放时的节拍器一样，见 click.js），没好之前用正弦波代替
let clickBuf = null, clickLoading = null;
function loadClick() {
  if (clickBuf || clickLoading || !ac) return;
  clickLoading = renderMetronomeClick(getApi(), ac.sampleRate)
    .then(x => { if (x && ac) { clickBuf = ac.createBuffer(1, x.length, ac.sampleRate); clickBuf.copyToChannel(x, 0); } })
    .finally(() => { clickLoading = null; });
}
const isOn = () => !!get('rhythm.on');

// ---------------------------------------------------------------------
// 要敲的内容
// ---------------------------------------------------------------------
function program(a, b) {
  const s = get('song'), v = get('view'), ts0 = (s.meta && s.meta.time_signature) || [4, 4], bars = [];
  for (let bi = a; bi <= b; bi++) bars.push({ bi, len: v.barLens[bi] || 4, ts: s.bars[bi].time_signature || ts0, events: v.bars[bi] || [] });
  return core.buildProgram(bars);
}
// 校准用：两小节 4/4 的四分音符，敲击的对象就是节拍器的咔哒声
function calibProgram() {
  const bar = { bi: -1, len: 4, ts: [4, 4], events: [0, 1, 2, 3].map(t => ({ t, d: 1, notes: [{}] })) };
  return core.buildProgram([bar, bar]);
}
function rebuild() {
  const v = get('view');
  if (!v || !v.bars.length) return;
  // 曲子刚变短（重新识别、删页）时，这里可能先于 song.js 的 onView 收到新 view，循环范围还没夹进来：先自己夹一下，
  // 免得读到不存在的小节（onView 随后 setLoop 时会再走 loopChanged 重建一次）
  const n = v.bars.length, p = get('play'), a = Math.min(p.a, n - 1), b = Math.max(a, Math.min(p.b, n - 1));
  RH.prog = program(a, b);
  // 换了段或者谱改过：之前几遍的成绩对不上了，清掉
  const key = v.ids.slice(a, b + 1).join(',') + '|' + RH.prog.onsets.map(o => o.q.toFixed(4)).join(',');
  if (key !== RH.key) { RH.key = key; RH.hist = []; RH.errs = []; RH.last = null; RH.shown = null; }
  $('rhRange').textContent = `${loopLabel(a, b, v.bars.length)} · ${RH.prog.onsets.length} 个音`;
  drawStrip(RH.prog);
  if (RH.shown) paintMarks(RH.shown);
  renderResult();
}

// ---------------------------------------------------------------------
// 打开 / 关闭
// ---------------------------------------------------------------------
// 打开时循环范围太长（比如全曲 86 小节、七百多个音）：先缩到正在看的那一小节所在的原谱那一行，
// GP 曲子（没有原谱的行）缩到它附近的 4 个小节，并说一声
const DRILL_MAX = 8;
function shrinkForDrill(v) {
  const { a, b } = get('play'), n = v.bars.length;
  if (b - a + 1 <= DRILL_MAX) return;
  const shown = get('ui.bar'), bi = shown >= a && shown <= b ? shown : a;
  const row = isScore(get('song')) ? null : v.sections.find(g => g.a <= bi && bi <= g.z);
  let na, nb;
  if (row && row.z - row.a + 1 <= DRILL_MAX) [na, nb] = [row.a, row.z];
  else { na = Math.max(0, Math.min(bi - 1, n - 4)); nb = Math.min(n - 1, na + 3); }
  setLoop(na, nb);
  revealLoop();
  UI.toast(`段太长，敲节奏先练${loopLabel(na, nb, n)}`, 'info', 3500);
}

function toggle(want) {
  if (want === isOn()) return;
  const v = get('view');
  if (want && !(v && v.bars.length)) { UI.toast('曲子还没加载好', 'info'); return; }
  if (!want) stopRun();
  if (want && get('play.playing')) stopPlayback(); // alphaTab 在放就先停下
  if (want) shrinkForDrill(v);
  set('rhythm.on', want); // 小节卡片、指板停靠条、播放键各自订阅
}
function paintOn(v) {
  $('rhythmCard').hidden = !v;
  $('rhythmBtn').setAttribute('aria-pressed', String(v));
  if (v) {
    initAudio(); loadClick(); // 节拍器的声音先渲染好（点按钮是用户操作，这时可以打开音频）
    rebuild(); // 卡片显示出来以后才量得出节奏条的宽度
    setStatus('敲节奏', '空格开始');
  } else setStatus('已停止', IDLE_HINT);
}
// 循环段变了（换段、点小节、只练这一小节）：player.setLoop 调过来
function loopChanged() {
  if (!isOn()) return;
  const restart = RH.run && !RH.run.calib;
  stopRun();
  rebuild();
  if (restart) start();
}

// ---------------------------------------------------------------------
// 计时和发声
// ---------------------------------------------------------------------
// 某个时刻（performance.now 的毫秒）耳朵里正在听到的音频时间：getOutputTimestamp 已经算上了输出延迟
// （没算上的那部分是校准测出来的 RH.latency，见 heardAdj）
function heardNow(perfMs = performance.now()) {
  if (ac.getOutputTimestamp) {
    const o = ac.getOutputTimestamp();
    if (o.performanceTime > 0) return o.contextTime + (perfMs - o.performanceTime) / 1000;
  }
  return ac.currentTime - (ac.outputLatency || ac.baseLatency || 0) - (performance.now() - perfMs) / 1000;
}
// 再扣掉校准测出的延迟补偿：敲击、判漏、判一遍结束、播放头都用它，几处用同一个时钟才对得上（校准那一遍不扣）
function heardAdj(perfMs) {
  const run = RH.run;
  return heardNow(perfMs) - (run && !run.calib ? RH.latency / 1000 : 0);
}
// 一下咔哒。音量和播放时的节拍器 / 预备拍用同一套换算（click.js）：预备拍按预备拍的音量，每一遍里按节拍器的
// （节拍器关着就不响），校准那一遍一定要听得见
function click(run, when, accent, pre) {
  const p = get('play'), level = run.calib ? audibleLevel(p) : pre ? countInLevel(p) : metronomeLevel(p);
  if (!level) return;
  const vol = clickGain(level, accent), g = ac.createGain();
  let src;
  if (clickBuf) {
    src = ac.createBufferSource(); src.buffer = clickBuf;
    g.gain.value = vol;
    src.start(when); src.stop(when + clickBuf.duration);
  } else {
    src = ac.createOscillator(); src.frequency.value = FALLBACK.freq[accent ? 0 : 1];
    g.gain.setValueAtTime(0, when);
    g.gain.linearRampToValueAtTime(vol * FALLBACK.gain, when + FALLBACK.attack);
    g.gain.exponentialRampToValueAtTime(0.001, when + FALLBACK.decay);
    src.start(when); src.stop(when + FALLBACK.decay + 0.01);
  }
  src.connect(g); g.connect(clickBus);
  run.nodes.push(src);
}
function makePre(run) { // 预备拍：第一小节的拍数，接在上一遍后面（开着预备拍时每一遍前都有）
  const prev = run.loops[run.loops.length - 1], start = prev ? prev.end : run.t0;
  const bpm = run.bpm || get('play.bpm'), spq = 60 / bpm, b0 = run.prog.bars[0];
  const items = b0.beats.map(b => ({ t: start + (b.q - b0.q0) * spq, click: b.accent ? 2 : 1 }));
  return { kind: 'pre', start, end: start + b0.len * spq, spq, bpm, items, next: 0, notes: [] };
}
function makeLoop(run) {
  const prev = run.loops[run.loops.length - 1], start = prev ? prev.end : run.t0;
  const n = ++run.count; // 第几遍（早过去的遍会被清掉，不能数 run.loops）
  const bpm = run.bpm || get('play.bpm'), spq = 60 / bpm, prog = run.prog;
  const mode = run.calib ? 'blind' : RH.mode;
  const demo = mode === 'follow' || (mode === 'listen' && n === 1);
  const graded = mode !== 'listen' || n > 1;
  const wins = core.windows(prog.onsets, prog.Q, spq, run.calib ? 0.35 : core.MAX_WIN);
  const notes = prog.onsets.map((o, i) => ({ o, T: start + o.q * spq, win: wins[i], err: null, missed: false }));
  const items = [];
  for (const b of prog.bars) for (const bt of b.beats) items.push({ t: start + bt.q * spq, click: bt.accent ? 2 : 1 }); // 节拍器关着时 click() 不出声
  if (demo) for (const nt of notes) items.push({ t: nt.T, ev: nt.o.ev });
  items.sort((x, y) => x.t - y.t);
  return { kind: 'loop', n, bpm, spq, start, end: start + prog.Q * spq, demo, graded, notes, taps: [], extra: 0, items, next: 0, result: null };
}

function start(calib = false) {
  if (RH.run) stopRun();
  if (!calib) rebuild(); // 谱可能刚改过
  const prog = calib ? calibProgram() : RH.prog;
  if (!prog || !prog.onsets.length) { UI.toast('这一段没有要弹的音', 'info'); return; }
  initAudio();
  if (!clickBus) { clickBus = ac.createGain(); clickBus.connect(ac.destination); }
  loadClick();
  setGuitarVolume((get('play.gtVol') ?? 80) / 100);
  const run = RH.run = { calib, prog, bpm: calib ? 80 : 0, loops: [], nodes: [], t0: ac.currentTime + 0.2, shown: null, dirty: true, passed: -1, status: '', count: 0, maxLoops: calib ? 1 : Infinity };
  if (calib) drawStrip(prog);
  if (calib || get('play.countIn')) run.loops.push(makePre(run));
  run.timer = setInterval(tick, 25);
  tick();
  run.raf = requestAnimationFrame(frame);
  set({ 'rhythm.calib': calib, 'rhythm.running': true }); // 播放键显示成停止；练习记录（practice-log.js）记敲节奏的时间，校准不算
}
function stopRun() {
  const run = RH.run;
  if (!run) return;
  RH.run = null;
  clearInterval(run.timer); cancelAnimationFrame(run.raf);
  for (const o of run.nodes) { try { o.stop(); } catch { /* 已经停了 */ } }
  stopAllVoices();
  set('rhythm.running', false);
  const head = $('rhHead'); if (head) head.setAttribute('hidden', '');
  if (run.calib && RH.prog) { drawStrip(RH.prog); if (RH.shown) paintMarks(RH.shown); }
  if (isOn()) setStatus('敲节奏 · 已停止', '空格开始');
}

function tick() {
  const run = RH.run;
  if (!run) return;
  const now = ac.currentTime, loops = run.loops;
  while (run.count < run.maxLoops && (!loops.length || loops[loops.length - 1].end < now + PREP)) {
    const prev = loops[loops.length - 1];
    // 开着预备拍：每一遍前都数一小节（第一遍的在 start 里排好了）
    loops.push(!run.calib && prev && prev.kind === 'loop' && get('play.countIn') ? makePre(run) : makeLoop(run));
  }
  for (const L of loops) {
    while (L.next < L.items.length && L.items[L.next].t < now + LOOKAHEAD) {
      const it = L.items[L.next++], when = Math.max(it.t, now);
      if (it.click) click(run, when, it.click === 2, L.kind === 'pre'); else playEvent(it.ev, when);
    }
  }
  const hn = heardAdj(), slack = run.calib ? 0.4 : core.MAX_WIN + 0.05;
  for (const L of loops) {
    if (L.kind !== 'loop' || !L.graded || L.result) continue;
    for (const nt of L.notes) if (nt.err == null && !nt.missed && hn > nt.T + nt.win + 0.03) { nt.missed = true; run.dirty = true; }
    if (hn > L.end + slack) { finishLoop(run, L); if (RH.run !== run) return; }
  }
  // 早就过去了的遍不用留着；还在显示的那一遍留着
  run.loops = loops.filter(L => L === run.shown || L.end > hn - 1 || (L.kind === 'loop' && L.graded && !L.result));
  if (run.nodes.length > 200) run.nodes = run.nodes.slice(-100);
}

// ---------------------------------------------------------------------
// 敲击和打分
// ---------------------------------------------------------------------
function tap(perfMs) {
  const run = RH.run;
  if (!run) return;
  tapAt(heardAdj(perfMs));
}
function tapAt(t) { // t：扣掉延迟补偿后的音频时间（秒，见 heardAdj）
  const run = RH.run;
  if (!run) return;
  const strip = $('rhStrip');
  strip.classList.remove('tap'); void strip.offsetWidth; strip.classList.add('tap');
  let best = null, bi = -1, bd = Infinity;
  for (const L of run.loops) {
    if (L.kind !== 'loop' || !L.graded || L.result) continue;
    const i = core.matchTap(L.notes, t);
    if (i >= 0 && Math.abs(t - L.notes[i].T) < bd) { bd = Math.abs(t - L.notes[i].T); best = L; bi = i; }
  }
  if (best) {
    const nt = best.notes[bi];
    nt.err = t - nt.T;
    nt.missed = false; // 定时器先判了漏（主线程卡了一下），按键的时间戳其实在窗口里：算敲到了
    best.taps.push({ t, i: bi });
  } else {
    const L = run.loops.find(x => x.kind === 'loop' && x.graded && !x.result && t >= x.start && t < x.end);
    if (L) { L.extra++; L.taps.push({ t, i: -1 }); }
  }
  run.dirty = true;
}

function finishLoop(run, L) {
  L.result = core.summarize(L.notes, L.extra);
  run.dirty = true;
  if (run.calib) { finishCalib(L); return; }
  RH.last = { ...L.result, n: L.n, bpm: L.bpm };
  RH.hist.push({ n: L.n, bpm: L.bpm, score: L.result.score });
  if (RH.hist.length > 60) RH.hist.shift();
  RH.errs.push(L.notes.map(nt => nt.err));
  if (RH.errs.length > 30) RH.errs.shift();
  renderResult();
}
function finishCalib(L) {
  const errs = L.notes.filter(nt => nt.err != null).map(nt => nt.err).sort((a, b) => a - b);
  stopRun();
  if (errs.length < 5) UI.toast(`只敲到了 ${errs.length} 下，补偿值没有改，再试一次`, 'err', 4000);
  else {
    RH.latency = Math.max(-300, Math.min(300, Math.round(errs[errs.length >> 1] * 1000)));
    remember('tt.tapLatency', String(RH.latency));
    UI.toast(`校准好了：延迟补偿 ${fmtMs(RH.latency)}`, 'ok', 3500);
  }
  paintCalib();
}
const fmtMs = v => (v > 0 ? '+' : '') + v + ' ms';
function paintCalib() {
  const calibrating = !!(RH.run && RH.run.calib);
  setButton($('rhCalib'), UI.icon('gauge') + (calibrating ? '停止校准' : RH.latency ? `校准 · ${fmtMs(RH.latency)}` : '校准'));
  $('rhCalib').setAttribute('aria-pressed', String(calibrating));
}

// ---------------------------------------------------------------------
// 节奏条（SVG）：每行最多 4 个小节；音符 = 圆点 + 时长条，下面是口令；敲完按准 / 偏 / 漏上色
// ---------------------------------------------------------------------
function drawStrip(prog) {
  const svg = $('rhSvg'), W = Math.max(280, $('rhStrip').clientWidth || 900);
  const capQ = Math.max(4, Math.floor((W - 2 * PAD) / 52)); // 每个四分音符至少 52 px
  const rows = [];
  for (const b of prog.bars) {
    const r = rows[rows.length - 1];
    if (r && r.bars.length < 4 && r.q1 - r.q0 + b.len <= capQ) { r.bars.push(b); r.q1 += b.len; } else rows.push({ q0: b.q0, q1: b.q0 + b.len, bars: [b] });
  }
  const ppq = (W - 2 * PAD) / Math.max(...rows.map(r => r.q1 - r.q0));
  rows.forEach((r, k) => { r.y = 4 + k * ROW; });
  const H = rows.length * ROW + 6;
  const pos = q => {
    const r = rows.find(x => q < x.q1 - 1e-9) || rows[rows.length - 1];
    return { x: PAD + (Math.min(q, r.q1) - r.q0) * ppq, y: r.y, row: r };
  };
  geom = { rows, ppq, pos, prog };
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('width', W); svg.setAttribute('height', H);
  svg.replaceChildren();
  const g = svgEl('g', {}, svg);
  for (const r of rows) {
    for (const b of r.bars) {
      const x0 = pos(b.q0).x;
      svgEl('line', { x1: x0, x2: x0, y1: r.y + 16, y2: r.y + 60, class: 'rh-barline' }, g);
      if (b.bi >= 0) svgEl('text', { x: x0 + 4, y: r.y + 11, class: 'rh-barno' }, g).textContent = b.bi + 1;
      for (const bt of b.beats) if (!bt.accent) { const x = pos(bt.q).x; svgEl('line', { x1: x, x2: x, y1: r.y + 20, y2: r.y + 56, class: 'rh-beat' }, g); }
    }
    const xe = PAD + (r.q1 - r.q0) * ppq;
    svgEl('line', { x1: xe, x2: xe, y1: r.y + 16, y2: r.y + 60, class: 'rh-barline' }, g);
  }
  const ons = prog.onsets;
  ons.forEach(o => {
    const p = pos(o.q), w = Math.max(4, Math.min(o.d, p.row.q1 - o.q) * ppq - 4);
    svgEl('rect', { x: p.x, y: p.y + 34, width: w, height: 6, rx: 3, class: 'rh-dur' }, g);
  });
  ons.forEach((o, i) => {
    const p = pos(o.q);
    const gap = Math.min(i > 0 ? o.q - ons[i - 1].q : 9, i < ons.length - 1 ? ons[i + 1].q - o.q : 9) * ppq;
    // 很密的音（三十二分、窄屏）圆点缩小，不叠在一起
    const c = svgEl('circle', { cx: p.x, cy: p.y + 37, r: Math.max(3.5, Math.min(7, gap / 2 - 1)), class: 'rh-note', id: 'rhn' + i }, g);
    svgEl('title', {}, c).textContent = core.placeName(o);
    // 口令：太挤时只标拍上的数字
    const onBeat = /^\d+$/.test(o.label);
    if (onBeat || gap >= 22) svgEl('text', { x: p.x, y: p.y + 74, class: 'rh-syl' + (onBeat ? ' on' : '') }, g).textContent = o.label;
  });
  svgEl('g', { id: 'rhDyn' }, svg);
  svgEl('line', { id: 'rhHead', x1: 0, x2: 0, y1: 0, y2: 0, class: 'rh-head', hidden: '' }, svg);
}
// 某一遍的上色：准绿、偏橙、漏红；偏和漏的音上方写早晚多少毫秒；敲击的位置在下方画短线，多敲的画叉
function paintMarks(L) {
  if (!geom || !L) return;
  const { pos, ppq } = geom, dyn = $('rhDyn');
  if (!dyn) return;
  dyn.replaceChildren();
  const notes = L.notes || [], hn = RH.run ? heardAdj() : Infinity;
  geom.prog.onsets.forEach((o, i) => {
    const c = $('rhn' + i), nt = notes[i];
    if (!c) return;
    let cls = 'rh-note';
    if (nt && nt.err != null) cls += ' ' + core.grade(nt.err, nt.win);
    else if (nt && nt.missed) cls += ' miss';
    else if (nt && !L.graded && nt.T <= hn) cls += ' played';
    c.setAttribute('class', cls);
    if (!nt || (nt.err == null && !nt.missed)) return;
    const p = pos(o.q), gap = Math.min(i > 0 ? o.q - geom.prog.onsets[i - 1].q : 9, i < notes.length - 1 ? geom.prog.onsets[i + 1].q - o.q : 9) * ppq;
    let txt = '';
    if (nt.missed) txt = '漏';
    else if (core.grade(nt.err, nt.win) === 'ok') txt = (nt.err > 0 ? '晚' : '早') + Math.round(Math.abs(nt.err) * 1000);
    if (txt && gap >= (nt.missed ? 16 : 34)) svgEl('text', { x: p.x, y: p.y + 25, class: 'rh-dev ' + (nt.missed ? 'miss' : 'ok') }, dyn).textContent = txt;
  });
  for (const tp of L.taps || []) {
    const q = tp.i >= 0 ? geom.prog.onsets[tp.i].q + notes[tp.i].err / L.spq : (tp.t - L.start) / L.spq;
    const p = pos(Math.max(0, Math.min(geom.prog.Q, q)));
    if (tp.i >= 0) svgEl('line', { x1: p.x, x2: p.x, y1: p.y + 48, y2: p.y + 56, class: 'rh-tap ' + core.grade(notes[tp.i].err, notes[tp.i].win) }, dyn);
    else svgEl('path', { d: `M${p.x - 3.5} ${p.y + 48.5}l7 7M${p.x + 3.5} ${p.y + 48.5}l-7 7`, class: 'rh-tap extra' }, dyn);
  }
}
function frame() {
  const run = RH.run;
  if (!run) return;
  run.raf = requestAnimationFrame(frame);
  const hn = heardAdj();
  let L = null;
  for (const x of run.loops) if (x.start <= hn) L = x;
  L = L || run.loops[0];
  if (!L) return;
  if (L !== run.shown) { run.shown = L; if (!run.calib && L.kind === 'loop') RH.shown = L; run.dirty = true; }
  if (L.kind === 'loop' && !L.graded) { const p = L.notes.filter(nt => nt.T <= hn).length; if (p !== run.passed) { run.passed = p; run.dirty = true; } }
  if (run.dirty) {
    run.dirty = false;
    // 遍与遍之间的预备拍：还显示上一遍每个音的早晚，不清掉
    const m = L.kind === 'loop' ? L : run.loops.filter(x => x.kind === 'loop' && x.end <= L.start).pop() || null;
    paintMarks(m);
    if (!m) clearMarks();
  }
  // 状态
  let st;
  if (run.calib) st = ['延迟校准', L.kind === 'pre' ? '预备…' : '跟着咔哒声敲 8 下'];
  else if (L.kind === 'pre') st = [`预备 ${Math.max(1, Math.ceil((L.end - hn) / L.spq - 1e-6))}…`, `${L.bpm} BPM`]; // 和播放的预备拍一样的写法
  else st = [`第 ${L.n} 遍 · ${L.bpm} BPM`, !L.graded ? '这一遍先听，下一遍自己敲' : L.demo ? '跟着示范敲' : '不放示范，自己敲'];
  const key = st.join('|');
  if (key !== run.status) { run.status = key; setStatus(st[0], st[1]); }
  // 播放头
  const head = $('rhHead');
  if (!head) return;
  if (L.kind !== 'loop' || !geom) { head.setAttribute('hidden', ''); return; }
  const p = geom.pos(Math.max(0, Math.min(geom.prog.Q - 1e-6, (hn - L.start) / L.spq)));
  head.removeAttribute('hidden');
  head.setAttribute('x1', p.x); head.setAttribute('x2', p.x); head.setAttribute('y1', p.y + 14); head.setAttribute('y2', p.y + 62);
  // 段很长、节奏条要滚动时，让播放头那一行留在视野里
  const box = $('rhStrip');
  if (box.scrollHeight > box.clientHeight + 4 && (p.y < box.scrollTop || p.y + ROW > box.scrollTop + box.clientHeight)) box.scrollTop = p.y - 4;
}
function clearMarks() {
  const dyn = $('rhDyn'); if (dyn) dyn.replaceChildren();
  document.querySelectorAll('#rhSvg .rh-note').forEach(c => c.setAttribute('class', 'rh-note'));
}

// ---------------------------------------------------------------------
// 成绩区：最近一遍的分数和诊断、每遍的分数、常错的位置；还没敲过时显示怎么练
// ---------------------------------------------------------------------
const tone = score => (score >= 90 ? 'good' : score >= 70 ? 'warn' : 'bad');
function renderResult() {
  const box = $('rhResult');
  if (!RH.last) {
    // 卡片上只留一句，按键、颜色、口令和三步练法都在标题栏的“?”里
    box.innerHTML = '<div class="rh-guide">空格开始，每个音响时敲一下</div>';
    return;
  }
  const s = RH.last, d = core.diagnose(s);
  const mean = s.good + s.ok ? `<span>平均 ${s.mean > 0 ? '晚' : s.mean < 0 ? '早' : ''} ${Math.abs(s.mean)} ms</span>` : '';
  const sd = s.good + s.ok > 1 ? `<span>波动 ±${s.sd} ms</span>` : '';
  const best = RH.hist.reduce((m, h) => (!m || h.score > m.score || (h.score === m.score && h.bpm > m.bpm) ? h : m), null);
  const hist = RH.hist.slice(-12).map(h => `<span class="rh-h ${tone(h.score)}" title="第 ${h.n} 遍 · ${h.bpm} BPM">${h.score}</span>`).join('');
  const weak = core.weakSpots(RH.prog.onsets, RH.errs);
  box.innerHTML = `<div class="rh-score ${tone(s.score)}"><b>${s.score}</b><span>分</span></div>
    <div class="rh-detail">
      <div class="hint">第 ${s.n} 遍 · ${s.bpm} BPM</div>
      <div class="rh-stats"><span class="good">准 ${s.good}</span><span class="ok">偏 ${s.ok}</span><span class="miss">漏 ${s.miss}</span><span>多敲 ${s.extra}</span>${mean}${sd}</div>
    </div>
    ${d.text ? `<div class="rh-diag ${d.tone}">${esc(d.text)}</div>` : ''}
    <div class="rh-side">
      <div class="rh-hist"><span class="lbl">每遍</span>${hist}${best ? `<span class="hint">最好 ${best.score} 分（${best.bpm} BPM）</span>` : ''}</div>
      ${weak.length ? `<div class="rh-weak"><span class="lbl">常错</span><div class="rh-weak-list">${weak.map(w => `<span class="rh-w"><button class="btn sm ghost" data-bi="${w.bi}" title="只循环这一小节">${esc(core.placeName(RH.prog.onsets[w.i]))}</button><span class="${w.kind}">${esc(w.text)}</span></span>`).join('')}</div></div>` : ''}
    </div>`;
  box.querySelectorAll('.rh-weak button').forEach(b => { b.onclick = () => setLoop(+b.dataset.bi, +b.dataset.bi); });
}

// ---------------------------------------------------------------------
// 键盘（keys.js 的一组，排在播放快捷键前面）：敲节奏进行中，空格 / F / J 是敲击、Esc 停止。
// 输入框、修饰键、浮层、别的视图这些判断 keys.js 统一做。
// ---------------------------------------------------------------------
function tapKey(e) {
  if (e.target.blur && e.target !== document.body) e.target.blur(); // 免得空格再“按”一次刚点过的按钮
  if (!e.repeat) tap(e.timeStamp); // 按下的那一刻就算（不等抬起）
}

/** 给 e2e 测试和调试用（window.tt.rhythm） */
export const rhythmDebug = {
  get state() { return RH; },
  get clickSampled() { return !!clickBuf; }, // 节拍器用的是 alphaTab 渲染的声音
  tapAt, start, stop: stopRun, toggle,
  now: () => heardAdj(), // tapAt 用的时钟
};

// ---------------------------------------------------------------------
// 绑定
// ---------------------------------------------------------------------
export function initRhythm() {
  provide('drill.start', () => start());
  provide('drill.stop', stopRun);
  provide('drill.loopChanged', loopChanged);
  addShortcuts({ name: 'rhythm', order: 20, when: () => isOn() && !!RH.run, keys: { Escape: () => stopRun(), Space: tapKey, KeyF: tapKey, KeyJ: tapKey } });
  $('rhythmBtn').onclick = () => toggle(!isOn());
  $('rhClose').onclick = () => toggle(false);
  $('rhCalib').onclick = () => { if (RH.run && RH.run.calib) stopRun(); else start(true); paintCalib(); };
  paintCalib();
  UI.seg($('rhMode'), RH.mode, v => {
    RH.mode = v; remember('tt.rhythmMode', v);
    if (RH.run && !RH.run.calib) start(); // 换了练法：从头开始
  });
  on('rhythm.on', paintOn);
  on('rhythm.running', paintCalib);
  on('play.gtVol', v => { if (RH.run) setGuitarVolume(v / 100); });
  // 谱改过（改音、重新识别）：没在敲的话节奏条马上换成新的；在敲的话下次开始时再换
  on('view', () => { if (isOn() && !RH.run) rebuild(); });
  // 切到调弦、练习建议等视图时停下
  on('ui.view', v => { if (v !== 'practice') stopRun(); });
  // 点节奏条也算敲（平板）
  $('rhStrip').addEventListener('pointerdown', e => { if (RH.run) { e.preventDefault(); tap(e.timeStamp); } });
  let rt = 0;
  addEventListener('resize', () => {
    if (!isOn()) return;
    clearTimeout(rt);
    rt = setTimeout(() => {
      const prog = RH.run ? RH.run.prog : RH.prog;
      if (!prog) return;
      drawStrip(prog);
      if (RH.run) RH.run.dirty = true; else if (RH.shown) paintMarks(RH.shown);
    }, 120);
  });
  // 切到别的浏览器标签页时停下（后台的定时器会被降频，节拍会乱）
  document.addEventListener('visibilitychange', () => { if (document.hidden) stopRun(); });
}
