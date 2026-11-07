// 修改谱子（只对识别出来的曲子）：小节的 JSON 编辑器、重新识别一行、在谱面上点音符改音，以及撤销。

import { get, set, on } from './store.js';
import { $, UI, setButton, songUrl } from './dom.js';
import { put, post, withBusy } from '../lib/api.js';
import { DUR_CHOICES, durToTicks, eventIndexAt, retimeEvent } from '../score.js';
import { TECH_NAME, STRINGS } from '../vocab.js';
import { stop, setPlayerHooks } from './player.js';
import { refresh } from './song.js';

// ---------------------------------------------------------------------
// 修改小节（JSON）、重新识别这一行
// ---------------------------------------------------------------------
let editorBarId = null;
const curBar = () => { const s = get('song'), bi = get('ui.bar'); return s && bi >= 0 ? s.bars[bi] : null; };
const busy = s => !!(s.busy ?? (s.running || s.status === 'parsing' || s.status === 'queued'));

/** 这个小节现在不能改的原因（后端给了 locked 就用它）：改了会被正在进行的识别结果覆盖 */
export function barLocked(bi) {
  const s = get('song'), bar = s && s.bars[bi];
  if (!bar) return '';
  if (bar.locked !== undefined) return bar.locked || '';
  if (s.restaging) return '正在重新识别整首，完成后再改';
  const sys = s.systems[bar.system];
  return sys && sys.status === 'parsing' ? '这一行正在重新识别，完成后再改' : '';
}
function closeEditor() { $('editor').hidden = true; $('edMsg').textContent = ''; editorBarId = null; }
function updateBarTools() {
  const s = get('song'), bar = curBar();
  if (!bar) return;
  if (bar.id !== editorBarId) closeEditor();
  const sys = s.systems[bar.system], parsing = sys && sys.status === 'parsing';
  $('reparseBtn').disabled = !sys || parsing || busy(s);
  setButton($('reparseBtn'), parsing ? '<span class="spinner"></span>正在重新识别…' : UI.icon('refresh') + '重新识别这一行');
}

// 保存一个小节；记下改之前的内容，⌘Z / Ctrl+Z 可以撤销
export const undoStack = [];
export let saving = 0; // 进行中的保存：这期间不能撤销，否则可能撤销错步骤
async function putBar(barId, events, msg, isUndo = false) {
  const s = get('song'), before = s.bars.find(b => b.id === barId);
  const entry = before && !isUndo ? { barId, events: before.events, n: before.n } : null;
  if (entry) { undoStack.push(entry); if (undoStack.length > 50) undoStack.shift(); } // 先记上，按发出的顺序
  saving++;
  try {
    const body = { events };
    if (s.rev !== undefined) body.rev = s.rev;
    await put(songUrl(`/bars/${encodeURIComponent(barId)}`), body);
  } catch (e) {
    if (entry) undoStack.splice(undoStack.lastIndexOf(entry), 1);
    UI.toast(e.message || '保存失败', 'err', 4000);
    if (e.status === 409) await refresh();
    return false;
  } finally { saving--; }
  await refresh();
  UI.toast(msg + (isUndo ? '' : (get('ui.editMode') ? `（${UNDO_KEY} 撤销）` : '')));
  return true;
}
async function undoEdit() {
  if (saving) { UI.toast('正在保存，稍等一下再撤销', 'info'); return; }
  const u = undoStack.pop();
  if (!u) { UI.toast('没有可以撤销的修改', 'info'); return; }
  if (!(await putBar(u.barId, u.events, `已撤销第 ${u.n} 小节的修改`, true))) undoStack.push(u); // 撤销失败：记录放回去
}

// ---------------------------------------------------------------------
// 在谱面上改音（只对识别出来的曲子：GP / MusicXML 导入的本来就是精确数据，播放也直接用原文件）
// ---------------------------------------------------------------------
const IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
const UNDO_KEY = IS_MAC ? '⌘Z' : 'Ctrl+Z';
const LEGEND_PLAY = '点小节 = 循环这一小节 · <kbd>Shift</kbd> + 点 = 扩展循环 · 点音符 = 试听 · <span class="review-key"></span> 待核对';
const LEGEND_EDIT = '改音：点音符改弦、品、技巧和时值 · 点休止符加音 · <kbd>Enter</kbd> 保存 · <kbd>Esc</kbd> 关闭 · <kbd>' + UNDO_KEY + '</kbd> 撤销';
// 改音面板里的技巧（推弦不在面板里，原样保留）
const NP_TECH = ['h', 'p', 'sl', 'si', 'pm', 'x', 'nh', 'ah', 'vib', 'roll'];
let np = null, npLayer = null, pendingClick = null, setNpStr = null;
const pointer = { x: 0, y: 0 };

