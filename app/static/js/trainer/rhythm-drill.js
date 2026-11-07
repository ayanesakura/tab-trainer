// 敲节奏：不拿琴，跟着示范（或只有节拍器）在键盘上敲出循环段的节奏；每个音判早晚，每遍打分，指出常错的位置。
// 入口是播放栏的“敲节奏”按钮。打开后播放键 / 空格开始的是敲节奏而不是 alphaTab 播放；
// 循环哪几个小节、速度、节拍器、预备拍、示范音量和自动提速都和平时共用播放栏的设置。
// 计时不走 alphaTab：这里用 WebAudio 自己排节拍器和示范音（synth.js 的拨弦合成），敲击时间换算到同一个音频时钟上比较，
// 误差在几毫秒以内。蓝牙耳机等输出延迟报得不准时，用“校准”测一个补偿值。
// 纯计算（要敲的点、配对、打分、诊断）在 /static/js/rhythm.js 模块里（window.rhythmCore），有单元测试。
// 用到的别处的东西：core（$ esc state SONG BARS barLen setButton）、synth（ac gtGain initAudio playEvent stopAllVoices）、
// fretboard（el）、bar-info（shownBar showBar）、player（stop setLoop setTempo setStatus setPlayBtn）、main（typing）。
// player.js 的 play / stop / setLoop 和 main.js 的启动顺序里调用这里的 rhythmStart / rhythmStop / rhythmLoopChanged / initRhythm。

const RH = {
  on: false, run: null, prog: null, key: '', hist: [], errs: [], last: null, streak: 0, shown: null,
  mode: ['follow', 'listen', 'blind'].includes(localStorage.getItem('tt.rhythmMode')) ? localStorage.getItem('tt.rhythmMode') : 'follow',
  latency: Math.max(-300, Math.min(300, Math.round(+localStorage.getItem('tt.tapLatency') || 0))), // 毫秒，正数表示声音实际听到得更晚
};
const TAP_KEYS = new Set(['Space', 'KeyF', 'KeyJ']);
const RH_LOOKAHEAD = 0.12, RH_PREP = 0.3; // 声音提前多久交给 WebAudio；下一遍提前多久定下来（换速度从下一遍生效）
const RH_ROW = 86, RH_PAD = 16;
const RH_ICON = '<span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="13" r="2.5"/><path d="M7.8 8.8a6 6 0 0 0 0 8.4M16.2 8.8a6 6 0 0 1 0 8.4M4.9 5.9a10 10 0 0 0 0 14.2M19.1 5.9a10 10 0 0 1 0 14.2"/></svg></span>';
let rhClickBus = null, rhGeom = null;

// ---------------------------------------------------------------------
// 要敲的内容
// ---------------------------------------------------------------------
function rhythmProgram(a, b) {
  const ts0 = (SONG.meta && SONG.meta.time_signature) || [4, 4], bars = [];
  for (let bi = a; bi <= b; bi++) bars.push({ bi, len: barLen(bi), ts: SONG.bars[bi].time_signature || ts0, events: BARS[bi] || [] });
  return rhythmCore.buildProgram(bars);
}
// 校准用：两小节 4/4 的四分音符，敲击的对象就是节拍器的咔哒声
function calibProgram() {
  const bar = { bi: -1, len: 4, ts: [4, 4], events: [0, 1, 2, 3].map(t => ({ t, d: 1, notes: [{}] })) };
  return rhythmCore.buildProgram([bar, bar]);
}
function rhythmRebuild() {
  RH.prog = rhythmProgram(state.a, state.b);
  // 换了段或者谱改过：之前几遍的成绩对不上了，清掉
  const key = SONG.bars.slice(state.a, state.b + 1).map(b => b.id).join(',') + '|' + RH.prog.onsets.map(o => o.q.toFixed(4)).join(',');
  if (key !== RH.key) { RH.key = key; RH.hist = []; RH.errs = []; RH.last = null; RH.streak = 0; RH.shown = null; }
  $('rhRange').textContent = `${$('loopLabel').textContent} · ${RH.prog.onsets.length} 个音`;
  drawStrip(RH.prog);
  if (RH.shown) paintMarks(RH.shown);
  renderRhythmResult();
}

