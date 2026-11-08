// 调音器的音高检测和读数稳定：纯计算，不碰 DOM 和 Web Audio，Node 里直接测（tests/js/pitch.test.mjs）。
//
// detectPitch：YIN（累积平均归一化差函数 CMNDF + 绝对阈值 0.15），差函数用 FFT 算自相关。先低通去掉高频噪声；
//   取第一个低于阈值的谷，再拿它的约数 / 倍数比一比、参考调弦和正在跟踪的音（防八度错），最后在原始差函数上抛物线插值。
// PitchSmoother：在音分上做稳定——离群值要连着几帧都这么说才认，平稳时越平均越稳（累积平均 → 按抖动大小定的指数平滑），
//   CUSUM 发现音真的变了（拧弦）就重新开始平均；没信号时保留上一个读数一会儿。
// LevelTracker：噪声底跟踪（自适应门限）和拨弦起音检测。
// TunerEngine：把上面几样串起来，再加选弦和“准了”的滞回，给 tuner.js 直接显示。

/** 频率 → “MIDI 音分”（MIDI × 100，A4 = 6900） */
export const ftoc = f => 6900 + 1200 * Math.log2(f / 440);
export const ctof = c => 440 * Math.pow(2, (c - 6900) / 1200);

// ---- FFT（基 2，原地，不归一化） ----
const tables = new Map();
function fftTables(n) {
  let t = tables.get(n);
  if (t) return t;
  const rev = new Uint32Array(n), bits = Math.round(Math.log2(n));
  for (let i = 0; i < n; i++) { let r = 0; for (let b = 0, x = i; b < bits; b++, x >>= 1) r = (r << 1) | (x & 1); rev[i] = r; }
  const cos = new Float64Array(n / 2), sin = new Float64Array(n / 2);
  for (let i = 0; i < n / 2; i++) { cos[i] = Math.cos((2 * Math.PI * i) / n); sin[i] = Math.sin((2 * Math.PI * i) / n); }
  t = { rev, cos, sin };
  tables.set(n, t);
  return t;
}
function fft(re, im, inverse) {
  const n = re.length, { rev, cos, sin } = fftTables(n);
  for (let i = 0; i < n; i++) {
    const j = rev[i];
    if (j > i) { let x = re[i]; re[i] = re[j]; re[j] = x; x = im[i]; im[i] = im[j]; im[j] = x; }
  }
  for (let size = 2; size <= n; size <<= 1) {
    const half = size >> 1, step = n / size;
    for (let i = 0; i < n; i += size) {
      for (let j = 0, k = 0; j < half; j++, k += step) {
        const wr = cos[k], wi = inverse ? sin[k] : -sin[k], a = i + j, b = a + half;
        const tr = re[b] * wr - im[b] * wi, ti = re[b] * wi + im[b] * wr;
        re[b] = re[a] - tr; im[b] = im[a] - ti; re[a] += tr; im[a] += ti;
      }
    }
  }
}
const nextPow2 = n => 2 ** Math.ceil(Math.log2(n));
const rmsOf = x => { let e = 0; for (let i = 0; i < x.length; i++) e += x[i] * x[i]; return Math.sqrt(e / x.length); };

/**
 * YIN 差函数 d(τ) = Σ_{j<W} (x_j − x_{j+τ})²，τ = 0..tauMax+1，W = 缓冲长度 − tauMax − 1。
 * 自相关 Σ a_j b_{j+τ} 用一次复数 FFT 同时变换两路实信号（a 是前 W 个样本，b 是整段）。
 */
function differences(x, tauMax) {
  const n = x.length, W = n - tauMax - 1, M = nextPow2(n);
  const re = new Float64Array(M), im = new Float64Array(M);
  for (let i = 0; i < n; i++) { im[i] = x[i]; if (i < W) re[i] = x[i]; }
  fft(re, im, false);
  const pr = new Float64Array(M), pi = new Float64Array(M);
  for (let k = 0; k < M; k++) {
    const m = (M - k) % M, zr = re[k], zi = im[k], wr = re[m], wi = -im[m]; // w = conj(Z[−k])
    const ar = (zr + wr) / 2, ai = (zi + wi) / 2, br = (zi - wi) / 2, bi = -(zr - wr) / 2; // A、B：两路信号各自的频谱
    pr[k] = ar * br + ai * bi; pi[k] = ar * bi - ai * br; // conj(A)·B
  }
  fft(pr, pi, true);
  const sq = new Float64Array(n + 1); // 平方的前缀和：窗口能量
  for (let i = 0; i < n; i++) sq[i + 1] = sq[i] + x[i] * x[i];
  const d = new Float64Array(tauMax + 2), e0 = sq[W];
  for (let t = 0; t <= tauMax + 1; t++) d[t] = Math.max(0, e0 + (sq[t + W] - sq[t]) - (2 * pr[t]) / M);
  return d;
}

