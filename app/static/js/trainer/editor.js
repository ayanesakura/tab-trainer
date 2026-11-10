// 修改谱子（只对识别出来的曲子）：小节的 JSON 编辑器（“改数据”）、重新识别整行、在谱面上点音符改音（“改音”），以及撤销。
// 入口都在小节卡片的“纠错”里（bar-card.js 管 AI 核对）；改音模式里谱面标题栏只有一个“完成”。

import { get, set, on } from './store.js';
import { $, UI, setButton, songUrl } from './dom.js';
import { put, post, withBusy } from '../lib/api.js';
import { DUR_CHOICES, durToTicks, eventIndexAt, retimeEvent } from '../score.js';
import { TECH_NAME, STRINGS, INSTRUMENT } from '../vocab.js';
import { stop, holdBarWhile, barBox } from './player.js';
import { provide } from './hooks.js';
import { addShortcuts } from './keys.js';
import { refresh, applySong, conflict, isLocked, lockedToast } from './song.js';

// ---------------------------------------------------------------------
// 修改小节（JSON）、重新识别这一行
// ---------------------------------------------------------------------
let editorBarId = null, editorRev; // 编辑器打开时的小节和它的 rev（保存时带这个，期间别处改过就报 409）
const curBar = () => { const s = get('song'), bi = get('ui.bar'); return s && bi >= 0 ? s.bars[bi] : null; };

/** 这个小节现在不能改的原因（后端算好的 locked），能改是空字符串 */
export function barLocked(bi) {
  const s = get('song'), bar = s && s.bars[bi];
  return (bar && bar.locked) || '';
}
function fillEditor(bar) {
  $('edText').value = '[\n' + bar.events.map(e => '  ' + JSON.stringify(e)).join(',\n') + '\n]';
  editorBarId = bar.id; editorRev = bar.rev;
}
function closeEditor() { $('editor').hidden = true; $('edMsg').textContent = ''; editorBarId = null; editorRev = undefined; }
function updateBarTools() {
  const s = get('song'), bar = curBar();
  if (!bar) return;
  if (bar.id !== editorBarId) closeEditor();
  const sys = s.systems[bar.system], parsing = sys && sys.status === 'parsing';
  $('reparseBtn').disabled = !sys || parsing || s.busy;
  setButton($('reparseBtn'), parsing ? '<span class="spinner"></span>正在重新识别…' : UI.icon('refresh') + '重新识别整行');
  const row = s.bars.filter(b => b.system === bar.system).map(b => b.n);
  if (!$('reparseBtn').classList.contains('ai-off')) $('reparseBtn').title = `最后的办法：让 AI 把第 ${row[0]}–${row[row.length - 1]} 小节这一整行重新识别一遍（会替换这一行的手改、提示和问答；写了“哪里不对”会一起告诉 AI）`;
}

// 保存一个小节；记下改之前的内容，⌘Z / Ctrl+Z 可以撤销。
// 乐观锁按小节：rev 是这次修改所基于的小节版本（打开编辑器 / 改音面板时记下的；撤销用的是那次保存后的版本）。
// 小节的 rev 只在内容变了时才 +1（写问答、练习记录、别的小节改动都不影响它），所以它过期 = 真的在别处改过。
export const undoStack = []; // {barId, events: 改之前的内容, n, rev: 这次保存后的版本}
export let saving = 0; // 进行中的保存：这期间不能撤销，否则可能撤销错步骤
const savingBars = new Set(); // 正在保存的小节：同一小节一次只发一个保存（否则第二个带的是旧 rev）
const barBusy = barId => { if (!savingBars.has(barId)) return false; UI.toast('这个小节正在保存，稍等一下再改', 'info'); return true; };
/**
 * → 'ok' | 'conflict'（别处改过这个小节，已刷新）| 'gone'（小节已经不在了，已刷新）| 'locked'（423：有任务在跑 / 小节锁着）
 *   | 'error' | 'busy'（这个小节还在保存上一次的修改）
 */