// ---------------------------------------------------------------------
// 打开 / 关闭
// ---------------------------------------------------------------------
function rhythmToggle(on) {
  if (on === RH.on) return;
  if (on && !BARS.length) { UI.toast('曲子还没加载好', 'info'); return; }
  if (!on) rhythmStop();
  if (on && state.playing) stop();
  RH.on = on;
  document.body.classList.toggle('rhythm-mode', on);
  $('rhythmBtn').classList.toggle('on', on);
  if (on) {
    rhythmRebuild();
    setStatus('敲节奏', '空格开始');
  } else {
    setStatus('已停止', '空格播放 · ←→ 换段 · ↑↓ 调速');
    if (shownBar >= 0) { const b = shownBar; shownBar = -1; showBar(b); } // 原谱截图隐藏时没法按宽度画，重画一次
  }
}
// 循环段变了（换段、点小节、只练这一小节）
function rhythmLoopChanged() {
  if (!RH.on) return;
  const restart = RH.run && !RH.run.calib;
  rhythmStop();
  rhythmRebuild();
  if (restart) rhythmStart();
}

// ---------------------------------------------------------------------
// 计时和发声
// ---------------------------------------------------------------------
// 某个时刻（performance.now 的毫秒）耳朵里正在听到的音频时间：getOutputTimestamp 已经算上了输出延迟
function heardNow(perfMs = performance.now()) {
  if (ac.getOutputTimestamp) {
    const o = ac.getOutputTimestamp();
    if (o.performanceTime > 0) return o.contextTime + (perfMs - o.performanceTime) / 1000;
  }
  return ac.currentTime - (ac.outputLatency || ac.baseLatency || 0) - (performance.now() - perfMs) / 1000;
}
function rhClick(run, when, accent) {
  const o = ac.createOscillator(), g = ac.createGain();
  const vol = (run.calib ? Math.max(0.5, $('metroVol').value / 100) : $('metroVol').value / 100) * (accent ? 0.75 : 0.5);
  o.frequency.value = accent ? 1760 : 1180;
  g.gain.setValueAtTime(0, when);
  g.gain.linearRampToValueAtTime(vol, when + 0.002);
  g.gain.exponentialRampToValueAtTime(0.001, when + 0.06);
  o.connect(g); g.connect(rhClickBus);
  o.start(when); o.stop(when + 0.07);
  run.nodes.push(o);
}
function makePre(run) { // 预备拍：第一小节的拍数
  const spq = 60 / (run.bpm || state.bpm), b0 = run.prog.bars[0];
  const items = b0.beats.map(b => ({ t: run.t0 + (b.q - b0.q0) * spq, click: b.accent ? 2 : 1 }));
  return { kind: 'pre', start: run.t0, end: run.t0 + b0.len * spq, spq, bpm: run.bpm || state.bpm, items, next: 0, notes: [] };
}
function makeLoop(run) {
  const prev = run.loops[run.loops.length - 1], start = prev ? prev.end : run.t0;
  const n = ++run.count; // 第几遍（早过去的遍会被清掉，不能数 run.loops）
  const bpm = run.bpm || state.bpm, spq = 60 / bpm, prog = run.prog;
  const mode = run.calib ? 'blind' : RH.mode;
  const demo = mode === 'follow' || (mode === 'listen' && n === 1);
  const graded = mode !== 'listen' || n > 1;
  const wins = rhythmCore.windows(prog.onsets, prog.Q, spq, run.calib ? 0.35 : rhythmCore.MAX_WIN);
  const notes = prog.onsets.map((o, i) => ({ o, T: start + o.q * spq, win: wins[i], err: null, missed: false }));
  const items = [];
  if (run.calib || $('metro').checked) for (const b of prog.bars) for (const bt of b.beats) items.push({ t: start + bt.q * spq, click: bt.accent ? 2 : 1 });
  if (demo) for (const nt of notes) items.push({ t: nt.T, ev: nt.o.ev });
  items.sort((x, y) => x.t - y.t);
  return { kind: 'loop', n, bpm, spq, start, end: start + prog.Q * spq, demo, graded, notes, taps: [], extra: 0, items, next: 0, result: null };
}

