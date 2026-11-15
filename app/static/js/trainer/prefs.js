// 播放设置记在本机（localStorage），分两份：
//   每首曲子各记各的（SONG_PREF_KEYS，tt.playPrefs.<曲子 id>）：节拍器、预备拍、示范音这三个开关；没记过的曲子用默认值（都开）
//   所有曲子共用（tt.playPrefs）：两个音量、自动跟随
// 速度和循环范围不在这里：它们跟着曲子走（接着练 / 练习记录）。
// 读的时候逐项检查：旧版本、手改坏的值不会把页面弄坏，不对的那一项用默认值。纯函数，不碰 DOM（有 node 测试）。

export const PREFS_KEY = 'tt.playPrefs';
/** 这首曲子自己那一份记在哪 */
export const songPrefsKey = id => `${PREFS_KEY}.${id}`;

// 本机存取（localStorage）：隐私模式、存满了、Node 测试里没有 localStorage 时都不报错——读不到是 null，写不进就只在这次打开时生效。
// 练习页记在本机的东西（播放设置、音色、敲节奏的练法和延迟补偿、指板折叠、谱面样式……）都走这两个函数
export const stored = k => { try { return localStorage.getItem(k); } catch { return null; } };
export const remember = (k, v) => { try { localStorage.setItem(k, v); } catch { /* 只在这次打开时生效 */ } };
export const PREF_DEFAULTS = { metro: true, metroVol: 70, countIn: true, demo: true, gtVol: 80, follow: true };
/** 每首曲子各记各的开关；PREF_DEFAULTS 里其余的所有曲子共用 */
export const SONG_PREF_KEYS = ['metro', 'countIn', 'demo'];
const SHARED_PREF_KEYS = Object.keys(PREF_DEFAULTS).filter(k => !SONG_PREF_KEYS.includes(k));
const RANGES = { metroVol: [0, 100], gtVol: [0, 100] };

/**
 * 输入框里的数字 → 合法的值（取整、夹到范围里），不是数字返回 null。max：再按输入框当时的上限收紧。
 * 浏览器不会拦住手打的负数、超出范围的值
 */
export function clampPref(key, v, max = Infinity) {
  const r = RANGES[key];
  if (!r || typeof v !== 'number' || !Number.isFinite(v)) return null;
  return Math.max(r[0], Math.min(r[1], max, Math.round(v)));
}

/** 存着的一份文本（可能是 null、坏 JSON、旧格式）里的这几项 → 合法的值，不对的那一项用默认值 */
function pick(text, keys) {
  let raw = null;
  try { raw = JSON.parse(text); } catch { /* 坏的就当没有 */ }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) raw = {};
  const out = {};
  for (const k of keys) {
    const d = PREF_DEFAULTS[k], v = raw[k];
    if (typeof d === 'boolean') out[k] = typeof v === 'boolean' ? v : d;
    else out[k] = Number.isInteger(v) && v >= RANGES[k][0] && v <= RANGES[k][1] ? v : d;
  }
  return out;
}

/**
 * 共用的那份文本 + 这首曲子的那份文本 → 完整、合法的设置。
 * 共用那份里旧版本留下的节拍器 / 预备拍 / 示范音开关不读（以前所有曲子共用）
 */
export const parsePrefs = (text, songText) => ({ ...PREF_DEFAULTS, ...pick(text, SHARED_PREF_KEYS), ...pick(songText, SONG_PREF_KEYS) });

const dump = (play, keys) => JSON.stringify(pick(JSON.stringify(Object.fromEntries(keys.map(k => [k, play[k]]))), keys));
/** store 的 play 切片 → 共用那份要存的文本（不合法的项存成默认值） */
export const serializePrefs = play => dump(play, SHARED_PREF_KEYS);
/** store 的 play 切片 → 这首曲子那份要存的文本 */
export const serializeSongPrefs = play => dump(play, SONG_PREF_KEYS);
