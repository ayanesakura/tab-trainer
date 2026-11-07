// 曲库页：上传谱子、曲子列表（识别进度、重新识别、删除）、曲子包导出 / 导入、设置（API key、模型、主题）
const $ = id => document.getElementById(id);
const { esc, icon, toast } = UI;
async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `请求失败 ${r.status}`);
  return data;
}

// ---------------- 导航 ----------------
const setNav = UI.seg($('nav'), 'library', v => { location.hash = v; });
function route() {
  const v = location.hash.slice(1) === 'settings' ? 'settings' : 'library';
  setNav(v);
  $('view-library').hidden = v !== 'library';
  $('view-settings').hidden = v !== 'settings';
}
addEventListener('hashchange', route);

// ---------------- 主题 ----------------
UI.themeButton($('themeBtn'));
const setThemeSeg = UI.seg($('themeSeg'), UI.getTheme(), v => UI.setTheme(v));
document.addEventListener('themechange', e => setThemeSeg(e.detail));

// ---------------- 上传 ----------------
let picked = [];
const isZip = f => /\.zip$/i.test(f.name);
function setFiles(list) {
  const zips = [...list].filter(isZip);
  if (zips.length) {  // 曲子包直接导入，不走“上传并识别”
    $('file').value = '';
    if (zips.length < list.length) return toast('曲子包（.zip）请单独导入，不要和图片、GP 文件混在一起', 'err', 4000);
    return importBundles(zips);
  }
  picked = [...list].sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
  $('pickedBox').hidden = !picked.length;
  $('picked').innerHTML = picked.map(f => `<span class="badge">${icon(f.type === 'application/pdf' ? 'book' : 'music', 12)}${esc(f.name)}</span>`).join('');
}
$('file').onchange = e => setFiles(e.target.files);
const drop = $('drop');
drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
drop.ondragleave = () => drop.classList.remove('over');
drop.ondrop = e => { e.preventDefault(); drop.classList.remove('over'); setFiles(e.dataTransfer.files); };
$('clearPick').onclick = () => { setFiles([]); $('file').value = ''; };
$('uploadBtn').onclick = async () => {
  const fd = new FormData();
  picked.forEach(f => fd.append('files', f));
  fd.append('title', $('title').value);
  $('uploadBtn').disabled = true;
  try {
    await api('/api/songs', { method: 'POST', body: fd });
    toast('已上传，开始识别');
    setFiles([]); $('title').value = ''; $('file').value = '';
    loadSongs();
  } catch (e) {
    toast(e.message, 'err', 4000);
  } finally {
    $('uploadBtn').disabled = false;
  }
};

// ---------------- 曲子包 ----------------
async function importBundles(files) {
  const fd = new FormData();
  files.forEach(f => fd.append('files', f));
  $('importBtn').disabled = true;
  toast('正在导入…', 'info', 60000);
  try {
    const r = await api('/api/import', { method: 'POST', body: fd });
    const n = r.imported.length, dup = r.skipped.filter(s => s.duplicate).length, bad = r.skipped.filter(s => !s.duplicate);
    const parts = [`已导入 ${n} 首`];
    if (dup) parts.push(`${dup} 首曲库里已有，跳过`);
    if (bad.length) parts.push(`${bad.length} 首无法导入：` + bad.slice(0, 3).map(s => `“${s.title}”${s.reason}`).join('；') + (bad.length > 3 ? ' 等' : ''));
    toast(parts.join('；'), bad.length ? (n ? 'info' : 'err') : 'ok', bad.length || dup ? 8000 : 2200);
  } catch (e) {
    toast(e.message, 'err', 5000);
  } finally {
    $('importBtn').disabled = false;
    loadSongs();
  }
}
$('importBtn').onclick = () => $('bundleFile').click();
$('bundleFile').onchange = e => { const f = [...e.target.files]; e.target.value = ''; if (f.length) importBundles(f); };