async function putBar(barId, events, rev, msg, isUndo = false) {
  if (barBusy(barId)) return 'busy';
  const s = get('song'), before = s.bars.find(b => b.id === barId);
  const entry = before && !isUndo ? { barId, events: before.events, n: before.n, rev: undefined } : null;
  if (entry) { undoStack.push(entry); if (undoStack.length > 50) undoStack.shift(); } // 先记上，按发出的顺序
  saving++; savingBars.add(barId);
  try {
    const res = await put(songUrl(`/bars/${encodeURIComponent(barId)}`), { events, rev });
    const after = (res.bars || []).find(b => b.id === barId);
    if (entry) entry.rev = after && after.rev;
    // 撤销成功：内容回到了上一条记录保存后的样子，那条记录改用现在的版本号
    else if (isUndo) { const prev = undoStack.findLast(u => u.barId === barId); if (prev) prev.rev = after && after.rev; }
    applySong(res);
  } catch (e) {
    if (entry) undoStack.splice(undoStack.lastIndexOf(entry), 1);
    if (e.status === 409) {
      // 这个小节在别处改过：它的撤销记录都是按旧内容记的，作废；别的小节的撤销记录保留
      for (let i = undoStack.length - 1; i >= 0; i--) if (undoStack[i].barId === barId) undoStack.splice(i, 1);
      await conflict(e);
      return 'conflict';
    }
    if (isLocked(e)) { lockedToast(e); return 'locked'; } // 不是冲突：撤销记录都留着，等一会再试
    if (e.status === 404) {
      // 小节不在了（比如这一行重新识别过，小节换了新的 id）：它的撤销记录都撤销不了，作废（不然一直挡在撤销栈顶上）
      for (let i = undoStack.length - 1; i >= 0; i--) if (undoStack[i].barId === barId) undoStack.splice(i, 1);
      UI.toast(isUndo ? '这个小节已经不在了（可能重新识别过这一行），这一步撤销不了' : e.message, 'err', 5000);
      await refresh();
      return 'gone';
    }
    UI.toast(e.message || '保存失败', 'err', 4000);
    return 'error';
  } finally { saving--; savingBars.delete(barId); }
  UI.toast(msg + (isUndo ? '' : (get('ui.editMode') ? `（${UNDO_KEY} 撤销）` : '')));
  return 'ok';
}
async function undoEdit() {
  if (saving) { UI.toast('正在保存，稍等一下再撤销', 'info'); return; }
  const u = undoStack.pop();
  if (!u) { UI.toast('没有可以撤销的修改', 'info'); return; }
  const r = await putBar(u.barId, u.events, u.rev, `已撤销第 ${u.n} 小节的修改`, true);
  // 撤销失败：记录放回去，下次再试（冲突、小节不在了时，这个小节的记录已作废）
  if (r === 'error' || r === 'busy' || r === 'locked') undoStack.push(u);
}

// ---------------------------------------------------------------------
// 在谱面上改音（只对识别出来的曲子：GP / MusicXML 导入的本来就是精确数据，播放也直接用原文件）
// ---------------------------------------------------------------------
const IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
const UNDO_KEY = IS_MAC ? '⌘Z' : 'Ctrl+Z';
// 谱面上方的操作说明：一项一个 <span>（.legend 是 flex，项与项之间有间距，项里面的文字和按键不能被拆开）
const legend = items => items.map(x => `<span>${x}</span>`).join('');
// 快捷键（处理在 keys.js：main.js 的播放组、practice-log 的这遍干净、rhythm-drill 的敲击、这里的撤销）
const LEGEND_KEYS = ['<b class="lg-h">快捷键</b>', '<kbd>空格</kbd>播放 / 停止', '<kbd>←</kbd><kbd>→</kbd>上一段 / 下一段', '<kbd>↑</kbd><kbd>↓</kbd>快 / 慢 4 BPM',
  '<kbd>Home</kbd>回到段首', '<kbd>M</kbd>节拍器开 / 关', '<kbd>Enter</kbd>这遍干净', `<kbd>${UNDO_KEY}</kbd>撤销改音（改音时）`,
  '敲节奏：<kbd>空格</kbd><kbd>F</kbd><kbd>J</kbd>敲，<kbd>Esc</kbd>停止'];
