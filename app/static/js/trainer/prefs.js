// 播放设置记在本机（localStorage），所有曲子共用：节拍器、预备拍、示范音开关、音量、自动跟随。
// 速度和循环范围不在这里：它们跟着曲子走（接着练 / 练习记录）。
// 读的时候逐项检查：旧版本、手改坏的值不会把页面弄坏，不对的那一项用默认值。纯函数，不碰 DOM（有 node 测试）。

export const PREFS_KEY = 'tt.playPrefs';
export const PREF_DEFAULTS = { metro: true, metroVol: 70, countIn: true, demo: true, gtVol: 80, follow: true };
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

/** 存着的文本（可能是 null、坏 JSON、旧格式）→ 完整、合法的设置 */
export function parsePrefs(text) {
  let raw = null;
  try { raw = JSON.parse(text); } catch { /* 坏的就当没有 */ }
  const out = { ...PREF_DEFAULTS };
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return out;
  for (const [k, d] of Object.entries(PREF_DEFAULTS)) {
    const v = raw[k];
    if (typeof d === 'boolean') { if (typeof v === 'boolean') out[k] = v; }
    else if (Number.isInteger(v) && v >= RANGES[k][0] && v <= RANGES[k][1]) out[k] = v;
  }
  return out;
}

/** store 的 play 切片 → 要存的文本（不合法的项存成默认值） */
export const serializePrefs = play => JSON.stringify(parsePrefs(JSON.stringify(Object.fromEntries(Object.keys(PREF_DEFAULTS).map(k => [k, play[k]])))));