async function exportSongs(ids) {
  toast(`正在打包 ${ids.length} 首…`, 'info', 60000);
  try {
    const r = await fetch('/api/export?' + ids.map(id => 'ids=' + encodeURIComponent(id)).join('&'));
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `导出失败 ${r.status}`);
    const cd = r.headers.get('content-disposition') || '';
    const m = cd.match(/filename\*=utf-8''([^;]+)/i) || cd.match(/filename="?([^";]+)/i);
    const a = document.createElement('a');
    a.href = URL.createObjectURL(await r.blob());
    a.download = m ? decodeURIComponent(m[1]) : '曲子包.zip';
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
const selectable = s => s.status !== 'corrupt';
function setSelecting(on) {
  selecting = on; selected.clear();
  $('selectBar').hidden = !on;
  $('selectBtn').hidden = on;
  renderSongs();
}
function updateSelectBar() {
  const all = visibleSongs().filter(selectable);
  for (const id of [...selected]) if (!all.some(s => s.id === id)) selected.delete(id);  // 已被删除的
  $('selectCount').textContent = `已选 ${selected.size} 首`;
  $('selectAll').checked = all.length > 0 && selected.size === all.length;
  $('selectAll').indeterminate = selected.size > 0 && selected.size < all.length;
  $('exportBtn').disabled = !selected.size;
}
$('selectBtn').onclick = () => setSelecting(true);
$('selectCancel').onclick = () => setSelecting(false);
$('selectAll').onchange = e => {
  selected.clear();
  if (e.target.checked) visibleSongs().filter(selectable).forEach(s => selected.add(s.id));
  renderSongs();
};
$('exportBtn').onclick = async () => {
  const ids = lastSongs.filter(s => selected.has(s.id)).map(s => s.id);  // 按曲库顺序
  $('exportBtn').disabled = true;
  if (await exportSongs(ids)) setSelecting(false); else updateSelectBar();
};
addEventListener('keydown', e => { if (e.key === 'Escape' && selecting) setSelecting(false); });

// ---------------- 曲库 ----------------
const TILE_COLORS = ['#4f46e5', '#0ea5e9', '#10b981', '#f59e0b', '#ec4899', '#8b5cf6', '#64748b', '#ef4444'];
const tileColor = s => TILE_COLORS[[...s].reduce((a, c) => a + c.codePointAt(0), 0) % TILE_COLORS.length];

function songState(s) {
  const p = s.progress || {};
  if (s.status === 'parsing' || s.status === 'queued' || s.job) {
    const pct = p.total ? Math.round(100 * p.done / p.total) : 6;
    const unit = s.job === 'explain' ? '批' : '行';
    return `<span class="spinner"></span><span>${esc(p.stage || '排队中')}${p.total ? ` · ${p.done}/${p.total} ${unit}` : ''}</span><span class="progress"><i style="width:${pct}%"></i></span>`;
  }
  if (s.status === 'corrupt') return `<span class="dot err"></span><span class="err-text">${esc(s.error)}（可在“…”菜单删除）</span>`;
  if (s.status === 'error') return `<span class="dot err"></span><span class="err-text">${s.kind === 'score' ? '曲谱读取失败' : '识别失败'}：${esc(s.error)}</span>`;
  if (s.status === 'cancelled') return `<span class="dot warn"></span><span>已取消${s.bars ? ` · 已识别 ${s.bars} 小节` : ''}</span>`;
  if (s.kind === 'score' && !s.bars) return `<span class="dot ok"></span><span>已导入</span>`;
  const parts = [`<span class="dot ${s.warn_bars ? 'warn' : 'ok'}"></span><span>${s.bars} 小节</span>`];
  if (s.warn_bars) parts.push(`<span class="badge warn">${s.warn_bars} 处待核对</span>`);
  if (s.error) parts.push(`<span class="badge err">部分行识别失败</span>`);
  return parts.join('');
}

