// 曲库页：上传原谱 / GP 文件、曲子列表（识别进度、练习记录、接着练）、曲子包导出 / 导入、管理原谱页面、设置（API key、模型、主题）。
// 词表：曲子 = 曲库里的一条；谱面 = 渲染出来的谱；原谱 = 上传的图片 / PDF。
import { UI, $, esc, icon, toast } from '/static/ui.js';
import * as api from '/static/js/lib/api.js';
import { SCORE_EXT, isScoreFile } from '/static/js/vocab.js';
import { isScore, selectable, rowHTML, sortSongs, filterSongs, SORTS, paginate, pagerHTML } from '/static/js/library-view.js';
import { refineConfirm, NO_REFINE } from '/static/js/lib/refine.js';

UI.initShell();

// ---------------- 导航 ----------------
const setNav = UI.tabs($('nav'), 'library', v => { location.hash = v; });
function route() {
  const v = location.hash.slice(1) === 'settings' ? 'settings' : 'library';
  setNav(v);
  $('view-library').hidden = v !== 'library';
  $('view-settings').hidden = v !== 'settings';
}
addEventListener('hashchange', route);

// ---------------- 主题 ----------------
const setThemeSeg = UI.seg($('themeSeg'), UI.getTheme(), v => UI.setTheme(v));
$('paletteSeg').innerHTML = UI.PALETTES.map(p => `<button data-v="${p.id}"><i class="swatch" style="background:${p.swatch}"></i>${p.name}</button>`).join('');
const setPaletteSeg = UI.seg($('paletteSeg'), UI.getPalette(), v => UI.setPalette(v));
document.addEventListener('themechange', e => { setThemeSeg(e.detail); setPaletteSeg(UI.getPalette()); });

// ---------------- 上传 ----------------
let picked = [];
const isZip = f => /\.zip$/i.test(f.name);
function setFiles(list) {
  const files = [...list]; // 先拷出来：清空文件框时，它的 FileList 可能跟着变空
  const zips = files.filter(isZip);
  if (zips.length) {  // 曲子包直接导入，不走“上传并识别”
    $('file').value = '';
    if (zips.length < files.length) return toast('曲子包（.zip）请单独导入，不要和原谱图片、GP 文件混在一起', 'err', 4000);
    return importBundles(zips);
  }
  picked = files.sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
  $('pickedBox').hidden = !picked.length;
  $('picked').innerHTML = picked.map(f => `<span class="badge">${icon(fileIcon(f), 12)}${esc(f.name)}</span>`).join('');
  const score = picked.length && picked.every(f => isScoreFile(f.name));
  $('uploadBtn').lastChild.textContent = score ? '导入' : '上传并识别';
}
const fileIcon = f => (f.type === 'application/pdf' || /\.pdf$/i.test(f.name) ? 'file' : f.type.startsWith('image/') ? 'image' : 'score');
$('file').accept = ['image/*', 'application/pdf', ...SCORE_EXT, '.zip'].join(','); // 曲谱扩展名和后端用同一份（vocab.json）
$('file').onchange = e => setFiles(e.target.files);
const drop = $('drop');
drop.onclick = () => $('file').click();
drop.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('file').click(); } };
drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
drop.ondragleave = () => drop.classList.remove('over');
drop.ondrop = e => { e.preventDefault(); drop.classList.remove('over'); setFiles(e.dataTransfer.files); };
// 文件没放进上传框就松手（落在页面别处、“管理原谱页面”对话框里）：Chrome 默认会打开这个文件、离开曲库，
// 对话框里调好没保存的页面顺序就丢了。上传框自己已经 preventDefault；别处一律拦下，光标显示“不能放”
const dragsFiles = e => !!e.dataTransfer && [...e.dataTransfer.types].includes('Files');
document.addEventListener('dragover', e => { if (!e.defaultPrevented && dragsFiles(e)) { e.preventDefault(); e.dataTransfer.dropEffect = 'none'; } });
document.addEventListener('drop', e => { if (dragsFiles(e)) e.preventDefault(); });
$('clearPick').onclick = () => { setFiles([]); $('file').value = ''; drop.focus(); };
$('uploadBtn').onclick = () => api.withBusy($('uploadBtn'), async () => {
  try {
    await api.upload('/api/songs', picked, { title: $('title').value });
    toast(picked.some(f => !isScoreFile(f.name)) ? '已上传，开始识别' : '已导入');
    setFiles([]); $('title').value = ''; $('file').value = '';
    setPage(1);
    songsPoll.now();
  } catch (e) {
    toast(e.message, 'err', 4000);
  }
});
$('title').onkeydown = e => { if (e.key === 'Enter' && !e.isComposing) $('uploadBtn').click(); }; // 输入法选词时的回车不算

