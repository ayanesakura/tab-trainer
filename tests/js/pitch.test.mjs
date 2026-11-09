// 调音器的音高检测和读数稳定（js/lib/pitch.js）：用合成的拨弦信号（泛音很强、基音很弱、带噪声、会衰减）测。
// 运行：node --test tests/js/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { detectPitch, PitchSmoother, LevelTracker, TunerEngine, ftoc, ctof } from '../../app/static/js/lib/pitch.js';

// 固定种子的随机数，结果每次一样
function rng(seed) {
  let s = seed >>> 0;
  const u = () => ((s = (Math.imul(s, 1664525) + 1013904223) >>> 0) / 4294967296);
  return { u, g: () => Math.sqrt(-2 * Math.log(u() + 1e-12)) * Math.cos(2 * Math.PI * u()) };
}
const mtof = m => 440 * 2 ** ((m - 69) / 12);
/**
 * 合成拨弦：各次泛音 amps[k−1]（之后按 0.7/k），高次泛音衰减更快，加白噪声。
 * f 可以是函数（秒 → Hz），相位连续地积分，用来模拟拧弦。
 */
function pluck(f, sr, dur, { amps = [0.15, 1, 0.8, 0.5], noise = 0.003, seed = 1, level = 0.1, start = 0 } = {}) {
  const r = rng(seed), N = Math.floor(sr * dur), y = new Float32Array(N), fn = typeof f === 'function' ? f : () => f;
  const nh = 24, ph = Array.from({ length: nh }, () => r.u() * 2 * Math.PI);
  let phase = 0;
  for (let i = 0; i < N; i++) {
    const t = i / sr, f0 = fn(t), age = t - start;
    phase += (2 * Math.PI * f0) / sr;
    if (age >= 0) {
      for (let k = 1; k <= nh && k * f0 < sr / 2.2; k++) {
        const a = amps[k - 1] ?? 0.7 / k;
        if (a) y[i] += level * a * Math.exp(-age / (1.6 / Math.sqrt(k))) * Math.sin(k * phase + ph[k - 1]);
      }
    }
    y[i] += noise * r.g();
  }
  return y;
}
const STD = [64, 59, 55, 50, 45, 40]; // 1 弦 → 6 弦
const targets = ms => ms.map((m, i) => ({ s: i + 1, m }));
/** 按 40ms 一帧把信号喂给调音器（和页面上一样：缓冲取最近 bufferSize 个样本，起音用最近 1024 个样本的音量） */
function run(eng, y, sr, step = 40) {
  const L = eng.bufferSize, out = [];
  for (let end = L; end <= y.length; end += Math.round((sr * step) / 1000)) {
    const buf = y.subarray(end - L, end);
    let e = 0;
    for (let i = L - 1024; i < L; i++) e += buf[i] * buf[i];
    out.push({ t: (end / sr) * 1000, ...eng.process(buf, (end / sr) * 1000, Math.sqrt(e / 1024)) });
  }
  return out;
}
const std = a => { const m = a.reduce((x, y) => x + y, 0) / a.length; return Math.sqrt(a.reduce((x, y) => x + (y - m) ** 2, 0) / a.length); };

test('detectPitch：标准调弦六根弦 + Drop D，泛音强 / 基音弱、带噪声、44.1 和 48 kHz，误差 < 1.5 音分，没有八度错', () => {
  const timbres = [[0.15, 1, 0.8, 0.5], [0.05, 1, 0.7, 0.6, 0.3], [1, 0.5, 0.3]]; // 基音弱、基音几乎没有、普通
  for (const sr of [44100, 48000]) {
    for (const [name, m, tune] of [...STD.map((m, i) => [`${i + 1} 弦`, m, STD]), ['Drop D', 38, [64, 59, 55, 50, 45, 38]]]) {
      const eng = new TunerEngine(targets(tune), sr);
      for (const [ti, amps] of timbres.entries()) {
        const f = mtof(m) * 2 ** (6 / 1200), y = pluck(f, sr, 2.2, { amps, seed: m * 7 + ti });
        let voiced = 0, frames = 0, worst = 0;
        for (let end = eng.bufferSize + Math.round(sr * 0.05); end <= y.length; end += Math.round(sr * 0.1)) {
          frames++;
          const p = detectPitch(y.subarray(end - eng.bufferSize, end), sr, { fmin: eng.fmin, fmax: eng.fmax });
          if (!p.freq) continue;
          voiced++;
          const err = 1200 * Math.log2(p.freq / f);
          assert.ok(Math.abs(err) < 600, `${sr} ${name} 音色${ti}：八度错（${p.freq.toFixed(1)} Hz，应为 ${f.toFixed(1)}）`);
          worst = Math.max(worst, Math.abs(err));
        }
        assert.ok(worst < 1.5, `${sr} ${name} 音色${ti}：最大误差 ${worst.toFixed(2)} 音分`);
        assert.ok(voiced >= frames * 0.9, `${sr} ${name} 音色${ti}：只有 ${voiced}/${frames} 帧测出音高`);
      }
    }
  }
});

