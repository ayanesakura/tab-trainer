// 示范音：Karplus-Strong 拨弦合成。
// 练习时的播放、节拍器都由 alphaTab 负责（player.js）；这里给调弦表的“听”按钮、记号说明的“试听”和敲节奏的示范音
// （rhythm-drill.js，自己排程，和敲击用同一个音频时钟）发声，麦克风调音器（tuner.js）也共用这里的 AudioContext。

import { get } from './store.js';
import { mtof, noteMidi, T3, STD_MIDI } from './view.js';

const STD_VIEW = { open: Object.fromEntries(STD_MIDI.map((m, i) => [i + 1, m])), capo: 0 };

export let ac = null;
let bus, gtGain;
const panners = {};
const GT_VOL = () => (get('play.gtVol') ?? 80) / 100;
export function initAudio() {
  if (ac) { if (ac.state === 'suspended') ac.resume(); return; }
  ac = new (window.AudioContext || window.webkitAudioContext)();
  const comp = ac.createDynamicsCompressor();
  comp.threshold.value = -14; comp.ratio.value = 4;
  comp.connect(ac.destination);
  const conv = ac.createConvolver();
  const len = ac.sampleRate * 1.8, ir = ac.createBuffer(2, len, ac.sampleRate);
  for (let c = 0; c < 2; c++) { const d = ir.getChannelData(c); for (let i = 0; i < len; i++) d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, 3); }
  conv.buffer = ir;
  const wet = ac.createGain(); wet.gain.value = 0.16; conv.connect(wet); wet.connect(comp);
  gtGain = ac.createGain(); gtGain.gain.value = GT_VOL();
  bus = ac.createGain(); bus.connect(gtGain); gtGain.connect(comp); gtGain.connect(conv);
  for (let s = 0; s <= 6; s++) { const p = ac.createStereoPanner(); p.pan.value = s ? (s - 3.5) * 0.09 : 0; p.connect(bus); panners[s] = p; }
}

const cache = new Map();
function ksBuffer(midi, mode) { // mode: n 正常  s 连音（较柔）  m 闷音
  const key = midi + mode; if (cache.has(key)) return cache.get(key);
  const sr = ac.sampleRate, f = mtof(midi);
  // 闷音：环路里再串一级一阶低通，泛音几十毫秒内就被“压掉”，基音还能响一小会儿（掌根压弦的效果）。
  // 一阶低通在低频处带来 (1-c)/c 个采样的延迟，算周期时要扣掉，否则音会偏低。
  const c = mode === 'm' ? 0.1 : 1, extra = (1 - c) / c;
  const N = Math.max(2, Math.round(sr / f - 0.5 - extra)), f0 = sr / (N + 0.5 + extra);
  const dur = mode === 'm' ? 2 : 3.4, len = Math.floor(sr * dur);
  const buf = ac.createBuffer(1, len, sr), y = buf.getChannelData(0);
  const T60 = mode === 'm' ? 1.6 : Math.max(1.1, 4.4 - (midi - 40) * 0.07);
  const rho = Math.pow(0.001, (N + 0.5) / (sr * T60));
  const sm = mode === 'n' ? 0.4 : mode === 'm' ? 0.85 : 0.72;
  let prev = 0, mean = 0;
  for (let i = 0; i < N; i++) { prev = sm * prev + (1 - sm) * (Math.random() * 2 - 1); y[i] = prev; mean += prev; }
  mean /= N; for (let i = 0; i < N; i++) y[i] -= mean;
  let z = 0;
  for (let i = N; i < len; i++) {
    const avg = 0.5 * (y[i - N] + (i - N - 1 >= 0 ? y[i - N - 1] : 0));
    z += c * (avg - z);
    y[i] = rho * z;
  }
  let pk = 1e-9; for (let i = 0; i < Math.min(len, N * 8); i++) pk = Math.max(pk, Math.abs(y[i]));
  let scale = 0.5 / pk;
  if (mode === 'm') { // 闷音能量集中在基音、峰值不代表响度：按起音后的有效值对齐到正常拨弦的响度
    let s = 0; const a = 2 * N, b = Math.min(len, a + 1024);
    for (let i = a; i < b; i++) s += y[i] * y[i];
    let pkAll = 1e-9; for (let i = 0; i < len; i++) pkAll = Math.max(pkAll, Math.abs(y[i]));
    scale = Math.min(0.13 / Math.sqrt(s / (b - a) + 1e-12), 0.9 / pkAll);
  }
  for (let i = 0; i < len; i++) y[i] *= scale;
  const r = { buf, f0 }; cache.set(key, r); return r;
}
function harmBuffer(midi) {
  const key = midi + 'h'; if (cache.has(key)) return cache.get(key);
  const sr = ac.sampleRate, f = mtof(midi), len = Math.floor(sr * 3.5);
  const buf = ac.createBuffer(1, len, sr), y = buf.getChannelData(0);
  // 泛音：以泛音音高为基音的钟声音色，高次分量衰减更快，基音拖得较长
  const P = [[1, 1, 2.6], [2, 0.3, 1.1], [3, 0.1, 0.6], [4, 0.04, 0.35]]; // [倍数, 幅度, 衰减时间 s]
  for (let i = 0; i < len; i++) {
    const t = i / sr;
    let v = 0;
    for (const [k, a, tau] of P) if (f * k < sr / 2) v += a * Math.sin(2 * Math.PI * f * k * t + k) * Math.exp(-t / tau);
    y[i] = v * (1 - Math.exp(-t / 0.0015)) * 0.38;
  }
  // 触弦瞬间的一点点噪声，避免听起来像电子正弦音
  const nl = Math.floor(sr * 0.012); let np = 0;
  for (let i = 0; i < nl; i++) { np = 0.5 * np + 0.5 * (Math.random() * 2 - 1); y[i] += np * 0.07 * (1 - i / nl); }
  const r = { buf, f0: f }; cache.set(key, r); return r;
}

