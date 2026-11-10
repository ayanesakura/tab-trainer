// 练习页入口：视图切换、键盘快捷键、“更多”弹层、指板停靠条，按顺序初始化各部分，然后加载曲子。
// 这个模块（连同它导入的 player.js → alphaTab）都加载完才会执行，所以第一次加载曲子时 alphaTab 已经就绪。

import { get, set, on, store } from './store.js';
import { $, UI, SONG_ID } from './dom.js';
import * as player from './player.js';
import { initPlayer, play, stop, setLoop, setTempo, stepSection, toLoopStart, applyAudio, onBeatClick, cursorOutOfView, refreshScoreTheme, getApi, machineState, revealLoop } from './player.js';
import { addShortcuts, initKeys, route as keyRoute } from './keys.js';
import { initControls } from './controls.js';
import { initLyrics } from './lyrics.js';
import { initReviewMarks } from './review-marks.js';
import { exportGp } from './gp-export.js';
import * as atAdapter from './at-adapter.js';
import { initFretboard } from './fretboard.js';
import { initBarCard } from './bar-card.js';
import { initOrig } from './orig.js';
import { initCoach } from './coach.js';
import { initPracticeLog, markClean } from './practice-log.js';
import { initEditor, setEditMode, openNotePop, editorState } from './editor.js';
import { initSong, startLoading, refresh, songGone } from './song.js';
import { initTuner } from './tuner.js';
import { initFollow, followDelta, followDebug } from './follow.js';
import { initGlossary } from './glossary.js';
import { initRhythm, rhythmDebug } from './rhythm-drill.js';
import { initStudy } from './study.js';
import { initAiKey } from './ai-key.js';
import { ac } from './audio.js';
import { DERIVE_VERSION } from '../score.js';
import { stored, remember } from './prefs.js';

// ---------------------------------------------------------------------
// 视图切换（顶栏：练习 / 调弦 / 分段 / 记号说明）
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
// 键盘（判断条件和唯一的监听在 keys.js）：空格 播放 / 停止；← → 上一段 / 下一段；↑ ↓ 快 / 慢 4 BPM；
// Home 回到段首（播放中就从段首重新开始）；M 节拍器开 / 关；Enter 这遍干净。滑块、按钮用过之后照样能用空格（按完让它失焦）。
// 敲节奏进行中的空格 / F / J / Esc、改音的 ⌘Z、原谱弹窗里的 ←→ 由各自的模块登记，排在这一组前面。
// ---------------------------------------------------------------------
// Enter：键盘 Tab 到按钮、链接上时归它们（照常“按下”）；鼠标点过的按钮也会拿着焦点（没有 :focus-visible），
// 这时 Enter 还是“这遍干净”，先让按钮失焦，免得再“按”一次刚点过的播放 / 选段
const cleanKey = e => {
  const t = e.target;
  if (t && t !== document.body && t.closest && t.closest('button, a[href], summary, [role=button], [tabindex]')) {
    if (t.matches && t.matches(':focus-visible')) return false;
    if (t.blur) t.blur();
  }
  markClean();
};
function initShortcuts() {
  const blur = e => { if (e.target.blur && e.target !== document.body) e.target.blur(); }; // 免得空格再“按”一次刚点过的按钮
  const act = fn => e => { blur(e); fn(); };
  addShortcuts({
    name: 'transport', order: 50,
    keys: {
      Space: act(() => (get('play.playing') ? stop() : play())),
      ArrowLeft: act(() => stepSection(-1)), ArrowRight: act(() => stepSection(1)),
      ArrowUp: act(() => setTempo(get('play.bpm') + 4)), ArrowDown: act(() => setTempo(get('play.bpm') - 4)),
      Home: act(() => { toLoopStart(); revealLoop(); }),
      KeyM: act(() => { const v = !get('play.metro'); set('play.metro', v); UI.toast(v ? '节拍器：开' : '节拍器：关', 'info', 1200); }),
      // 这遍干净（见 cleanKey）
      Enter: cleanKey, NumpadEnter: cleanKey,
    },
  });
  initKeys();
}

// ---------------------------------------------------------------------
// “更多”弹层：节拍器、预备拍、示范音量、音色、自动跟随（这些设置记在本机，见 prefs.js）
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
}

