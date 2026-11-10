// 吸顶的控制条和“更多”弹层：按钮、滑块、输入框 ⇄ store 的 play 切片，段落按钮，起止小节输入框，
// “回到播放位置”按钮，以及记在本机的播放设置（prefs.js）。播放本身都交给 player.js。

import { get, set, on } from './store.js';
import { $, UI } from './dom.js';
import { parseBarInput } from './view.js';
import { PREFS_KEY, PREF_DEFAULTS, parsePrefs, serializePrefs, clampPref, stored, remember } from './prefs.js';
import { play, stop, setLoop, setTempo, stepSection, revealLoop, cursorOutOfView, scrollToCursor, TIMBRES } from './player.js';
import { exportGp } from './gp-export.js';

// 跟着曲子走的默认值（加载谱子后按原速调整）；其余播放设置记在本机，见 prefs.js
const SONG_DEFAULTS = { bpmMax: 140 };
const CHORD_NOTATION_KEY = 'tt.notation.chordSheet';
const hasChords = song => !!song && (song.bars || []).some(b => (b.events || []).some(e => e.chord));
const nBars = () => (get('view') ? get('view').bars.length : 0);
const busy = () => get('play.playing') || !!get('rhythm.running');

// 快捷键写在悬停提示里（键盘处理在 keys.js / main.js）
const PLAY_TIP = '播放（空格）· 回到段首（Home）', STOP_TIP = '停止（空格）· 回到段首（Home）';

function renderSections(v) {
  const host = $('secBtns');
  host.replaceChildren();
  if (!v || !v.bars.length) return;
  const add = (label, a, z, all) => {
    const btn = document.createElement('button');
    btn.textContent = label; btn.dataset.a = a; btn.dataset.z = z;
    btn.onclick = () => { setLoop(a, z); revealLoop(); };
    if (!all && v.review.slice(a, z + 1).some(Boolean)) { btn.classList.add('warn'); btn.title = '这段有待核对的小节'; }
    host.appendChild(btn);
  };
  add('全曲', 0, v.bars.length - 1, true); // 放最前面：窄屏时也不会被挤出可见范围
  v.sections.forEach(g => add(g.a === g.z ? `${g.a + 1}` : `${g.a + 1}–${g.z + 1}`, g.a, g.z));
  paintLoop();
}
/** 控制条上的起止小节（输入框）和段落按钮；拖选过程中（ui.loopPreview）先显示拖到的范围 */
function paintLoop() {
  const preview = get('ui.loopPreview'), n = nBars(), { a, b } = preview || get('play');
  for (const [id, v] of [['loopA', a], ['loopB', b]]) {
    const el = $(id);
    el.disabled = !n;
    if (document.activeElement !== el) el.value = n ? v + 1 : '';
  }
  if (preview) return;
  $('secBtns').querySelectorAll('button').forEach(x => {
    const on_ = +x.dataset.a === a && +x.dataset.z === b;
    x.classList.toggle('on', on_);
    x.setAttribute('aria-pressed', on_);
    if (on_) x.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  });
}

/** 控制条和“更多”弹层里的输入框 ⇄ store 的 play 切片 */
function bindInputs() {
  const bind = (id, key, kind) => {
    const el = $(id);
    if (kind === 'check') el.addEventListener('change', () => set('play.' + key, el.checked));
    else {
      // 数字：打字过程中（清空了、只打了负号、超出范围）先不改设置；输完（change：回车、离开输入框）夹到范围里并写回输入框
      const num = () => clampPref(key, el.value === '' ? NaN : +el.value, el.max === '' ? Infinity : +el.max);
      el.addEventListener('input', () => { const v = num(); if (v !== null && v === +el.value) set('play.' + key, v); });
      el.addEventListener('change', () => { const v = num() ?? get('play.' + key); set('play.' + key, v); el.value = v; if (el.type === 'range') UI.fillRange(el); });
    }
    const paint = v => { if (kind === 'check') el.checked = v; else if (+el.value !== v) el.value = v; if (el.type === 'range') UI.fillRange(el); };
    on('play.' + key, paint);
    paint(get('play.' + key));
  };
  bind('metro', 'metro', 'check'); bind('metroVol', 'metroVol'); bind('countIn', 'countIn', 'check'); bind('gtVol', 'gtVol'); bind('demo', 'demo', 'check');
  bind('follow', 'follow', 'check');
  const lbl = $('metro').closest('label') || $('metro');
  lbl.title = '节拍器（M）';
}

