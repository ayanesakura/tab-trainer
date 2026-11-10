// 浏览器标签上的标题 = 前缀（正在计时的时间）+ 页面名（练习页是曲名）。两半由不同的模块管（study-timer.js 每秒改前缀，
// 练习页的 song.js 在曲子更新时改页面名），都只改自己那一半、在这里拼起来，谁也不会把对方的那一半冲掉。

/** 拼标题：没有前缀就只有页面名 */
export const composeTitle = (prefix, base) => (prefix ? `${prefix} · ${base}` : base);

let base = null, prefix = '';
function paint() {
  if (typeof document === 'undefined') return;
  if (base === null) base = document.title; // 没设过页面名：用 HTML 里写的
  const t = composeTitle(prefix, base);
  if (document.title !== t) document.title = t;
}
/** 页面名（不带前缀） */
export function setTitleBase(t) { base = String(t); paint(); }
/** 前缀；空字符串去掉前缀 */
export function setTitlePrefix(p) { prefix = p ? String(p) : ''; paint(); }
