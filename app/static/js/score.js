// 我们的小节数据 ⇄ alphaTab 谱面 的转换。浏览器和 Node 测试共用（不依赖 DOM）。
//
// 我们的数据：bar.events = [{ dur: "1/2", notes: [{ string, fret, tech: [...], grace, bend }] }]
//   - dur：到下一个起拍的时长，单位是四分音符；string：1 = 最高音弦（最细）
//   - 击弦 h / 勾弦 p / 滑音 sl 标在“目标音”上
// alphaTab：alphaTex 里 string 1 也是最高音弦，但内部模型里 note.string 1 = 最低音弦；
//   击弦、滑音标在“起始音”上。转换时要把这些技巧挪到同一根弦的前一个音。

export const TICKS = 960; // 每个四分音符的 MIDI tick 数

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

/** 把一段时长拆成能写出来的拍（优先一个就能表示；否则从大到小拆，后面的用连音线接上）。 */
export function splitTicks(t) {
  const exact = UNITS.find(u => u[0] === t);
  if (exact) return [exact];
  const out = [];
  let rest = t;
  for (const u of UNITS) {
    while (rest >= u[0]) { out.push(u); rest -= u[0]; }
  }
  // 小于 64 分三连音的零头（正常数据不会出现）直接忽略
  return out.length ? out : [UNITS[UNITS.length - 1]];
}

const esc = s => String(s ?? '').replace(/["\\]/g, ' ');

// 反复 / 跳转记号：名字和 alphaTex 的 \jump 一致，下标就是 alphaTab 的 Direction 枚举值
const DIRECTION_NAMES = ['fine', 'segno', 'segnosegno', 'coda', 'doublecoda', 'dacapo', 'dacapoalcoda', 'dacapoaldoublecoda',
  'dacapoalfine', 'dalsegno', 'dalsegnoalcoda', 'dalsegnoaldoublecoda', 'dalsegnoalfine', 'dalsegnosegno',
  'dalsegnosegnoalcoda', 'dalsegnosegnoaldoublecoda', 'dalsegnosegnoalfine', 'dacoda', 'dadoublecoda'];

/** 小节开头的 alphaTex 元数据：反复开始 / 结束（一共弹几遍）、跳房子、跳转。 */
function barMetaTex(bar) {
  const m = [];
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
  const barTicks = Math.round(ts[0] * 4 / ts[1] * TICKS);

  // 第一遍：把每个音转成可挂技巧的对象，并把 h/p/sl 挪到同一根弦的前一个音（或倚音）上
  const lastOnString = {};
  const bars = song.bars.map(bar => {
    const evs = bar.events && bar.events.length ? bar.events : [{ dur: ticksToDur(barTicks), notes: [] }];
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
      return { ticks: durToTicks(e.dur), notes, roll: notes.length > 1 && (e.notes || []).some(n => (n.tech || []).includes('roll')) };
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
    let t = 0;
    for (const ev of evs) {
      onsets.push(t);
      t += ev.ticks;
      const graces = ev.notes.filter(x => x.grace).map(x => x.grace);
      if (graces.length) parts.push(beatTex(graces.map(g => noteTex(g)), [0, 16, 0, 0], ['gr']));
      const units = splitTicks(ev.ticks);
      units.forEach((u, i) => {
        // 第一段带上音符和技巧；后面的段用连音线（{t}）延续，不重新拨弦
        const notes = i === 0 ? ev.notes.map(x => noteTex(x.note)) : ev.notes.map(x => `${x.note.fret}.${x.note.string}{t}`);
        parts.push(beatTex(notes, u, i === 0 && ev.roll ? ['au'] : []));
      });
    }
    beatMap.push(onsets);
    return barMetaTex(song.bars[bi]) + parts.join(' ');
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

  const bars = staff.bars.map((bar, bi) => {
    const length = score.masterBars[bi].calculateDuration();
    const byTick = new Map();
    const pendingGrace = [];
    for (const voice of bar.voices) {
      for (const beat of voice.beats) {
        if (beat.graceType) { // 倚音：记下来，交给同一根弦上紧跟着的音
          for (const n of beat.notes) pendingGrace.push({ s: ours(n.string), fret: n.fret, at: beat.playbackStart });
          continue;
        }
        const start = beat.playbackStart;
        if (!byTick.has(start)) byTick.set(start, []);
        if (beat.isRest) continue;
        for (const n of beat.notes) {
          if (n.isTieDestination) continue;
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
          byTick.get(start).push({ string: ours(n.string), fret: n.fret, tech, grace: -1, bend });
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
    if (mb.isRepeatStart) out.repeat_start = true;
    if (mb.repeatCount > 0) out.repeat_end = mb.repeatCount;
    if (mb.alternateEndings) out.endings = [...Array(8).keys()].filter(i => mb.alternateEndings & (1 << i)).map(i => i + 1);
    if (mb.directions && mb.directions.size) out.directions = [...mb.directions].map(d => DIRECTION_NAMES[d]).filter(Boolean);
    return out;
  });
  return { meta, bars, trackName: track.name };
}
