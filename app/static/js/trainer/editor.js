// 修改谱子（只对识别出来的曲子）：小节的 JSON 编辑器、重新识别一行、在谱面上点音符改音，以及撤销

// ---------------------------------------------------------------------
// 修改小节（JSON）、重新识别这一行
// ---------------------------------------------------------------------
let editorBarId = null;
function closeEditor() { $('editor').style.display = 'none'; $('edMsg').textContent = ''; editorBarId = null; }
function updateBarTools() {
  if (shownBar < 0 || !SONG || !SONG.bars[shownBar]) return;
  const sys = SONG.systems[SONG.bars[shownBar].system];
  const busy = SONG.running || SONG.status === 'parsing' || SONG.status === 'queued';
  $('reparseBtn').disabled = !sys || sys.status === 'parsing' || busy;
  setButton($('reparseBtn'), sys && sys.status === 'parsing' ? '<span class="spinner"></span>正在重新识别…' : UI.icon('refresh') + '重新识别这一行');
}

// 保存一个小节；记下改之前的内容，⌘Z / Ctrl+Z 可以撤销
const undoStack = [];
let saving = 0; // 进行中的保存：这期间不能撤销，否则可能撤销错步骤
async function putBar(barId, events, msg, isUndo = false) {
  const before = SONG.bars.find(b => b.id === barId);
  const entry = before && !isUndo ? { barId, events: before.events, n: before.n } : null;
  if (entry) { undoStack.push(entry); if (undoStack.length > 50) undoStack.shift(); } // 先记上，按发出的顺序
  saving++;
  try {
    const r = await fetch(`/api/songs/${SONG_ID}/bars/${encodeURIComponent(barId)}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ events }) });
    if (!r.ok) {
      if (entry) undoStack.splice(undoStack.lastIndexOf(entry), 1);
      UI.toast((await r.json().catch(() => ({}))).detail || '保存失败', 'err', 4000);
      return false;
    }
  } finally { saving--; }
  await refresh(true);
  UI.toast(msg + (isUndo ? '' : (editMode ? `（${UNDO_KEY} 撤销）` : '')));
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
const LEGEND_PLAY = '点击小节 = 只循环这一小节 · <kbd>Shift</kbd> + 点击 = 扩展循环范围 · 点击音符 = 试听 · 淡橙色框 = 可能识别有误';
const LEGEND_EDIT = '改音模式：点击音符修改弦、品、技巧和时值 · 点击休止符在这一拍加音 · 输入品位后按 <kbd>Enter</kbd> 保存，<kbd>Esc</kbd> 关闭 · <kbd>' + UNDO_KEY + '</kbd> 撤销';
const NP_TECH = [['h', '击弦'], ['p', '勾弦'], ['sl', '滑音'], ['si', '滑入'], ['pm', '闷音'], ['x', '死音'], ['nh', '泛音'], ['ah', '人工泛音'], ['vib', '揉弦'], ['roll', '琶音']];
let editMode = false, np = null, pendingClick = null;
const pointer = { x: 0, y: 0 };

function setEditMode(on) {
  editMode = !!on && !!SONG && SONG.kind !== 'score';
  $('editModeBtn').classList.toggle('on', editMode);
  document.querySelector('.score-card').classList.toggle('editing', editMode);
  $('scoreLegend').innerHTML = editMode ? LEGEND_EDIT : LEGEND_PLAY;
  if (!editMode) closeNotePop();
  else if (state.playing) stop();
}
// 点音符时 alphaTab 会先后触发拍和音符两个事件：攒到同一轮里，有音符就改音符，没有就是在这一拍加音
function queueEditClick(beat, note) {
  if (pendingClick) { if (note) pendingClick.note = note; return; }
  pendingClick = { beat, note };
  setTimeout(() => { const c = pendingClick; pendingClick = null; openNotePop(c.beat, c.note); }, 0);
}
const barEvents = bi => { const b = SONG.bars[bi]; return b.events && b.events.length ? b.events : [{ dur: String(barLen(bi)), notes: [] }]; };

function openNotePop(beat, note) {
  const bi = beat.voice.bar.index;
  if (bi >= SONG.bars.length) return;
  if (beat.graceType) { UI.toast('倚音请在小节说明里的“识别有误？→ 修改本小节”里改', 'info'); return; }
  const ei = scoreConv.eventIndexAt(beatMap[bi] || [0], beat.playbackStart);
  const ev = barEvents(bi)[ei];
  if (!ev) return;
  const ni = note ? ev.notes.findIndex(n => n.string === 7 - note.string) : -1; // alphaTab 的弦号 1 = 最低音弦
  const cur = ni >= 0 ? ev.notes[ni] : null;
  np = { bi, ei, ni, barId: SONG.bars[bi].id, snap: JSON.stringify(SONG.bars[bi].events), string: cur ? cur.string : freeString(ev, note ? 7 - note.string : 1), fret: cur ? cur.fret : 0, tech: new Set(cur ? cur.tech : []), dur: ev.dur };
  if (!$('npDur').children.length) $('npDur').innerHTML = scoreConv.DUR_CHOICES.map(([d, l]) => `<button data-d="${d}">${l}</button>`).join('');
  showBar(bi);
  renderNotePop();
  $('npFret').value = np.fret;
  const card = document.querySelector('.score-card').getBoundingClientRect(), pop = $('notePop');
  pop.hidden = false;
  const x = Math.max(8, Math.min(pointer.x - card.left + 14, card.width - pop.offsetWidth - 8));
  const y = pointer.y - card.top + 14;
  pop.style.left = x + 'px'; pop.style.top = y + 'px';
  $('npFret').focus(); $('npFret').select();
}
function freeString(ev, prefer) {
  const used = new Set(ev.notes.map(n => n.string));
  if (!used.has(prefer)) return prefer;
  return [1, 2, 3, 4, 5, 6].find(s => !used.has(s)) || prefer;
}
function npConflict() {
  const ev = barEvents(np.bi)[np.ei];
  return ev.notes.some((n, i) => i !== np.ni && n.string === np.string);
}
function renderNotePop() {
  const add = np.ni < 0;
  $('npTitle').textContent = add ? '在这一拍加一个音' : '修改这个音';
  $('npSub').textContent = `第 ${np.bi + 1} 小节 · 从左数第 ${np.ei + 1} 个音`;
  setNpStr(String(np.string));
  $('npTech').querySelectorAll('button').forEach(b => b.classList.toggle('on', np.tech.has(b.dataset.t)));
  $('npDel').hidden = $('npAdd').hidden = add;
  const curT = scoreConv.durToTicks(np.dur);
  let durOk = true;
  $('npDur').querySelectorAll('button').forEach(b => {
    b.classList.toggle('on', scoreConv.durToTicks(b.dataset.d) === curT);
  });
  let msg = '';
  try { scoreConv.retimeEvent(barEvents(np.bi), np.ei, np.dur); } catch (e) { durOk = false; msg = e.message; }
  const clash = npConflict();
  if (clash) msg = `这一拍的 ${np.string} 弦上已经有一个音了，换一根弦或去改那个音`;
  $('npMsg').textContent = msg;
  $('npSave').disabled = clash || !durOk;
}
let setNpStr; // 弦号分段开关的 set 函数（initEditor 里建）
function closeNotePop() { np = null; $('notePop').hidden = true; }
const npStep = d => { if (!np) return; np.fret = Math.max(0, Math.min(30, (parseInt($('npFret').value, 10) || 0) + d)); $('npFret').value = np.fret; };

async function saveNotePop() {
  if (!np || npConflict()) return;
  const fret = parseInt($('npFret').value, 10);
  if (!(fret >= 0 && fret <= 30)) { $('npMsg').textContent = '品位要在 0–30 之间'; return; }
  const bar = SONG.bars[np.bi], events = structuredClone(barEvents(np.bi)), ev = events[np.ei];
  if (!bar || bar.id !== np.barId || !ev || (np.ni >= 0 && !ev.notes[np.ni])) { closeNotePop(); UI.toast('谱子刚刚更新过，请重新点一下这个音', 'info'); return; }
  const old = np.ni >= 0 ? ev.notes[np.ni] : { grace: -1, bend: 0 };
  const tech = NP_TECH.map(t => t[0]).filter(k => np.tech.has(k));
  if (old.tech && old.tech.includes('bend')) tech.push('bend'); // 推弦不在面板里，原样保留
  const n = { ...old, string: np.string, fret, tech };
  const retimed = scoreConv.durToTicks(np.dur) !== scoreConv.durToTicks(ev.dur);
  const same = !retimed && np.ni >= 0 && old.string === n.string && old.fret === n.fret && [...(old.tech || [])].sort().join() === [...tech].sort().join();
  if (same) { closeNotePop(); return; } // 没改就不保存（保存会清掉这一小节的 AI 不确定说明）
  if (np.ni >= 0) ev.notes[np.ni] = n; else ev.notes.push(n);
  ev.notes.sort((a, b) => a.string - b.string);
  let out = events;
  try { out = scoreConv.retimeEvent(events, np.ei, np.dur); } catch (e) { $('npMsg').textContent = e.message; return; }
  const where = `第 ${np.bi + 1} 小节`;
  closeNotePop();
  await putBar(bar.id, out, `已修改${where}`);
}
async function deleteNotePop() {
  if (!np || np.ni < 0) return;
  const bar = SONG.bars[np.bi], events = structuredClone(barEvents(np.bi));
  if (!bar || bar.id !== np.barId || !events[np.ei] || !events[np.ei].notes[np.ni]) { closeNotePop(); return; }
  events[np.ei].notes.splice(np.ni, 1);
  const where = `第 ${np.bi + 1} 小节`;
  closeNotePop();
  await putBar(bar.id, events, `已删除${where}的一个音`);
}

function initEditor() {
  $('editBtn').onclick = () => {
    if (shownBar < 0) return;
    const evs = SONG.bars[shownBar].events;
    $('edText').value = '[\n' + evs.map(e => '  ' + JSON.stringify(e)).join(',\n') + '\n]';
    editorBarId = SONG.bars[shownBar].id;
    $('editor').style.display = '';
  };
  $('edCancel').onclick = closeEditor;
  $('edSave').onclick = async () => {
    let events;
    try { events = JSON.parse($('edText').value); } catch (e) { UI.toast('JSON 格式不对：' + e.message, 'err', 4000); return; }
    if (await putBar(editorBarId, events, '已保存')) closeEditor();
  };
  $('reparseBtn').onclick = async () => {
    if (shownBar < 0) return;
    const si = SONG.bars[shownBar].system;
    const row = SONG.bars.filter(b => b.system === si).map(b => b.n);
    if (!confirm(`重新识别第 ${row[0]}–${row[row.length - 1]} 小节这一行？\n\n会调用一次模型（产生少量费用），这一行里手工改过的音、AI 写的提示和问答都会被新的识别结果替换。`)) return;
    const r = await fetch(`/api/songs/${SONG_ID}/systems/${si}/reparse`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ hint: $('hintInput').value }) });
    if (!r.ok) { UI.toast((await r.json()).detail || '请求失败', 'err'); return; }
    UI.toast('已开始重新识别这一行', 'info');
    $('hintInput').value = '';
    await refresh(true);
  };

  // 谱面上改音
  $('npTech').innerHTML = NP_TECH.map(([k, l]) => `<button data-t="${k}">${l}</button>`).join('');
  $('scoreLegend').innerHTML = LEGEND_PLAY;
  setNpStr = UI.seg($('npStr'), '1', v => { np.string = +v; renderNotePop(); });
  $('editModeBtn').onclick = () => setEditMode(!editMode);
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
  document.addEventListener('mousedown', e => {
    if (np && !e.target.closest('#notePop, #score')) closeNotePop();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && np) { closeNotePop(); return; }
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'z' && !e.shiftKey && editMode && !np && !(e.target.closest && e.target.closest('input, textarea'))) { e.preventDefault(); undoEdit(); }
  });
}
