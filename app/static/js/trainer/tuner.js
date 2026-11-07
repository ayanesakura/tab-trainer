// 调弦页：调弦表（每根弦要怎么调，带“听”按钮）和麦克风调音器

const STD = { 1: 64, 2: 59, 3: 55, 4: 50, 5: 45, 6: 40 };
let TUNE = [];
function howTo(d) {
  if (d === 0) return ['不变', 'chg-same'];
  const words = { 1: '半音', 2: '一个全音', 3: '一个半全音（3 个半音）' };
  const w = words[Math.abs(d)] || `${Math.abs(d)} 个半音`;
  return [(d < 0 ? '降' : '升') + w, d < 0 ? 'chg-down' : 'chg-up'];
}
function renderTune() {
  TUNE = [6, 5, 4, 3, 2, 1].map(s => { const [how, cls] = howTo(OPEN[s] - STD[s]); return { s, std: mname(STD[s]), m: OPEN[s], how, cls }; });
  $('tuneRows').innerHTML = TUNE.map(t => `<tr id="tr${t.s}"><td><b>${t.s} 弦</b></td><td class="muted">${t.std}</td><td><b>${mname(t.m)}</b> <span class="hint">${mtof(t.m).toFixed(1)} Hz</span></td><td class="${t.cls}">${t.how}</td><td style="text-align:right"><button class="btn sm" data-m="${t.m}">${UI.icon('volume', 13)}听</button></td></tr>`).join('');
  const names = [6, 5, 4, 3, 2, 1].map(s => OPEN_NAME[s]).join('–');
  const std = TUNE.every(t => t.how === '不变');
  $('tuneTitle').textContent = `调弦：${std ? '标准调弦' : names}${CAPO ? ` · 变调夹 ${CAPO} 品` : ''}`;
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
let micOn = false;
const MIC_BLOCKED = '浏览器只允许在本机地址（http://127.0.0.1）或 HTTPS 页面里用麦克风。用平板通过局域网打开时调音器用不了，可以用上面每根弦的“听”按钮对音。';
async function startMic() {
  if (micOn) return;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { $('tRead').textContent = MIC_BLOCKED; return; }
  initAudio();
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false } }); }
  catch (err) { $('tRead').textContent = '麦克风打开失败：' + err.message; return; }
  micOn = true; $('micBtn').innerHTML = '<span class="dot ok"></span>调音器已开启';
  const an = ac.createAnalyser(); an.fftSize = 2048;
  ac.createMediaStreamSource(stream).connect(an);
  const buf = new Float32Array(an.fftSize), hist = [];
  setInterval(() => {
    an.getFloatTimeDomainData(buf);
    const f = autoCorrelate(buf, ac.sampleRate);
    if (f < 60 || f > 400) return;
    hist.push(f); if (hist.length > 5) hist.shift();
    const fm = [...hist].sort((x, y) => x - y)[Math.floor(hist.length / 2)];
    const m = 69 + 12 * Math.log2(fm / 440);
    let best = TUNE[0]; for (const t of TUNE) if (Math.abs(m - t.m) < Math.abs(m - best.m)) best = t;
    const cents = Math.round((m - best.m) * 100);
    TUNE.forEach(t => $('tr' + t.s).classList.toggle('hit', t === best));
    $('tNote').textContent = mname(Math.round(m));
    $('tNeedle').style.left = (50 + Math.max(-50, Math.min(50, cents))) + '%';
    const ok = Math.abs(cents) <= 5;
    $('tNeedle').style.background = ok ? 'var(--green)' : 'var(--amber)';
    $('tRead').innerHTML = `${best.s} 弦 → 目标 <b>${mname(best.m)}</b>　当前 ${fm.toFixed(1)} Hz，${cents > 0 ? '+' : ''}${cents} 音分　` +
      (ok ? '<span style="color:var(--green)">✓ 准了</span>' : Math.abs(cents) > 300 ? '差得较多，先大致拧到附近' : cents > 0 ? '偏高，往下调' : '偏低，往上调');
  }, 80);
}

function initTuner() {
  $('micBtn').onclick = startMic;
  if (!window.isSecureContext) { $('micBtn').disabled = true; $('tRead').textContent = MIC_BLOCKED; }
  $('tuneRows').addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return; initAudio(); pluck(0, +b.dataset.m, ac.currentTime + 0.02); });
}