function rhythmStart(calib = false) {
  if (RH.run) rhythmStop();
  if (!calib) rhythmRebuild(); // 谱可能刚改过
  const prog = calib ? calibProgram() : RH.prog;
  if (!prog.onsets.length) { UI.toast('这一段没有要弹的音', 'info'); return; }
  initAudio();
  if (!rhClickBus) { rhClickBus = ac.createGain(); rhClickBus.connect(ac.destination); }
  gtGain.gain.cancelScheduledValues(ac.currentTime);
  gtGain.gain.value = $('gtVol').value / 100;
  const run = RH.run = { calib, prog, bpm: calib ? 80 : 0, loops: [], nodes: [], t0: ac.currentTime + 0.2, shown: null, dirty: true, passed: -1, status: '', count: 0, maxLoops: calib ? 1 : Infinity };
  if (calib) drawStrip(prog);
  if (calib || $('countIn').checked) run.loops.push(makePre(run));
  run.timer = setInterval(rhythmTick, 25);
  rhythmTick();
  run.raf = requestAnimationFrame(rhythmFrame);
  setPlayBtn(true);
}
function rhythmStop() {
  const run = RH.run;
  if (!run) return;
  RH.run = null;
  clearInterval(run.timer); cancelAnimationFrame(run.raf);
  for (const o of run.nodes) { try { o.stop(); } catch (e) { /* 已经停了 */ } }
  stopAllVoices();
  setPlayBtn(false);
  const head = $('rhHead'); if (head) head.style.display = 'none';
  if (run.calib) { drawStrip(RH.prog); if (RH.shown) paintMarks(RH.shown); }
  if (RH.on) setStatus('敲节奏 · 已停止', '空格开始');
}

