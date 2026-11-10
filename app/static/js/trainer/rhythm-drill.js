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
//   - player.js 不导入这里：initRhythm 用 setPlayerHooks 注册 drill.start / stop / loopChanged，
//     player 的 play / stop / setLoop 在 rhythm.on / rhythm.running 时交给它。
//   - 键盘：main.js 的快捷键先问 rhythmKey(e)（同一个监听、同样的 UI.hasLayer / 输入框判断），处理了就不再往下走。

import * as core from '../rhythm.js';
import { get, set, on } from './store.js';
import { $, UI, esc, setButton, svgEl } from './dom.js';
import { stop as stopPlayback, setLoop, setStatus, setPlayerHooks, IDLE_HINT } from './player.js';
import { ac, initAudio, playEvent, stopAllVoices, setGuitarVolume } from './audio.js';
import { loopLabel } from './view.js';
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
function toggle(want) {
  if (want === isOn()) return;
  const v = get('view');
  if (want && !(v && v.bars.length)) { UI.toast('曲子还没加载好', 'info'); return; }
  if (!want) stopRun();
  if (want && get('play.playing')) stopPlayback(); // alphaTab 在放就先停下
  set('rhythm.on', want); // 小节卡片、指板停靠条、播放键各自订阅
}
function paintOn(v) {
  $('rhythmCard').hidden = !v;
  $('rhythmBtn').setAttribute('aria-pressed', String(v));
  if (v) {
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
function click(run, when, accent) {
  const o = ac.createOscillator(), g = ac.createGain();
  const mv = (get('play.metroVol') ?? 70) / 100;
  const vol = (run.calib ? Math.max(0.5, mv) : mv) * (accent ? 0.75 : 0.5);
  o.frequency.value = accent ? 1760 : 1180;
  g.gain.setValueAtTime(0, when);
  g.gain.linearRampToValueAtTime(vol, when + 0.002);
  g.gain.exponentialRampToValueAtTime(0.001, when + 0.06);
  o.connect(g); g.connect(clickBus);
  o.start(when); o.stop(when + 0.07);
  run.nodes.push(o);
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
  if (run.calib || get('play.metro')) for (const b of prog.bars) for (const bt of b.beats) items.push({ t: start + bt.q * spq, click: bt.accent ? 2 : 1 });
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
      if (it.click) click(run, when, it.click === 2); else playEvent(it.ev, when);
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
  else if (L.kind === 'pre') st = [`预备 · ${L.bpm} BPM`, `${Math.max(1, Math.ceil((L.end - hn) / L.spq - 1e-6))}…`];
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
    // 卡片上只留一行，三步练法放在“?”的说明里（悬停或 Tab 聚焦时弹出）
    box.innerHTML = `<div class="rh-guide"><b>怎么练</b>空格开始，每个音响时敲一下 <span class="help-wrap"><button class="icon-btn help-btn xs" aria-label="怎么练（详细）" aria-describedby="rhHelp">?</button><span class="help-pop left up" id="rhHelp" role="tooltip">
      <span>1. 先用“跟着示范”：按空格开始，每个音响的时候按一下空格（或 F、J 键，也可以点节奏条）。</span>
      <span>2. 能连续几遍 90 分以上，换“听一遍再敲”，再换“只有节拍器”。不靠示范也敲得准，节奏才算记住了。</span>
      <span>3. 循环哪几个小节、速度用控制条，节拍器和预备拍在播放设置里；从慢速开始。</span></span></span></div>`;
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
// 键盘（main.js 的快捷键先调这里）：敲节奏进行中，空格 / F / J 是敲击、Esc 停止。处理了返回 true。
// 调用前 main.js 已经排除了输入框、修饰键、浮层和别的视图。
// ---------------------------------------------------------------------
export function rhythmKey(e) {
  if (!isOn() || !RH.run) return false;
  if (e.code === 'Escape') { e.preventDefault(); stopRun(); return true; }
  if (!TAP_KEYS.has(e.code)) return false;
  e.preventDefault();
  if (e.target.blur && e.target !== document.body) e.target.blur(); // 免得空格再“按”一次刚点过的按钮
  if (!e.repeat) tap(e.timeStamp); // 按下的那一刻就算（不等抬起）
  return true;
}

/** 给 e2e 测试和调试用（window.tt.rhythm） */
export const rhythmDebug = {
  get state() { return RH; },
  tapAt, start, stop: stopRun, toggle,
  now: () => heardAdj(), // tapAt 用的时钟
};

// ---------------------------------------------------------------------
// 绑定
// ---------------------------------------------------------------------
export function initRhythm() {
  setPlayerHooks({ drill: { start: () => start(), stop: stopRun, loopChanged } });
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