/**
 * 低通（两级 RBJ 双二阶，4 阶巴特沃斯）：调音只需要基音和前几个泛音，滤掉高频噪声，差函数的谷就不会被噪声
 * 的细小起伏带偏（宽带噪声下低音弦能差十几音分）。线性时不变滤波不改变周期；开头 skip 个样本是滤波器的起振，丢掉。
 */
function lowpass(x, sr, fc, skip) {
  const w = (2 * Math.PI * fc) / sr, al = Math.sin(w) / (2 * Math.SQRT1_2), cw = Math.cos(w), a0 = 1 + al;
  const b0 = (1 - cw) / 2 / a0, b1 = (1 - cw) / a0, a1 = (-2 * cw) / a0, a2 = (1 - al) / a0;
  for (let pass = 0; pass < 2; pass++) {
    let x1 = 0, x2 = 0, y1 = 0, y2 = 0;
    for (let i = 0; i < x.length; i++) {
      const v = x[i], y = b0 * v + b1 * x1 + b0 * x2 - a1 * y1 - a2 * y2;
      x2 = x1; x1 = v; y2 = y1; y1 = y; x[i] = y;
    }
  }
  return x.subarray(skip);
}

/** 在 cm 上找 [lo, hi] 里的最小值（倍数 / 约数附近的谷） */
function dip(cm, lo, hi) {
  let best = -1;
  for (let t = Math.max(1, lo); t <= Math.min(cm.length - 2, hi); t++) if (best < 0 || cm[t] < cm[best]) best = t;
  return best;
}

/**
 * 一帧的音高。buf：时域样本（Float32Array），sr：采样率。
 * fmin / fmax：只在这个范围里找（调音器按当前调弦给）；threshold：YIN 的绝对阈值；
 * cutoff：先低通到这个频率（默认 fmax 的 4 倍，1–4 kHz）；near：正在跟踪的频率（有的话优先取同一个八度）。
 * 返回 {freq, clarity, rms}：freq 为 0 表示这一帧没有可信的音高；clarity = 1 − CMNDF（1 最干净）。
 * 不做音量门限（rms 给调用方自己判断，见 LevelTracker）。
 */