function rhythmTick() {
  const run = RH.run;
  if (!run) return;
  if ($('view-practice').hidden) { rhythmStop(); return; } // 切到了调弦、练习建议等视图
  const now = ac.currentTime, loops = run.loops;
  while (run.count < run.maxLoops && (!loops.length || loops[loops.length - 1].end < now + RH_PREP)) loops.push(makeLoop(run));
  for (const L of loops) {
    while (L.next < L.items.length && L.items[L.next].t < now + RH_LOOKAHEAD) {
      const it = L.items[L.next++], when = Math.max(it.t, now);
      if (it.click) rhClick(run, when, it.click === 2); else playEvent(it.ev, when);
    }
  }
  const hn = heardNow(), slack = run.calib ? 0.4 : rhythmCore.MAX_WIN + 0.05;
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
function rhythmTap(perfMs) {
  const run = RH.run;
  if (!run) return;
  rhythmTapAt(heardNow(perfMs) - (run.calib ? 0 : RH.latency / 1000));
}
function rhythmTapAt(t) { // t：耳朵听到的音频时间（秒）
  const run = RH.run;
  if (!run) return;
  const strip = $('rhStrip');
  strip.classList.remove('tap'); void strip.offsetWidth; strip.classList.add('tap');
  let best = null, bi = -1, bd = Infinity;
  for (const L of run.loops) {
    if (L.kind !== 'loop' || !L.graded || L.result) continue;
    const i = rhythmCore.matchTap(L.notes, t);
    if (i >= 0 && Math.abs(t - L.notes[i].T) < bd) { bd = Math.abs(t - L.notes[i].T); best = L; bi = i; }
  }
  if (best) {
    const nt = best.notes[bi];
    nt.err = t - nt.T;
    best.taps.push({ t, i: bi });
  } else {
    const L = run.loops.find(x => x.kind === 'loop' && x.graded && !x.result && t >= x.start && t < x.end);
    if (L) { L.extra++; L.taps.push({ t, i: -1 }); }
  }
  run.dirty = true;
}

function finishLoop(run, L) {
  L.result = rhythmCore.summarize(L.notes, L.extra);
  run.dirty = true;
  if (run.calib) { finishCalib(L); return; }
  RH.last = { ...L.result, n: L.n, bpm: L.bpm };
  RH.hist.push({ n: L.n, bpm: L.bpm, score: L.result.score });
  if (RH.hist.length > 60) RH.hist.shift();
  RH.errs.push(L.notes.map(nt => nt.err));
  if (RH.errs.length > 30) RH.errs.shift();
  rhythmAutoSpeed(L.result.score);
  renderRhythmResult();
}
// 自动提速（播放设置里的开关）：敲节奏时要连续几遍 90 分以上才加速
function rhythmAutoSpeed(score) {
  if (!$('auto').checked) return;
  RH.streak = score >= 90 ? RH.streak + 1 : 0;
  const every = Math.max(1, +$('autoEvery').value || 1), step = +$('autoStep').value || 0, target = +$('autoTarget').value || state.bpm;
  if (RH.streak >= every && state.bpm < target && step > 0) {
    RH.streak = 0;
    setTempo(Math.min(target, state.bpm + step));
    UI.toast(`连续 ${every} 遍 90 分以上，下一遍提速到 ${state.bpm} BPM`);
  }
}
function finishCalib(L) {
  const errs = L.notes.filter(nt => nt.err != null).map(nt => nt.err).sort((a, b) => a - b);
  rhythmStop();
  if (errs.length < 5) UI.toast(`只敲到了 ${errs.length} 下，补偿值没有改，再试一次`, 'err', 4000);
  else {
    RH.latency = Math.max(-300, Math.min(300, Math.round(errs[errs.length >> 1] * 1000)));
    localStorage.setItem('tt.tapLatency', String(RH.latency));
    UI.toast(`校准好了：延迟补偿 ${fmtMs(RH.latency)}`, 'ok', 3500);
  }
  updateCalibBtn();
}
const fmtMs = v => (v > 0 ? '+' : '') + v + ' ms';
function updateCalibBtn() {
  setButton($('rhCalib'), UI.icon('gauge') + (RH.latency ? `校准 · ${fmtMs(RH.latency)}` : '校准'));
}

// ---------------------------------------------------------------------
// 节奏条（SVG）：每行最多 4 个小节；音符 = 圆点 + 时长条，下面是口令；敲完按准 / 偏 / 漏上色
// ---------------------------------------------------------------------
const svgEl = (tag, attrs, parent) => el(tag, attrs, parent); // fretboard.js 的 el（建 SVG 元素）
function drawStrip(prog) {
  const svg = $('rhSvg'), W = Math.max(360, $('rhStrip').clientWidth || 900);
  const capQ = Math.max(4, Math.floor((W - 2 * RH_PAD) / 52)); // 每个四分音符至少 52 px
  const rows = [];
  for (const b of prog.bars) {
    const r = rows[rows.length - 1];
    if (r && r.bars.length < 4 && r.q1 - r.q0 + b.len <= capQ) { r.bars.push(b); r.q1 += b.len; } else rows.push({ q0: b.q0, q1: b.q0 + b.len, bars: [b] });
  }
  const ppq = (W - 2 * RH_PAD) / Math.max(...rows.map(r => r.q1 - r.q0));
  rows.forEach((r, k) => { r.y = 4 + k * RH_ROW; });
  const H = rows.length * RH_ROW + 6;
  const pos = q => {
    const r = rows.find(x => q < x.q1 - 1e-9) || rows[rows.length - 1];
    return { x: RH_PAD + (Math.min(q, r.q1) - r.q0) * ppq, y: r.y, row: r };
  };
  rhGeom = { rows, ppq, pos, prog };
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('width', W); svg.setAttribute('height', H);
  svg.innerHTML = '';
  const g = svgEl('g', {}, svg);
  for (const r of rows) {
    for (const b of r.bars) {
      const x0 = pos(b.q0).x;
      svgEl('line', { x1: x0, x2: x0, y1: r.y + 16, y2: r.y + 60, class: 'rh-barline' }, g);
      if (b.bi >= 0) svgEl('text', { x: x0 + 4, y: r.y + 11, class: 'rh-barno' }, g).textContent = b.bi + 1;
      for (const bt of b.beats) if (!bt.accent) { const x = pos(bt.q).x; svgEl('line', { x1: x, x2: x, y1: r.y + 20, y2: r.y + 56, class: 'rh-beat' }, g); }
    }
    const xe = RH_PAD + (r.q1 - r.q0) * ppq;
    svgEl('line', { x1: xe, x2: xe, y1: r.y + 16, y2: r.y + 60, class: 'rh-barline' }, g);
  }
  const ons = prog.onsets;
  ons.forEach((o, i) => {
    const p = pos(o.q), w = Math.max(4, Math.min(o.d, p.row.q1 - o.q) * ppq - 4);
    svgEl('rect', { x: p.x, y: p.y + 34, width: w, height: 6, rx: 3, class: 'rh-dur' }, g);
  });
  ons.forEach((o, i) => {
    const p = pos(o.q);
    const c = svgEl('circle', { cx: p.x, cy: p.y + 37, r: 7, class: 'rh-note', id: 'rhn' + i }, g);
    svgEl('title', {}, c).textContent = rhythmCore.placeName(o);
    // 口令：太挤时只标拍上的数字
    const gap = Math.min(i > 0 ? o.q - ons[i - 1].q : 9, i < ons.length - 1 ? ons[i + 1].q - o.q : 9) * ppq;
    const onBeat = /^\d+$/.test(o.label);
    if (onBeat || gap >= 22) svgEl('text', { x: p.x, y: p.y + 74, class: 'rh-syl' + (onBeat ? ' on' : '') }, g).textContent = o.label;
  });
  svgEl('g', { id: 'rhDyn' }, svg);
  svgEl('line', { id: 'rhHead', x1: 0, x2: 0, y1: 0, y2: 0, class: 'rh-head', style: 'display:none' }, svg);
}
// 某一遍的上色：准绿、偏橙、漏红；偏和漏的音上方写早晚多少毫秒；敲击的位置在下方画短线，多敲的画叉
function paintMarks(L) {
  if (!rhGeom || !L) return;
  const { pos, ppq } = rhGeom, dyn = $('rhDyn');
  if (!dyn) return;
  dyn.innerHTML = '';
  const notes = L.notes || [], hn = RH.run ? heardNow() : Infinity;
  rhGeom.prog.onsets.forEach((o, i) => {
    const c = $('rhn' + i), nt = notes[i];
    if (!c) return;
    let cls = 'rh-note';
    if (nt && nt.err != null) cls += ' ' + rhythmCore.grade(nt.err, nt.win);
    else if (nt && nt.missed) cls += ' miss';
    else if (nt && !L.graded && nt.T <= hn) cls += ' played';
    c.setAttribute('class', cls);
    if (!nt || (nt.err == null && !nt.missed)) return;
    const p = pos(o.q), gap = Math.min(i > 0 ? o.q - rhGeom.prog.onsets[i - 1].q : 9, i < notes.length - 1 ? rhGeom.prog.onsets[i + 1].q - o.q : 9) * ppq;
    let txt = '';
    if (nt.missed) txt = '漏';
    else if (rhythmCore.grade(nt.err, nt.win) === 'ok') txt = (nt.err > 0 ? '晚' : '早') + Math.round(Math.abs(nt.err) * 1000);
    if (txt && gap >= (nt.missed ? 16 : 34)) svgEl('text', { x: p.x, y: p.y + 25, class: 'rh-dev ' + (nt.missed ? 'miss' : 'ok') }, dyn).textContent = txt;
  });
  for (const tp of L.taps || []) {
    const q = tp.i >= 0 ? rhGeom.prog.onsets[tp.i].q + notes[tp.i].err / L.spq : (tp.t - L.start) / L.spq;
    const p = pos(Math.max(0, Math.min(rhGeom.prog.Q, q)));
    if (tp.i >= 0) svgEl('line', { x1: p.x, x2: p.x, y1: p.y + 48, y2: p.y + 56, class: 'rh-tap ' + rhythmCore.grade(notes[tp.i].err, notes[tp.i].win) }, dyn);
    else svgEl('path', { d: `M${p.x - 3.5} ${p.y + 48.5}l7 7M${p.x + 3.5} ${p.y + 48.5}l-7 7`, class: 'rh-tap extra' }, dyn);
  }
}
function rhythmFrame() {
  const run = RH.run;
  if (!run) return;
  run.raf = requestAnimationFrame(rhythmFrame);
  const hn = heardNow();
  let L = null;
  for (const x of run.loops) if (x.start <= hn) L = x;
  L = L || run.loops[0];
  if (!L) return;
  if (L !== run.shown) { run.shown = L; if (!run.calib && L.kind === 'loop') RH.shown = L; run.dirty = true; }
  if (L.kind === 'loop' && !L.graded) { const p = L.notes.filter(nt => nt.T <= hn).length; if (p !== run.passed) { run.passed = p; run.dirty = true; } }
  if (run.dirty) { run.dirty = false; paintMarks(L.kind === 'loop' ? L : null); if (L.kind !== 'loop') clearMarks(); }
  // 状态栏
  let st;
  if (run.calib) st = ['延迟校准', L.kind === 'pre' ? '预备…' : '跟着咔哒声敲 8 下'];
  else if (L.kind === 'pre') st = [`预备 · ${L.bpm} BPM`, `${Math.max(1, Math.ceil((L.end - hn) / L.spq - 1e-6))}…`];
  else st = [`第 ${L.n} 遍 · ${L.bpm} BPM`, !L.graded ? '这一遍先听，下一遍自己敲' : L.demo ? '跟着示范敲' : '不放示范，自己敲'];
  const key = st.join('|');
  if (key !== run.status) { run.status = key; setStatus(st[0], st[1]); }
  // 播放头
  const head = $('rhHead');
  if (L.kind !== 'loop' || !rhGeom) { head.style.display = 'none'; return; }
  const p = rhGeom.pos(Math.max(0, Math.min(rhGeom.prog.Q - 1e-6, (hn - L.start) / L.spq)));
  head.style.display = '';
  head.setAttribute('x1', p.x); head.setAttribute('x2', p.x); head.setAttribute('y1', p.y + 14); head.setAttribute('y2', p.y + 62);
  // 段很长、节奏条要滚动时，让播放头那一行留在视野里
  const box = $('rhStrip');
  if (box.scrollHeight > box.clientHeight + 4 && (p.y < box.scrollTop || p.y + RH_ROW > box.scrollTop + box.clientHeight)) box.scrollTop = p.y - 4;
}
function clearMarks() {
  const dyn = $('rhDyn'); if (dyn) dyn.innerHTML = '';
  document.querySelectorAll('#rhSvg .rh-note').forEach(c => c.setAttribute('class', 'rh-note'));
}

// ---------------------------------------------------------------------
// 成绩区：最近一遍的分数和诊断、每遍的分数、常错的位置；还没敲过时显示怎么练
// ---------------------------------------------------------------------
function renderRhythmResult() {
  const box = $('rhResult');
  const auto = $('auto').checked ? `<div class="hint">自动提速已打开：连续 ${Math.max(1, +$('autoEvery').value || 1)} 遍 90 分以上才加速。</div>` : '';
  if (!RH.last) {
    box.innerHTML = `<div class="rh-guide"><b>怎么练</b><ol>
      <li>先用“跟着示范”：按空格开始，每个音响的时候按一下空格（或 F、J 键，也可以点节奏条）。</li>
      <li>能连续几遍 90 分以上，换“听一遍再敲”，再换“只有节拍器”。不靠示范也敲得准，节奏才算记住了。</li>
      <li>循环哪几个小节、速度、节拍器和预备拍都用上面播放栏的设置；从慢速开始。</li></ol>${auto}</div>`;
    return;
  }
  const s = RH.last, d = rhythmCore.diagnose(s);
  const tone = s.score >= 90 ? 'good' : s.score >= 70 ? 'warn' : 'bad';
  const mean = s.good + s.ok ? `<span>平均 ${s.mean > 0 ? '晚' : s.mean < 0 ? '早' : ''} ${Math.abs(s.mean)} ms</span>` : '';
  const sd = s.good + s.ok > 1 ? `<span>波动 ±${s.sd} ms</span>` : '';
  const best = RH.hist.reduce((m, h) => (!m || h.score > m.score || (h.score === m.score && h.bpm > m.bpm) ? h : m), null);
  const hist = RH.hist.slice(-12).map(h => `<span class="rh-h ${h.score >= 90 ? 'good' : h.score >= 70 ? 'warn' : 'bad'}" title="第 ${h.n} 遍 · ${h.bpm} BPM">${h.score}</span>`).join('');
  const weak = rhythmCore.weakSpots(RH.prog.onsets, RH.errs);
  box.innerHTML = `<div class="rh-score ${tone}"><b>${s.score}</b><span>分</span></div>
    <div class="rh-detail">
      <div class="hint">第 ${s.n} 遍 · ${s.bpm} BPM</div>
      <div class="rh-stats"><span class="good">准 ${s.good}</span><span class="ok">偏 ${s.ok}</span><span class="miss">漏 ${s.miss}</span><span>多敲 ${s.extra}</span>${mean}${sd}</div>
      ${d.text ? `<div class="rh-diag ${d.tone}">${esc(d.text)}</div>` : ''}
      ${auto}
    </div>
    <div class="rh-side">
      <div class="rh-hist"><span class="lbl">每遍</span>${hist}${best ? `<span class="hint">最好 ${best.score} 分（${best.bpm} BPM）</span>` : ''}</div>
      ${weak.length ? `<div class="rh-weak"><span class="lbl">常错</span>${weak.map(w => `<button class="btn sm ghost" data-bi="${w.bi}" title="只循环这一小节">${esc(rhythmCore.placeName(RH.prog.onsets[w.i]))}</button><span class="${w.kind}">${esc(w.text)}</span>`).join('')}</div>` : ''}
    </div>`;
  box.querySelectorAll('.rh-weak button').forEach(b => { b.onclick = () => setLoop(+b.dataset.bi, +b.dataset.bi); });
}

// ---------------------------------------------------------------------
// 绑定
// ---------------------------------------------------------------------
function initRhythm() {
  setButton($('rhythmBtn'), RH_ICON + '敲节奏');
  $('rhythmBtn').onclick = () => rhythmToggle(!RH.on);
  $('rhClose').onclick = () => rhythmToggle(false);
  $('rhCalib').onclick = () => { if (RH.run && RH.run.calib) rhythmStop(); else rhythmStart(true); };
  updateCalibBtn();
  UI.seg($('rhMode'), RH.mode, v => {
    RH.mode = v; localStorage.setItem('tt.rhythmMode', v);
    if (RH.run && !RH.run.calib) rhythmStart(); // 换了练法：从头开始
  });
  $('gtVol').addEventListener('input', () => { if (RH.run && gtGain) gtGain.gain.value = $('gtVol').value / 100; });
  $('auto').addEventListener('change', () => { RH.streak = 0; if (RH.on) renderRhythmResult(); });
  // 敲：按下的那一刻就算（不等抬起）；点节奏条也行（平板）
  $('rhStrip').addEventListener('pointerdown', e => { if (RH.run) { e.preventDefault(); rhythmTap(e.timeStamp); } });
  // 敲节奏进行中：空格 / F / J 是敲击、Esc 停止；在 main.js 的快捷键之前处理（捕获阶段）
  addEventListener('keydown', e => {
    if (!RH.on || !RH.run || typing(e.target) || e.metaKey || e.ctrlKey || e.altKey || $('view-practice').hidden || !$('origModal').hidden) return;
    if (e.code === 'Escape') { e.preventDefault(); e.stopImmediatePropagation(); rhythmStop(); return; }
    if (!TAP_KEYS.has(e.code)) return;
    e.preventDefault(); e.stopImmediatePropagation();
    if (e.target.blur && e.target !== document.body) e.target.blur(); // 免得空格再“按”一次刚点过的按钮
    if (!e.repeat) rhythmTap(e.timeStamp);
  }, true);
  let rt = 0;
  addEventListener('resize', () => {
    if (!RH.on) return;
    clearTimeout(rt);
    rt = setTimeout(() => { drawStrip(RH.run ? RH.run.prog : RH.prog); if (RH.run) RH.run.dirty = true; else if (RH.shown) paintMarks(RH.shown); }, 120);
  });
  // 切到别的浏览器标签页时停下（后台的定时器会被降频，节拍会乱）；切到调弦等视图在 rhythmTick 里停
  document.addEventListener('visibilitychange', () => { if (document.hidden) rhythmStop(); });
}
