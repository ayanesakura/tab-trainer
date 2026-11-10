// 吸顶的控制条和“更多”弹层：按钮、滑块、输入框 ⇄ store 的 play 切片，段落按钮，起止小节输入框，
// “回到播放位置”按钮，以及记在本机的播放设置（prefs.js）。播放本身都交给 player.js。

import { get, set, on } from './store.js';
import { $, UI } from './dom.js';
import { parseBarInput, planRange } from './view.js';
import { PREFS_KEY, PREF_DEFAULTS, parsePrefs, serializePrefs, clampPref, stored, remember } from './prefs.js';
import { play, stop, setLoop, setTempo, stepSection, revealLoop, cursorOutOfView, scrollToCursor, TIMBRES } from './player.js';
import { exportGp } from './gp-export.js';
import { markClean } from './practice-log.js';

// 跟着曲子走的默认值（加载谱子后按原速调整）；其余播放设置记在本机，见 prefs.js
const SONG_DEFAULTS = { bpmMax: 140 };
const CHORD_NOTATION_KEY = 'tt.notation.chordSheet';
const hasChords = song => !!song && (song.bars || []).some(b => (b.events || []).some(e => e.chord));
const nBars = () => (get('view') ? get('view').bars.length : 0);
const busy = () => get('play.playing') || !!get('rhythm.running');

// 快捷键写在悬停提示里（键盘处理在 keys.js / main.js）
const PLAY_TIP = '播放（空格）· 回到段首（Home）', STOP_TIP = '停止（空格）· 回到段首（Home）';

// 选段：按原谱的行（view.sections），或者按 AI 分的段落（分段页的练习建议，song.plan.steps）。
// 选哪种记在本机（tt.secMode）；没有练习建议时只有“行”，开关藏起来。←→ 换段也按现在这种（ui.secs，player.js 的 stepSection）
const SEC_MODE_KEY = 'tt.secMode';
let setSecMode = null;
/** 练习建议的每一步 → [{a, z, title, advice, k}]（读不出小节范围的跳过） */
function planSections(s, n) {
  return ((s && s.plan && s.plan.steps) || []).map((st, k) => { const r = planRange(st.bars, n); return r && { a: r.a, z: r.z, title: st.title || '', advice: st.advice || '', k }; }).filter(Boolean);
}
function renderSections() {
  const v = get('view'), host = $('secBtns');
  host.replaceChildren();
  if (!v || !v.bars.length) { $('secMode').hidden = true; set('ui.secs', null); return; }
  const plan = planSections(get('song'), v.bars.length);
  const mode = plan.length && get('ui.secMode') === 'plan' ? 'plan' : 'rows';
  $('secMode').hidden = !plan.length;
  setSecMode(mode);
  const add = (label, a, z, all, tip = '', cls = '') => {
    const btn = document.createElement('button');
    btn.textContent = label; btn.dataset.a = a; btn.dataset.z = z;
    if (cls) btn.className = cls;
    btn.onclick = () => { setLoop(a, z); revealLoop(); };
    btn.title = tip;
    if (!all && v.review.slice(a, z + 1).some(Boolean)) { btn.classList.add('warn'); btn.title = [tip, '这段有待核对的小节'].filter(Boolean).join('\n'); }
    host.appendChild(btn);
  };
  add('全曲', 0, v.bars.length - 1, true); // 放最前面：窄屏时也不会被挤出可见范围
  const range = g => (g.a === g.z ? `${g.a + 1}` : `${g.a + 1}–${g.z + 1}`);
  if (mode === 'plan') plan.forEach(g => add(`${g.k + 1} ${g.title}`.trim(), g.a, g.z, false, `第 ${range(g)} 小节${g.advice ? ` · ${g.advice}` : ''}`, 'plan-sec'));
  else v.sections.forEach(g => add(range(g), g.a, g.z));
  set('ui.secs', mode === 'plan' ? plan.map(g => ({ a: g.a, z: g.z })) : null);
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
  // 速度：数字后面是原速的百分比（“72 · 80%”，悬停看原速），滑块上原速那里有一道刻度；不知道原速就写 BPM
  const paintBpm = () => {
    const bpm = get('play.bpm'), orig = get('play.origBpm') || 0, el = $('bpm');
    el.value = bpm; $('bpmVal').textContent = bpm; UI.fillRange(el);
    $('bpmUnit').textContent = orig ? `· ${Math.round(bpm / orig * 100)}%` : 'BPM';
    $('bpmBox').title = orig ? `${bpm} BPM，原速 ${orig}` : `${bpm} BPM`;
    const o = (orig - +el.min) / (+el.max - +el.min);
    $('bpmOrig').hidden = !orig || o < 0 || o > 1;
    $('bpmOrig').style.setProperty('--o', Math.max(0, Math.min(1, o)));
    $('bpmOrig').title = `原速 ${orig}`;
  };
  const paintMax = m => { $('bpm').max = m; paintBpm(); };
  on(['play.bpm', 'play.origBpm'], paintBpm);
  on('play.bpmMax', paintMax);
  paintMax(get('play.bpmMax'));
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
  setSecMode = UI.seg($('secMode'), 'rows', v => { set('ui.secMode', v); remember(SEC_MODE_KEY, v); });
  set('ui.secMode', stored(SEC_MODE_KEY) === 'plan' ? 'plan' : 'rows');
  on(['view', 'song.plan', 'ui.secMode'], renderSections);
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
  $('cleanBtn').onclick = e => { e.currentTarget.blur(); markClean(); };
  on('play.clean', v => $('cleanBtn').setAttribute('aria-pressed', String(!!v)));
  $('bpm').oninput = e => setTempo(+e.target.value);
  $('bpmDown').onclick = () => setTempo(get('play.bpm') - 4);
  $('bpmUp').onclick = () => setTempo(get('play.bpm') + 4);
  $('loopPrev').onclick = () => stepSection(-1);
  $('loopNext').onclick = () => stepSection(1);
  bindLoopInput('loopA', true); bindLoopInput('loopB', false);
}
