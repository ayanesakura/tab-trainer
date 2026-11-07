// 我们的小节数据 ⇄ alphaTab 谱面 的转换。浏览器和 Node 测试共用（不依赖 DOM）。
//
// 我们的数据：bar.events = [{ dur: "1/2", notes: [{ string, fret, tech: [...], grace, bend }] }]
//   - dur：到下一个起拍的时长，单位是四分音符；string：1 = 最高音弦（最细）
//   - 击弦 h / 勾弦 p / 滑音 sl 标在“目标音”上
// alphaTab：alphaTex 里 string 1 也是最高音弦，但内部模型里 note.string 1 = 最低音弦；
//   击弦、滑音标在“起始音”上。转换时要把这些技巧挪到同一根弦的前一个音。

export const TICKS = 960; // 每个四分音符的 MIDI tick 数

// deriveSong 的输出格式 / 转换规则的版本。改了 deriveSong（音符、技巧、拍号的转换）就加 1：
// 已经导入的曲子下次打开时会重新转换（alphaTab 升级也会）。
// 2：每小节带自己的拍号；多声部同一根弦同时起拍的音只保留一个
// 3：按谱面位置分起拍（拍上倚音不再在小节开头多出假休止）
// 4：闷音的品格记 0；自然泛音的品格记谱上标的泛音点（<12> → 12）
// 5：中文 GP3–5 文件按 GBK 重新导入（曲名、艺人不再是乱码）
export const DERIVE_VERSION = 5;

// 可以直接写成一个拍的时值：[tick 数, alphaTex 时值, 附点数, 是否三连音]
const UNITS = [
  [3840, 1, 0, 0], [2880, 2, 1, 0], [1920, 2, 0, 0], [1440, 4, 1, 0], [960, 4, 0, 0],
  [720, 8, 1, 0], [640, 4, 0, 1], [480, 8, 0, 0], [360, 16, 1, 0], [320, 8, 0, 1],
  [240, 16, 0, 0], [180, 32, 1, 0], [160, 16, 0, 1], [120, 32, 0, 0], [80, 32, 0, 1],
  [60, 64, 0, 0], [40, 64, 0, 1],
];

export function durToTicks(dur) {
  const [a, b] = String(dur).split('/');
  return Math.round((b === undefined ? +a : +a / +b) * TICKS);
}

function gcd(a, b) { while (b) [a, b] = [b, a % b]; return a; }
export function ticksToDur(t) {
  const g = gcd(t, TICKS);
  return TICKS / g === 1 ? String(t / g) : `${t / g}/${TICKS / g}`;
}

export const GRID = 20; // 所有可写的时值都是 20 tick 的整数倍（最短是 64 分三连音 40 tick）

/**
 * 把一段时长精确拆成能写出来的拍（后面的段用连音线接上）。用动态规划找段数最少、尽量不用三连音的拆法；
 * 贪心会出错（140 tick 先取 120 就剩下拼不出的 20，其实 80 + 60 正好）。拼不出来时返回 null。
 */
export function splitTicks(t) {
  const exact = UNITS.find(u => u[0] === t);
  if (exact) return [exact];
  if (t <= 0 || t % GRID) return null;
  const n = t / GRID, cost = new Array(n + 1).fill(Infinity), pick = new Array(n + 1).fill(-1);
  cost[0] = 0;
  for (let i = 1; i <= n; i++) {
    UNITS.forEach((u, k) => {
      const step = u[0] / GRID;
      if (step <= i && cost[i - step] + 10 + u[3] < cost[i]) { cost[i] = cost[i - step] + 10 + u[3]; pick[i] = k; }
    });
  }
  if (!Number.isFinite(cost[n])) return null;
  const out = [];
  for (let i = n; i > 0; i -= UNITS[pick[i]][0] / GRID) out.push(UNITS[pick[i]]);
  return out.sort((x, y) => y[0] - x[0]);
}

/** 拼不出精确时值时的近似：取不超过它的最长可写拍，至少一个最短单位（误差由调用方补到后面的音上）。 */
function approxTicks(t) {
  for (let r = Math.floor(t / GRID) * GRID; r >= 40; r -= GRID) {
    const s = splitTicks(r);
    if (s) return s;
  }
  return [UNITS[UNITS.length - 1]];
}