export function detectPitch(buf, sr, { fmin = 60, fmax = 500, threshold = 0.15, near = 0, targets = null, cutoff = Math.min(4000, Math.max(1000, fmax * 4)) } = {}) {
  let mean = 0;
  for (let i = 0; i < buf.length; i++) mean += buf[i];
  mean /= buf.length;
  let x = new Float64Array(buf.length), e = 0;
  for (let i = 0; i < buf.length; i++) { x[i] = buf[i] - mean; e += x[i] * x[i]; }
  const rms = Math.sqrt(e / buf.length);
  if (cutoff < sr / 2.5) x = lowpass(x, sr, cutoff, Math.min(256, Math.round(sr / 200)));
  const n = x.length;
  const tauMax = Math.min(Math.ceil(sr / fmin) + 1, Math.floor(n / 3)), tauMin = Math.max(2, Math.floor(sr / fmax));
  if (!(rms > 0) || tauMin >= tauMax) return { freq: 0, clarity: 0, rms };
  const d = differences(x, tauMax);
  const cm = new Float64Array(tauMax + 2); // 累积平均归一化差函数
  cm[0] = 1;
  for (let t = 1, run = 0; t <= tauMax + 1; t++) { run += d[t]; cm[t] = run > 0 ? (d[t] * t) / run : 1; }
  // 第一个低于阈值的谷（最先出现的强周期）；没有就算这一帧没音高
  let t0 = -1;
  for (let t = tauMin; t <= tauMax; t++) {
    if (cm[t] < threshold) { while (t + 1 <= tauMax && cm[t + 1] < cm[t]) t++; t0 = t; break; }
  }
  if (t0 < 0) return { freq: 0, clarity: 1 - Math.min(1, cm[dip(cm, tauMin, tauMax)]), rms };
  // 防八度错。候选：第一个谷 τ0、它的约数（τ0/2、τ0/3）和倍数（2τ0…4τ0）附近最深的谷。
  const cands = [];
  for (const k of [1 / 3, 1 / 2, 1, 2, 3, 4]) {
    const c = t0 * k;
    if (c < tauMin - 1 || c > tauMax) continue;
    const t = k === 1 ? t0 : dip(cm, Math.floor(c * 0.97), Math.ceil(c * 1.03));
    if (t > 0) cands.push({ k, t, v: cm[t] });
  }
  let best = cands.find(c => c.k === 1);
  // 约数和 τ0 几乎一样深：τ0 其实是两个周期（低了八度），取短的
  const shorter = cands.filter(c => c.k < 1 && c.v <= best.v * 1.25 + 0.003);
  if (shorter.length) best = shorter[0];
  // 倍数明显更深、τ0 本身又不太干净：基音和奇次泛音都很弱时 τ0 是半个周期（高了八度）。
  // 只在差得很明显时才换——别的空弦被带响时，倍数处也会更深一点
  else if (best.v > 0.05) { const longer = cands.find(c => c.k > 1 && c.v < best.v / 3); if (longer) best = longer; }
  // 调弦先验：选出来的音离哪根弦都很远（超过 5 个半音），而另一个候选靠近某根弦，就用那个。
  // 有先验撑腰的候选，谷的深度放宽到阈值的两倍（音快衰减完时真周期的谷会变浅）
  const loose = threshold * 2;
  const plausible = c => !targets || targets.some(f => Math.abs(Math.log2(sr / c.t / f)) < 5 / 12);
  if (!plausible(best)) { const alt = cands.find(c => c.v < loose && plausible(c)); if (alt) best = alt; }
  // 音在衰减时奇次泛音先沉到噪声里，看起来像高八度（或者真周期的谷变浅、倍数先过了阈值，像低八度）：
  // 已经锁定了一个音（near）就优先取和它同一个八度的候选
  if (near) {
    const keep = cands.find(c => c.v < loose && Math.abs(Math.log2((sr / c.t) / near)) < 0.1);
    if (keep) best = keep;
  }
  // 精确周期：在原始差函数的谷上抛物线插值（CMNDF 和原始差函数的谷可能差一个样本）
  let tp = best.t;
  while (tp > 1 && d[tp - 1] < d[tp]) tp--;
  while (tp < tauMax && d[tp + 1] < d[tp]) tp++;
  const a = d[tp - 1], b = d[tp], c = d[tp + 1], den = a - 2 * b + c;
  const shift = den > 0 ? Math.max(-1, Math.min(1, (a - c) / (2 * den))) : 0;
  return { freq: sr / (tp + shift), clarity: 1 - Math.min(1, cm[best.t]), rms };
}

/**
 * 读数稳定（音分域）。每帧 push(c, t)：c 是这一帧的音分（ftoc），没有可信音高时传 null；t 是毫秒。
 * 返回 {cents, held, seg}：cents 为稳定后的音分（还没有读数时为 null），held 表示这一帧没用上、显示的是之前的读数，
 * seg 在“开始一个新音”（锁定到新的音）时加一。
 *
 * - 锁定：刚开始 / 起音之后，最近 3 帧里有 2 帧对得上（相差 ≤ agree 音分）才出读数，确认之前继续显示旧读数。
 * - 离群：和当前读数差 max(gate, 4σ) 以上的帧先不用；连着 confirm 帧都一致才认为换了音（重新锁定），
 *   差的正好是八度这类倍频关系时要再多 5 帧。
 * - 平均：锁定后先累积平均（1/n），再过渡到指数平滑；平滑系数下限按抖动 σ 定（≈ 1/σ²，σ 越大越平滑），
 *   这样干净的信号反应快，抖得厉害的信号也稳。σ 用相邻两帧的差估计，拧弦时的匀速变化不会把 σ 抬高。
 * - 变化：CUSUM 发现读数持续往一边偏（在拧弦），就用这一串偏离的帧重新开始平均，跟得上。
 */
