// 敲节奏的纯计算：从小节数据算出要敲的点和口令，给每一下敲击找对应的音，给一遍打分、找常错的位置。
// 不碰 DOM 和音频，练习页的 js/trainer/rhythm-drill.js 导入使用；单元测试见 tests/js/rhythm.test.mjs。
// 时间单位：位置用四分音符数（q），时间用秒。

export const GOOD = 0.04; // |偏差| ≤ 40 ms 算“准”
export const MAX_WIN = 0.2; // 离最近的音超过 200 ms 的敲击算多敲
export const MIN_WIN = 0.05;
const EPS = 1e-6;

// 拍内位置的口令：1 e & a（十六分）、1 trip let（三连音）；别的位置用 ·
export function syllable(t, unit) {
  const x = t / unit, beat = Math.floor(x + EPS), f = x - beat;
  const near = v => Math.abs(f - v) < 1e-3;
  if (near(0)) return String(beat + 1);
  if (near(0.5)) return '&';
  if (near(0.25)) return 'e';
  if (near(0.75)) return 'a';
  if (near(1 / 3)) return 'trip';
  if (near(2 / 3)) return 'let';
  return '·';
}

// bars：循环段里的每个小节 { bi, len（四分音符数）, ts: [分子, 分母], events: [{ t, d, notes }] }
// 返回 { Q: 一遍的长度, bars: [{ bi, q0, len, beats: [{ q, accent }] }], onsets: [{ q, d, bi, beat, label, ev }] }
// 要敲的点 = 有音的事件（和弦算一下）；空事件是休止或延音，不敲。倚音跟着主音，不单独算。
export function buildProgram(bars) {
  let q0 = 0;
  const outBars = [], onsets = [];
  for (const b of bars) {
    const unit = 4 / b.ts[1]; // 一拍是几个四分音符（6/8 拍按八分音符打拍子）
    const beats = [];
    for (let q = 0; q < b.len - EPS; q += unit) beats.push({ q: q0 + q, accent: q === 0 });
    outBars.push({ bi: b.bi, q0, len: b.len, beats });
    for (const e of b.events) {
      if (!e.notes.length || e.t >= b.len - EPS) continue;
      const q = q0 + e.t, last = onsets[onsets.length - 1];
      if (last && Math.abs(last.q - q) < EPS) continue; // 同一时刻只敲一下
      onsets.push({ q, d: Math.max(0, Math.min(e.d, b.len - e.t)), bi: b.bi, beat: Math.floor(e.t / unit + EPS) + 1, label: syllable(e.t, unit), ev: e });
    }
    q0 += b.len;
  }
  return { Q: q0, bars: outBars, onsets };
}

// 每个音的判定窗口（秒）：到前后相邻音距离的一半，限制在 [MIN_WIN, maxWin]。循环时最后一个音的“后一个”是下一遍的第一个音。
export function windows(onsets, Q, spq, maxWin = MAX_WIN) {
  const n = onsets.length;
  return onsets.map((o, i) => {
    const prev = i > 0 ? o.q - onsets[i - 1].q : o.q + Q - onsets[n - 1].q;
    const next = i < n - 1 ? onsets[i + 1].q - o.q : onsets[0].q + Q - o.q;
    const gap = Math.min(prev, next) * spq;
    return Math.max(MIN_WIN, Math.min(maxWin, gap / 2));
  });
}

// 一下敲击对应哪个音：还没敲过、在判定窗口内、离得最近的那个。notes: [{ T, win, err, missed }]；找不到返回 -1
export function matchTap(notes, t) {
  let best = -1, bd = Infinity;
  notes.forEach((nt, i) => {
    if (nt.err != null || nt.missed) return;
    const d = Math.abs(t - nt.T);
    if (d <= nt.win && d < bd) { bd = d; best = i; }
  });
  return best;
}

export const grade = (err, win) => (Math.abs(err) <= Math.min(GOOD, win) + EPS ? 'good' : 'ok');