export function setEditMode(v) {
  const s = get('song');
  set('ui.editMode', !!v && !!s && s.kind !== 'score' && !!(s.can_edit_bars ?? true));
}
function paintEditMode(on_) {
  $('editModeBtn').setAttribute('aria-pressed', on_);
  $('scoreCard').classList.toggle('editing', on_);
  $('scoreLegend').innerHTML = on_ ? LEGEND_EDIT : LEGEND_PLAY;
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
  if (beat.graceType) { UI.toast('倚音请在“识别有误？→ 修改本小节”里改', 'info'); return; }
  const ei = eventIndexAt(get('view').beatMap[bi] || [0], beat.playbackStart);
  const ev = barEvents(bi)[ei];
  if (!ev) return;
  const ni = note ? ev.notes.findIndex(n => n.string === STRINGS + 1 - note.string) : -1; // alphaTab 的弦号 1 = 最低音弦
  const cur = ni >= 0 ? ev.notes[ni] : null;
  if (npLayer) npLayer.close('replaced'); // 点了另一个音：换成新的
  np = { bi, ei, ni, barId: s.bars[bi].id, snap: s.bars[bi].events, string: cur ? cur.string : freeString(ev, note ? STRINGS + 1 - note.string : 1), fret: cur ? cur.fret : 0, tech: new Set(cur ? cur.tech : []), dur: ev.dur };
  set('ui.bar', bi);
  renderNotePop();
  $('npFret').value = np.fret;
  const card = $('scoreCard').getBoundingClientRect(), pop = $('notePop');
  pop.hidden = false;
  npLayer = UI.layer(pop, { ignore: [$('score')], focus: $('npFret'), onClose: () => { npLayer = null; np = null; pop.hidden = true; } });
  const x = Math.max(8, Math.min(pointer.x - card.left + 14, card.width - pop.offsetWidth - 8));
  const y = pointer.y - card.top + 14;
  pop.style.left = x + 'px'; pop.style.top = y + 'px';
  setTimeout(() => $('npFret').select(), 0);
}
function freeString(ev, prefer) {
  const used = new Set(ev.notes.map(n => n.string));
  if (!used.has(prefer)) return prefer;
  return Array.from({ length: STRINGS }, (_, i) => i + 1).find(x => !used.has(x)) || prefer;
}
function npConflict() {
  const ev = barEvents(np.bi)[np.ei];
  return ev.notes.some((n, i) => i !== np.ni && n.string === np.string);
}
function renderNotePop() {
  const add = np.ni < 0;
  $('npTitle').textContent = add ? '在这一拍加一个音' : '修改这个音';
  $('npSub').textContent = `第 ${np.bi + 1} 小节 · 第 ${np.ei + 1} 个音`;
  setNpStr(String(np.string));
  $('npTech').querySelectorAll('button').forEach(b => { const v = np.tech.has(b.dataset.t); b.classList.toggle('on', v); b.setAttribute('aria-pressed', v); });
  $('npDel').hidden = $('npAdd').hidden = add;
  const curT = durToTicks(np.dur);
  let durOk = true;
  $('npDur').querySelectorAll('button').forEach(b => { const v = durToTicks(b.dataset.d) === curT; b.classList.toggle('on', v); b.setAttribute('aria-pressed', v); });
  let msg = '';
  try { retimeEvent(barEvents(np.bi), np.ei, np.dur); } catch (e) { durOk = false; msg = e.message; }
  const clash = npConflict();
  if (clash) msg = `这一拍 ${np.string} 弦已经有音了`;
  $('npMsg').textContent = msg;
  $('npSave').disabled = clash || !durOk;
}
function closeNotePop() { if (npLayer) npLayer.close(); np = null; $('notePop').hidden = true; }
const npStep = d => { if (!np) return; np.fret = Math.max(0, Math.min(30, (parseInt($('npFret').value, 10) || 0) + d)); $('npFret').value = np.fret; };

async function saveNotePop() {
  if (!np || npConflict()) return;
  const fret = parseInt($('npFret').value, 10);
  if (!(fret >= 0 && fret <= 30)) { $('npMsg').textContent = '品位要在 0–30 之间'; return; }
  const bar = get('song').bars[np.bi], events = structuredClone(barEvents(np.bi)), ev = events[np.ei];
  if (!bar || bar.id !== np.barId || !ev || (np.ni >= 0 && !ev.notes[np.ni])) { closeNotePop(); UI.toast('谱子刚更新过，请重新点这个音', 'info'); return; }
  const old = np.ni >= 0 ? ev.notes[np.ni] : { grace: -1, bend: 0 };
  const tech = NP_TECH.filter(k => np.tech.has(k));
  if (old.tech && old.tech.includes('bend')) tech.push('bend'); // 推弦不在面板里，原样保留
  const n = { ...old, string: np.string, fret, tech };
  const retimed = durToTicks(np.dur) !== durToTicks(ev.dur);
  const same = !retimed && np.ni >= 0 && old.string === n.string && old.fret === n.fret && [...(old.tech || [])].sort().join() === [...tech].sort().join();
  if (same) { closeNotePop(); return; } // 没改就不保存（保存会清掉这一小节的 AI 不确定说明）
  if (np.ni >= 0) ev.notes[np.ni] = n; else ev.notes.push(n);
  ev.notes.sort((a, b) => a.string - b.string);
  let out = events;
  try { out = retimeEvent(events, np.ei, np.dur); } catch (e) { $('npMsg').textContent = e.message; return; }
  const where = `第 ${np.bi + 1} 小节`;
  closeNotePop();
  await putBar(bar.id, out, `已修改${where}`);
}
async function deleteNotePop() {
  if (!np || np.ni < 0) return;
  const bar = get('song').bars[np.bi], events = structuredClone(barEvents(np.bi));
  if (!bar || bar.id !== np.barId || !events[np.ei] || !events[np.ei].notes[np.ni]) { closeNotePop(); return; }
  events[np.ei].notes.splice(np.ni, 1);
  const where = `第 ${np.bi + 1} 小节`;
  closeNotePop();
  await putBar(bar.id, events, `已删除${where}的一个音`);
}