let pollTimer = null;
async function loadSongs() {
  let songs;
  try { songs = await api('/api/songs'); } catch (e) { $('songs').innerHTML = `<div class="empty-state"><b>加载失败</b>${esc(e.message)}</div>`; return; }
  lastSongs = songs;
  renderSongs();
  clearTimeout(pollTimer);
  if (songs.some(s => s.status === 'parsing' || s.status === 'queued' || s.job)) pollTimer = setTimeout(loadSongs, 2500);
}
// 搜索、标签筛选、排序（只在浏览器里做；排序方式记在本机）
const lib = { q: '', tag: '', sort: ['created', 'practiced', 'title'].includes(localStorage.getItem('tt.libSort')) ? localStorage.getItem('tt.libSort') : 'created' };
UI.seg($('sortSeg'), lib.sort, v => { lib.sort = v; localStorage.setItem('tt.libSort', v); renderSongs(); });
$('search').oninput = e => { lib.q = e.target.value.trim().toLowerCase(); renderSongs(); };
$('tagFilter').onclick = e => { const b = e.target.closest('button'); if (b) { lib.tag = b.dataset.tag; renderSongs(); } };
function visibleSongs() {
  const hit = s => [s.title, s.artist, ...(s.tags || [])].some(x => (x || '').toLowerCase().includes(lib.q));
  const list = lastSongs.filter(s => (!lib.tag || (s.tags || []).includes(lib.tag)) && (!lib.q || hit(s)));
  if (lib.sort === 'title') list.sort((a, b) => a.title.localeCompare(b.title, 'zh-CN', { numeric: true }));
  if (lib.sort === 'practiced') list.sort((a, b) => (b.last_practiced || '').localeCompare(a.last_practiced || '')); // 没练过的按添加时间排在后面
  return list;
}
function renderTagFilter() {
  const tags = [...new Set(lastSongs.flatMap(s => s.tags || []))].sort((a, b) => a.localeCompare(b, 'zh-CN'));
  if (lib.tag && !tags.includes(lib.tag)) lib.tag = '';
  $('tagFilter').innerHTML = tags.length ? ['', ...tags].map(t => `<button data-tag="${esc(t)}" class="${t === lib.tag ? 'on' : ''}">${t ? esc(t) : '全部'}</button>`).join('') : '';
}
// AI 花费：早期曲子只记了 token，按 Opus 价格估算
function costText(s) {
  if (!s.tokens) return '';
  const tip = [`共 ${s.tokens.toLocaleString()} token`];
  if (s.cost_estimated) tip.push('早期的曲子只记了 token 数，按 Opus 的价格估算');
  if (s.unpriced_tokens) tip.push(`其中 ${s.unpriced_tokens.toLocaleString()} token 用的是自定义模型，没算进金额`);
  const usd = s.cost_usd >= 0.005 ? '$' + s.cost_usd.toFixed(2) : s.cost_usd > 0 ? '<$0.01' : '';
  const text = usd ? `AI 花费${s.cost_estimated ? '约' : ''} ${usd}` : `AI 用量 ${s.tokens.toLocaleString()} token`;
  return `<span class="cost" title="${esc(tip.join('；'))}">${text}${s.unpriced_tokens && usd ? ' +' : ''}</span>`;
}
function renderSongs() {
  const songs = visibleSongs();
  if (selecting) updateSelectBar();
  $('libTools').hidden = !lastSongs.length;
  renderTagFilter();
  if (!lastSongs.length) {
    $('songs').innerHTML = `<div class="empty-state"><b>还没有曲子</b>上传一份谱子开始。</div>`;
  } else if (!songs.length) {
    $('songs').innerHTML = `<div class="empty-state"><b>没有符合条件的曲子</b>换个关键词或标签试试。</div>`;
  } else {
    const SOURCE = { image: '图片', pdf: 'PDF', gp: 'GP', musicxml: 'MusicXML' }; // 导入渠道
    $('songs').innerHTML = songs.map(s => {
      const busy = s.status === 'parsing' || s.status === 'queued';
      const canPlay = !busy && (s.bars > 0 || s.kind === 'score');
      const sub = [esc(s.artist), esc(s.created), s.last_practiced && `上次练习 ${esc(s.last_practiced)}`, costText(s)].filter(Boolean).join(' · ');
      const tags = (s.tags || []).length ? `<span class="tags">${s.tags.map(t => `<span class="badge" data-tag="${esc(t)}" title="只看“${esc(t)}”">${esc(t)}</span>`).join('')}</span>` : '';
      const pick = selecting && selectable(s), on = pick && selected.has(s.id);
      return `<div class="row song-row ${canPlay || pick ? 'click' : ''} ${on ? 'on' : ''}" data-id="${s.id}" data-play="${canPlay ? 1 : ''}" data-pick="${pick ? 1 : ''}">
        ${selecting ? `<input type="checkbox" class="pick" aria-label="选择 ${esc(s.title)}" ${on ? 'checked' : ''} ${pick ? '' : 'disabled'}>` : ''}
        <div class="tile" style="background:${tileColor(s.title)}">${esc([...s.title][0] || '♪')}</div>
        <div class="who"><div class="name">${esc(s.title)}${SOURCE[s.source_type] ? ` <span class="badge">${SOURCE[s.source_type]}</span>` : ''}${tags}</div><div class="state">${songState(s)}</div>${sub ? `<div class="sub">${sub}</div>` : ''}</div>
        <div class="ctl" ${selecting ? 'hidden' : ''}>
          ${canPlay ? `<a class="btn primary sm" href="/trainer?id=${s.id}">${icon('play', 12)}练习</a>` : ''}
          <button class="icon-btn sm" data-more="${s.id}" data-busy="${busy || s.job ? 1 : ''}" data-corrupt="${s.status === 'corrupt' ? 1 : ''}" title="更多">${icon('more')}</button>
        </div>
      </div>`;
    }).join('');
  }
}
$('songs').addEventListener('click', e => {
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
  const tag = e.target.closest('.song-row [data-tag]');
  if (tag) { e.stopPropagation(); lib.tag = tag.dataset.tag; renderSongs(); return; }
  const more = e.target.closest('[data-more]');
  if (more) {
    e.stopPropagation();
    const id = more.dataset.more, s = lastSongs.find(x => x.id === id) || {};
    if (more.dataset.busy) {
      UI.menu(more, [{ label: s.job === 'explain' ? '取消生成讲解' : '取消识别', icon: 'x', onClick: () => cancel(id) }]);
      return;
    }
    if (more.dataset.corrupt) {
      UI.menu(more, [{ label: '删除', icon: 'trash', danger: true, onClick: () => del(id) }]);
      return;
    }
    const image = s.kind !== 'score'; // GP / MusicXML 没有页面，也不需要识别
    UI.menu(more, [
      { label: '重命名', icon: 'edit', onClick: () => rename(s) },
      { label: '编辑标签', icon: 'list', onClick: () => editTags(s) },
      image && { label: '管理页面', icon: 'image', onClick: () => openPages(id) },
      image && { label: '整首重新识别', icon: 'refresh', onClick: () => reparse(id) },
      { label: '导出曲子包', icon: 'upload', onClick: () => exportSongs([id]) },
      'sep',
      { label: '删除', icon: 'trash', danger: true, onClick: () => del(id) },
    ].filter(Boolean));
    return;
  }
  if (e.target.closest('a,button')) return;
  const row = e.target.closest('.row[data-play="1"]');
  if (row) location.href = '/trainer?id=' + row.dataset.id;
});
async function rename(s) {
  const title = prompt('曲名', s.title);
  if (title === null || !title.trim() || title.trim() === s.title) return;
  try {
    await api(`/api/songs/${s.id}/meta`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: title.trim() }) });
    toast('已改名');
  } catch (e) { toast(e.message, 'err'); }
  loadSongs();
}
async function editTags(s) {
  const raw = prompt('标签（用逗号分隔，如：指弹, 练习中）', (s.tags || []).join(', '));
  if (raw === null) return;
  const tags = raw.split(/[,，、;；\n]/).map(x => x.trim()).filter(Boolean);
  try {
    await api(`/api/songs/${s.id}/tags`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tags }) });
    toast(tags.length ? '标签已保存' : '已清除标签');
  } catch (e) { toast(e.message, 'err'); }
  loadSongs();
}
async function reparse(id) {
  if (!confirm('整首重新识别会再调用一次 AI（按页数计费）。\n和原来一样的小节保留提示、问答和练习记录，手改过的小节保留你的修改；识别失败或取消时原结果不变。继续？')) return;
  try { await api(`/api/songs/${id}/parse`, { method: 'POST' }); toast('已开始重新识别', 'info'); } catch (e) { toast(e.message, 'err'); }
  loadSongs();
}
async function cancel(id) {
  try { await api(`/api/songs/${id}/cancel`, { method: 'POST' }); toast('正在取消…', 'info'); } catch (e) { toast(e.message, 'err'); }
  loadSongs();
}
async function del(id) {
  if (!confirm('删除这首曲子？')) return;
  try { await api(`/api/songs/${id}`, { method: 'DELETE' }); toast('已删除'); } catch (e) { toast(e.message, 'err'); }
  loadSongs();
}