const LEGEND_PLAY = legend(['<b class="lg-h">谱面</b>', '点小节：从这里开始播放', '<kbd>Shift</kbd> + 点：把循环扩到这个小节', '按住拖过几个小节：循环它们', '点音符：试听', '<i class="review-key"></i>待核对', '<i class="refine-key"></i>精修改过', ...LEGEND_KEYS]);
const LEGEND_EDIT = legend(['<b class="lg-h">改音</b>', '点音符：改弦、品、技巧和时值', '点拍上的空白处：改和弦、填品位加音', '<kbd>Enter</kbd>保存', '<kbd>Esc</kbd>关闭面板', `<kbd>${UNDO_KEY}</kbd>撤销`, '改完点“完成”']);
// 改音面板里的技巧（推弦不在面板里，原样保留）
const NP_TECH = ['h', 'p', 'sl', 'si', 'pm', 'x', 'nh', 'ah', 'th', 'vib', 'roll'];
let np = null, npLayer = null, pendingClick = null, setNpStr = null, setNpStrum = null;
const pointer = { x: 0, y: 0 };

export function setEditMode(v) {
  const s = get('song');
  set('ui.editMode', !!v && !!s && s.can_edit_bars);
}
function paintEditMode(on_) {
  $('editModeBtn').setAttribute('aria-pressed', on_);
  $('editModeBtn').hidden = !on_; // 改音模式里只有“完成”；进入改音在小节卡片的“纠错”里
  $('editBadge').hidden = !on_;
  $('barEditBtn').setAttribute('aria-pressed', on_);
  $('scoreCard').classList.toggle('editing', on_);
  // 操作说明都收在谱面标题栏的“?”里（省出谱面的地方），改音时换成改音的说明
  $('scoreHelp').innerHTML = on_ ? LEGEND_EDIT : LEGEND_PLAY;
  if (!on_) closeNotePop();
  else if (get('play.playing')) stop();
}
// 点音符时 alphaTab 会先后触发拍和音符两个事件：攒到同一轮里，有音符就改音符，没有就是在这一拍加音
function queueEditClick(beat, note) {
  if (pendingClick) { if (note) pendingClick.note = note; return; }
  pendingClick = { beat, note };
  setTimeout(() => { const c = pendingClick; pendingClick = null; openNotePop(c.beat, c.note); }, 0);
}
const barEvents = bi => { const b = get('song').bars[bi]; return b.events && b.events.length ? b.events : [{ dur: String(get('view').barLens[bi]), notes: [] }]; };

