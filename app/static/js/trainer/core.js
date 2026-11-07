// 练习页的脚本按顺序加载（见 trainer.html），都是普通脚本、不是 ES 模块：
// 顶层的 const / let / function 在所有文件之间共享。各文件只做声明（外加 initXxx() 绑定函数），
// 事件绑定和第一次加载曲子都在最后的 main.js 里进行，这样不会用到还没加载的文件里的函数。
// alphaTab 和 /static/js/score.js 是 ES 模块，由 trainer.html 里的模块脚本挂到 window.alphaTab / window.scoreConv。
// 这个文件：共用的小工具、播放状态、曲子数据和音高。

const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const frac = s => { const [a, b] = String(s).split('/'); return b === undefined ? +a : +a / +b; };
const SONG_ID = new URLSearchParams(location.search).get('id');

// 按钮内容由代码管理时：去掉 data-ic，免得页面加载时 UI.hydrate 再补一个图标
function setButton(el, html) { el.removeAttribute('data-ic'); el.innerHTML = html; }

// 播放状态：a..b 是循环的小节范围
const state = { playing: false, ready: false, bpm: 50, a: 0, b: 0, loops: 0, lastTick: -1, tempoSet: false };

// ---------------------------------------------------------------------
// 曲子数据：页面加载后从 /api/songs/<id> 取得，由 applySong（song.js）转换成下面这些结构
// ---------------------------------------------------------------------
let SONG = null, BARS = [], TIPS = [], REG = [], PAGES = [], BLS = []; // BLS：每小节的长度（四分音符数，中途可能变拍号）
const barLen = bi => BLS[bi] || 4;
const T3 = 1/3, S6 = 1/6; // 三连音、六连音的时值（拍）

// 展平成全曲事件表（指板上的“下一个音”要跨小节找）
const ALL = [];
function buildAll() {
  ALL.length = 0;
  BARS.forEach((evs, bi) => evs.forEach(e => { e.bar = bi; e.idx = ALL.length; ALL.push(e); }));
}

// ---------------------------------------------------------------------
// 音高
// ---------------------------------------------------------------------
let OPEN = { 1: 64, 2: 59, 3: 55, 4: 50, 5: 45, 6: 40 }; // 空弦音高（不含变调夹），按曲子的调弦设置
let OPEN_NAME = { 1: 'E', 2: 'B', 3: 'G', 4: 'D', 5: 'A', 6: 'E' };
let CAPO = 0;
const HARM = { 12: 12, 24: 24, 7: 19, 19: 19, 5: 24, 4: 28, 9: 28, 16: 28, 3: 31, 2: 36 }; // 自然泛音品位 → 比空弦高几个半音
const NN = ['C','C♯','D','D♯','E','F','F♯','G','G♯','A','A♯','B'];
const mtof = m => 440 * Math.pow(2, (m - 69) / 12);
const mname = m => NN[((m % 12) + 12) % 12] + (Math.floor(m / 12) - 1);
function noteMidi(n) {
  if (n.tech.has('nh')) return OPEN[n.s] + CAPO + (HARM[n.f] ?? 12);
  return OPEN[n.s] + CAPO + n.f + (n.tech.has('ah') ? 12 : 0);
}
