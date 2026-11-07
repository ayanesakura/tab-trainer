// 练习页入口（最后加载）：视图切换、主题、键盘快捷键，按顺序绑定各部分的事件，再加载曲子。

// ---------------------------------------------------------------------
// 视图切换、主题
// ---------------------------------------------------------------------
const VIEWS = ['practice', 'tune', 'plan', 'glossary'];
let setNav; // 顶栏分段开关的 set 函数
function showView(v) {
  if (!VIEWS.includes(v)) v = 'practice';
  VIEWS.forEach(x => { $('view-' + x).hidden = x !== v; });
  setNav(v);
  if (location.hash.slice(1) !== v) history.replaceState(null, '', '#' + v);
  if (v === 'practice' && shownBar >= 0) { const b = shownBar; shownBar = -1; showBar(b); }
  scrollTo({ top: 0 });
}

// 键盘：空格 播放 / 停止；← → 上一段 / 下一段；↑ ↓ 快 / 慢 4 BPM。只在文字输入框里不响应（滑块、按钮用过之后照样能用空格）
const typing = el => el && el.closest && el.closest('textarea, [contenteditable], input:not([type=range]):not([type=checkbox])');
function initShortcuts() {
  document.addEventListener('keydown', e => {
    if (typing(e.target) || e.metaKey || e.ctrlKey || e.altKey || np || $('view-practice').hidden || !$('origModal').hidden) return;
    const k = { Space: () => (state.playing ? stop() : play()), ArrowLeft: () => $('loopPrev').click(), ArrowRight: () => $('loopNext').click(),
      ArrowUp: () => setTempo(state.bpm + 4), ArrowDown: () => setTempo(state.bpm - 4) }[e.code];
    if (!k) return;
    e.preventDefault();
    if (e.target.blur && e.target !== document.body) e.target.blur(); // 免得空格再“按”一次刚点过的按钮
    k();
  });
  window.addEventListener('resize', () => { if (shownBar < 0) return; const b = shownBar; shownBar = -1; showBar(b); });
}

function initViews() {
  setNav = UI.seg($('nav'), 'practice', v => showView(v));
  addEventListener('hashchange', () => showView(location.hash.slice(1)));
  UI.themeButton($('themeBtn'));
  document.addEventListener('themechange', () => setTimeout(refreshScoreTheme, 50));
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => setTimeout(refreshScoreTheme, 50));
}

// ---------------------------------------------------------------------
// 启动
// ---------------------------------------------------------------------
initPlayer();
initTuner();
initDemos();
initShortcuts();
initCoach();
initOrig();
initPracticeLog();
initEditor();
initRhythm();
initSong();
initViews();

if (!SONG_ID) $('songTitle').textContent = '缺少曲子 id，请从曲库进入';
else atReady.then(() => refresh(true)).then(() => showView(location.hash.slice(1))); // applySong 要用 scoreConv（ES 模块，可能比曲子数据晚到）
