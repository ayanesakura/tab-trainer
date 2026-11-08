// 练习页的纯函数：曲子数据 → 播放和显示要用的结构，段落、换段、练习记录文案等。
// 不碰 DOM、不读 store，Node 测试直接导入（tests/js/trainer.test.mjs）。

import { toAlphaTex, durToTicks, TICKS } from '../score.js';
import { STRINGS, STANDARD, TECH_NAME, WARN_CODES, parseDur } from '../vocab.js';

export const NN = ['C', 'C♯', 'D', 'D♯', 'E', 'F', 'F♯', 'G', 'G♯', 'A', 'A♯', 'B'];
export const mtof = m => 440 * Math.pow(2, (m - 69) / 12);
export const mname = m => NN[((m % 12) + 12) % 12] + (Math.floor(m / 12) - 1);
/** 音名的显示写法：F#3 → F♯3（输入框里两种都认，见 tuningInput） */
export const showNote = s => String(s ?? '').replace(/#/g, '♯');
export const tuningInput = s => String(s ?? '').trim().replace(/♯/g, '#').replace(/♭/g, 'b');
export const T3 = 1 / 3, S6 = 1 / 6; // 三连音、六连音的时值（拍）
const NOTE_PC = { C: 0, D: 2, E: 4, F: 5, G: 7, A: 9, B: 11 };
/** 音名 → MIDI（E4 → 64、F#3 / F♯3 → 54），读不懂返回 NaN */
export function nameToMidi(name) {
  const m = /^([A-G])([#♯b♭]?)(-?\d)$/.exec(String(name ?? '').trim());
  if (!m) return NaN;
  return NOTE_PC[m[1]] + ({ '#': 1, '♯': 1, b: -1, '♭': -1 }[m[2]] || 0) + (+m[3] + 1) * 12;
}
export const STD_MIDI = STANDARD.map(nameToMidi); // 标准调弦，1 弦 → 6 弦
const HARM = { 12: 12, 24: 24, 7: 19, 19: 19, 5: 24, 4: 28, 9: 28, 16: 28, 3: 31, 2: 36 }; // 自然泛音品位 → 比空弦高几个半音

/** GP / MusicXML 导入的曲子（小节来自原文件，谱面直接用原文件渲染） */
export const isScore = s => !!(s && s.is_score); // GP / MusicXML：后端给的（SongView.is_score），前端不再按来源推断
/** 小节要不要核对（后端算好的） */
export const needsReview = b => !!b.needs_review;
/** 警告 {code, msg} 的文字 */
export const warnText = w => (w && (w.msg || WARN_CODES[w.code])) || '';

/** 原谱的一行是一段：[{sys, a, z}]（小节下标） */
export function sectionsOf(bars) {
  const out = [];
  bars.forEach((b, i) => { const g = out[out.length - 1]; if (g && g.sys === b.system) g.z = i; else out.push({ sys: b.system, a: i, z: i }); });
  return out;
}

/**
 * 曲子（后端的 SongView）→ 练习页要用的结构。纯函数，结果放在 store 的 view 里。
 * bars：每小节的事件 {t, d, notes: [{s, f, tech: Set, grace, bend}], tech: Set, bar, idx}；all：全曲展平；
 * open / openName / capo：空弦音高和音名；barLens：每小节几拍（四分音符）；regions：原谱上的位置；
 * beatMap：每小节每个事件起拍的 tick；source：谱面从哪来（GP 原文件的音轨，或我们生成的 alphaTex）。
 */
export function deriveView(song) {
  const meta = song.meta || {};
  const tm = meta.tuning_midi || STD_MIDI;
  const open = {}, openName = {};
  tm.forEach((m, i) => { open[i + 1] = m; openName[i + 1] = NN[((m % 12) + 12) % 12]; });
  const capo = meta.capo || 0;
  const ts = meta.time_signature || [4, 4];
  const barLens = song.bars.map(b => { const t = b.time_signature || ts; return t[0] * 4 / t[1]; });
  const all = [];
  const bars = song.bars.map((b, bi) => {
    let t = 0;
    const evs = b.events && b.events.length ? b.events : [{ dur: String(barLens[bi]), notes: [] }]; // 空小节按整小节休止显示
    return evs.map(e => {
      const notes = (e.notes || [])
        .filter(n => n.string >= 1 && n.string <= STRINGS && Number.isFinite(n.fret)) // 非法弦号会让音高变成 NaN，播放卡死
        .map(n => ({ s: n.string, f: n.fret, tech: new Set(n.tech || []), grace: n.grace >= 0 ? n.grace : null, bend: Number.isFinite(n.bend) ? n.bend : 0 }));
      const tech = new Set(); notes.forEach(n => n.tech.forEach(k => tech.add(k)));
      const d = parseDur(e.dur), r = { t, d, notes, tech, bar: bi, idx: 0 };
      t += d;
      return r;
    }).filter(e => e.t < barLens[bi] - 1e-9); // 时值超出小节长度的部分不播放（会在提示里标出）
  });
  bars.forEach(evs => evs.forEach(e => { e.idx = all.length; all.push(e); }));
  let beatMap = [], source = null;
  if (isScore(song)) {
    beatMap = song.bars.map(b => { let t = 0; return (b.events || []).map(e => { const o = t; t += durToTicks(e.dur); return o; }); });
    source = { kind: 'score', id: song.id, track: song.track || 0 };
  } else if (song.bars.length) {
    const known = meta.tempo || 0; // 谱上没标速度时不显示速度记号（否则总是写着 ♩ = 60）
    const r = toAlphaTex(song, { tempo: known || 60 });
    beatMap = r.beatMap;
    source = { kind: 'tex', tex: r.tex, showTempo: !!known };
  }
  return {
    bars, all, open, openName, capo, barLens, beatMap, source,
    ids: song.bars.map(b => b.id),
    tips: song.bars.map(b => b.tip || ''),
    regions: song.bars.map(b => (b.region ? [b.page, ...b.region] : null)),
    pages: (song.pages || []).map(p => `/files/${song.id}/${p.file}`),
    sections: sectionsOf(song.bars),
    review: song.bars.map(needsReview),
  };
}

/** 一个音的 MIDI 音高（含变调夹；泛音按泛音点算） */
export function noteMidi(n, v) {
  if (n.tech.has('nh')) return v.open[n.s] + v.capo + (HARM[n.f] ?? 12);
  return v.open[n.s] + v.capo + n.f + (n.tech.has('ah') ? 12 : 0);
}

/**
 * 上一段 / 下一段（←→）。{a, b} 是当前循环范围，n 小节数，shown 正在看的小节，secs 段落。
 * 循环全曲时从正在看的那一段开始；正好是某一段时跳到相邻的段；自己选的范围按范围长度平移。
 * 返回新的 {a, b}，到头了返回 {msg}。
 */
export function stepSection({ a, b, n, shown, secs }, dir) {
  if (!n) return null;
  const all = a === 0 && b === n - 1;
  const k = secs.findIndex(g => g.a === a && g.z === b);
  if (all && secs.length > 1) {
    const here = Math.max(0, secs.findIndex(g => g.a <= shown && shown <= g.z));
    const g = secs[dir > 0 ? here : Math.max(0, here - 1)];
    return { a: g.a, b: g.z };
  }
  if (k >= 0) {
    const g = secs[Math.max(0, Math.min(secs.length - 1, k + dir))];
    return g !== secs[k] ? { a: g.a, b: g.z } : { msg: dir > 0 ? '已经是最后一段了' : '已经是第一段了' };
  }
  const len = b - a + 1, na = Math.max(0, Math.min(n - len, a + dir * len));
  if (na === a) return { msg: dir > 0 ? '已经到曲尾了' : '已经到开头了' }; // 不能平移时别从段首重新播放
  return { a: na, b: na + len - 1 };
}

/** 小节里的第几拍（播放状态栏用）：按拍号的分母数，6/8 拍一小节 6 拍（八分音符一拍），不是按四分音符数成 3 拍。tick 从小节开头算 */
export const beatNumber = (tick, den = 4) => Math.floor(tick / (TICKS * 4 / (den || 4)) + 1e-6) + 1;

export const rangeLabel = (a, b, n) => (!n ? '—' : a === 0 && b === n - 1 && n > 1 ? '全曲' : a === b ? `第 ${a + 1} 小节` : `第 ${a + 1}–${b + 1} 小节`);
export const loopLabel = (a, b, n) => (!n ? '—' : a === b ? `第 ${a + 1} 小节` : `第 ${a + 1}–${b + 1} 小节`);

const mins = s => (s < 60 ? '不到 1 分钟' : `${Math.round(s / 60)} 分钟`);
/** 小节详情里的练习记录一行 */
export function practiceText(p) {
  if (!p) return '还没练过这一小节';
  return p.loops
    ? `循环 ${p.loops} 遍 · 累计 ${mins(p.seconds)}${p.best_bpm ? ` · 最快 ${p.best_bpm} BPM` : ''} · 上次 ${p.last}`
    : `听过 ${mins(p.seconds)} · 上次 ${p.last}`;
}

/** 上次练到哪：{a, z, label, bpm, at}，找不到返回 null */
export function resumeInfo(song) {
  const last = (song.practice || {}).last;
  if (!last || !song.bars.length) return null;
  const pos = (id, nn) => { const i = song.bars.findIndex(b => b.id === id); return i >= 0 ? i : nn > 0 && nn <= song.bars.length ? nn - 1 : -1; };
  let a = pos(last.a, last.a_n), z = pos(last.b, last.b_n);
  if (a < 0 || z < 0) return null;
  if (a > z) [a, z] = [z, a]; // 调整过页序以后原来的首尾可能对调了
  return { a, z, label: rangeLabel(a, z, song.bars.length), bpm: last.bpm, at: last.at };
}

/** 小节的技巧标签（名字取词表） */
export function barTags(events) {
  const tags = new Set();
  for (const e of events) {
    e.tech.forEach(k => TECH_NAME[k] && tags.add(TECH_NAME[k]));
    if (e.notes.some(n => n.grace != null)) tags.add('倚音');
  }
  if (events.some(e => Math.abs(e.d - T3) < 1e-6)) tags.add('三连音');
  if (events.some(e => Math.abs(e.d - S6) < 1e-6)) tags.add('六连音');
  return [...tags];
}

/** 练习建议里的“第 3–5 小节” → 小节下标 {a, z}（超出范围的截掉；读不出来返回 null） */
export function planRange(bars, n) {
  const s = String(bars ?? '').trim();
  const m = /^(\d+)\s*[-–~]\s*(\d+)$/.exec(s) || /^(\d+)$/.exec(s);
  if (!m) return null;
  const a = +m[1] - 1, z = +(m[2] || m[1]) - 1;
  if (a < 0 || a >= n) return null;
  return { a, z: Math.max(a, Math.min(z, n - 1)) };
}

/** 曲名下的副标题：艺人 · 编配 · 调弦 · 变调夹 · 拍号 · 小节数 */
export function songSubtitle(song, v) {
  const m = song.meta || {};
  const std = v && STD_MIDI.every((m, i) => v.open[i + 1] === m);
  const tuning = v ? (std ? '标准调弦' : '调弦 ' + [6, 5, 4, 3, 2, 1].map(s => v.openName[s]).join(' ')) : '';
  const capo = v && v.capo ? `变调夹 ${v.capo} 品` : '';
  const ts = m.time_signature ? m.time_signature.join('/') + ' 拍' + (song.bars.some(b => b.time_signature) ? '（有变拍号）' : '') : '';
  return [m.artist, m.arranger && m.arranger + ' 编配', song.bars.length && tuning, song.bars.length && capo, song.bars.length && ts, song.bars.length ? `${song.bars.length} 小节` : '']
    .filter(Boolean).join(' · ');
}

/** 每个小节有没有提示、练习建议在不在 / 过没过时 → 讲解按钮怎么显示 */
export function explainState(song) {
  const missing = song.bars.filter(b => !(b.tip || '').trim()).length;
  const plan = song.plan, planStale = !!(plan && plan.stale);
  const fresh = isScore(song) && missing === song.bars.length;
  return { missing, noPlan: !plan, planStale, fresh };
}