export function openNotePop(beat, note) {
  const bi = beat.voice.bar.index, s = get('song');
  if (bi >= s.bars.length) return;
  const locked = barLocked(bi);
  if (locked) { UI.toast(locked, 'info'); return; }
  if (beat.graceType) { UI.toast('倚音请在小节卡片的“纠错 → 改数据”里改', 'info'); return; }
  if (barBusy(s.bars[bi].id)) return; // 上一次修改还没存好：这时的内容是旧的
  const ei = eventIndexAt(get('view').beatMap[bi] || [0], beat.playbackStart);
  const ev = barEvents(bi)[ei];
  if (!ev) return;
  const ni = note ? ev.notes.findIndex(n => n.string === STRINGS + 1 - note.string) : -1; // alphaTab 的弦号 1 = 最低音弦
  const cur = ni >= 0 ? ev.notes[ni] : null;
  if (npLayer) npLayer.close('replaced'); // 点了另一个音：换成新的
  np = { bi, ei, ni, barId: s.bars[bi].id, rev: s.bars[bi].rev, snap: s.bars[bi].events, string: cur ? cur.string : freeString(ev, note ? STRINGS + 1 - note.string : 1), fret: cur ? cur.fret : '', tech: new Set(cur ? cur.tech : []), dur: ev.dur, chord: ev.chord || '', strum: ev.strum || '' };
  set('ui.bar', bi);
  $('npFret').value = np.fret; // 先填好：renderNotePop 按它判断加不加音
  $('npHarm').value = cur && cur.harm ? cur.harm : '';
  renderNotePop();
  $('npChord').value = np.chord;
  const card = $('scoreCard').getBoundingClientRect(), pop = $('notePop');
  pop.hidden = false;
  npLayer = UI.layer(pop, { ignore: [$('score')], focus: $('npFret'), onClose: () => { npLayer = null; np = null; pop.hidden = true; } });
  const x = Math.max(8, Math.min(pointer.x - card.left + 14, card.width - pop.offsetWidth - 8));
  pop.style.left = x + 'px'; pop.style.top = notePopY(card, pop.offsetHeight) + 'px';
  setTimeout(() => $('npFret').select(), 0);
}
// 面板放在点的位置下方；下面放不下（会压到底部指板或出屏幕）就翻到上方，两边都放不下就贴着可见区域
function notePopY(card, h) {
  const css = getComputedStyle(document.documentElement);
  const topH = parseFloat(css.getPropertyValue('--top-h')) || 0, dockH = parseFloat(css.getPropertyValue('--dock-h')) || 0;
  const minY = Math.max(topH, $('transport').getBoundingClientRect().bottom) + 8, maxY = innerHeight - dockH - 8; // 可见区域（视口坐标，去掉吸顶的控制条和底部指板）
  let y = pointer.y + 14;
  if (y + h > maxY) y = pointer.y - 14 - h >= minY ? pointer.y - 14 - h : Math.max(minY, maxY - h);
  return y - card.top;
}
function freeString(ev, prefer) {
  const used = new Set(ev.notes.map(n => n.string));
  if (!used.has(prefer)) return prefer;
  return Array.from({ length: STRINGS }, (_, i) => i + 1).find(x => !used.has(x)) || prefer;
}
// 点在拍上（不是音符上）是“加音”：品位空着就不加音，只改这一拍的和弦、扫弦和时值（满六根弦的拍也能改和弦）
const npAddsNote = () => !!np && (np.ni >= 0 || $('npFret').value.trim() !== '');
function npConflict() {
  if (!npAddsNote()) return false;
  const ev = barEvents(np.bi)[np.ei];
  return ev.notes.some((n, i) => i !== np.ni && n.string === np.string);
}
function renderNotePop() {
  const add = np.ni < 0;
  $('npTitle').textContent = add ? '在这一拍加音或改和弦' : '修改这个音';
  $('npFret').placeholder = add ? '不加音' : '';
  $('npSub').textContent = `第 ${np.bi + 1} 小节 · 第 ${np.ei + 1} 个音`;
  setNpStr(String(np.string));
  setNpStrum(np.strum);
  $('npTech').querySelectorAll('button').forEach(b => { const v = np.tech.has(b.dataset.t); b.classList.toggle('on', v); b.setAttribute('aria-pressed', v); });
  $('npHarmRow').hidden = !np.tech.has('th'); // 点弦泛音才有“点哪一品”
  $('npDel').hidden = $('npAdd').hidden = add;
  const curT = durToTicks(np.dur);
  let durOk = true;
  $('npDur').querySelectorAll('button').forEach(b => { const v = durToTicks(b.dataset.d) === curT; b.classList.toggle('on', v); b.setAttribute('aria-pressed', v); });
  let msg = '';
  try { retimeEvent(barEvents(np.bi), np.ei, np.dur); } catch (e) { durOk = false; msg = e.message; }
  const clash = npConflict();
  if (clash) msg = `这一拍 ${np.string} 弦已经有音了`;
  const info = !msg && !npAddsNote(); // 不是问题，只是说明
  $('npMsg').textContent = info ? '品位空着：不加音，只改这一拍的和弦、扫弦和时值' : msg;
  $('npMsg').classList.toggle('info', info);
  $('npSave').disabled = clash || !durOk;
}
function closeNotePop() { if (npLayer) npLayer.close(); np = null; $('notePop').hidden = true; }
const npStep = d => { if (!np) return; np.fret = Math.max(0, Math.min(30, (parseInt($('npFret').value, 10) || 0) + d)); $('npFret').value = np.fret; renderNotePop(); };