export const editorState = { get saving() { return saving; }, get undo() { return undoStack.length; }, get np() { return np; } };

export function initEditor() {
  setPlayerHooks({ editClick: queueEditClick });
  $('editBtn').onclick = () => {
    const bar = curBar();
    if (!bar) return;
    const locked = barLocked(get('ui.bar'));
    if (locked) { UI.toast(locked, 'info'); return; }
    $('edText').value = '[\n' + bar.events.map(e => '  ' + JSON.stringify(e)).join(',\n') + '\n]';
    editorBarId = bar.id;
    $('editor').hidden = false;
    $('edText').focus();
  };
  $('edCancel').onclick = closeEditor;
  $('edSave').onclick = () => withBusy($('edSave'), async () => {
    let events;
    try { events = JSON.parse($('edText').value); } catch (e) { UI.toast('JSON 格式不对：' + e.message, 'err', 4000); return; }
    if (await putBar(editorBarId, events, '已保存')) closeEditor();
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
      } catch (e) { UI.toast(e.message, 'err', 5000); }
    });
    await refresh();
  };

  // 谱面上改音
  $('npTech').innerHTML = NP_TECH.map(k => `<button data-t="${k}" aria-pressed="false">${TECH_NAME[k]}</button>`).join('');
  $('npDur').innerHTML = DUR_CHOICES.map(([d, l]) => `<button data-d="${d}" aria-pressed="false">${l}</button>`).join('');
  setNpStr = UI.seg($('npStr'), '1', v => { np.string = +v; renderNotePop(); });
  $('editModeBtn').onclick = () => setEditMode(!get('ui.editMode'));
  $('npClose').onclick = closeNotePop;
  $('npSave').onclick = saveNotePop;
  $('npDel').onclick = deleteNotePop;
  $('npAdd').onclick = () => { const ev = barEvents(np.bi)[np.ei]; np = { ...np, ni: -1, string: freeString(ev, np.string), fret: 0, tech: new Set() }; renderNotePop(); $('npFret').value = 0; $('npFret').focus(); $('npFret').select(); };
  $('npFretDown').onclick = () => npStep(-1);
  $('npFretUp').onclick = () => npStep(1);
  $('npFret').addEventListener('input', () => { if (np) np.fret = $('npFret').value === '' ? '' : parseInt($('npFret').value, 10); });
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
      // 互斥的几组：击弦 / 勾弦 / 滑音 / 滑入只能有一个；两种泛音只能有一个
      for (const g of [['h', 'p', 'sl', 'si'], ['nh', 'ah']]) if (g.includes(k)) g.forEach(o => o !== k && np.tech.delete(o));
    }
    renderNotePop();
  };
  $('npFret').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); saveNotePop(); }
  });
  $('score').addEventListener('mousedown', e => { pointer.x = e.clientX; pointer.y = e.clientY; }, true);
  document.addEventListener('keydown', e => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'z' && !e.shiftKey && get('ui.editMode') && !np && !(e.target.closest && e.target.closest('input, textarea'))) { e.preventDefault(); undoEdit(); }
  });

  on('ui.editMode', paintEditMode);
  paintEditMode(false);
  on(['ui.bar', 'song'], updateBarTools);
  // 正在改的小节被删掉或内容变了才关面板；别的刷新（比如后台轮询）不打断
  on('song.bars', bars => {
    if (!np) return;
    const k = bars.findIndex(b => b.id === np.barId);
    if (k < 0 || JSON.stringify(bars[k].events) !== JSON.stringify(np.snap)) closeNotePop(); else np.bi = k;
  });
  on('song', s => {
    const can = s && s.kind !== 'score' && (s.can_edit_bars ?? true);
    $('fixBox').hidden = !can;
    $('editModeBtn').hidden = !can;
    if (!can && get('ui.editMode')) setEditMode(false);
  });
}