test('detectPitch：噪声和静音不报音高', () => {
  const sr = 48000, r = rng(5), noise = Float32Array.from({ length: 8192 }, () => 0.05 * r.g());
  assert.equal(detectPitch(noise, sr, { fmin: 55, fmax: 500 }).freq, 0);
  assert.equal(detectPitch(new Float32Array(8192), sr).freq, 0);
});

test('detectPitch：已经锁定的音（near）在衰减末尾不跳到高八度', () => {
  // 基音和三次泛音都很弱：末尾奇次泛音沉进噪声，单看一帧像高八度
  const sr = 48000, f = mtof(40), y = pluck(f, sr, 3, { amps: [0.03, 1, 0.1, 0.5], noise: 0.008, seed: 3 });
  let free = 0, held = 0;
  for (let end = 8192 + sr * 1.8; end <= y.length; end += 2400) {
    const buf = y.subarray(end - 8192, end);
    const a = detectPitch(buf, sr, { fmin: 55, fmax: 500 }), b = detectPitch(buf, sr, { fmin: 55, fmax: 500, near: f });
    if (a.freq && Math.abs(ftoc(a.freq) - ftoc(f)) > 600) free++;
    if (b.freq) { assert.ok(Math.abs(ftoc(b.freq) - ftoc(f)) < 10, `${b.freq}`); held++; }
  }
  assert.ok(free > 0, '这段信号本来就该有八度歧义'); // 确认测试有意义
  assert.ok(held > 5);
});

/** 抖得很厉害的输入：每帧 ±30 音分均匀噪声，5% 的帧是八度错 */
function jittery(n, center, seed) {
  const r = rng(seed);
  return Array.from({ length: n }, (_, i) => {
    const c = typeof center === 'function' ? center(i) : center, x = r.u();
    return x < 0.025 ? c + 1200 : x < 0.05 ? c - 1200 : c + (r.u() * 2 - 1) * 30;
  });
}

test('PitchSmoother：±30 音分的抖动 + 偶尔的八度错 → 稳定后（4 秒）标准差 < 2 音分、帧间跳动 < 1 音分，没有跳八度', () => {
  const sm = new PitchSmoother(), truth = 4012, xs = jittery(400, truth, 11);
  const out = xs.map((c, i) => sm.push(c, i * 40).cents);
  assert.ok(out.slice(10).every(c => Math.abs(c - truth) < 15), '不该跳到别的八度');
  const settled = out.slice(100);
  assert.ok(std(settled) < 2, `std ${std(settled).toFixed(2)}`);
  assert.ok(Math.abs(settled.reduce((a, b) => a + b, 0) / settled.length - truth) < 3);
  const steps = settled.slice(1).map((c, i) => Math.abs(c - settled[i]));
  assert.ok(Math.max(...steps) < 1, `帧间最大跳动 ${Math.max(...steps).toFixed(2)}`); // 显示上看不出抖
  assert.ok(std(xs.filter(c => Math.abs(c - truth) < 100)) > 15); // 输入确实抖：标准差 ~17 音分
});

test('PitchSmoother：真的调了弦（音高阶跃 / 慢慢拧）能很快跟上', () => {
  // 阶跃 +50 音分（输入仍然抖 ±30）：12 帧（0.5 秒）内到 ±10 音分以内，之后继续稳定
  const sm = new PitchSmoother(), xs = jittery(200, i => (i < 100 ? 4500 : 4550), 12);
  const out = xs.map((c, i) => sm.push(c, i * 40).cents);
  const reach = out.slice(100).findIndex(c => Math.abs(c - 4550) < 10);
  assert.ok(reach >= 0 && reach <= 12, `第 ${reach} 帧才跟上`);
  assert.ok(std(out.slice(150)) < 3);
  // 干净的信号慢慢拧（每秒 60 音分）：滞后不超过 10 音分
  const s2 = new PitchSmoother(), ramp = i => 4500 + Math.max(0, i - 25) * 2.4;
  const lag = [];
  for (let i = 0; i < 100; i++) { const c = s2.push(ramp(i) + (i % 2 ? 0.5 : -0.5), i * 40).cents; if (i > 40) lag.push(ramp(i) - c); }
  assert.ok(Math.max(...lag) < 10, `最大滞后 ${Math.max(...lag).toFixed(1)}`);
});