// ---------------- 曲子包 ----------------
async function importBundles(files) {
  // 上一个包还在导入：withBusy 会直接忽略，这里说一声（不然拖进来的包悄悄没了）
  if ($('importBtn').getAttribute('aria-busy') === 'true') { toast('正在导入，稍后再试', 'err', 4000); return; }
  await api.withBusy($('importBtn'), async () => {
    toast('正在导入…', 'info', 60000);
    try {
      const r = await api.upload('/api/import', files);
      const n = r.imported.length, dup = r.skipped.filter(s => s.duplicate).length, bad = r.skipped.filter(s => !s.duplicate);
      const parts = [`已导入 ${n} 首`];
      if (dup) parts.push(`${dup} 首曲库里已有，跳过`);
      if (bad.length) parts.push(`${bad.length} 首无法导入：` + bad.slice(0, 3).map(s => `“${s.title}”${s.reason}`).join('；') + (bad.length > 3 ? ' 等' : ''));
      toast(parts.join('；'), bad.length ? (n ? 'info' : 'err') : 'ok', bad.length || dup ? 8000 : 2200);
    } catch (e) {
      toast(e.message, 'err', 5000);
    }
  });
  songsPoll.now();
}
$('importBtn').onclick = () => $('bundleFile').click();
$('bundleFile').onchange = e => { const f = [...e.target.files]; e.target.value = ''; if (f.length) importBundles(f); };

async function exportSongs(ids) {
  toast(`正在打包 ${ids.length} 首…`, 'info', 60000);
  try {
    const { blob, filename } = await api.download('/api/export?' + ids.map(id => 'ids=' + encodeURIComponent(id)).join('&'));
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename || '曲子包.zip';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    toast(`已导出 ${ids.length} 首`);
    return true;
  } catch (e) {
    toast(e.message, 'err', 5000);
    return false;
  }
}

// 选择模式：勾选曲子，一起导出成一个包
let selecting = false, lastSongs = [];
const selected = new Set();
function setSelecting(on) {
  selecting = on; selected.clear();
  $('selectBar').hidden = !on;
  $('selectBtn').hidden = on;
  renderSongs();
  (on ? $('selectAll') : $('selectBtn')).focus();
}
// 搜索 / 标签筛掉的曲子保留勾选（导出时一起导出），只去掉已经删除或不能选的
function updateSelectBar() {
  for (const id of [...selected]) { const s = lastSongs.find(x => x.id === id); if (!s || !selectable(s)) selected.delete(id); }
  const all = visibleSongs().filter(selectable), shown = all.filter(s => selected.has(s.id)).length, hidden = selected.size - shown;
  $('selectCount').textContent = `已选 ${selected.size} 首` + (hidden ? `（${hidden} 首不在当前列表里）` : '');
  $('selectAll').checked = all.length > 0 && shown === all.length;
  $('selectAll').indeterminate = shown > 0 && shown < all.length;
  $('exportBtn').disabled = !selected.size;
}
$('selectBtn').onclick = () => setSelecting(true);
$('selectCancel').onclick = () => setSelecting(false);
$('selectAll').onchange = e => {  // 只管当前列表里的
  visibleSongs().filter(selectable).forEach(s => (e.target.checked ? selected.add(s.id) : selected.delete(s.id)));
  renderSongs();
};
$('exportBtn').onclick = () => api.withBusy($('exportBtn'), async () => {
  const ids = sortSongs(lastSongs.filter(s => selected.has(s.id)), lib.sort).map(s => s.id);  // 按列表顺序（包括筛掉的）
  if (await exportSongs(ids)) setSelecting(false);
});
addEventListener('keydown', e => { if (e.key === 'Escape' && selecting && !UI.hasLayer()) setSelecting(false); });