export class PitchSmoother {
  constructor({ holdMs = 1500, gate = 50, confirm = 3, agree = 40, alphaMin = 0.01, alphaMax = 0.5, cusumK = 0.75, cusumH = 8, sigmaFloor = 1, sigma0 = 2 } = {}) {
    Object.assign(this, { holdMs, gate, confirm, agree, alphaMin, alphaMax, cusumK, cusumH, sigmaFloor, sigma0 });
    this.est = null; this.seg = 0; this.lastT = -Infinity;
    this.unlock();
  }
  /** 新的拨弦：之后的值重新确认、重新平均；确认之前继续显示旧读数 */
  unlock() { this.locked = false; this.pending = []; }
  /** 一组值里和中位数相差不超过 agree 的那些（至少 need 个才算数） */
  cluster(vals, need) {
    const med = [...vals].sort((a, b) => a - b)[vals.length >> 1], near = vals.filter(v => Math.abs(v - med) <= this.agree);
    return near.length >= need ? near : null;
  }
  restart(vals) {
    this.est = vals.reduce((a, b) => a + b, 0) / vals.length;
    this.n = vals.length; this.gp = this.gn = 0; this.run = 0; this.recent = vals.slice(-8);
  }
  lock(vals, t) {
    this.restart(vals);
    this.locked = true; this.pending = []; this.sigma = this.sigma0; this.prevRaw = vals.at(-1); this.lastT = t;
    this.seg++;
  }
  push(c, t) {
    const out = held => ({ cents: this.est, held, seg: this.seg });
    if (this.est != null && t - this.lastT > this.holdMs) { this.est = null; this.unlock(); } // 保留够久了：清掉
    if (c == null || !Number.isFinite(c)) return out(this.est != null);
    if (!this.locked) {
      this.pending.push(c);
      if (this.pending.length > 3) this.pending.shift();
      const near = this.cluster(this.pending, 2);
      if (!near) return out(this.est != null);
      this.lock(near, t);
      return out(false);
    }
    const dlt = c - this.est;
    if (Math.abs(dlt) > Math.max(this.gate, 4 * this.sigma)) { // 离群（多半是八度错）：连着几帧都这么说才换过去
      // 正好差八度 / 八度加五度 / 两个八度：多半是同一个音认错了周期（衰减末尾、别的弦共鸣），要更多帧才信
      const harm = [1200, 1902, 2400].some(h => Math.abs(Math.abs(dlt) - h) < 50), need = harm ? this.confirm + 5 : this.confirm;
      this.pending.push(c);
      while (this.pending.length > need) this.pending.shift();
      if (this.pending.length >= need) {
        const near = this.cluster(this.pending, need);
        if (near) { this.lock(near, t); return out(false); }
      }
      return out(true);
    }
    this.pending = []; this.lastT = t;
    // 相邻两帧之差的平均绝对值 × 1.25 / √2 ≈ 每帧抖动的标准差
    this.sigma = Math.max(this.sigmaFloor, 0.9 * this.sigma + 0.1 * 0.886 * Math.abs(c - this.prevRaw));
    this.prevRaw = c;
    this.recent.push(c);
    if (this.recent.length > 8) this.recent.shift();
    const z = dlt / this.sigma;
    this.gp = Math.max(0, this.gp + z - this.cusumK);
    this.gn = Math.max(0, this.gn - z - this.cusumK);
    this.run = this.gp > 0 || this.gn > 0 ? this.run + 1 : 0;
    if (this.gp > this.cusumH || this.gn > this.cusumH) { // 读数持续往一边偏：在拧弦，用偏离以来的这几帧重新平均
      this.restart(this.recent.slice(-Math.max(1, Math.min(this.run, 4))));
    } else {
      this.n++;
      const floor = Math.min(this.alphaMax, Math.max(this.alphaMin, 1 / (this.sigma * this.sigma)));
      this.est += Math.max(floor, 1 / this.n) * dlt;
    }
    return out(false);
  }
}

/**
 * 音量：噪声底（更安静时立刻跟下去，没有音高的帧里每秒最多涨 riseDb dB）和起音（短时音量比上一帧跳了 jump 倍）。
 * update(rms, voiced, dtMs) → {open, onset}：open 表示高过门限（噪声底 × ratio，且不低于 minGate）。
 */
export class LevelTracker {
  constructor({ ratio = 2.5, minGate = 0.0015, maxFloor = 0.002, riseDb = 6, jump = 2 } = {}) {
    Object.assign(this, { ratio, minGate, maxFloor, riseDb, jump });
    this.floor = null; this.prev = 0;
  }
  get gate() { return Math.max(this.minGate, (this.floor ?? 0) * this.ratio); }
  update(rms, voiced, dtMs) {
    if (this.floor == null) this.floor = Math.min(rms, this.maxFloor); // 第一帧：当成环境噪声（打开时可能正响着，所以设上限）
    else if (rms < this.floor) this.floor = rms > 0 ? rms : this.floor;
    else if (!voiced) this.floor = Math.min(rms, this.floor * Math.pow(10, (this.riseDb * dtMs) / 1000 / 20));
    const open = rms >= this.gate, onset = open && rms > this.jump * Math.max(this.prev, this.gate);
    this.prev = rms;
    return { open, onset };
  }
}

