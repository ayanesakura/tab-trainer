// 调弦页：调弦表（每根弦要怎么调，带“听”按钮）和麦克风调音器。音名显示用 ♯。

import { get, on } from './store.js';
import { $, UI } from './dom.js';
import { ac, initAudio, pluck } from './audio.js';
import { mname, mtof, STD_MIDI } from './view.js';

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
}
function autoCorrelate(buf, sr) {
  let n = buf.length, rms = 0;
  for (let i = 0; i < n; i++) rms += buf[i] * buf[i];
  if (Math.sqrt(rms / n) < 0.012) return -1;
  let r1 = 0, r2 = n - 1; const th = 0.2;
  for (let i = 0; i < n / 2; i++) if (Math.abs(buf[i]) < th) { r1 = i; break; }
  for (let i = 1; i < n / 2; i++) if (Math.abs(buf[n - i]) < th) { r2 = n - i; break; }
  buf = buf.slice(r1, r2); n = buf.length;
  const c = new Float32Array(n);
  for (let i = 0; i < n; i++) { let s = 0; for (let j = 0; j < n - i; j++) s += buf[j] * buf[j + i]; c[i] = s; }
  let d = 0; while (d < n - 1 && c[d] > c[d + 1]) d++;
  let mv = -1, mp = -1;
  for (let i = d; i < n; i++) if (c[i] > mv) { mv = c[i]; mp = i; }
  if (mp <= 0 || mp >= n - 1) return -1;
  const x1 = c[mp - 1], x2 = c[mp], x3 = c[mp + 1], a = (x1 + x3 - 2 * x2) / 2, b = (x3 - x1) / 2;
  return sr / (a ? mp - b / (2 * a) : mp);
}
let mic = null; // 开着时 {stream, src, timer}
const MIC_BLOCKED = '麦克风只能在本机地址或 HTTPS 下使用。局域网打开时请用每根弦的“听”按钮对音。';
const MIC_IDLE = '弹一根空弦，自动识别是哪根';
let starting = false;
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
  const an = ac.createAnalyser(); an.fftSize = 2048;
  const src = ac.createMediaStreamSource(stream);
  src.connect(an);
  const buf = new Float32Array(an.fftSize), hist = [];
  const timer = setInterval(() => {
    an.getFloatTimeDomainData(buf);
    const f = autoCorrelate(buf, ac.sampleRate);
    if (f < 60 || f > 400) return;
    hist.push(f); if (hist.length > 5) hist.shift();
    const fm = [...hist].sort((x, y) => x - y)[Math.floor(hist.length / 2)];
    const m = 69 + 12 * Math.log2(fm / 440);
    let best = TUNE[0]; for (const t of TUNE) if (Math.abs(m - t.m) < Math.abs(m - best.m)) best = t;
    const cents = Math.round((m - best.m) * 100), ok = Math.abs(cents) <= 5;
    TUNE.forEach(t => $('tr' + t.s).classList.toggle('hit', t === best));
    $('tNote').textContent = mname(Math.round(m));
    $('tNeedle').style.left = (50 + Math.max(-50, Math.min(50, cents))) + '%';
    $('tNeedle').classList.add('live');
    $('tNeedle').classList.toggle('ok', ok);
    $('tRead').innerHTML = `${best.s} 弦 → 目标 <b>${mname(best.m)}</b>　当前 ${fm.toFixed(1)} Hz，${cents > 0 ? '+' : ''}${cents} 音分　` +
      (ok ? '<span class="chg-same">✓ 准了</span>' : Math.abs(cents) > 300 ? '差得较多' : cents > 0 ? '偏高，往下调' : '偏低，往上调');
  }, 80);
  mic = { stream, src, timer };
}
/** 关掉麦克风（再点一次按钮、离开调弦页时）：不然系统的麦克风指示一直亮着，后台每 80ms 还在算音高 */
function stopMic() {
  if (!mic) return;
  clearInterval(mic.timer);
  mic.src.disconnect();
  mic.stream.getTracks().forEach(t => t.stop());
  mic = null;
  $('micBtn').innerHTML = UI.icon('mic') + '开启麦克风';
  $('tNeedle').classList.remove('live', 'ok'); $('tNeedle').style.left = '50%';
  $('tNote').textContent = '--';
  $('tRead').textContent = MIC_IDLE;
  TUNE.forEach(t => { const r = $('tr' + t.s); if (r) r.classList.remove('hit'); });
}

export function initTuner() {
  $('micBtn').onclick = () => (mic ? stopMic() : startMic());
  on('ui.view', v => { if (v !== 'tune') stopMic(); });
  if (!window.isSecureContext) { $('micBtn').disabled = true; $('tRead').textContent = MIC_BLOCKED; }
  $('tuneRows').addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return; initAudio(); pluck(0, +b.dataset.m, ac.currentTime + 0.02); });
  on('view', v => { if (v) renderTune(v); });
  if (get('view')) renderTune(get('view'));
}