// ---------------- 管理页面（图片 / PDF 曲子） ----------------
// items：已有的页 {kind:'old', file, url, bars} 或新选的文件 {kind:'new', k, name, url, pdf}（PDF 在浏览器里不渲染，整份算一张卡片）
const pg = { sid: null, items: [], files: [], drag: -1 };
const pageSig = items => items.map(it => (it.kind === 'old' ? it.file : 'new')).join('|');
async function openPages(id) {
  let song;
  try { song = await api(`/api/songs/${id}`); } catch (e) { toast(e.message, 'err'); return; }
  const bars = {};
  for (const b of song.bars || []) if (b.page != null) bars[b.page] = (bars[b.page] || 0) + 1;
  Object.assign(pg, { sid: id, files: [], drag: -1 });
  pg.items = (song.pages || []).map((p, i) => ({ kind: 'old', file: p.file, url: `/files/${id}/${encodeURIComponent(p.file)}`, bars: bars[i] || 0 }));
  pg.orig = pageSig(pg.items);
  pg.all = pg.items.slice(); // 打开时的全部页：算删了几页、多少小节
  $('pagesTitle').textContent = `管理页面 · ${song.title || (song.meta && song.meta.title) || ''}`;
  renderPages();
  $('pagesDlg').showModal();
}
function removedPages() {
  const kept = new Set(pg.items.filter(it => it.kind === 'old').map(it => it.file));
  const gone = (pg.all || []).filter(it => !kept.has(it.file));
  return { pages: gone.length, bars: gone.reduce((a, it) => a + it.bars, 0) };
}
function renderPages() {
  const n = pg.items.length;
  $('pageGrid').innerHTML = n ? pg.items.map((it, i) => {
    const lbl = it.kind === 'old' ? `第 ${i + 1} 页 · ${it.bars} 小节` : it.pdf ? `${esc(it.name)}（整份 PDF）` : `新页 · ${esc(it.name)}`;
    return `<div class="page-card ${it.kind === 'new' ? 'new' : ''}" draggable="true" data-i="${i}">
      <div class="thumb">${it.url ? `<img src="${it.url}" alt="" loading="lazy">` : icon('book')}</div>
      <div class="pc-f"><span class="lbl" title="${it.name ? esc(it.name) : ''}">${it.kind === 'new' ? '<span class="badge accent">新</span> ' : ''}${lbl}</span>
        <button class="icon-btn sm" data-mv="-1" title="往前移" ${i === 0 ? 'disabled' : ''}>${icon('left')}</button>
        <button class="icon-btn sm" data-mv="1" title="往后移" ${i === n - 1 ? 'disabled' : ''}>${icon('right')}</button>
        <button class="icon-btn sm" data-rm title="删除这一页">${icon('trash')}</button></div></div>`;
  }).join('') : '<div class="empty-state"><b>没有页面了</b>至少要留一页，或点“添加页面”。</div>';
  const removed = removedPages(), added = pg.items.filter(it => it.kind === 'new').length;
  const changed = pageSig(pg.items) !== pg.orig;
  const parts = [];
  if (removed.pages) parts.push(`删除 ${removed.pages} 页（${removed.bars} 小节）`);
  if (added) parts.push(`新加 ${added} 个文件，保存后只识别新页（会产生 AI 费用）`);
  else if (changed && !removed.pages) parts.push('调整了顺序，不需要重新识别');
  $('pagesMsg').textContent = parts.join('；');
  $('pagesSave').disabled = !changed || !n;
}
function movePage(from, to) {
  if (to < 0 || to >= pg.items.length || from === to) return;
  const [it] = pg.items.splice(from, 1);
  pg.items.splice(to, 0, it);
  renderPages();
}
$('pageGrid').addEventListener('click', e => {
  const card = e.target.closest('.page-card');
  if (!card) return;
  const i = +card.dataset.i, mv = e.target.closest('[data-mv]');
  if (mv) movePage(i, i + +mv.dataset.mv);
  else if (e.target.closest('[data-rm]')) {
    const [it] = pg.items.splice(i, 1);
    if (it.kind === 'new' && it.url) URL.revokeObjectURL(it.url);
    renderPages();
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
  const to = +card.dataset.i;
  movePage(pg.drag, pg.drag < to ? to - 1 + (e.offsetX > card.offsetWidth / 2 ? 1 : 0) : to + (e.offsetX > card.offsetWidth / 2 ? 1 : 0));
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
function closePages() { $('pagesDlg').close(); }
$('pagesDlg').addEventListener('close', () => { pg.items.forEach(it => { if (it.kind === 'new' && it.url) URL.revokeObjectURL(it.url); }); pg.items = []; pg.files = []; $('pageGrid').innerHTML = ''; });
$('pagesClose').onclick = closePages;
$('pagesCancel').onclick = closePages;
$('pagesSave').onclick = async () => {
  const removed = removedPages();
  if (removed.bars && !confirm(`删除的页上有 ${removed.bars} 个小节，它们的提示、问答和练习记录会一起删掉。继续？`)) return;
  const files = [];
  const order = pg.items.map(it => (it.kind === 'old' ? it.file : `new:${files.push(pg.files[it.k]) - 1}`));
  const fd = new FormData();
  fd.append('order', JSON.stringify(order));
  files.forEach(f => fd.append('files', f));
  $('pagesSave').disabled = true;
  try {
    await api(`/api/songs/${pg.sid}/pages`, { method: 'POST', body: fd });
    toast(files.length ? '已保存，正在识别新加的页' : '页面已更新', files.length ? 'info' : 'ok');
    closePages();
  } catch (e) {
    toast(e.message, 'err', 5000);
    $('pagesSave').disabled = false;
  }
  loadSongs();
};

// ---------------- 设置 ----------------
const PRESETS = ['claude-opus-5-5', 'claude-sonnet-5-5'];
let setModelSeg, setEffortSeg;
function showSettings(c) {
  const hasKey = !!(c.api_key_masked || c.env_key);
  $('keyNotice').hidden = hasKey;
  $('keyState').innerHTML = c.api_key_masked ? `已保存：<code>${esc(c.api_key_masked)}</code>`
    : c.env_key ? '正在使用环境变量 <code>ANTHROPIC_API_KEY</code>。'
    : '只保存在本机，也可以用环境变量 <code>ANTHROPIC_API_KEY</code>。';
  $('clearKey').hidden = !c.api_key_masked;
  const custom = !PRESETS.includes(c.model);
  setModelSeg(custom ? 'custom' : c.model);
  $('modelCustom').hidden = !custom;
  if (custom) $('modelCustom').value = c.model;
  setEffortSeg(c.effort);
  $('baseUrl').value = c.base_url;
}
async function save(body, msg = '已保存') {
  try { showSettings(await api('/api/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })); toast(msg); }
  catch (e) { toast(e.message, 'err'); }
}
setModelSeg = UI.seg($('modelSeg'), 'claude-opus-5-5', v => {
  $('modelCustom').hidden = v !== 'custom';
  if (v === 'custom') $('modelCustom').focus(); else save({ model: v });
});
$('modelCustom').onchange = () => { if ($('modelCustom').value.trim()) save({ model: $('modelCustom').value.trim() }); };
setEffortSeg = UI.seg($('effortSeg'), 'high', v => save({ effort: v }));
$('baseUrl').onchange = () => save({ base_url: $('baseUrl').value });
$('saveKey').onclick = () => { const k = $('apiKey').value.trim(); if (!k) return toast('先填写 key', 'err'); $('apiKey').value = ''; save({ api_key: k }, 'API key 已保存'); };
$('apiKey').onkeydown = e => { if (e.key === 'Enter') $('saveKey').click(); };
$('clearKey').onclick = () => { if (confirm('清除已保存的 API key？')) save({ clear_api_key: true }, '已清除'); };
$('testBtn').onclick = async () => {
  $('testBtn').disabled = true;
  $('testState').innerHTML = '<span class="spinner"></span>测试中…';
  try {
    const r = await api('/api/settings/test', { method: 'POST' });
    $('testState').innerHTML = r.ok ? '<span class="dot ok"></span><span style="color:var(--green)">连接正常</span>' : `<span class="dot err"></span><span style="color:var(--red)">${esc(r.error)}</span>`;
  } catch (e) {
    $('testState').innerHTML = `<span class="dot err"></span>${esc(e.message)}`;
  } finally { $('testBtn').disabled = false; }
};

document.addEventListener('DOMContentLoaded', () => {
  route();
  api('/api/settings').then(showSettings).catch(() => {});
  loadSongs();
});