const voices = {};
function stopVoice(s, when) {
  const v = voices[s]; if (!v) return;
  v.g.gain.setTargetAtTime(0, when, 0.012);
  try { v.src.stop(when + 0.2); } catch (e) {}
  voices[s] = null;
}
export function stopAllVoices() { for (const s in voices) stopVoice(s, ac.currentTime); }
export function pluck(s, midi, when, { mode = 'n', gain = 0.9, harm = false, slideIn = false } = {}) {
  stopVoice(s, when);
  const { buf, f0 } = harm ? harmBuffer(midi) : ksBuffer(midi, mode);
  const src = ac.createBufferSource(); src.buffer = buf;
  const rate = mtof(midi) / f0;
  if (slideIn) { // 从低两品处滑入
    src.playbackRate.setValueAtTime(rate * Math.pow(2, -2 / 12), when);
    src.playbackRate.linearRampToValueAtTime(rate, when + 0.07);
  } else src.playbackRate.setValueAtTime(rate, when);
  const g = ac.createGain(); g.gain.setValueAtTime(gain, when);
  let node = src;
  if (mode === 'm') { const lp = ac.createBiquadFilter(); lp.type = 'lowpass'; lp.frequency.value = 2000; src.connect(lp); node = lp; }
  node.connect(g); g.connect(panners[s]);
  src.start(when);
  voices[s] = { src, g, f0, rate };
}
function slideTo(s, midi, when) {
  const v = voices[s];
  const newRate = mtof(midi) / v.f0;
  v.src.playbackRate.setValueAtTime(v.rate, Math.max(ac.currentTime, when - 0.08));
  v.src.playbackRate.linearRampToValueAtTime(newRate, when);
  v.rate = newRate;
}
/** 示范音的音量（0–1）；敲节奏开始时和拖“示范音量”时设 */
export function setGuitarVolume(v) {
  if (!gtGain) return;
  gtGain.gain.cancelScheduledValues(ac.currentTime);
  gtGain.gain.value = v;
}
/** 在音频时间 when 弹一个事件（deriveView 的事件：{notes: [{s, f, tech: Set, grace, bend}]}） */
export function playEvent(e, when) {
  const v = get('view') || STD_VIEW; // 按曲子的调弦和变调夹
  const ns = [...e.notes].sort((a, b) => b.s - a.s);
  ns.forEach((n, i) => {
    const tk = n.tech, t = when + (tk.has('roll') ? i * 0.045 : 0), m = noteMidi(n, v);
    const legato = tk.has('h') || tk.has('p');
    if (n.grace != null) pluck(n.s, v.open[n.s] + v.capo + n.grace, Math.max(ac.currentTime, t - 0.07)); // 倚音
    if (tk.has('sl') && voices[n.s]) slideTo(n.s, m, t);
    else pluck(n.s, m, t, {
      mode: tk.has('pm') || tk.has('x') ? 'm' : legato ? 's' : 'n',
      gain: tk.has('x') ? 0.35 : legato ? 0.6 : 0.9,
      harm: tk.has('nh') || tk.has('ah'),
      slideIn: tk.has('si'),
    });
    if (n.bend > 0 && voices[n.s]) slideTo(n.s, m + n.bend, t + 0.18); // 推弦
  });
}