// 起止小节输入框：回车或离开输入框时生效，Esc 放弃，↑↓ 加减一小节（马上生效）。
// 起点改到终点后面时终点跟过去（反过来也一样），这样先改起点再改终点不会被对调
function bindLoopInput(id, isStart) {
  const el = $(id);
  const apply = bi => {
    const { a, b } = get('play');
    const na = isStart ? bi : Math.min(a, bi), nb = isStart ? Math.max(b, bi) : bi;
    if (na !== a || nb !== b) { setLoop(na, nb); revealLoop(); }
  };
  // 点进去就全选，直接打新的数（鼠标点进来时松开鼠标会把全选取消掉：这一次的 mouseup 不让它动）
  let justFocused = false;
  el.addEventListener('focus', () => { el.select(); justFocused = true; });
  el.addEventListener('mouseup', e => { if (justFocused) e.preventDefault(); justFocused = false; });
  el.addEventListener('blur', () => { justFocused = false; });
  el.addEventListener('change', () => {
    const bi = parseBarInput(el.value, nBars());
    if (bi === null) { if (el.value.trim()) UI.toast(`请输入 1–${nBars()} 之间的小节号`, 'info'); } // 清空了就是不改
    else apply(bi);
    el.value = get('play')[isStart ? 'a' : 'b'] + 1; // 夹到范围里、或改回原来的值
  });
  el.addEventListener('keydown', e => {
    if (e.isComposing) return;
    if (e.key === 'Enter') { e.preventDefault(); el.blur(); }
    else if (e.key === 'Escape') { e.preventDefault(); el.value = get('play')[isStart ? 'a' : 'b'] + 1; el.blur(); }
    else if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
      e.preventDefault();
      const cur = get('play')[isStart ? 'a' : 'b'], bi = Math.max(0, Math.min(nBars() - 1, cur + (e.key === 'ArrowUp' ? 1 : -1)));
      apply(bi);
      el.value = get('play')[isStart ? 'a' : 'b'] + 1;
      el.select();
    }
  });
}

// “回到播放位置”：播放中光标走到看不见的地方时显示
function updateFollowBtn() { $('followBtn').hidden = !(get('play.playing') && cursorOutOfView()); }

export function initControls() {
  // 记在本机的播放设置：读的时候逐项检查（prefs.js），改了就存
  set('play', { ...get('play'), ...SONG_DEFAULTS, ...parsePrefs(stored(PREFS_KEY)), timbre: TIMBRES[stored('tt.timbre')] ? stored('tt.timbre') : 'steel' });
  on(Object.keys(PREF_DEFAULTS).map(k => 'play.' + k), () => remember(PREFS_KEY, serializePrefs(get('play'))));
  on('play.timbre', v => remember('tt.timbre', v));
  bindInputs();
  const paintBpm = bpm => { $('bpm').value = bpm; $('bpmVal').textContent = bpm; UI.fillRange($('bpm')); };
  const paintMax = m => { $('bpm').max = m; UI.fillRange($('bpm')); };
  on('play.bpm', paintBpm);
  on('play.bpmMax', paintMax);
  paintMax(get('play.bpmMax')); paintBpm(get('play.bpm'));
  // 播放键：alphaTab 在放，或者敲节奏在进行，都显示成“停止”
  const paintPlay = () => {
    const now = busy();
    $('playBtn').classList.toggle('on', now);
    $('playBtn').innerHTML = UI.icon(now ? 'stop' : 'play');
    $('playBtn').setAttribute('aria-label', now ? '停止（空格）' : '播放（空格）');
    $('playBtn').title = now ? STOP_TIP : PLAY_TIP;
    $('transport').classList.toggle('playing', now);
  };
  on(['play.playing', 'rhythm.running'], paintPlay);
  paintPlay();
  on(['play.playing', 'play.note', 'ui.bar'], updateFollowBtn);
  let followTick = 0;
  addEventListener('scroll', () => { if (!followTick) followTick = requestAnimationFrame(() => { followTick = 0; updateFollowBtn(); }); }, { passive: true });
  $('followBtn').onclick = scrollToCursor;
  on(['play.a', 'play.b', 'ui.loopPreview'], () => paintLoop());
  on('view', renderSections);
  // 选段只有一行、横着滚：竖着的滚轮也拿来横滚（触控板本来就能横滑）
  $('secBtns').addEventListener('wheel', e => {
    const el = e.currentTarget;
    if (Math.abs(e.deltaY) <= Math.abs(e.deltaX) || el.scrollWidth <= el.clientWidth) return;
    e.preventDefault();
    el.scrollLeft += e.deltaY;
  }, { passive: false });

  $('playBtn').onclick = () => (busy() ? stop() : play());
  $('demoBarBtn').onclick = () => { const bi = get('ui.bar') >= 0 ? get('ui.bar') : get('play.a'); setLoop(bi, bi, !get('play.playing')); if (!get('play.playing')) play(); };
  UI.seg($('timbreSeg'), get('play.timbre'), v => set('play.timbre', v));
  // 谱面样式：弹唱谱（有和弦名的曲子）默认只看六线谱（五线谱对弹唱没什么用，还占地方），换了就记住；其他曲子默认两种都显示
  const setNotationSeg = UI.seg($('notationSeg'), 'both', v => {
    set('ui.notation', v);
    if (hasChords(get('song'))) remember(CHORD_NOTATION_KEY, v);
  });
  on('view', v => {
    if (!v || get('ui.notation') || !hasChords(get('song'))) return;
    const n = stored(CHORD_NOTATION_KEY) === 'both' ? 'both' : 'tab';
    setNotationSeg(n);
    set('ui.notation', n);
  });
  $('exportBtn').onclick = exportGp;
  $('bpm').oninput = e => setTempo(+e.target.value);
  $('bpmDown').onclick = () => setTempo(get('play.bpm') - 4);
  $('bpmUp').onclick = () => setTempo(get('play.bpm') + 4);
  $('loopPrev').onclick = () => stepSection(-1);
  $('loopNext').onclick = () => stepSection(1);
  bindLoopInput('loopA', true); bindLoopInput('loopB', false);
}