const npError = m => { $('npMsg').textContent = m; $('npMsg').classList.remove('info'); };
async function saveNotePop() {
  if (!np || npConflict() || barBusy(np.barId)) return;
  const addNote = npAddsNote(), fret = parseInt($('npFret').value, 10);
  if (addNote && !(fret >= 0 && fret <= 30)) { npError('品位要在 0–30 之间'); return; }
  const th = addNote && np.tech.has('th'), harm = th ? parseInt($('npHarm').value, 10) : 0, maxTap = INSTRUMENT.max_tap_fret;
  if (th && !(harm > fret && harm <= maxTap)) { npError(`点弦泛音点的品要比按的品高、最多 ${maxTap} 品`); return; }
  const bar = get('song').bars[np.bi], events = structuredClone(barEvents(np.bi)), ev = events[np.ei];
  if (!bar || bar.id !== np.barId || !ev || (np.ni >= 0 && !ev.notes[np.ni])) { closeNotePop(); UI.toast('谱子刚更新过，请重新点这个音', 'info'); return; }
  const old = np.ni >= 0 ? ev.notes[np.ni] : { grace: -1, bend: 0 };
  const tech = NP_TECH.filter(k => np.tech.has(k));
  if (old.tech && old.tech.includes('bend')) tech.push('bend'); // 推弦不在面板里，原样保留
  const n = { ...old, string: np.string, fret, tech };
  if (th) n.harm = harm; else delete n.harm; // harm 只有点弦泛音才有
  const retimed = durToTicks(np.dur) !== durToTicks(ev.dur);
  const chord = $('npChord').value.trim().slice(0, 24);
  const beatSame = chord === (ev.chord || '') && np.strum === (ev.strum || '');
  const same = beatSame && !retimed && (!addNote || np.ni >= 0 && old.string === n.string && old.fret === n.fret && (old.harm || 0) === (n.harm || 0) && [...(old.tech || [])].sort().join() === [...tech].sort().join());
  if (chord) ev.chord = chord; else delete ev.chord;
  if (np.strum) ev.strum = np.strum; else delete ev.strum;
  if (same) { closeNotePop(); return; } // 没改就不保存（保存会清掉这一小节的 AI 不确定说明）
  if (np.ni >= 0) ev.notes[np.ni] = n; else if (addNote) ev.notes.push(n);
  ev.notes.sort((a, b) => a.string - b.string);
  let out = events;
  try { out = retimeEvent(events, np.ei, np.dur); } catch (e) { npError(e.message); return; }
  const where = `第 ${np.bi + 1} 小节`, rev = np.rev;
  closeNotePop();
  await putBar(bar.id, out, rev, `已修改${where}`);
}
async function deleteNotePop() {
  if (!np || np.ni < 0 || barBusy(np.barId)) return;
  const bar = get('song').bars[np.bi], events = structuredClone(barEvents(np.bi));
  if (!bar || bar.id !== np.barId || !events[np.ei] || !events[np.ei].notes[np.ni]) { closeNotePop(); return; }
  events[np.ei].notes.splice(np.ni, 1);
  const where = `第 ${np.bi + 1} 小节`, rev = np.rev;
  closeNotePop();
  await putBar(bar.id, events, rev, `已删除${where}的一个音`);
}