test('PitchSmoother：音衰减了先保留读数，过一会儿才清掉；换到另一个音要连续几帧确认', () => {
  const sm = new PitchSmoother({ holdMs: 1500 });
  for (let i = 0; i < 10; i++) sm.push(4000, i * 40);
  assert.deepEqual([sm.push(null, 500).cents, sm.push(null, 500).held], [4000, true]);
  assert.equal(sm.push(null, 1000).cents, 4000);
  assert.equal(sm.push(null, 2000).cents, null);
  // 新的音：两帧离群不算，第三帧一致才换过去，并且开始新的一段（seg + 1）
  for (let i = 0; i < 10; i++) sm.push(4000, 3000 + i * 40);
  const seg = sm.seg;
  assert.equal(sm.push(4500, 3400).cents, 4000);
  assert.equal(sm.push(4502, 3440).cents, 4000);
  const r = sm.push(4498, 3480);
  assert.ok(Math.abs(r.cents - 4500) < 2 && r.seg === seg + 1);
  // 起音（unlock）之后：新的值两帧对得上就锁定，之前仍显示旧读数
  sm.unlock();
  const before = sm.est;
  assert.deepEqual(sm.push(5000, 3520), { cents: before, held: true, seg: seg + 1 }); // 还是旧的
  assert.deepEqual(sm.push(5001, 3560), { cents: 5000.5, held: false, seg: seg + 2 });
});

test('LevelTracker：噪声底跟着安静的环境走，拨弦时报起音，衰减时不误报', () => {
  const lv = new LevelTracker();
  for (let i = 0; i < 50; i++) lv.update(0.004 + (i % 3) * 0.0005, false, 40); // 环境噪声（风扇）
  assert.ok(lv.gate > 0.009 && lv.gate < 0.02, `gate ${lv.gate}`);
  assert.deepEqual(lv.update(0.005, false, 40), { open: false, onset: false });
  assert.deepEqual(lv.update(0.08, true, 40), { open: true, onset: true });
  for (let i = 1; i < 30; i++) assert.equal(lv.update(0.08 * 0.9 ** i, true, 40).onset, false);
  assert.equal(lv.update(0.2, true, 40).onset, true); // 再拨一次
});

test('TunerEngine：拨 6 弦再拨 5 弦，选弦正确、读数稳定；Drop D 也一样', () => {
  const sr = 48000;
  const y = new Float32Array(sr * 5);
  const a = pluck(mtof(40) * 2 ** (-12 / 1200), sr, 5, { seed: 1, noise: 0.002 }), b = pluck(mtof(45) * 2 ** (8 / 1200), sr, 5, { seed: 2, noise: 0, start: 2.5 });
  for (let i = 0; i < y.length; i++) y[i] = a[i] * (i < sr * 2.5 ? 1 : 0.3) + b[i];
  const out = run(new TunerEngine(targets(STD), sr), y, sr);
  const at = (t0, t1) => out.filter(o => o.t >= t0 * 1000 && o.t < t1 * 1000);
  const first = at(0.4, 2.4), second = at(3.0, 4.8);
  assert.ok(first.every(o => o.live && o.target.s === 6 && Math.abs(o.cents + 12) <= 1), JSON.stringify(first.map(o => [o.target?.s, o.cents])));
  assert.ok(second.every(o => o.live && o.target.s === 5 && Math.abs(o.cents - 8) <= 1), JSON.stringify(second.map(o => [o.target?.s, o.cents])));
  assert.ok(first.every(o => !o.ok) && second.every(o => !o.ok));
  // Drop D：6 弦目标是 D2，弹 D2 低 20 音分
  const d = run(new TunerEngine(targets([64, 59, 55, 50, 45, 38]), 44100), pluck(mtof(38) * 2 ** (-20 / 1200), 44100, 2.5, { seed: 9 }), 44100);
  const ds = d.filter(o => o.t > 400);
  assert.ok(ds.length > 20 && ds.every(o => o.target.s === 6 && Math.abs(o.cents + 20) <= 1), JSON.stringify(ds.map(o => [o.target?.s, o.cents])));
});

/**
 * 更像真吉他：泛音不和谐（B = 1e-4，高次泛音偏高）、起音时音高偏高 12 音分再在 80ms 里回落、
 * 另一根空弦被带响（−20 dB）、50 / 150 Hz 交流哼声和噪声；拨弦前先有 1 秒环境声（打开麦克风到拨弦之间）。
 */
