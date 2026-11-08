// 我们的小节数据 ⇄ alphaTab 谱面 的转换。浏览器和 Node 测试共用（不依赖 DOM）。
//
// 我们的数据：bar.events = [{ dur: "1/2", notes: [{ string, fret, tech: [...], grace, bend }], chord?, strum? }]
//   - dur：到下一个起拍的时长，单位是四分音符；string：1 = 最高音弦（最细）
//   - chord：从这个拍开始的和弦名；strum："down" 下扫（从低音弦扫向高音弦）/ "up" 上扫
//   - lyrics：从这个拍开始唱的歌词，每段一个字符串（第 1 段在前）
//   - bar.shapes = { 和弦名: [1–6 弦的品位，-1 = 不弹] }：这个小节用到的和弦指法（谱面上方画和弦图）
//   - 击弦 h / 勾弦 p / 滑音 sl 标在“目标音”上
// alphaTab：alphaTex 里 string 1 也是最高音弦，但内部模型里 note.string 1 = 最低音弦；
//   击弦、滑音标在“起始音”上。转换时要把这些技巧挪到同一根弦的前一个音；前一个音拆成了几段（连音线接着）时
//   挪到最后一段上（alphaTab 把击弦 / 滑音连到起始音后面紧跟着的那个音，标在第一段就连到了它自己的延续上）。

import { DIRECTIONS, GRID as GRID_TICKS, TICKS_PER_QUARTER, STRINGS, parseDur } from './vocab.js';

export const TICKS = TICKS_PER_QUARTER; // 每个四分音符的 MIDI tick 数（960）

// deriveSong 的输出格式 / 转换规则的版本。改了 deriveSong（音符、技巧、拍号的转换）就加 1：
// 已经导入的曲子下次打开时会重新转换（alphaTab 升级也会）。
// 2：每小节带自己的拍号；多声部同一根弦同时起拍的音只保留一个
// 3：按谱面位置分起拍（拍上倚音不再在小节开头多出假休止）
// 4：闷音的品格记 0；自然泛音的品格记谱上标的泛音点（<12> → 12）
// 5：中文 GP3–5 文件按 GBK 重新导入（曲名、艺人不再是乱码）
// 6：GBK 乱码判断收紧（Canção、Größe 这类西文曲名不再被误当成乱码），只看 GP3–5 / GPX 文件
// 8：弹唱谱：拍上的和弦名（chord）、扫弦方向（strum）、小节用到的和弦指法（shapes）
// 9：歌词（lyrics，每段一个字符串）
// 10：写着和弦名或歌词的休止 / 延音拍保留成没有新音的事件（原来并进前一个音，和弦、歌词丢了）
export const DERIVE_VERSION = 10;

// 可以直接写成一个拍的时值：[tick 数, alphaTex 时值, 附点数, 是否三连音]
const UNITS = [
  [3840, 1, 0, 0], [2880, 2, 1, 0], [1920, 2, 0, 0], [1440, 4, 1, 0], [960, 4, 0, 0],
  [720, 8, 1, 0], [640, 4, 0, 1], [480, 8, 0, 0], [360, 16, 1, 0], [320, 8, 0, 1],
  [240, 16, 0, 0], [180, 32, 1, 0], [160, 16, 0, 1], [120, 32, 0, 0], [80, 32, 0, 1],
  [60, 64, 0, 0], [40, 64, 0, 1],
];

export function durToTicks(dur) {
  return Math.round(parseDur(dur) * TICKS);
}

function gcd(a, b) { while (b) [a, b] = [b, a % b]; return a; }
export function ticksToDur(t) {
  const g = gcd(t, TICKS);
  return TICKS / g === 1 ? String(t / g) : `${t / g}/${TICKS / g}`;
}

export const GRID = GRID_TICKS; // 所有可写的时值都是 20 tick 的整数倍（最短是 64 分三连音 40 tick）

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