// ---------------------------------------------------------------------
// 指板停靠条：底部，可折叠（记住）；敲节奏打开时整条收起来
// ---------------------------------------------------------------------
function initDock() {
  let open = true;
  // 没设过的话：屏幕够高才默认展开（1440×900 这类矮屏幕上先把地方留给谱面，收起时只剩一条细栏）
  const v = stored('tt.fb');
  open = v ? v === '1' : innerHeight >= 1000;
  const paint = v => {
    $('fbBody').hidden = !v;
    $('fbToggle').setAttribute('aria-expanded', v);
    $('fbToggle').title = v ? '收起指板' : '展开指板';
    document.body.classList.toggle('fb-open', v);
  };
  on('ui.fb', paint);
  set('ui.fb', open);
  paint(open);
  const toggle = () => {
    set('ui.fb', !get('ui.fb'));
    remember('tt.fb', get('ui.fb') ? '1' : '0');
  };
  $('fbToggle').onclick = toggle;
  // 停靠条的高度给布局用（侧栏高度、页面底部留白、“回到播放位置”按钮的位置）
  const dock = $('fbDock');
  // 指板是 position: fixed（offsetParent 永远是 null，不能拿它判断显没显示）：用 getClientRects 判断是否显示
  new ResizeObserver(() => document.documentElement.style.setProperty('--dock-h', (dock.getClientRects().length ? dock.offsetHeight : 0) + 'px')).observe(dock);
  on('rhythm.on', v => { dock.hidden = v; });
}

// ---------------------------------------------------------------------
// 窗口窄于 1000px：侧栏（小节卡片 / 敲节奏）是右边滑出的抽屉（ui.drawer）。控制条上的按钮开关，Esc 收起；
// 打开时焦点移进抽屉，收起时还给按钮；收着的时候抽屉 inert（Tab 不会走进看不见的地方）。
// 不用 UI.layer：抽屉开着时空格、方向键这些播放快捷键照样要能用（浮层开着时 keys.js 只问浮层自己的那组）
// ---------------------------------------------------------------------
function initDrawer() {
  const narrow = matchMedia('(max-width: 999px)'), side = $('side'), btn = $('drawerBtn');
  const open = v => set('ui.drawer', !!v && narrow.matches);
  const paint = v => {
    document.body.classList.toggle('drawer-open', v);
    btn.setAttribute('aria-expanded', String(v));
    side.inert = narrow.matches && !v;
    if (v) {
      const first = [...side.querySelectorAll('button, input, [tabindex]')].find(x => !x.disabled && x.offsetParent);
      if (first) first.focus({ preventScroll: true });
    } else if (side.contains(document.activeElement)) btn.focus({ preventScroll: true });
  };
  on('ui.drawer', paint);
  paint(false);
  narrow.addEventListener('change', () => { if (!narrow.matches) open(false); paint(get('ui.drawer')); });
  btn.onclick = () => open(!get('ui.drawer'));
  $('drawerClose').onclick = () => open(false);
  // 敲节奏的卡片也在抽屉里：窄屏上打开敲节奏时抽屉跟着打开
  on('rhythm.on', v => { if (v) open(true); });
  addShortcuts({ name: 'drawer', order: 30, when: () => !!get('ui.drawer'), keys: { Escape: () => open(false) } });
  // 焦点在抽屉里的输入框上时 keys.js 不管按键：Esc 在这里收起
  side.addEventListener('keydown', e => {
    if (e.key === 'Escape' && get('ui.drawer') && !UI.hasLayer() && !e.defaultPrevented && !e.isComposing) { e.preventDefault(); open(false); }
  });
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
  get machine() { return machineState(); },
  keyRoute, toLoopStart,
  play, stop, setLoop, setTempo, stepSection, applyAudio, onBeatClick, cursorOutOfView, setEditMode, openNotePop, refresh, player, exportGp,
  at: atAdapter,
  rhythm: rhythmDebug,
  followDelta, follow: followDebug,
};

// ---------------------------------------------------------------------
// 启动
// ---------------------------------------------------------------------
initViews();
initAiKey();
initPlayer();
initControls();
initLyrics();
initReviewMarks();
initFollow();
initFretboard();
initBarCard();
initOrig();
initCoach();
initPracticeLog();
initEditor();
initRhythm();
initStudy();
initSong();
initTuner();
initGlossary();
initShortcuts();
initMore();
initDock();
initDrawer();
showView(location.hash.slice(1));

if (!SONG_ID) { $('songTitle').textContent = '缺少曲子 id，请从曲库进入'; songGone('缺少曲子 id'); }
else startLoading();
