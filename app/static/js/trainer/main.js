// 练习页入口：视图切换、键盘快捷键、“更多”弹层、指板停靠条，按顺序初始化各部分，然后加载曲子。
// 这个模块（连同它导入的 player.js → alphaTab）都加载完才会执行，所以第一次加载曲子时 alphaTab 已经就绪。

import { get, set, on, store } from './store.js';
import { $, UI, SONG_ID } from './dom.js';
import * as player from './player.js';
import { initPlayer, play, stop, setLoop, setTempo, stepSection, applyAudio, onBeatClick, cursorOutOfView, refreshScoreTheme, getApi } from './player.js';
import { initFretboard } from './fretboard.js';
import { initBarCard } from './bar-card.js';
import { initOrig } from './orig.js';
import { initCoach } from './coach.js';
import { initPracticeLog } from './practice-log.js';
import { initEditor, setEditMode, openNotePop, editorState } from './editor.js';
import { initSong, startLoading, refresh } from './song.js';
import { initTuner } from './tuner.js';
import { initGlossary } from './glossary.js';
import { initRhythm, rhythmKey, rhythmDebug } from './rhythm-drill.js';
import { ac } from './audio.js';
import { DERIVE_VERSION } from '../score.js';

// ---------------------------------------------------------------------
// 视图切换（顶栏：练习 / 调弦 / 练习建议 / 记号说明）
// ---------------------------------------------------------------------
const VIEWS = ['practice', 'tune', 'plan', 'glossary'];
let setNav;
function showView(v) {
  if (!VIEWS.includes(v)) v = 'practice';
  VIEWS.forEach(x => { $('view-' + x).hidden = x !== v; });
  document.body.dataset.view = v;
  setNav(v);
  if (location.hash.slice(1) !== v) history.replaceState(null, '', location.pathname + location.search + '#' + v);
  set('ui.view', v);
  scrollTo({ top: 0 });
}

// ---------------------------------------------------------------------
// 键盘：空格 播放 / 停止；← → 上一段 / 下一段；↑ ↓ 快 / 慢 4 BPM。
// 在文字输入框里、有浮层（改音面板、原谱弹窗、“更多”）时不响应；滑块、按钮用过之后照样能用空格。
// 敲节奏进行中：空格 / F / J 是敲击、Esc 停止（rhythmKey 先看，处理了就不再往下走）；没在敲时空格照常“开始”（play 交给敲节奏）。
// ---------------------------------------------------------------------
const typing = el => el && el.closest && el.closest('textarea, select, [contenteditable], input:not([type=range]):not([type=checkbox])');
function initShortcuts() {
  document.addEventListener('keydown', e => {
    if (typing(e.target) || e.metaKey || e.ctrlKey || e.altKey || UI.hasLayer() || get('ui.view') !== 'practice') return;
    if (rhythmKey(e)) return;
    const k = {
      Space: () => (get('play.playing') ? stop() : play()), ArrowLeft: () => stepSection(-1), ArrowRight: () => stepSection(1),
      ArrowUp: () => setTempo(get('play.bpm') + 4), ArrowDown: () => setTempo(get('play.bpm') - 4),
    }[e.code];
    if (!k) return;
    e.preventDefault();
    if (e.target.blur && e.target !== document.body) e.target.blur(); // 免得空格再“按”一次刚点过的按钮
    k();
  });
}

// ---------------------------------------------------------------------
// “更多”弹层：节拍器、预备拍、示范音量、音色、自动提速
// ---------------------------------------------------------------------
function initMore() {
  const btn = $('moreBtn'), pop = $('morePop');
  let lay = null;
  const close = () => { if (lay) lay.close(); };
  btn.onclick = () => {
    if (lay) { close(); return; }
    pop.hidden = false;
    const r = btn.getBoundingClientRect(), host = $('transport').getBoundingClientRect();
    pop.style.top = r.bottom - host.top + 6 + 'px';
    pop.style.right = Math.max(8, host.right - r.right) + 'px';
    btn.setAttribute('aria-expanded', 'true');
    lay = UI.layer(pop, { ignore: [btn], onClose: () => { lay = null; pop.hidden = true; btn.setAttribute('aria-expanded', 'false'); } });
  };
  // 开着自动提速时按钮上标一个点，收起来也知道
  on('play.auto', v => btn.classList.toggle('flag', !!v));
}

// ---------------------------------------------------------------------
// 指板停靠条：底部，可折叠（记住）；敲节奏打开时整条收起来
// ---------------------------------------------------------------------
function initDock() {
  let open = true;
  // 没设过的话：屏幕够高才默认展开（矮屏幕上先把地方留给谱面）
  try { const v = localStorage.getItem('tt.fb'); open = v ? v === '1' : innerHeight >= 900; } catch { open = innerHeight >= 900; }
  const paint = v => {
    $('fbBody').hidden = !v;
    $('fbToggle').setAttribute('aria-expanded', v);
    $('fbToggle').title = v ? '收起指板' : '展开指板';
    $('fbBtn').setAttribute('aria-pressed', v);
    document.body.classList.toggle('fb-open', v);
  };
  on('ui.fb', paint);
  set('ui.fb', open);
  paint(open);
  const toggle = () => {
    set('ui.fb', !get('ui.fb'));
    try { localStorage.setItem('tt.fb', get('ui.fb') ? '1' : '0'); } catch { /* 只在这次打开时生效 */ }
  };
  $('fbToggle').onclick = toggle;
  // 停靠条的高度给布局用（侧栏高度、页面底部留白、“回到播放位置”按钮的位置）
  const dock = $('fbDock');
  new ResizeObserver(() => document.documentElement.style.setProperty('--dock-h', (dock.offsetParent ? dock.offsetHeight : 0) + 'px')).observe(dock);
  $('fbBtn').onclick = toggle;
  on('rhythm.on', v => { dock.hidden = v; $('fbBtn').disabled = v; });
}

function initViews() {
  setNav = UI.tabs($('nav'), 'practice', v => showView(v));
  addEventListener('hashchange', () => showView(location.hash.slice(1)));
  UI.initShell();
  document.addEventListener('themechange', () => setTimeout(refreshScoreTheme, 50));
}

// 给 e2e 测试和调试用：window.tt
window.tt = {
  store, get, set,
  get state() { return get('play'); },
  get song() { return get('song'); },
  get view() { return get('view'); },
  get api() { return getApi(); },
  get ac() { return ac; },
  get editor() { return editorState; },
  DERIVE_VERSION,
  play, stop, setLoop, setTempo, stepSection, applyAudio, onBeatClick, cursorOutOfView, setEditMode, openNotePop, refresh, player,
  rhythm: rhythmDebug,
};

// ---------------------------------------------------------------------
// 启动
// ---------------------------------------------------------------------
initViews();
initPlayer();
initFretboard();
initBarCard();
initOrig();
initCoach();
initPracticeLog();
initEditor();
initRhythm();
initSong();
initTuner();
initGlossary();
initShortcuts();
initMore();
initDock();
showView(location.hash.slice(1));

if (!SONG_ID) $('songTitle').textContent = '缺少曲子 id，请从曲库进入';
else startLoading();
