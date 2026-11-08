// 调弦页：调弦表（每根弦要怎么调，带“听”按钮）和麦克风调音器。音名显示用 ♯。
// 音高检测和读数稳定在 lib/pitch.js（纯计算，Node 里测）；这里只管麦克风、取样和显示。

import { get, on } from './store.js';
import { $, UI } from './dom.js';
import { ac, initAudio, pluck } from './audio.js';
import { mname, mtof, STD_MIDI } from './view.js';
import { TunerEngine } from '../lib/pitch.js';

let TUNE = [];
function howTo(d) {
  if (d === 0) return ['不变', 'chg-same'];
  const words = { 1: '半音', 2: '一个全音', 3: '3 个半音' };
  const w = words[Math.abs(d)] || `${Math.abs(d)} 个半音`;
  return [(d < 0 ? '降' : '升') + w, d < 0 ? 'chg-down' : 'chg-up'];
}
function renderTune(v) {
  TUNE = [6, 5, 4, 3, 2, 1].map(s => { const [how, cls] = howTo(v.open[s] - STD_MIDI[s - 1]); return { s, std: mname(STD_MIDI[s - 1]), m: v.open[s], how, cls }; });
  $('tuneRows').innerHTML = TUNE.map(t => `<tr id="tr${t.s}"><td><b>${t.s} 弦</b></td><td class="muted">${t.std}</td><td><b>${mname(t.m)}</b> <span class="hint">${mtof(t.m).toFixed(1)} Hz</span></td><td class="${t.cls}">${t.how}</td><td class="tune-play"><button class="btn sm" data-m="${t.m}" aria-label="听 ${t.s} 弦的目标音 ${mname(t.m)}">${UI.icon('volume', 13)}听</button></td></tr>`).join('');
  const names = [6, 5, 4, 3, 2, 1].map(s => v.openName[s]).join('–');
  const std = TUNE.every(t => t.how === '不变');
  $('tuneTitle').textContent = `调弦：${std ? '标准调弦' : names}${v.capo ? ` · 变调夹 ${v.capo} 品` : ''}`;
  if (mic) { makeEngine(); mic.shown = null; } // 表格重画了（高亮没了）；调弦改了还要按新的目标重新识别
}

let mic = null; // 开着时 {stream, src, an, buf, eng, key, raf, last, shown}
const MIC_BLOCKED = '麦克风只能在本机地址或 HTTPS 下使用。局域网打开时请用每根弦的“听”按钮对音。';
const MIC_IDLE = '弹一根空弦，自动识别是哪根';
const FRAME_MS = 40; // 每秒算 25 次音高就够了（一次不到 1ms），指针的平滑靠 CSS 过渡
let starting = false;
/** 按当前调弦建音高跟踪（目标没变就沿用，不打断正在显示的读数）；缓冲长度跟着最低弦变 */
function makeEngine() {
  const key = TUNE.map(t => t.m).join();
  if (mic.eng && mic.key === key) return;
  const eng = new TunerEngine(TUNE.map(t => ({ s: t.s, m: t.m })), ac.sampleRate);
  if (mic.an.fftSize !== eng.bufferSize) mic.an.fftSize = eng.bufferSize;
  mic.buf = new Float32Array(mic.an.fftSize);
  Object.assign(mic, { eng, key });
}
/** 显示一帧的读数（只在变了的时候动 DOM） */
function show(r) {
  const key = r.live ? [r.target.s, r.cents, r.ok, r.held, r.note, r.hz.toFixed(1)].join() : '';
  if (key === mic.shown) return;
  mic.shown = key;
  if (!r.live) { resetDisplay(); return; }
  TUNE.forEach(t => $('tr' + t.s).classList.toggle('hit', t.s === r.target.s));
  $('tNote').textContent = mname(r.note);
  const nd = $('tNeedle');
  nd.style.left = 50 + Math.max(-50, Math.min(50, r.cents)) + '%';
  nd.classList.add('live');
  nd.classList.toggle('ok', r.ok);
  nd.classList.toggle('held', r.held); // 这一帧没测到（音在衰减）：显示的是刚才的读数
  const tip = r.ok ? '<span class="chg-same">✓ 准了</span>' : Math.abs(r.cents) > 300 ? '差得较多' : r.cents > 0 ? '偏高，往下调' : '偏低，往上调';
  $('tRead').innerHTML = `${r.target.s} 弦 → 目标 <b>${mname(r.target.m)}</b>　当前 ${r.hz.toFixed(1)} Hz，${r.cents > 0 ? '+' : ''}${r.cents} 音分　${tip}`;
}
function resetDisplay() {
  const nd = $('tNeedle');
  nd.classList.remove('live', 'ok', 'held'); nd.style.left = '50%';
  $('tNote').textContent = '--';
  $('tRead').textContent = MIC_IDLE;
  TUNE.forEach(t => { const r = $('tr' + t.s); if (r) r.classList.remove('hit'); });
}
function tick(now) {
  if (!mic) return;
  mic.raf = requestAnimationFrame(tick);
  if (now - mic.last < FRAME_MS) return;
  mic.last = now;
  const { an, buf } = mic;
  an.getFloatTimeDomainData(buf);
  let e = 0; // 最近 1024 个样本（约 20ms）的音量：判断是不是刚拨了弦
  for (let i = buf.length - 1024; i < buf.length; i++) e += buf[i] * buf[i];
  show(mic.eng.process(buf, now, Math.sqrt(e / 1024)));
}
async function startMic() {
  if (mic || starting) return;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { $('tRead').textContent = MIC_BLOCKED; return; }
  initAudio();
  let stream;
  starting = true;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false } }); }
  catch (err) { $('tRead').textContent = '麦克风打开失败：' + err.message; return; }
  finally { starting = false; }
  if (get('ui.view') !== 'tune') { stream.getTracks().forEach(t => t.stop()); return; } // 等授权的时候已经离开了调弦页
  $('micBtn').innerHTML = '<span class="dot ok"></span>调音器已开启 · 点击关闭';
  const an = ac.createAnalyser();
  const src = ac.createMediaStreamSource(stream);
  src.connect(an);
  mic = { stream, src, an, buf: null, eng: null, key: '', raf: 0, last: -Infinity, shown: null };
  makeEngine();
  mic.raf = requestAnimationFrame(tick);
}
/** 关掉麦克风（再点一次按钮、离开调弦页时）：不然系统的麦克风指示一直亮着，后台还在算音高 */
function stopMic() {
  if (!mic) return;
  cancelAnimationFrame(mic.raf);
  mic.src.disconnect();
  mic.stream.getTracks().forEach(t => t.stop());
  mic = null;
  $('micBtn').innerHTML = UI.icon('mic') + '开启麦克风';
  resetDisplay();
}

export function initTuner() {
  $('micBtn').onclick = () => (mic ? stopMic() : startMic());
  on('ui.view', v => { if (v !== 'tune') stopMic(); });
  if (!window.isSecureContext) { $('micBtn').disabled = true; $('tRead').textContent = MIC_BLOCKED; }
  $('tuneRows').addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return; initAudio(); pluck(0, +b.dataset.m, ac.currentTime + 0.02); });
  on('view', v => { if (v) renderTune(v); });
  if (get('view')) renderTune(get('view'));
}