/** 小节卡片的“改音”：进入改音模式，谱面滚到这一小节并闪一下 */
function editThisBar() {
  const bi = get('ui.bar');
  if (bi < 0) return;
  const locked = barLocked(bi);
  if (locked) { UI.toast(locked, 'info'); return; }
  if (get('ui.editMode')) { setEditMode(false); return; }
  setEditMode(true);
  if (get('ui.drawer')) set('ui.drawer', false); // 窄屏：抽屉挡着谱面
  const box = barBox(bi), score = $('score');
  if (!box) return;
  const t = $('transport'), stuck = parseFloat(getComputedStyle(t).top) + t.offsetHeight;
  const y = scrollY + score.getBoundingClientRect().top + box.y;
  if (y < scrollY + stuck || y + box.h > scrollY + innerHeight - 60) scrollTo({ top: y - stuck - 24, behavior: 'smooth' });
  const f = document.createElement('div');
  f.className = 'bar-flash';
  Object.assign(f.style, { left: box.x + 'px', top: box.y + 'px', width: box.w + 'px', height: box.h + 'px' });
  score.appendChild(f);
  setTimeout(() => f.remove(), 1600);
}

export const editorState = { get saving() { return saving; }, get undo() { return undoStack.length; }, get undoBars() { return undoStack.map(u => u.barId); }, get undoRevs() { return undoStack.map(u => u.rev); }, get np() { return np; } };