// ---------------- 曲库列表（一行的 HTML、状态说明、排序等纯函数在 library-view.js） ----------------




// 只在内容变了的时候改 DOM（轮询每 2.5 秒刷新一次，不能把正在用的按钮、焦点冲掉）
function setHTML(el, html) { if (el._html !== html) { el.innerHTML = html; el._html = html; } }
function renderList(box, songs) {
  if (!songs.length) return;
  if (box._html !== undefined) { box.innerHTML = ''; box._html = undefined; } // 原来是空状态 / 加载中
  const active = document.activeElement, activeRow = box.contains(active) ? active.closest('.song-row') : null;
  const focusId = activeRow && activeRow.dataset.id, focusKey = active && active.dataset && active.dataset.k;
  const old = new Map([...box.children].map(r => [r.dataset.id, r]));
  let prev = null;
  for (const s of songs) {
    const html = rowHTML(s, { selecting, selected });
    let row = old.get(s.id);
    old.delete(s.id);
    if (!row || row._html !== html) {
      const t = document.createElement('template');
      t.innerHTML = html.trim();
      const fresh = t.content.firstElementChild;
      fresh._html = html;
      if (row) row.replaceWith(fresh);
      row = fresh;
    }
    const want = prev ? prev.nextElementSibling : box.firstElementChild;
    if (want !== row) box.insertBefore(row, want);
    prev = row;
  }
  old.forEach(r => r.remove());
  // 刚才有焦点的那一行被换掉了：把焦点放回新行里对应的控件
  if (focusId && !box.contains(document.activeElement)) {
    const row = [...box.children].find(r => r.dataset.id === focusId);
    const el = row && (focusKey ? [...row.querySelectorAll('[data-k]')].find(x => x.dataset.k === focusKey) : null);
    if (el) el.focus({ preventScroll: true });
  }
}

// 搜索、标签筛选、排序（只在浏览器里做；排序方式记在本机。网站存储被禁用时读写 localStorage 会抛错：当没记过，只在这次打开时生效）
const SORT_KEY = 'tt.libSort';
let savedSort = null;
try { savedSort = localStorage.getItem(SORT_KEY); } catch { /* 读不了就用默认排序 */ }
// 翻到第几页、按哪个标签筛选记在这条浏览记录里（history.state）：从练习页按“后退”回来还在原来那一页
const saved = (history.state && history.state.lib) || {};
const lib = { q: '', tag: typeof saved.tag === 'string' ? saved.tag : '', sort: SORTS.includes(savedSort) ? savedSort : 'practiced', page: Number(saved.page) || 1 };
function remember() {
  try { history.replaceState({ ...(history.state || {}), lib: { page: lib.page, tag: lib.tag } }, ''); } catch { /* 记不下就算了 */ }
}
function setPage(n) { lib.page = n; remember(); }
// 换了搜索词、标签、排序：回到第一页
function refilter(change) { Object.assign(lib, change, { page: 1 }); remember(); renderSongs(); }
UI.seg($('sortSeg'), lib.sort, v => {
  try { localStorage.setItem(SORT_KEY, v); } catch { /* 存不了就只在这次打开时生效 */ }
  refilter({ sort: v });
});
$('search').oninput = e => refilter({ q: e.target.value.trim().toLowerCase() });
$('tagFilter').onclick = e => { const b = e.target.closest('button'); if (b) refilter({ tag: b.dataset.tag }); };
const visibleSongs = () => filterSongs(lastSongs, lib);
$('pager').onclick = e => {
  const b = e.target.closest('button[data-page]');
  if (!b || b.disabled) return;
  const v = b.dataset.page, n = v === 'prev' ? lib.page - 1 : v === 'next' ? lib.page + 1 : Number(v);
  if (n === lib.page) return;
  setPage(n);
  renderSongs();
  // 焦点：上一页 / 下一页按钮留在原处（到头变成不可用时换到当前页码），点的页码换到新的当前页码
  const pager = $('pager');
  let el = v === 'prev' || v === 'next' ? pager.querySelector(`[data-page="${v}"]`) : null;
  if (!el || el.disabled) el = pager.querySelector('[aria-current="page"]');
  if (el) el.focus({ preventScroll: true });
  // 列表顶部翻出了屏幕：滚回到搜索栏，从这一页的第一首看起
  const top = ($('libTools').hidden ? $('songs') : $('libTools')).getBoundingClientRect().top;
  if (top < 0) ($('libTools').hidden ? $('songs') : $('libTools')).scrollIntoView({ block: 'start' });
};
function renderTagFilter() {
  const tags = [...new Set(lastSongs.flatMap(s => s.tags || []))].sort((a, b) => a.localeCompare(b, 'zh-CN'));
  if (lib.tag && !tags.includes(lib.tag)) { lib.tag = ''; lib.page = 1; remember(); }
  setHTML($('tagFilter'), tags.length ? ['', ...tags].map(t => `<button data-tag="${esc(t)}" aria-pressed="${t === lib.tag}">${t ? esc(t) : '全部'}</button>`).join('') : '');
  $('tagFilter').hidden = !tags.length;
}
function renderSongs() {
  const songs = visibleSongs(), box = $('songs');
  if (selecting) updateSelectBar();
  $('libTools').hidden = !lastSongs.length;
  renderTagFilter();
  box.removeAttribute('aria-busy');
  // 删掉了最后一页上的曲子、筛选后变少了：停在还有曲子的最后一页
  const pg = paginate(songs, lib.page);
  if (pg.page !== lib.page) setPage(pg.page);
  if (!lastSongs.length) setHTML(box, '<div class="empty-state"><b>还没有曲子</b>上传原谱或 Guitar Pro 文件开始。</div>');
  else if (!songs.length) setHTML(box, '<div class="empty-state"><b>没有符合条件的曲子</b>换个关键词或标签试试。</div>');
  else renderList(box, pg.items);
  const pager = pagerHTML(pg, songs.length);
  setHTML($('pager'), pager);
  $('pager').hidden = !pager;
}