const esc = s => String(s ?? '').replace(/["\\]/g, ' ');

// 反复 / 跳转记号（词表）：名字和 alphaTex 的 \jump 一致，下标就是 alphaTab 的 Direction 枚举值
const DIRECTION_NAMES = DIRECTIONS;

const barQ = ts => ts[0] * 4 / ts[1]; // 一小节几拍（四分音符）

/**
 * 小节里真正弹的事件：[{e, q: 时值（拍）}]。和练习页的 deriveView 一致：起拍在小节长度处或更后面的事件不弹（不写进谱子，
 * 否则会叠到下一小节上、这一小节画得太满），最后一个音超出小节的部分截掉。写不满的小节照原样。
 */
function fitBar(events, len) {
  const out = [];
  let t = 0;
  for (const e of events) {
    if (t >= len - 1e-9) break;
    const d = parseDur(e.dur);
    out.push({ e, q: t + d > len + 1e-9 ? len - t : d });
    t += d;
  }
  return out;
}

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

  const chordTex = chordTexNames(song.bars);
  // 第一遍：把每个音转成可挂技巧的对象，并把 h/p/sl 挪到同一根弦的前一个音（或倚音）上（记在 out 里：写在这个音的最后一段）
  const lastOnString = {};
  const bars = song.bars.map(bar => {
    const evs = fitBar(bar.events && bar.events.length ? bar.events : [{ dur: ticksToDur(barTicks(bar)), notes: [] }], barQ(tsOf(bar)));
    return evs.map(({ e, q }) => {
      const notes = (e.notes || []).filter(n => n.string >= 1 && n.string <= STRINGS).map(n => {
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
        if (n.grace != null && n.grace >= 0) grace = { string: n.string, fret: n.grace, fx: new Set(), out: new Set() };
        const origin = grace || lastOnString[n.string];
        if (origin && (tech.has('h') || tech.has('p'))) origin.out.add('h');
        if (origin && tech.has('sl')) origin.out.add('sl');
        const note = { string: n.string, fret: n.fret, fx, out: new Set() };
        lastOnString[n.string] = note;
        return { note, grace };
      });
      const roll = notes.length > 1 && (e.notes || []).some(n => (n.tech || []).includes('roll'));
      const fx = [];
      if (e.chord) fx.push(`ch "${chordTex.get(e.chord)}"`);
      (e.lyrics || []).forEach((l, i) => { if (l) fx.push(`lyrics ${i} "${esc(l)}"`); });
      // 扫弦：alphaTab 的 bd = 下扫（从低音弦扫到高音弦，六线谱上画 ▲）、bu = 上扫（▼）。
      // 琶音（波浪线）正好反过来：ad 画 ▲（从低音弦到高音弦，最常见的写法）、au 画 ▼
      if (roll) fx.push(e.strum === 'up' ? 'au' : 'ad');
      else if (notes.length > 1 && e.strum === 'down') fx.push('bd');
      else if (notes.length > 1 && e.strum === 'up') fx.push('bu');
      return { q, notes, fx };
    });
  });

  // 第二遍：输出文本
  // 一个音的第 i 段（一共 count 段）：第一段带技巧，后面的段用连音线（t）延续、不重新拨弦；挪过来的 h / sl 写在最后一段
  const noteTex = (n, i = 0, count = 1) => {
    const fx = [...(i === 0 ? n.fx : ['t']), ...(i === count - 1 ? n.out : [])];
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
      units.forEach((u, i) => parts.push(beatTex(ev.notes.map(x => noteTex(x.note, i, units.length)), u, i === 0 ? ev.fx : [])));
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
    ...chordDefs(song.bars, chordTex),
    `\\ts (${ts[0]} ${ts[1]})`,
  ].filter(Boolean).join('\n');
  return { tex: `${head}\n${barTexts.join(' |\n')}`, beatMap };
}

// 只差大小写的和弦名（Am7 / AM7、Bm / BM）：alphaTab 按“名字转小写 + 谱表 + 音轨”给和弦编号，
// 后定义的会盖掉先定义的，两个拍都画成同一个和弦。后出现的那个在名字后面加零宽空格，看着一样、编号不同
export const CHORD_PAD = '\u200b';
/** 全曲的和弦名 → 写进 alphaTex 的名字（已转义；大小写撞车的后来者加零宽空格） */
function chordTexNames(bars) {
  const out = new Map(), used = new Map(); // 小写名 → 已经用了几个写法
  const add = name => {
    if (!name || out.has(name)) return;
    const t = esc(name), k = t.toLowerCase(), n = used.get(k) || 0;
    used.set(k, n + 1);
    out.set(name, t + CHORD_PAD.repeat(n));
  };
  for (const bar of bars) {
    for (const name of Object.keys(bar.shapes || {})) add(name);
    for (const e of bar.events || []) add(e.chord);
  }
  return out;
}

/** 全曲用到的和弦指法 → alphaTex 的和弦定义（谱面开头画和弦图；拍上的 ch "名字" 按名字找到它）。 */
function chordDefs(bars, texName) {
  const shapes = new Map();
  for (const bar of bars) {
    for (const [name, frets] of Object.entries(bar.shapes || {})) {
      if (!shapes.has(name) && Array.isArray(frets) && frets.length === STRINGS) shapes.set(name, frets);
    }
  }
  return [...shapes].map(([name, frets]) => {
    const played = frets.filter(f => f > 0);
    const low = played.length ? Math.min(...played) : 1;
    const first = Math.max(...frets) > 4 && low > 1 ? low : 1; // 高把位的和弦图从最低的按弦品位画起（“3fr.”）
    const f = frets.map(x => (x < 0 ? 'x' : x)).join(' ');
    return `\\chord ("${texName.get(name)}" ${f}) {firstfret ${first} showdiagram true}`;
  });
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
  if (nStr !== STRINGS) throw new Error(`这个音轨是 ${nStr} 弦的，目前只支持六弦吉他`);
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
    // 最后一个音到哪里结束：一般是小节长度；GP 里的小节可以写得比拍号长（多出来的拍从小节长度处或更后面开始），
    // 这时按拍自己的时值算到它结束，不能让最后一个音的时值变成 0 或负数（后端会拒收，整小节变成休止）
    let end = length;
    const byTick = new Map();
    const beatInfo = new Map(); // 起拍 → { chord, strum }
    const shapes = {};
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
        end = Math.max(end, start + (beat.displayDuration || 0));
        if (!byTick.has(start)) byTick.set(start, []);
        // 和弦名（GP 的 Chord）、扫弦（brushType 1 = 上扫、2 = 下扫）记在这个起拍上
        const info = beatInfo.get(start) || {};
        const chord = beat.chord || (beat.chordId != null && staff.chords && staff.chords.get(beat.chordId));
        if (chord && chord.name && !info.chord) {
          info.chord = String(chord.name).trim().slice(0, 24);
          const frets = [...(chord.strings || [])];
          if (frets.length === STRINGS && frets.some(f => f >= 0)) shapes[info.chord] = frets.map(f => (f < 0 ? -1 : f));
        }
        // 方向：1 上扫、2 下扫；琶音 3 / 4 的方向和扫弦相反（4 = 从低音弦到高音弦，记成下扫 + roll）
        if (!beat.isRest && beat.brushType >= 1 && beat.brushType <= 4 && !info.strum) {
          info.strum = beat.brushType === 2 || beat.brushType === 4 ? 'down' : 'up';
        }
        if (beat.lyrics && beat.lyrics.some(Boolean) && !info.lyrics) {
          const l = beat.lyrics.map(x => String(x || '').trim().slice(0, 40));
          while (l.length && !l[l.length - 1]) l.pop();
          if (l.length) info.lyrics = l;
        }
        beatInfo.set(start, info);
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
    // 只保留有新音的起拍、小节开头，以及写着和弦名或歌词的休止 / 延音（连音线延续的音）：
    // 这种拍记成没有新音的事件（同小节开头被延续的音一样），和弦、歌词才不会丢。别的休止并进前一个音
    const marked = t => { const i = beatInfo.get(t); return !!(i && (i.chord || i.lyrics)); };
    const ticks = [...byTick.keys()].filter(t => byTick.get(t).length || t === 0 || marked(t)).sort((a, b) => a - b);
    if (!ticks.length || ticks[0] !== 0) {
      ticks.unshift(0);
      byTick.set(0, byTick.get(0) || []);
    }
    for (const g of pendingGrace) {
      const t = ticks.find(x => x >= g.at && byTick.get(x).some(n => n.string === g.s));
      const n = t !== undefined && byTick.get(t).find(n => n.string === g.s);
      if (n) n.grace = g.fret;
    }
    if (end <= ticks[ticks.length - 1]) end = ticks[ticks.length - 1] + TICKS; // 拍没有时值（不会发生）时兜底：算一拍
    const events = ticks.map((t, i) => {
      const ev = { dur: ticksToDur((i + 1 < ticks.length ? ticks[i + 1] : end) - t), notes: byTick.get(t) };
      const info = beatInfo.get(t) || {};
      if (info.chord) ev.chord = info.chord;
      if (info.strum && ev.notes.length > 1) ev.strum = info.strum;
      if (info.lyrics) ev.lyrics = info.lyrics;
      return ev;
    });
    const mb = score.masterBars[bi], out = { events };
    const used = Object.fromEntries(Object.entries(shapes).filter(([k]) => events.some(e => e.chord === k)));
    if (Object.keys(used).length) out.shapes = used;
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
// 中文 GP3–5 / GPX 文件的乱码：老版本 Guitar Pro 按系统编码存文字，中文系统上是 GBK；alphaTab 默认按
// windows-1252 读，曲名、艺人就成了“ÑºÎ²¹âÌ«ÀÉ”。认出这种乱码后用 settings.importer.encoding = 'gbk' 重新导入。
// 判断规则和后端 migrate.fix_mojibake 是同一套（见 tests/js/score.test.mjs 里的例子）。
// ---------------------------------------------------------------------
let CP1252 = null; // windows-1252 的字符 → 字节
function cp1252Bytes(s) {
  if (!CP1252) {
    CP1252 = new Map();
    const dec = new TextDecoder('windows-1252');
    for (let b = 0; b < 256; b++) CP1252.set(dec.decode(new Uint8Array([b])), b);
  }
  const out = [];
  for (const ch of s) {
    let b = CP1252.get(ch);
    if (b === undefined) { const cp = ch.codePointAt(0); if (cp < 256) b = cp; else return null; } // latin-1 能表示的直接取码位
    out.push(b);
  }
  return new Uint8Array(out);
}
const CJK = /[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/g; // 和后端 migrate.fix_mojibake 同一套（含假名）

/**
 * 按 windows-1252 读出来的 GBK 文字 → 原来的中文；看着不像这种乱码就返回 null。
 * 先按 cp1252 还原成字节、再按 GBK 严格解码（任一步失败 → null）；并且同时满足才算：
 * 非 ASCII 字符 ≥ 4 个、非 ASCII 字符比 ASCII 字母多、解出来至少 2 个汉字、没有替换符。
 * 这样“Canção”“Größe”“Für Elise”这类西文不会被误判成“Can玢o”“Gr鲞e”“F黵 Elise”。
 */
export function gbkFromMojibake(s) {
  s = String(s ?? '');
  let nonAscii = 0, letters = 0;
  for (const ch of s) { if (ch.codePointAt(0) > 0x7f) nonAscii++; else if (/[A-Za-z]/.test(ch)) letters++; }
  if (nonAscii < 4 || nonAscii <= letters) return null;
  const bytes = cp1252Bytes(s);
  if (!bytes) return null;
  let out;
  try { out = new TextDecoder('gbk', { fatal: true }).decode(bytes); } catch { return null; }
  // 私用区：浏览器的 GBK 解码把用户自定义区解成私用区字符，Python 的 gbk 解不了（两边结果一致）
  if (out.includes('\ufffd') || /[\ue000-\uf8ff]/.test(out)) return null;
  return (out.match(CJK) || []).length >= 2 ? out : null;
}

/** 文件是不是 GP3–5（“FICHIER GUITAR PRO”开头）或 GPX（BCFZ / BCFS 容器）：只有这些老格式会按系统编码存文字 */
export function isLegacyGp(bytes) {
  if (!bytes || bytes.length < 20) return false;
  const head = String.fromCharCode(...bytes.slice(0, 20));
  return head.startsWith('BCFZ') || head.startsWith('BCFS') || head.slice(1).startsWith('FICHIER GUITAR');
}

/** alphaTab 读出来的谱子是不是 GBK 被当成 windows-1252 读了（只看 GP3–5 / GPX 文件；看曲名、艺人等和音轨名） */
export function scoreNeedsGbk(score, bytes) {
  if (!isLegacyGp(bytes)) return false;
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