export function initEditor() {
  provide('score.editClick', queueEditClick);
  holdBarWhile(() => editorBarId !== null); // 编辑器开着时侧栏一切小节，编辑器就关了，没保存的内容会丢
  $('editBtn').onclick = () => {
    const bar = curBar();
    if (!bar) return;
    const locked = barLocked(get('ui.bar'));
    if (locked) { UI.toast(locked, 'info'); return; }
    fillEditor(bar);
    $('editor').hidden = false;
    $('edText').focus();
  };
  $('edCancel').onclick = closeEditor;
  $('edSave').onclick = () => withBusy($('edSave'), async () => {
    let events;
    try { events = JSON.parse($('edText').value); } catch (e) { UI.toast('JSON 格式不对：' + e.message, 'err', 4000); return; }
    const r = await putBar(editorBarId, events, editorRev, '已保存');
    if (r === 'ok' || r === 'gone') closeEditor();
    else if (r === 'conflict') { // 别处改过：换成刷新后的内容和版本，不然之后每次保存都是 409
      const bar = get('song').bars.find(b => b.id === editorBarId);
      if (bar) fillEditor(bar); else closeEditor();
    }
  });
  $('reparseBtn').onclick = async () => {
    const bar = curBar();
    if (!bar) return;
    const si = bar.system, row = get('song').bars.filter(b => b.system === si).map(b => b.n);
    const ok = await UI.confirm('这一行的手工修改、提示和问答都会被替换。', { title: `重新识别第 ${row[0]}–${row[row.length - 1]} 小节？`, ok: '重新识别' });
    if (!ok) return;
    await withBusy($('reparseBtn'), async () => {
      try {
        await post(songUrl(`/systems/${si}/reparse`), { hint: $('hintInput').value });
        UI.toast('已开始重新识别这一行', 'info');
        $('hintInput').value = '';
        $('hintInput').dispatchEvent(new Event('input'));
      } catch (e) { UI.toast(e.message, 'err', 5000); }
    });
    await refresh();
  };

  // 谱面上改音
  $('npTech').innerHTML = NP_TECH.map(k => `<button data-t="${k}" aria-pressed="false">${TECH_NAME[k]}</button>`).join('');
  $('npDur').innerHTML = DUR_CHOICES.map(([d, l]) => `<button data-d="${d}" aria-pressed="false">${l}</button>`).join('');
  setNpStr = UI.seg($('npStr'), '1', v => { np.string = +v; renderNotePop(); });
  setNpStrum = UI.seg($('npStrum'), '', v => { np.strum = v; });
  $('npChord').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); saveNotePop(); }
  });
  $('editModeBtn').onclick = () => setEditMode(false);
  $('barEditBtn').onclick = editThisBar;
  $('npClose').onclick = closeNotePop;
  $('npSave').onclick = saveNotePop;
  $('npDel').onclick = deleteNotePop;
  $('npAdd').onclick = () => { const ev = barEvents(np.bi)[np.ei]; np = { ...np, ni: -1, string: freeString(ev, np.string), fret: 0, tech: new Set() }; $('npFret').value = 0; $('npHarm').value = ''; renderNotePop(); $('npFret').focus(); $('npFret').select(); };
  $('npFretDown').onclick = () => npStep(-1);
  $('npFretUp').onclick = () => npStep(1);
  $('npFret').addEventListener('input', () => { if (!np) return; np.fret = $('npFret').value === '' ? '' : parseInt($('npFret').value, 10); renderNotePop(); });
  $('npDur').onclick = e => {
    const b = e.target.closest('button[data-d]'); if (!b || !np) return;
    np.dur = b.dataset.d;
    renderNotePop();
  };
  $('npTech').onclick = e => {
    const b = e.target.closest('button[data-t]'); if (!b || !np) return;
    const k = b.dataset.t;
    if (np.tech.has(k)) np.tech.delete(k);
    else {
      np.tech.add(k);
      // 互斥的几组：击弦 / 勾弦 / 滑音 / 滑入只能有一个；几种泛音只能有一个
      for (const g of [['h', 'p', 'sl', 'si'], ['nh', 'ah', 'th']]) if (g.includes(k)) g.forEach(o => o !== k && np.tech.delete(o));
      // 点弦泛音还没写点哪一品：先填最常见的高 12 品
      if (k === 'th' && $('npHarm').value === '') $('npHarm').value = Math.min((parseInt($('npFret').value, 10) || 0) + 12, INSTRUMENT.max_tap_fret);
    }
    renderNotePop();
  };
  for (const id of ['npFret', 'npHarm']) $(id).addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); saveNotePop(); }
  });
  $('score').addEventListener('mousedown', e => { pointer.x = e.clientX; pointer.y = e.clientY; }, true);
  // ⌘Z / Ctrl+Z 撤销：只在练习页、改音模式里、没有浮层（改音面板也是浮层）、不在输入框里（这些判断在 keys.js）
  addShortcuts({ name: 'undo', order: 10, mod: true, when: () => !!get('ui.editMode') && !np, keys: { z: e => (e.shiftKey ? false : (undoEdit(), true)) } });
  // 离开练习页（调弦、练习建议…）就退出改音模式：回来时谱面上点一下是播放，不会意外改音
  on('ui.view', v => { if (v !== 'practice' && get('ui.editMode')) setEditMode(false); });

  on('ui.editMode', paintEditMode);
  paintEditMode(false);
  on(['ui.bar', 'song.bars', 'song.systems', 'song.busy'], updateBarTools);
  // 正在改的小节被删掉或内容变了才关面板；别的刷新（比如后台轮询）不打断
  on('song.bars', bars => {
    if (!np) return;
    const k = bars.findIndex(b => b.id === np.barId);
    if (k < 0 || JSON.stringify(bars[k].events) !== JSON.stringify(np.snap)) closeNotePop(); else np.bi = k;
  });
  on('song.can_edit_bars', v => {
    const can = !!v;
    $('fixBox').hidden = !can;
    if (!can && get('ui.editMode')) setEditMode(false);
  });
}