// 一遍的成绩。notes 里 err 为 null 的算漏；extra 是多敲的次数。
// 得分 = (准 + 偏 × 0.5) / (音数 + 多敲)；平均偏差和波动（标准差）只算敲到了的音，单位毫秒，正数是晚。
export function summarize(notes, extra = 0) {
  const n = notes.length, errs = notes.filter(x => x.err != null).map(x => x.err);
  const good = notes.filter(x => x.err != null && grade(x.err, x.win) === 'good').length;
  const ok = errs.length - good, miss = n - errs.length;
  const mean = errs.length ? errs.reduce((a, b) => a + b, 0) / errs.length : 0;
  const sd = errs.length > 1 ? Math.sqrt(errs.reduce((a, b) => a + (b - mean) ** 2, 0) / (errs.length - 1)) : 0;
  const score = n + extra ? Math.round((100 * (good + ok * 0.5)) / (n + extra)) : 0;
  return { n, good, ok, miss, extra, mean: Math.round(mean * 1000), sd: Math.round(sd * 1000), score };
}

// 一句话诊断：先说最严重的问题
export function diagnose(s) {
  if (!s.n) return { tone: 'muted', text: '' };
  if (s.miss >= Math.max(2, s.n * 0.3)) return { tone: 'bad', text: `漏了 ${s.miss} 个音：先放慢，或者切到“跟着示范”多听几遍再敲。` };
  const hits = s.good + s.ok;
  if (hits >= 3 && s.mean >= 25) return { tone: 'warn', text: `整体偏晚 ${s.mean} ms：别等声音出来再敲，心里提前数着拍子。` };
  if (hits >= 3 && s.mean <= -25) return { tone: 'warn', text: `整体偏早 ${-s.mean} ms（抢拍）：放松，等拍子落下来再敲。` };
  if (hits >= 3 && s.sd >= 35) return { tone: 'warn', text: `快慢不稳（前后差 ±${s.sd} ms）：先降速，敲稳了再提速。` };
  if (s.extra) return { tone: 'warn', text: `多敲了 ${s.extra} 下：休止和延长的地方别敲。` };
  if (s.score >= 90) return { tone: 'good', text: '很稳，可以提速或者撤掉示范了。' };
  return { tone: 'muted', text: '继续保持，注意橙色的音。' };
}

// 常错的位置：至少练了 2 遍以后，漏掉一半以上、或者平均偏差超过 50 ms 的音（最多 3 个，最严重的在前）。
// loops: 每遍每个音的 err（null 是漏）组成的数组，长度都和 onsets 一样
export function weakSpots(onsets, loops) {
  if (loops.length < 2) return [];
  const out = [];
  onsets.forEach((o, i) => {
    const errs = loops.map(l => l[i]), hit = errs.filter(e => e != null);
    const missRate = 1 - hit.length / errs.length;
    const mean = hit.length ? hit.reduce((a, b) => a + b, 0) / hit.length : 0;
    if (missRate >= 0.5) out.push({ i, bi: o.bi, kind: 'miss', sev: 1 + missRate, text: '常漏' });
    else if (hit.length >= 2 && Math.abs(mean) >= 0.05) {
      const ms = Math.round(Math.abs(mean) * 1000);
      out.push({ i, bi: o.bi, kind: mean > 0 ? 'late' : 'early', sev: Math.abs(mean) * 10, text: `总是偏${mean > 0 ? '晚' : '早'} ${ms} ms` });
    }
  });
  return out.sort((a, b) => b.sev - a.sev).slice(0, 3);
}

// “第 3 小节 · 第 2 拍的 &”
export function placeName(o) {
  // 口令以外的位置（三十二分音符、五连音等，口令是 ·）没有名字可叫，只说在第几拍里
  const beat = /^\d+$/.test(o.label) ? `第 ${o.beat} 拍` : o.label === '·' ? `第 ${o.beat} 拍中间` : `第 ${o.beat} 拍的 ${o.label}`;
  return `第 ${o.bi + 1} 小节 · ${beat}`;
}