function guitar(m, other, sr, { cents = 7, seed = 1 } = {}) {
  const r = rng(seed), y = new Float32Array(sr * 4), at = sr;
  const string = (f, level, glide) => {
    for (let k = 1; k <= 24; k++) {
      const fk = k * f * Math.sqrt(1 + 1e-4 * k * k), a = [0.15, 1, 0.8, 0.5][k - 1] ?? 0.7 / k;
      if (fk > sr / 2.2) break;
      let ph = r.u() * 2 * Math.PI;
      for (let i = at; i < y.length; i++) {
        const t = (i - at) / sr;
        ph += (2 * Math.PI * fk * 2 ** ((glide * Math.exp(-t / 0.08)) / 1200)) / sr;
        y[i] += level * a * Math.exp(-t / (1.6 / Math.sqrt(k))) * Math.sin(ph) * Math.min(1, (t * sr) / 48);
      }
    }
  };
  string(mtof(m) * 2 ** (cents / 1200), 0.1, 12);
  string(mtof(other), 0.01, 0);
  for (let i = 0; i < y.length; i++) y[i] += 0.003 * r.g() + 0.004 * (Math.sin((2 * Math.PI * 50 * i) / sr) + 0.5 * Math.sin((2 * Math.PI * 150 * i) / sr));
  return y;
}

test('TunerEngine：更像真吉他的信号（不和谐泛音、起音偏高、别的弦共鸣、交流哼声）读数也稳、选弦对', () => {
  const sr = 48000;
  for (const [m, other, s] of [[40, 45, 6], [45, 40, 5], [50, 45, 4], [59, 40, 2], [64, 40, 1]]) {
    const out = run(new TunerEngine(targets(STD), sr), guitar(m, other, sr, { seed: m }), sr);
    assert.ok(out.filter(o => o.t < 1000).every(o => !o.live), '拨弦之前没有读数（哼声、噪声不算）');
    const first = out.find(o => o.live);
    assert.ok(first && first.t < 1200, `拨弦后 ${first?.t - 1000}ms 才出读数`);
    const live = out.filter(o => o.live && o.t > 1400); // 起音后 0.4 秒起
    assert.ok(live.length > 40 && live.every(o => o.target.s === s), `${m}：${JSON.stringify(live.map(o => o.target.s))}`);
    assert.ok(live.every(o => o.cents >= 3 && o.cents <= 11), `${m}：${JSON.stringify(live.map(o => o.cents))}`); // 真值 +7（不和谐让它略偏高）
    const steps = live.slice(1).map((o, i) => Math.abs(o.cents - live[i].cents));
    assert.ok(Math.max(...steps) <= 1, `${m}：帧间跳动 ${Math.max(...steps)}`);
  }
});

test('TunerEngine：“准了”有滞回（±4 以内进，超过 ±7 才出），音衰减后保留读数', () => {
  const sr = 48000, f0 = mtof(50);
  // 一直响的 D3，音高：+3 → +6（仍然算准）→ +10（不准了）
  const cents = t => (t < 1.5 ? 3 : t < 3 ? 6 : 10);
  const y = pluck(t => f0 * 2 ** (cents(t) / 1200), sr, 4.5, { amps: [0.5, 1, 0.6], seed: 4, noise: 0 });
  // 不让它衰减完：把包络补回来，模拟一直在响（噪声在补完之后再加）
  const rn = rng(4);
  for (let i = 0; i < y.length; i++) y[i] = y[i] * Math.exp(i / sr / 1.6) + 0.003 * rn.g();
  const out = run(new TunerEngine(targets(STD), sr), y, sr);
  const at = (t0, t1) => out.filter(o => o.t >= t0 * 1000 && o.t < t1 * 1000), dump = JSON.stringify(out.map(o => [o.t | 0, o.ok, o.cents]));
  assert.ok(at(0.6, 1.5).every(o => o.ok && o.cents === 3), dump);
  assert.ok(at(2.0, 3.0).every(o => o.ok && o.cents === 6), dump);
  assert.ok(at(3.5, 4.5).every(o => !o.ok && o.cents === 10), dump);
  // 拨一下就不管：读数一直到衰减结束都稳，之后保留一会儿（held）再清掉
  const z = new Float32Array(sr * 6);
  z.set(pluck(f0, sr, 2.5, { seed: 6, noise: 0 }));
  const r = rng(8);
  for (let i = 0; i < z.length; i++) z[i] += 0.001 * r.g();
  const o2 = run(new TunerEngine(targets(STD), sr), z, sr), live = o2.filter(o => o.live);
  assert.ok(live.every(o => Math.abs(o.cents) <= 1 && o.target.s === 4));
  assert.ok(o2.some(o => o.held) && !o2.at(-1).live);
});

test('ftoc / ctof 互逆', () => {
  assert.equal(ftoc(440), 6900);
  assert.ok(Math.abs(ctof(ftoc(82.4069)) - 82.4069) < 1e-9);
});

test('TunerEngine：没有目标音时直接报错，不会算出 fftSize = 2 这样的缓冲长度', () => {
  assert.throws(() => new TunerEngine([], 48000), RangeError);
  for (const sr of [44100, 48000]) {
    const n = new TunerEngine(targets(STD), sr).bufferSize;
    assert.ok(n >= 32 && n <= 32768 && (n & (n - 1)) === 0, String(n)); // AnalyserNode.fftSize 的范围
  }
});