async function loadSongs() {
  try {
    lastSongs = await api.get('/api/songs');
  } catch (e) {
    if (!lastSongs.length) setHTML($('songs'), `<div class="empty-state"><b>加载失败</b>${esc(e.message)}</div>`);
    throw e; // 交给 poll 退避重试
  }
  renderSongs();
  return lastSongs;
}
// 有曲子在后台忙（识别、讲解）就每 2.5 秒刷新；做完操作后 songsPoll.now() 立刻刷新
const songsPoll = api.poll(loadSongs, { interval: 2500, while: songs => songs.some(s => s.busy) });
// 没有曲子在忙时轮询就停了：从练习页按“后退”回来（Chrome 从往返缓存原样恢复页面）、
// 在别的标签页练过再切回来，都立刻刷新一次（接着练的位置、在练习页改的曲名）
addEventListener('pageshow', e => { if (e.persisted) songsPoll.now(); });
document.addEventListener('visibilitychange', () => { if (!document.hidden) songsPoll.now(); });

$('songs').addEventListener('click', e => {
  // 标签先处理：选择模式里点标签也是筛选（勾选会保留），不是勾选这一行
  const tag = e.target.closest('.song-row [data-tag]');
  if (tag) { e.stopPropagation(); refilter({ tag: tag.dataset.tag }); return; }
  if (selecting) {
    const row = e.target.closest('.row[data-pick="1"]');
    if (row) {
      e.preventDefault();
      const id = row.dataset.id;
      if (selected.has(id)) selected.delete(id); else selected.add(id);
      renderSongs();
    }
    return;
  }
  const more = e.target.closest('[data-more]');
  if (more) { e.stopPropagation(); openMore(more); return; }
  const retry = e.target.closest('[data-retry]');
  if (retry) { const s = lastSongs.find(x => x.id === retry.dataset.retry); if (s) reparse(s); return; }
  const rf = e.target.closest('[data-retry-failed]');
  if (rf) { const s = lastSongs.find(x => x.id === rf.dataset.retryFailed); if (s) retryFailed(s); return; }
  if (e.target.closest('a,button')) return;
  const row = e.target.closest('.row[data-play="1"]');
  if (row) location.href = row.querySelector('a.title').href;
});
function openMore(btn) {
  const id = btn.dataset.more, s = lastSongs.find(x => x.id === id);
  if (!s) return;
  if (s.busy) {
    const kind = s.activity && s.activity.kind;
    UI.menu(btn, [{ label: s.recognizing ? '取消识别' : kind === 'refine' ? '取消精修' : '取消生成讲解', icon: 'x', onClick: () => cancel(id) }]);
    return;
  }
  if (s.status === 'corrupt') {
    UI.menu(btn, [{ label: '删除', icon: 'trash', danger: true, onClick: () => remove(s) }]);
    return;
  }
  const image = !isScore(s); // GP / MusicXML 没有原谱页面，也不需要识别
  UI.menu(btn, [
    { label: '重命名', icon: 'edit', onClick: () => rename(s) },
    { label: '编辑标签', icon: 'list', onClick: () => editTags(s) },
    image && { label: '管理原谱页面', icon: 'image', onClick: () => openPages(id) },
    image && s.failed_rows > 0 && { label: `重试失败的 ${s.failed_rows} 行`, icon: 'refresh', onClick: () => retryFailed(s) },
    image && { label: '整首重新识别', icon: 'refresh', onClick: () => reparse(s) },
    image && s.bars > 0 && { label: '精修整首', icon: 'sparkle', onClick: () => refineSong(s) },
    { label: '导出曲子包', icon: 'upload', onClick: () => exportSongs([id]) },
    'sep',
    { label: '删除', icon: 'trash', danger: true, onClick: () => remove(s) },
  ]);
}
async function act(fn, okMsg, kind = 'ok') {
  try { await fn(); if (okMsg) toast(okMsg, kind); } catch (e) { toast(e.message, 'err', 4000); }
  songsPoll.now();
}
async function rename(s) {
  const title = await UI.prompt('曲名', s.title, { title: '重命名', ok: '保存' });
  if (title === null || !title.trim() || title.trim() === s.title) return;
  await act(() => api.put(`/api/songs/${s.id}/meta`, { title: title.trim() }), '已改名');
}
async function editTags(s) {
  const raw = await UI.prompt('标签', (s.tags || []).join(', '), { title: '编辑标签', ok: '保存', placeholder: '指弹, 练习中', hint: '用逗号分隔，留空清除标签' });
  if (raw === null) return;
  const tags = raw.split(/[,，、;；\n]/).map(x => x.trim()).filter(Boolean);
  await act(() => api.put(`/api/songs/${s.id}/tags`, { tags }), tags.length ? '标签已保存' : '已清除标签');
}
async function reparse(s) {
  const ok = await UI.confirm('会再调用一次 AI（按原谱页数计费）。\n和原来一样的小节保留提示、问答和练习记录，手改过的小节保留你的修改；识别失败或取消时原结果不变。',
    { title: `整首重新识别“${s.title}”？`, ok: '重新识别' });
  if (ok) await act(() => api.post(`/api/songs/${s.id}/parse`), '已开始重新识别', 'info');
}
// 精修整首：确认框里给出要核对的小节数、预估费用和时间（后端算好的 refine_estimate）
async function refineSong(s) {
  const c = refineConfirm(s.refine_estimate, s.title);
  if (!c) { toast(NO_REFINE, 'info', 4000); return; }
  if (await UI.confirm(c.text, { title: c.title, ok: c.ok })) await act(() => api.post(`/api/songs/${s.id}/refine`), '已开始精修整首', 'info');
}
async function retryFailed(s) {
  const ok = await UI.confirm('会再调用 AI，只重新识别失败的行；已经识别好的小节不动。', { title: `重试“${s.title}”失败的 ${s.failed_rows} 行？`, ok: '重试' });
  if (ok) await act(() => api.post(`/api/songs/${s.id}/retry-failed`), '已开始重试失败的行', 'info');
}
async function cancel(id) { await act(() => api.post(`/api/songs/${id}/cancel`), '正在取消…', 'info'); }
async function remove(s) {
  const ok = await UI.confirm('识别结果、提示、问答和练习记录会一起删除，不能恢复。想留一份可以先“导出曲子包”。', { title: `删除“${s.title}”？`, ok: '删除', danger: true });
  if (ok) await act(() => api.del(`/api/songs/${s.id}`), '已删除');
}