const quarters = dur => { const [a, b] = String(dur).split('/'); return b === undefined ? +a : +a / +b; };

const esc = s => String(s ?? '').replace(/["\\]/g, ' ');

// 反复 / 跳转记号：名字和 alphaTex 的 \jump 一致，下标就是 alphaTab 的 Direction 枚举值
const DIRECTION_NAMES = ['fine', 'segno', 'segnosegno', 'coda', 'doublecoda', 'dacapo', 'dacapoalcoda', 'dacapoaldoublecoda',
  'dacapoalfine', 'dalsegno', 'dalsegnoalcoda', 'dalsegnoaldoublecoda', 'dalsegnoalfine', 'dalsegnosegno',
  'dalsegnosegnoalcoda', 'dalsegnosegnoaldoublecoda', 'dalsegnosegnoalfine', 'dacoda', 'dadoublecoda'];

/** 小节开头的 alphaTex 元数据：拍号（和上一小节不同时）、反复开始 / 结束（一共弹几遍）、跳房子、跳转。 */
function barMetaTex(bar, ts, prevTs) {
  const m = [];
  if (prevTs && (ts[0] !== prevTs[0] || ts[1] !== prevTs[1])) m.push(`\\ts (${ts[0]} ${ts[1]})`);
  if (bar.repeat_start) m.push('\\ro');
  if (bar.repeat_end > 0) m.push(`\\rc ${Math.max(2, bar.repeat_end)}`);
  if (bar.endings && bar.endings.length) m.push(`\\ae (${bar.endings.join(' ')})`);
  for (const d of bar.directions || []) if (DIRECTION_NAMES.includes(d)) m.push(`\\jump ${d}`);
  return m.length ? m.join(' ') + ' ' : '';
}

/**
 * 我们的曲子 → alphaTex 文本。返回 { tex, beatMap }：
 * beatMap[barIndex] = 每个事件起拍的 tick（在本小节内），用来把播放到的拍对应回我们的事件。
 */
export function toAlphaTex(song, { tempo = 60, staves = 'score tabs', program = 25 } = {}) {
  const meta = song.meta || {};
  const tuning = (meta.tuning || ['E4', 'B3', 'G3', 'D3', 'A2', 'E2']).join(' ');
  const ts = meta.time_signature || [4, 4];
  const tsOf = bar => bar.time_signature || ts; // 小节自己的拍号（中途变拍号）优先
  const barTicks = bar => { const t = tsOf(bar); return Math.round(t[0] * 4 / t[1] * TICKS); };

  // 第一遍：把每个音转成可挂技巧的对象，并把 h/p/sl 挪到同一根弦的前一个音（或倚音）上
  const lastOnString = {};
  const bars = song.bars.map(bar => {
    const evs = bar.events && bar.events.length ? bar.events : [{ dur: ticksToDur(barTicks(bar)), notes: [] }];
    return evs.map(e => {
      const notes = (e.notes || []).filter(n => n.string >= 1 && n.string <= 6).map(n => {
        const tech = new Set(n.tech || []);
        const fx = new Set();
        if (tech.has('si')) fx.add('sib');
        if (tech.has('nh')) fx.add('nh');
        if (tech.has('ah')) fx.add('ah 12');
        if (tech.has('pm')) fx.add('pm');
        if (tech.has('x')) fx.add('x');
        if (tech.has('vib')) fx.add('v');
        if (n.bend > 0) fx.add(`b (0 ${Math.round(n.bend * 2)})`);
        let grace = null;
        if (n.grace != null && n.grace >= 0) grace = { string: n.string, fret: n.grace, fx: new Set() };
        const origin = grace || lastOnString[n.string];
        if (origin && (tech.has('h') || tech.has('p'))) origin.fx.add('h');
        if (origin && tech.has('sl')) origin.fx.add('sl');
        const note = { string: n.string, fret: n.fret, fx };
        lastOnString[n.string] = note;
        return { note, grace };
      });
      return { ticks: durToTicks(e.dur), q: quarters(e.dur), notes, roll: notes.length > 1 && (e.notes || []).some(n => (n.tech || []).includes('roll')) };
    });
  });

  // 第二遍：输出文本
  const noteTex = (n, extra) => {
    const fx = [...n.fx, ...(extra ? [extra] : [])];
    return `${n.fret}.${n.string}${fx.length ? `{${fx.join(' ')}}` : ''}`;
  };
  const beatTex = (notesTex, unit, beatFx) => {
    const [, d, dots, tri] = unit;
    const fx = [...beatFx];
    if (dots) fx.push('d');
    if (tri) fx.push('tu 3');
    const body = notesTex.length === 0 ? 'r' : notesTex.length === 1 ? notesTex[0] : `(${notesTex.join(' ')})`;
    return `${body}.${d}${fx.length ? `{${fx.join(' ')}}` : ''}`;
  };

  const beatMap = [];
  const barTexts = bars.map((evs, bi) => {
    const parts = [];
    const onsets = [];
    // 起拍位置按累计时间对齐到 20 tick 的网格（1/5、1/7 拍这类写不出来的时值也不会在小节里越积越多），
    // 每个音写到下一个音的网格位置为止；实在拼不出时近似，误差留给下一个音
    const grid = x => Math.round(x * TICKS / GRID) * GRID;
    let cum = 0;
    const targets = evs.map(ev => { const o = cum; cum += ev.q; return grid(o); });
    const end = grid(cum);
    let t = 0;
    evs.forEach((ev, k) => {
      onsets.push(t);
      const want = (k + 1 < evs.length ? targets[k + 1] : end) - t;
      const units = splitTicks(want) || approxTicks(want);
      t += units.reduce((a, u) => a + u[0], 0);
      const graces = ev.notes.filter(x => x.grace).map(x => x.grace);
      if (graces.length) parts.push(beatTex(graces.map(g => noteTex(g)), [0, 16, 0, 0], ['gr']));
      units.forEach((u, i) => {
        // 第一段带上音符和技巧；后面的段用连音线（{t}）延续，不重新拨弦
        const notes = i === 0 ? ev.notes.map(x => noteTex(x.note)) : ev.notes.map(x => `${x.note.fret}.${x.note.string}{t}`);
        parts.push(beatTex(notes, u, i === 0 && ev.roll ? ['au'] : []));
      });
    });
    beatMap.push(onsets);
    return barMetaTex(song.bars[bi], tsOf(song.bars[bi]), bi ? tsOf(song.bars[bi - 1]) : ts) + parts.join(' ');
  });

  const title = meta.title || song.title || '';
  const head = [
    title && `\\title "${esc(title)}"`,
    meta.artist && `\\artist "${esc(meta.artist)}"`,
    `\\tempo ${tempo}`,
    '.',
    `\\track "Guitar" { instrument ${program} }`,
    `\\staff {${staves}}`,
    `\\tuning (${tuning})`,
    meta.capo ? `\\capo ${meta.capo}` : '',
    `\\ts (${ts[0]} ${ts[1]})`,
  ].filter(Boolean).join('\n');
  return { tex: `${head}\n${barTexts.join(' |\n')}`, beatMap };
}

/** 播放到的拍 → 我们这个小节里的第几个事件（起拍不晚于这个拍的最后一个事件）。 */
export function eventIndexAt(onsets, tick) {
  let k = 0;
  for (let i = 0; i < onsets.length; i++) if (onsets[i] <= tick + 1) k = i;
  return k;
}

/**
 * alphaTab 谱面（GTP / MusicXML 导入）→ 我们的数据。多声部合并成按起拍排列的事件；
 * 连音线延续的音不算新起拍；倚音合并到后面那个音的 grace 字段。
 */
export function deriveSong(score, trackIndex = 0) {
  const track = score.tracks[trackIndex];
  const staff = track.staves[0];
  const nStr = staff.tuning.length;
  if (nStr !== 6) throw new Error(`这个音轨是 ${nStr} 弦的，目前只支持六弦吉他`);
  const NN = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];
  const name = m => NN[m % 12] + (Math.floor(m / 12) - 1);
  const mb0 = score.masterBars[0];
  const meta = {
    title: score.title || '',
    artist: score.artist || '',
    arranger: score.tab || '',
    tuning: staff.tuning.map(name),
    capo: staff.capo || 0,
    time_signature: [mb0.timeSignatureNumerator, mb0.timeSignatureDenominator],
    tempo: Math.round(score.tempo || 0),
    notes: '',
  };
  const ours = s => nStr + 1 - s; // 模型里 1 = 最低音弦，我们 1 = 最高音弦
  // 我们的品格 = 谱上写的数字：闷音画成 x，GP 文件里却可能留着按弦时的品格，记 0；
  // 自然泛音 GP 里品格常记 0、泛音点另存（谱上画 <12>），取泛音点（小数的四舍五入）
  // （倚音只存一个品格、没有技巧，闷音倚音记 0 会变成空弦倚音，所以倚音不走这里）
  const fretOf = n => {
    if (n.isDead) return 0;
    if (n.harmonicType === 1 && n.harmonicValue > 0) return Math.round(n.harmonicValue);
    return n.fret;
  };

  const bars = staff.bars.map((bar, bi) => {
    const length = score.masterBars[bi].calculateDuration();
    const byTick = new Map();
    const pendingGrace = [];
    for (const voice of bar.voices) {
      for (const beat of voice.beats) {
        // 用谱面位置（displayStart），不用播放位置：拍上倚音会让 alphaTab 把主音的 playbackStart 往后推
        // 一个三十二分，按 playbackStart 算会在小节开头多出一个假休止
        if (beat.graceType) { // 倚音：记下来，交给同一根弦上紧跟着的音
          for (const n of beat.notes) pendingGrace.push({ s: ours(n.string), fret: n.fret, at: beat.displayStart });
          continue;
        }
        const start = beat.displayStart;
        if (!byTick.has(start)) byTick.set(start, []);
        if (beat.isRest) continue;
        for (const n of beat.notes) {
          if (n.isTieDestination) continue;
          if (byTick.get(start).some(x => x.string === ours(n.string))) continue; // 两个声部在同一根弦上同时有音：一根弦只能弹一个
          const tech = [];
          if (n.isHammerPullDestination && n.hammerPullOrigin) tech.push(n.fret >= n.hammerPullOrigin.fret ? 'h' : 'p');
          if (n.slideOrigin) tech.push('sl');
          if (n.slideInType === 1) tech.push('si');
          if (n.harmonicType === 1) tech.push('nh');
          if (n.harmonicType === 2) tech.push('ah');
          if (n.isPalmMute) tech.push('pm');
          if (n.isDead) tech.push('x');
          if (n.vibrato) tech.push('vib');
          let bend = 0;
          if (n.bendType && n.maxBendPoint) { tech.push('bend'); bend = n.maxBendPoint.value / 2; }
          if (beat.brushType === 3 || beat.brushType === 4) tech.push('roll');
          byTick.get(start).push({ string: ours(n.string), fret: fretOf(n), tech, grace: -1, bend });
        }
      }
    }
    // 只保留有新音的起拍（以及小节开头的休止）
    const ticks = [...byTick.keys()].filter(t => byTick.get(t).length || t === 0).sort((a, b) => a - b);
    if (!ticks.length || ticks[0] !== 0) {
      ticks.unshift(0);
      byTick.set(0, byTick.get(0) || []);
    }
    for (const g of pendingGrace) {
      const t = ticks.find(x => x >= g.at && byTick.get(x).some(n => n.string === g.s));
      const n = t !== undefined && byTick.get(t).find(n => n.string === g.s);
      if (n) n.grace = g.fret;
    }
    const events = ticks.map((t, i) => ({
      dur: ticksToDur((i + 1 < ticks.length ? ticks[i + 1] : length) - t),
      notes: byTick.get(t),
    }));
    const mb = score.masterBars[bi], out = { events };
    if (mb.timeSignatureNumerator !== meta.time_signature[0] || mb.timeSignatureDenominator !== meta.time_signature[1]) {
      out.time_signature = [mb.timeSignatureNumerator, mb.timeSignatureDenominator];
    }
    if (mb.isRepeatStart) out.repeat_start = true;
    if (mb.repeatCount > 0) out.repeat_end = mb.repeatCount;
    if (mb.alternateEndings) out.endings = [...Array(8).keys()].filter(i => mb.alternateEndings & (1 << i)).map(i => i + 1);
    if (mb.directions && mb.directions.size) out.directions = [...mb.directions].map(d => DIRECTION_NAMES[d]).filter(Boolean);
    return out;
  });
  return { meta, bars, trackName: track.name, version: DERIVE_VERSION };
}

// ---------------------------------------------------------------------
// 中文 GP3–5 文件的乱码：老版本 Guitar Pro 按系统编码存文字，中文系统上是 GBK；alphaTab 默认按
// windows-1252 读，曲名、艺人就成了“Ñº¹ãÌ«ÀÉ”。认出这种乱码后用 settings.importer.encoding = 'gbk' 重新导入。
// ---------------------------------------------------------------------
let CP1252 = null; // windows-1252 的字符 → 字节
function cp1252Bytes(s) {
  if (!CP1252) {
    CP1252 = new Map();
    const dec = new TextDecoder('windows-1252');
    for (let b = 0; b < 256; b++) CP1252.set(dec.decode(new Uint8Array([b])), b);
  }
  const out = [];
  for (const ch of s) { const b = CP1252.get(ch); if (b === undefined) return null; out.push(b); }
  return new Uint8Array(out);
}
const CJK = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/;

/**
 * 按 windows-1252 读出来的 GBK 文字 → 原来的中文；看着不像这种乱码就返回 null。
 * 判断：只有 ASCII 和 1252 的高位字符，高位字符连续成对出现（GBK 一个汉字两个高位字节），
 * 按 GBK 严格解码成功，并且解出了汉字。“Café”“Für Elise”这类西文不会被误判（单个高位字符）。
 */
export function gbkFromMojibake(s) {
  s = String(s ?? '');
  if (!/[\u0080-\uffff]/.test(s)) return null;
  const bytes = cp1252Bytes(s);
  if (!bytes) return null;
  let run = 0;
  for (const b of [...bytes, 0]) { if (b >= 0x80) run++; else { if (run % 2) return null; run = 0; } }
  let out;
  try { out = new TextDecoder('gbk', { fatal: true }).decode(bytes); } catch { return null; }
  return CJK.test(out) && !/[\ue000-\uf8ff]/.test(out) ? out : null;
}

/** alphaTab 读出来的谱子是不是 GBK 被当成 windows-1252 读了（看曲名、艺人等和音轨名） */
export function scoreNeedsGbk(score) {
  const texts = [score.title, score.subTitle, score.artist, score.album, score.words, score.music, score.tab, score.copyright,
    ...(score.tracks || []).map(t => t.name)].filter(Boolean);
  return texts.some(t => gbkFromMojibake(t) !== null);
}

/** 改音面板里可选的时值（到下一个音的时长，单位四分音符）。 */
export const DUR_CHOICES = [
  ['2', '二分'], ['3/2', '附点四分'], ['1', '四分'], ['3/4', '附点八分'], ['1/2', '八分'],
  ['1/3', '三连音'], ['1/4', '十六分'], ['1/6', '六连音'],
];

/**
 * 把第 ei 个起拍的时值改成 dur，小节总长不变：后面还有起拍时由下一个起拍补上差值；
 * 是最后一个起拍时只能缩短，缩出来的时间补一个休止。做不到时抛出带中文说明的 Error。
 */
export function retimeEvent(events, ei, dur) {
  // 用精确的分数算（tick 会四舍五入：1/7 拍改成 1/6 时，小节总长会差一点）
  const F = d => { const [n, m] = String(d).split('/').map(Number); return [n, m || 1]; };
  const g = (a, b) => (b ? g(b, a % b) : Math.abs(a));
  const norm = ([n, m]) => { const k = g(n, m) || 1; return [n / k, m / k]; };
  const add = ([a, b], [c, d]) => norm([a * d + c * b, b * d]);
  const sub = (x, [c, d]) => add(x, [-c, d]);
  const str = ([n, m]) => (m === 1 ? String(n) : `${n}/${m}`);
  const out = events.map(e => ({ ...e, notes: e.notes.map(n => ({ ...n })) }));
  const ev = out[ei];
  const oldD = F(ev.dur), newD = norm(F(dur));
  if (newD[0] <= 0) throw new Error('时值必须大于 0');
  const diff = sub(oldD, newD); // 旧 - 新：正数表示改短了
  if (diff[0] === 0) return out;
  if (ei + 1 < out.length) {
    const next = add(F(out[ei + 1].dur), diff);
    if (next[0] <= 0) throw new Error('会盖过下一个音，先把它改短或删掉');
    out[ei + 1].dur = str(next);
  } else {
    if (diff[0] < 0) throw new Error('最后一个音只能改短');
    out.push({ dur: str(diff), notes: [] });
  }
  ev.dur = str(newD);
  return out;
}
