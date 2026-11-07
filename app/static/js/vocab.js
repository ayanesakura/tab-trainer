// 前后端共用的词表（js/vocab.json，后端 app/music.py 读同一个文件）：弦数和标准调弦、播放网格、技巧、反复记号、来源、警告代码。
import vocab from './vocab.json' with { type: 'json' };

const freeze = o => Object.freeze(o);

export const INSTRUMENT = freeze({ ...vocab.instrument, standard_tuning: freeze([...vocab.instrument.standard_tuning]) });
export const STRINGS = INSTRUMENT.strings;
export const MAX_FRET = INSTRUMENT.max_fret;
export const STANDARD = INSTRUMENT.standard_tuning; // 1 弦 → 6 弦，如 ['E4', 'B3', …]

export const TICKS_PER_QUARTER = vocab.ticks_per_quarter;
export const GRID = vocab.grid_ticks; // 播放网格（tick），时值要落在它的整数倍上

// 技巧：[{code, sym, name}]，顺序就是界面上的顺序；TECH_SYM / TECH_NAME：code → 谱上的记号 / 中文名
export const TECHS = freeze(vocab.techs.map(t => freeze({ ...t })));
export const TECH_SYM = freeze(Object.fromEntries(TECHS.map(t => [t.code, t.sym])));
export const TECH_NAME = freeze(Object.fromEntries(TECHS.map(t => [t.code, t.name])));

// 反复记号 code，顺序 = alphaTab 的 Direction 枚举（DIRECTIONS.indexOf(code) 就是枚举值）
export const DIRECTIONS = freeze(vocab.directions.map(d => d.code));

export const SOURCES = freeze({ ...vocab.sources }); // source_type → 显示名（图片 / PDF / GP / MusicXML）
export const WARN_CODES = freeze({ ...vocab.warn_codes }); // 小节警告 code → 中文说明

// 时值字符串 → 四分音符数："1" → 1，"3/2" → 1.5，"1/3" → 0.333…；数字原样返回，读不懂的是 NaN
export function parseDur(s) {
  if (typeof s === 'number') return s;
  const [a, b] = String(s).trim().split('/');
  return b === undefined ? +a : +a / +b;
}