// ---------------- 管理原谱页面（图片 / PDF 曲子） ----------------
// items：已有的页 {kind:'old', file, url, bars} 或新选的文件 {kind:'new', k, name, url, pdf}（PDF 在浏览器里不渲染，整份算一张卡片）
const pg = { sid: null, items: [], files: [], drag: -1, layer: null };
const pageSig = items => items.map(it => (it.kind === 'old' ? it.file : 'new')).join('|');
async function openPages(id) {
  let song;
  try { song = await api.get(`/api/songs/${id}`); } catch (e) { toast(e.message, 'err'); return; }
  const bars = {};
  for (const b of song.bars || []) if (b.page != null) bars[b.page] = (bars[b.page] || 0) + 1;
  Object.assign(pg, { sid: id, files: [], drag: -1 });
  pg.items = (song.pages || []).map((p, i) => ({ kind: 'old', file: p.file, url: `/files/${id}/${encodeURIComponent(p.file)}`, bars: bars[i] || 0 }));
  pg.orig = pageSig(pg.items);
  pg.all = pg.items.slice(); // 打开时的全部页：算删了几页、多少小节
  $('pagesTitle').textContent = `管理原谱页面 · ${song.title || (song.meta && song.meta.title) || ''}`;
  renderPages();
  // 点遮罩不关（免得丢掉调整好的顺序），Esc / 取消 / × 关闭
  pg.layer = UI.layer($('pagesDlg'), { modal: true, outside: false, focus: $('pagesClose'), onClose: cleanupPages });
}
function cleanupPages() {
  pg.items.forEach(it => { if (it.kind === 'new' && it.url) URL.revokeObjectURL(it.url); });
  pg.items = []; pg.files = []; pg.layer = null;
  $('pageGrid').innerHTML = '';
}
function closePages() { if (pg.layer) pg.layer.close(); }
function removedPages() {
  const kept = new Set(pg.items.filter(it => it.kind === 'old').map(it => it.file));
  const gone = (pg.all || []).filter(it => !kept.has(it.file));
  return { pages: gone.length, bars: gone.reduce((a, it) => a + it.bars, 0) };
}
function renderPages(focusSel) {
  const n = pg.items.length;
  $('pageGrid').innerHTML = n ? pg.items.map((it, i) => {
    const lbl = it.kind === 'old' ? `第 ${i + 1} 页 · ${it.bars} 小节` : it.pdf ? `${esc(it.name)}（整份 PDF）` : `新页 · ${esc(it.name)}`;
    return `<div class="page-card ${it.kind === 'new' ? 'new' : ''}" draggable="true" data-i="${i}">
      <div class="thumb">${it.url ? `<img src="${it.url}" alt="${it.kind === 'old' ? `第 ${i + 1} 页` : esc(it.name)}" loading="lazy">` : icon('file')}</div>
      <div class="pc-f"><span class="lbl" title="${it.name ? esc(it.name) : ''}">${it.kind === 'new' ? '<span class="badge accent">新</span> ' : ''}${lbl}</span>
        <button class="icon-btn sm" data-mv="-1" title="往前移" aria-label="往前移" ${i === 0 ? 'disabled' : ''}>${icon('left')}</button>
        <button class="icon-btn sm" data-mv="1" title="往后移" aria-label="往后移" ${i === n - 1 ? 'disabled' : ''}>${icon('right')}</button>
        <button class="icon-btn sm" data-rm title="删除这一页" aria-label="删除这一页">${icon('trash')}</button></div></div>`;
  }).join('') : '<div class="empty-state"><b>没有页面了</b>至少要留一页，或点“添加页面”。</div>';
  const removed = removedPages(), added = pg.items.filter(it => it.kind === 'new').length;
  const changed = pageSig(pg.items) !== pg.orig;
  const parts = [];
  if (removed.pages) parts.push(`删除 ${removed.pages} 页（${removed.bars} 小节）`);
  if (added) parts.push(`新加 ${added} 个文件，保存后只识别新页（会产生 AI 费用）`);
  else if (changed && !removed.pages) parts.push('调整了顺序，不需要重新识别');
  $('pagesMsg').textContent = parts.join('；');
  $('pagesSave').disabled = !changed || !n;
  if (focusSel) { const el = $('pageGrid').querySelector(focusSel); (el && !el.disabled ? el : $('pagesAdd')).focus(); }
}
function movePage(from, to, focusSel) {
  if (to < 0 || to >= pg.items.length || from === to) return;
  const [it] = pg.items.splice(from, 1);
  pg.items.splice(to, 0, it);
  renderPages(focusSel && focusSel.replace('{i}', to));
}
$('pageGrid').addEventListener('click', e => {
  const card = e.target.closest('.page-card');
  if (!card) return;
  const i = +card.dataset.i, mv = e.target.closest('[data-mv]');
  if (mv) movePage(i, i + +mv.dataset.mv, `.page-card[data-i="{i}"] [data-mv="${mv.dataset.mv}"]`);
  else if (e.target.closest('[data-rm]')) {
    const [it] = pg.items.splice(i, 1);
    if (it.kind === 'new' && it.url) URL.revokeObjectURL(it.url);
    renderPages(`.page-card[data-i="${Math.min(i, pg.items.length - 1)}"] [data-rm]`);
  }
});
$('pageGrid').addEventListener('dragstart', e => {
  const card = e.target.closest('.page-card');
  if (!card) return;
  pg.drag = +card.dataset.i; card.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move';
});
$('pageGrid').addEventListener('dragend', () => { pg.drag = -1; renderPages(); });
$('pageGrid').addEventListener('dragover', e => {
  const card = e.target.closest('.page-card');
  if (pg.drag < 0 || !card) return;
  e.preventDefault();
  document.querySelectorAll('.page-card.drop-before').forEach(c => c.classList.remove('drop-before'));
  card.classList.add('drop-before');
});
$('pageGrid').addEventListener('drop', e => {
  const card = e.target.closest('.page-card');
  if (pg.drag < 0 || !card) return;
  e.preventDefault();
  const r = card.getBoundingClientRect(); // e.offsetX 是相对落点所在的子元素（图片、页脚按钮）的，不是相对卡片
  const to = +card.dataset.i, after = e.clientX - r.left > r.width / 2 ? 1 : 0;
  if (to !== pg.drag) movePage(pg.drag, pg.drag < to ? to - 1 + after : to + after); // 放回自己身上（不管左半还是右半）不动
  pg.drag = -1;
});
$('pagesAdd').onclick = () => $('pagesFile').click();
$('pagesFile').onchange = e => {
  for (const f of e.target.files) {
    const pdf = f.type === 'application/pdf' || /\.pdf$/i.test(f.name);
    if (!pdf && !f.type.startsWith('image/')) { toast(`“${f.name}”不是图片或 PDF`, 'err', 4000); continue; }
    pg.items.push({ kind: 'new', k: pg.files.length, name: f.name, pdf, url: pdf ? null : URL.createObjectURL(f) });
    pg.files.push(f);
  }
  e.target.value = '';
  renderPages();
};
$('pagesClose').onclick = closePages;
$('pagesCancel').onclick = closePages;
$('pagesSave').onclick = async () => {
  const removed = removedPages();
  if (removed.bars && !await UI.confirm(`删除的页上有 ${removed.bars} 个小节，它们的提示、问答和练习记录会一起删掉。`, { title: `删除 ${removed.pages} 页原谱？`, ok: '删除并保存', danger: true })) return;
  const files = [];
  const order = pg.items.map(it => (it.kind === 'old' ? it.file : `new:${files.push(pg.files[it.k]) - 1}`));
  await api.withBusy($('pagesSave'), async () => {
    try {
      await api.upload(`/api/songs/${pg.sid}/pages`, files, { order: JSON.stringify(order) });
      toast(files.length ? '已保存，正在识别新加的页' : '页面已更新', files.length ? 'info' : 'ok');
      closePages();
    } catch (e) {
      toast(e.message, 'err', 5000);
    }
  });
  songsPoll.now();
};