/**
 * 调音器：targets = [{s, m}]（弦号、目标 MIDI），sr = 采样率。
 * process(buf, t, shortRms) → 显示用的状态：
 *   {live, held, hz, cents（相对目标弦，整数）, target, ok, note（最近的半音 MIDI）}；live 为 false 表示没有读数，
 *   held 表示这一帧没测到、显示的是刚才的读数（音在衰减）。
 * shortRms：最近 20ms 左右的音量（起音检测要快，整段缓冲 170ms 太钝），不给就用整段的。
 */
export class TunerEngine {
  constructor(targets, sr, { okIn = 4, okOut = 7, switchMargin = 30, switchFrames = 4, ...opts } = {}) {
    this.targets = targets; this.sr = sr;
    Object.assign(this, { okIn, okOut, switchMargin, switchFrames });
    const ms = targets.map(t => t.m);
    this.freqs = ms.map(m => 440 * Math.pow(2, (m - 69) / 12));
    // 只在调弦范围附近找：最低弦再低 7 个半音（还没调上去的弦）到最高弦再高 7 个半音
    this.fmin = Math.max(30, 440 * Math.pow(2, (Math.min(...ms) - 7 - 69) / 12));
    this.fmax = 440 * Math.pow(2, (Math.max(...ms) + 7 - 69) / 12);
    this.smoother = new PitchSmoother(opts);
    this.level = new LevelTracker(opts);
    this.target = null; this.ok = false; this.seg = -1; this.votes = 0; this.lastT = null; this.onsetT = null; this.voiced = false;
  }
  /** 缓冲长度（2 的幂，给 AnalyserNode.fftSize）：差函数的积分窗口要盖住最低弦 6 个以上周期，再加上最长的延迟 */
  get bufferSize() {
    const low = 440 * Math.pow(2, (Math.min(...this.targets.map(t => t.m)) - 69) / 12);
    return Math.min(32768, nextPow2((6.5 * this.sr) / low + this.sr / this.fmin + 2));
  }
  process(buf, t, shortRms) {
    const dt = this.lastT == null ? 40 : t - this.lastT;
    this.lastT = t;
    const sm = this.smoother, sr = this.sr;
    const lv = this.level.update(shortRms ?? rmsOf(buf), this.voiced, dt);
    if (lv.onset) { sm.unlock(); this.onsetT = t; }
    // 刚拨弦：整段缓冲里大半还是拨弦之前的声音（上一个音、拨弦的瞬间），只分析拨弦之后的部分（跳过起音的头 20ms），
    // 够长了（能放下 3 个最长周期）才开始测，等它涨满整段缓冲再恢复正常
    let win = buf;
    if (this.onsetT != null) {
      const post = Math.round(((t - this.onsetT - 20) * sr) / 1000) + 512;
      if (post >= buf.length) this.onsetT = null;
      else win = post >= (3 * sr) / this.fmin ? buf.subarray(buf.length - post) : null;
    }
    const hint = sm.locked && sm.est != null ? ctof(sm.est) : 0;
    const p = win ? detectPitch(win, sr, { fmin: this.fmin, fmax: this.fmax, near: hint, targets: this.freqs }) : { freq: 0 };
    this.voiced = p.freq > 0;
    const s = sm.push(lv.open && p.freq > 0 ? ftoc(p.freq) : null, t);
    if (s.cents == null) { this.target = null; this.ok = false; return { live: false }; }
    const m = s.cents / 100, dist = tg => Math.abs(m - tg.m) * 100;
    const near = this.targets.reduce((a, b) => (dist(b) < dist(a) ? b : a));
    if (s.seg !== this.seg || !this.target) { this.target = near; this.votes = 0; this.ok = false; } // 新的音：直接选最近的弦
    else if (near !== this.target && dist(this.target) - dist(near) > this.switchMargin) {
      if (++this.votes >= this.switchFrames) { this.target = near; this.votes = 0; this.ok = false; }
    } else this.votes = 0;
    this.seg = s.seg;
    const rel = s.cents - this.target.m * 100;
    this.ok = Math.abs(rel) <= (this.ok ? this.okOut : this.okIn);
    return { live: true, held: s.held, hz: ctof(s.cents), cents: Math.round(rel) || 0, target: this.target, ok: this.ok, note: Math.round(m) };
  }
}