// ---------------------------------------------------------------------
// 记号说明里“试听”的示范片段
// ev(拍内位置, 时值(拍), "弦-品 弦-品 ...", "技巧")
// 技巧：h 击弦  p 勾弦  sl 滑音（从同一根弦上一个音滑过来）  si 从下方滑入  pm 闷音
//       nh 自然泛音（品数=泛音点）  ah 人工泛音（按品 + 12 品处泛音）  roll 琶音
// 第 4 个参数的技巧作用于整组音；只作用于某个音时写在音后面，如 "1-4:h:g2 2-4"
// （g2 = 先弹 2 品倚音再进入该音）。
// 品数从变调夹算起，与原谱一致。
// ---------------------------------------------------------------------
function ev(t, d, notes, tech = '') {
  const et = tech.split(/\s+/).filter(Boolean);
  const ns = notes.trim().split(/\s+/).map(x => {
    const [sf, ...mods] = x.split(':');
    const [s, f] = sf.split('-').map(Number);
    const n = { s, f, tech: new Set(et), grace: null };
    for (const m of mods) { if (/^g\d+$/.test(m)) n.grace = +m.slice(1); else n.tech.add(m); }
    return n;
  });
  const all = new Set(); ns.forEach(n => n.tech.forEach(k => all.add(k)));
  return { t, d, notes: ns, tech: all };
}

const DEMOS = {
  pm: [ev(0,1,'5-0','pm'), ev(1,.25,'6-4','pm'), ev(1.25,1.75,'6-7','sl pm'), ev(3.5,1,'5-0'), ev(4.5,.25,'6-4'), ev(4.75,1.75,'6-7','sl')],
  sl: [ev(0,.25,'1-2'), ev(.25,.5,'1-4','sl'), ev(.75,1.5,'1-2','sl')],
  h: [ev(0,.5,'2-0'), ev(.5,1.5,'2-2','h')],
  p: [ev(0,.5,'1-2'), ev(.5,1.5,'1-0','p')],
  nh: [ev(0,2,'2-5','nh')],
  ah: [ev(0,1.5,'3-2'), ev(1.5,2,'3-2','ah')],
  roll: [ev(0,2,'4-13 3-15 2-16 1-14','roll')],
  grace: [ev(0,1,'1-4:h:g2'), ev(1.5,1,'3-3:sl:g2')],
  si: [ev(0,1.5,'2-5 3-5','si'), ev(1.5,1.5,'1-6 2-7','si')],
  tri: [ev(0,T3,'4-3'), ev(T3,T3,'3-0'), ev(2*T3,T3,'2-0'), ev(1,T3,'4-3'), ev(1+T3,T3,'3-0'), ev(1+2*T3,T3,'2-0')],
};
/** 记号说明里的“试听”：按 72 BPM 弹一段示范 */
export function playDemo(name) {
  initAudio(); stopAllVoices();
  if (gtGain) gtGain.gain.value = 0.8;
  const spb = 60 / 72, t0 = ac.currentTime + 0.05;
  for (const e of DEMOS[name] || []) playEvent(e, t0 + e.t * spb);
}