// ---------------- 设置 ----------------
const PRESETS = ['claude-opus-5-5', 'claude-sonnet-5-5'];
let settings = null; // 服务器上当前的设置（showSettings 时更新），“测试”时比较哪些改了还没保存
function showSettings(c) {
  settings = c;
  const hasKey = !!(c.api_key_masked || c.env_key);
  UI.notice($('keyNotice'), hasKey ? null : { kind: 'warn', html: '还没设置 API key，识别原谱前请先到 <a href="#settings">设置</a> 填写。（导入 Guitar Pro / MusicXML 不需要。）' });
  $('keyState').innerHTML = c.api_key_masked ? `已保存：<code>${esc(c.api_key_masked)}</code>`
    : c.env_key ? '正在使用环境变量 <code>ANTHROPIC_API_KEY</code>。'
    : '只保存在本机，也可以用环境变量 <code>ANTHROPIC_API_KEY</code>。';
  $('clearKey').hidden = !c.api_key_masked;
  const custom = !PRESETS.includes(c.model);
  setModelSeg(custom ? 'custom' : c.model);
  $('modelCustom').hidden = !custom;
  if (custom) $('modelCustom').value = c.model;
  setEffortSeg(c.effort);
  setDoubleSeg(c.double_check || 'off');
  setRefineSeg(c.refine || 'manual');
  $('baseUrl').value = c.base_url;
}
// → 是否保存成功（失败时已经提示过）
async function save(body, msg = '已保存') {
  try { showSettings(await api.put('/api/settings', body)); toast(msg); return true; } catch (e) { toast(e.message, 'err', 4000); return false; }
}
const setModelSeg = UI.seg($('modelSeg'), 'claude-opus-5-5', v => {
  $('modelCustom').hidden = v !== 'custom';
  if (v === 'custom') $('modelCustom').focus(); else save({ model: v });
});
$('modelCustom').onchange = () => { if ($('modelCustom').value.trim()) save({ model: $('modelCustom').value.trim() }); };
const setEffortSeg = UI.seg($('effortSeg'), 'high', v => save({ effort: v }));
const setDoubleSeg = UI.seg($('doubleSeg'), 'off', v => save({ double_check: v }, v === 'off' ? '已关闭双重校对' : '已打开双重校对'));
const setRefineSeg = UI.seg($('refineSeg'), 'manual', v => save({ refine: v }, v === 'auto' ? '识别完会自动精修' : '精修改成手动'));
$('baseUrl').onchange = () => save({ base_url: $('baseUrl').value });
$('saveKey').onclick = () => api.withBusy($('saveKey'), async () => {
  const k = $('apiKey').value.trim();
  if (!k) { toast('先填写 key', 'err'); $('apiKey').focus(); return; }
  if (await save({ api_key: k }, 'API key 已保存')) $('apiKey').value = ''; // 保存失败时留着，不用再粘贴一次
});
$('apiKey').onkeydown = e => { if (e.key === 'Enter' && !e.isComposing) $('saveKey').click(); };
$('clearKey').onclick = async () => {
  if (await UI.confirm('清除后识别原谱前要重新填写。', { title: '清除已保存的 API key？', ok: '清除', danger: true })) save({ clear_api_key: true }, '已清除');
};
// 填了还没保存的 key、改了的 Base URL / 自定义模型名（测试测的是服务器上保存的设置）
function unsavedSettings() {
  const body = {}, k = $('apiKey').value.trim(), url = $('baseUrl').value, model = $('modelCustom').value.trim();
  if (k) body.api_key = k;
  if (settings && url.trim() !== settings.base_url) body.base_url = url;
  if (settings && !$('modelCustom').hidden && model && model !== settings.model) body.model = model;
  return body;
}
$('testBtn').onclick = () => api.withBusy($('testBtn'), async () => {
  // 先保存还没保存的改动再测，免得刚粘贴的 key 测出来是“没有 key”；保存失败就不测了
  const body = unsavedSettings();
  if (Object.keys(body).length) {
    if (!await save(body, body.api_key ? 'API key 已保存' : '已保存')) { $('testState').innerHTML = ''; return; }
    if (body.api_key) $('apiKey').value = '';
  }
  $('testState').innerHTML = '<span class="spinner"></span>测试中…';
  try {
    const r = await api.post('/api/settings/test');
    $('testState').innerHTML = r.ok ? '<span class="dot ok"></span><span class="ok-text">连接正常</span>' : `<span class="dot err"></span><span class="err-text">${esc(r.error)}</span>`;
  } catch (e) {
    $('testState').innerHTML = `<span class="dot err"></span><span class="err-text">${esc(e.message)}</span>`;
  }
});

route();
api.get('/api/settings').then(showSettings).catch(() => {});

// 端到端测试 / 控制台调试用
window.tt = { reload: loadSongs, refresh: () => songsPoll.now(), openPages, songs: () => lastSongs };
