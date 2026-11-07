// 曲库页：上传原谱 / GP 文件、曲子列表（识别进度、练习记录、接着练）、曲子包导出 / 导入、管理原谱页面、设置（API key、模型、主题）。
// 词表：曲子 = 曲库里的一条；谱面 = 渲染出来的谱；原谱 = 上传的图片 / PDF。
import { UI, $, esc, icon, toast } from '/static/ui.js';
import * as api from '/static/js/lib/api.js';
import { SOURCES } from '/static/js/vocab.js';

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
document.addEventListener('themechange', e => setThemeSeg(e.detail));

// ---------------- 上传 ----------------
let picked = [];
const isZip = f => /\.zip$/i.test(f.name);
function setFiles(list) {
  const zips = [...list].filter(isZip);
  if (zips.length) {  // 曲子包直接导入，不走“上传并识别”
    $('file').value = '';
    if (zips.length < list.length) return toast('曲子包（.zip）请单独导入，不要和原谱图片、GP 文件混在一起', 'err', 4000);
    return importBundles(zips);
  }
  picked = [...list].sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
  $('pickedBox').hidden = !picked.length;
  $('picked').innerHTML = picked.map(f => `<span class="badge">${icon(fileIcon(f), 12)}${esc(f.name)}</span>`).join('');
  const score = picked.length && picked.every(f => /\.(gpx?|gp[345]|musicxml|xml|mxl)$/i.test(f.name));
  $('uploadBtn').lastChild.textContent = score ? '导入' : '上传并识别';
}
const fileIcon = f => (f.type === 'application/pdf' || /\.pdf$/i.test(f.name) ? 'file' : f.type.startsWith('image/') ? 'image' : 'score');
$('file').onchange = e => setFiles(e.target.files);
const drop = $('drop');
drop.onclick = () => $('file').click();
drop.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('file').click(); } };
drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
drop.ondragleave = () => drop.classList.remove('over');
drop.ondrop = e => { e.preventDefault(); drop.classList.remove('over'); setFiles(e.dataTransfer.files); };
$('clearPick').onclick = () => { setFiles([]); $('file').value = ''; drop.focus(); };
$('uploadBtn').onclick = () => api.withBusy($('uploadBtn'), async () => {
  try {
    await api.upload('/api/songs', picked, { title: $('title').value });
    toast(picked.some(f => !/\.(gpx?|gp[345]|musicxml|xml|mxl)$/i.test(f.name)) ? '已上传，开始识别' : '已导入');
    setFiles([]); $('title').value = ''; $('file').value = '';
    songsPoll.now();
  } catch (e) {
    toast(e.message, 'err', 4000);
  }
});
$('title').onkeydown = e => { if (e.key === 'Enter') $('uploadBtn').click(); };

// ---------------- 曲子包 ----------------
async function importBundles(files) {
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
const selectable = s => s.status !== 'corrupt';
function setSelecting(on) {
  selecting = on; selected.clear();
  $('selectBar').hidden = !on;
  $('selectBtn').hidden = on;
  renderSongs();
  (on ? $('selectAll') : $('selectBtn')).focus();
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
$('exportBtn').onclick = () => api.withBusy($('exportBtn'), async () => {
  const ids = visibleSongs().filter(s => selected.has(s.id)).map(s => s.id);  // 按列表顺序
  if (await exportSongs(ids)) setSelecting(false);
});
addEventListener('keydown', e => { if (e.key === 'Escape' && selecting && !UI.hasLayer()) setSelecting(false); });

// ---------------- 曲库列表 ----------------
const SOURCE_ICON = { image: 'image', pdf: 'file', gp: 'score', musicxml: 'score' };
const isScore = s => s.source_type === 'gp' || s.source_type === 'musicxml'; // GP / MusicXML：没有原谱页面，不需要识别
const canPlay = s => s.status !== 'corrupt' && (s.bars > 0 || isScore(s));

// “3 天前”：练习时间是本地时间字符串 “YYYY-MM-DD HH:MM”
function ago(at) {
  const d = new Date(String(at).replace(' ', 'T'));
  if (Number.isNaN(+d)) return String(at);
  const now = new Date(), mins = (now - d) / 60000;
  if (mins < 1) return '刚刚';
  if (mins < 60) return `${Math.floor(mins)} 分钟前`;
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  if (d >= today) return '今天';
  const days = Math.ceil((today - d) / 86400000);
  if (days === 1) return '昨天';
  if (days < 30) return `${days} 天前`;
  return d.getFullYear() === now.getFullYear() ? `${d.getMonth() + 1} 月 ${d.getDate()} 日` : `${d.getFullYear()} 年 ${d.getMonth() + 1} 月`;
}
function duration(sec) {
  const m = Math.round((sec || 0) / 60);
  if (m < 1) return '不到 1 分钟';
  if (m < 60) return `${m} 分钟`;
  return `${Math.floor(m / 60)} 小时${m % 60 ? ` ${m % 60} 分钟` : ''}`;
}
// 上次练的范围（小节号从 1 开始；后端已经按当前顺序排好 a_n ≤ b_n，小节被删了是 null）
function rangeText(s) {
  const l = s.practice && s.practice.last;
  if (!l || l.a_n == null || l.b_n == null) return '';
  if (l.a_n === 1 && l.b_n === s.bars && s.bars > 1) return '全曲';
  return l.a_n === l.b_n ? `第 ${l.a_n} 小节` : `第 ${l.a_n}–${l.b_n} 小节`;
}
function practiceText(s) {
  const p = s.practice;
  if (!p || !p.last_at) return '';
  const range = rangeText(s);
  return [ago(p.last_at), range && (range === '全曲' ? '练全曲' : `练到${range}`), p.total_seconds > 0 && `累计 ${duration(p.total_seconds)}`].filter(Boolean).join(' · ');
}

// 需要注意的状态（忙、出错、待核对、警告）；一切正常时返回 ''，小节数放在说明行里
function stateHTML(s) {
  const n = s.notice;
  if (s.status === 'corrupt') return `<span class="dot err"></span><span class="err-text">${esc(n && n.msg)}（可在“…”菜单删除）</span>`;
  if (s.busy) {
    const p = s.progress || {};
    const pct = p.total ? Math.round(100 * p.done / p.total) : 6;
    const unit = s.recognizing ? '行' : '批';
    return `<span class="spinner"></span><span>${esc(p.stage || '排队中')}${p.total ? ` · ${p.done}/${p.total} ${unit}` : ''}</span><span class="progress" role="progressbar" aria-label="进度" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"><i style="width:${pct}%"></i></span>`;
  }
  if (s.status === 'error') return `<span class="dot err"></span><span class="err-text">${isScore(s) ? '曲谱读取失败' : '识别失败'}${n ? `：${esc(n.msg)}` : ''}</span>`;
  if (s.status === 'cancelled') return `<span class="dot warn"></span><span>已取消${s.bars ? ` · 已识别 ${s.bars} 小节` : ''}</span>`;
  const review = s.needs_review_bars || 0;
  const parts = [];
  if (review) parts.push(`<span class="badge warn" title="有 ${review} 个小节需要对照原谱核对">${review} 小节待核对</span>`);
  if (n) parts.push(`<span class="badge ${n.kind === 'error' ? 'err' : 'warn'} notice-badge" title="${esc(n.msg)}">${esc(n.msg)}</span>`);
  return parts.join('');
}
function barsText(s) {
  if (s.status === 'corrupt' || s.busy || s.status === 'error' || s.status === 'cancelled') return '';
  return s.bars ? `${s.bars} 小节` : isScore(s) ? '已导入' : '';
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

function rowHTML(s) {
  const play = canPlay(s), href = `/trainer?id=${encodeURIComponent(s.id)}`;
  const pick = selecting && selectable(s), on = pick && selected.has(s.id);
  const src = SOURCES[s.source_type] || '';
  const sub = [src, barsText(s), esc(s.artist), s.created && `添加于 ${esc(s.created)}`, costText(s)].filter(Boolean).join(' · ');
  const state = stateHTML(s);
  const prac = practiceText(s);
  const range = rangeText(s), last = s.practice && s.practice.last;
  const tags = (s.tags || []).length ? `<span class="tags">${s.tags.map(t => `<button class="badge tag" data-tag="${esc(t)}" data-k="tag:${esc(t)}" title="只看“${esc(t)}”">${esc(t)}</button>`).join('')}</span>` : '';
  const title = play && !selecting ? `<a class="title" href="${href}" data-k="open">${esc(s.title)}</a>` : `<span class="title">${esc(s.title)}</span>`;
  let action = '';
  if (play && last) action = `<a class="btn primary sm" data-k="resume" href="${href}&resume=1" title="${esc(`从${range || '上次的位置'}${last.bpm ? `、${last.bpm} BPM` : ''} 接着练`)}">${icon('repeat', 12)}接着练</a>`;
  else if (play) action = `<a class="btn primary sm" data-k="play" href="${href}">${icon('play', 12)}练习</a>`;
  return `<div class="row song-row${play || pick ? ' click' : ''}${on ? ' on' : ''}" data-id="${esc(s.id)}" data-play="${play ? 1 : ''}" data-pick="${pick ? 1 : ''}">
    ${selecting ? `<input type="checkbox" class="pick" data-k="pick" aria-label="选择 ${esc(s.title)}" ${on ? 'checked' : ''} ${pick ? '' : 'disabled'}>` : ''}
    <span class="src-ic" title="${esc(src ? `来源：${src}` : '')}">${icon(SOURCE_ICON[s.source_type] || 'music', 18)}</span>
    <div class="who"><div class="name">${title}${tags}</div>
      ${state ? `<div class="state">${state}</div>` : ''}
      ${prac ? `<div class="sub practice">${icon('clock', 12)}<span>${prac}</span></div>` : ''}
      ${sub ? `<div class="sub">${sub}</div>` : ''}</div>
    <div class="ctl"${selecting ? ' hidden' : ''}>${action}
      <button class="icon-btn sm" data-more="${esc(s.id)}" data-k="more" title="更多" aria-label="${esc(s.title)}：更多操作" aria-haspopup="menu">${icon('more')}</button></div>
  </div>`;
}

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
    const html = rowHTML(s);
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

// 搜索、标签筛选、排序（只在浏览器里做；排序方式记在本机）
const SORTS = ['practiced', 'created', 'title'];
const lib = { q: '', tag: '', sort: SORTS.includes(localStorage.getItem('tt.libSort')) ? localStorage.getItem('tt.libSort') : 'practiced' };
UI.seg($('sortSeg'), lib.sort, v => { lib.sort = v; localStorage.setItem('tt.libSort', v); renderSongs(); });
$('search').oninput = e => { lib.q = e.target.value.trim().toLowerCase(); renderSongs(); };
$('tagFilter').onclick = e => { const b = e.target.closest('button'); if (b) { lib.tag = b.dataset.tag; renderSongs(); } };
const byCreated = (a, b) => (b.created || '').localeCompare(a.created || '');
function visibleSongs() {
  const hit = s => [s.title, s.artist, ...(s.tags || [])].some(x => (x || '').toLowerCase().includes(lib.q));
  const list = lastSongs.filter(s => (!lib.tag || (s.tags || []).includes(lib.tag)) && (!lib.q || hit(s)));
  if (lib.sort === 'title') list.sort((a, b) => a.title.localeCompare(b.title, 'zh-CN', { numeric: true }));
  else if (lib.sort === 'practiced') { // 没练过的按添加时间排在后面
    const at = s => (s.practice && s.practice.last_at) || '';
    list.sort((a, b) => at(b).localeCompare(at(a)) || byCreated(a, b));
  } else list.sort(byCreated);
  return list;
}
function renderTagFilter() {
  const tags = [...new Set(lastSongs.flatMap(s => s.tags || []))].sort((a, b) => a.localeCompare(b, 'zh-CN'));
  if (lib.tag && !tags.includes(lib.tag)) lib.tag = '';
  setHTML($('tagFilter'), tags.length ? ['', ...tags].map(t => `<button data-tag="${esc(t)}" aria-pressed="${t === lib.tag}">${t ? esc(t) : '全部'}</button>`).join('') : '');
  $('tagFilter').hidden = !tags.length;
}
function renderSongs() {
  const songs = visibleSongs(), box = $('songs');
  if (selecting) updateSelectBar();
  $('libTools').hidden = !lastSongs.length;
  renderTagFilter();
  box.removeAttribute('aria-busy');
  if (!lastSongs.length) setHTML(box, '<div class="empty-state"><b>还没有曲子</b>上传原谱或 Guitar Pro 文件开始。</div>');
  else if (!songs.length) setHTML(box, '<div class="empty-state"><b>没有符合条件的曲子</b>换个关键词或标签试试。</div>');
  else renderList(box, songs);
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
  if (more) { e.stopPropagation(); openMore(more); return; }
  if (e.target.closest('a,button')) return;
  const row = e.target.closest('.row[data-play="1"]');
  if (row) location.href = row.querySelector('a.title').href;
});
function openMore(btn) {
  const id = btn.dataset.more, s = lastSongs.find(x => x.id === id);
  if (!s) return;
  if (s.busy) {
    UI.menu(btn, [{ label: s.recognizing ? '取消识别' : '取消生成讲解', icon: 'x', onClick: () => cancel(id) }]);
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
    image && { label: '整首重新识别', icon: 'refresh', onClick: () => reparse(s) },
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
  const to = +card.dataset.i, after = e.offsetX > card.offsetWidth / 2 ? 1 : 0;
  movePage(pg.drag, pg.drag < to ? to - 1 + after : to + after);
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
function showSettings(c) {
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
  $('baseUrl').value = c.base_url;
}
async function save(body, msg = '已保存') {
  try { showSettings(await api.put('/api/settings', body)); toast(msg); } catch (e) { toast(e.message, 'err'); }
}
const setModelSeg = UI.seg($('modelSeg'), 'claude-opus-5-5', v => {
  $('modelCustom').hidden = v !== 'custom';
  if (v === 'custom') $('modelCustom').focus(); else save({ model: v });
});
$('modelCustom').onchange = () => { if ($('modelCustom').value.trim()) save({ model: $('modelCustom').value.trim() }); };
const setEffortSeg = UI.seg($('effortSeg'), 'high', v => save({ effort: v }));
$('baseUrl').onchange = () => save({ base_url: $('baseUrl').value });
$('saveKey').onclick = () => api.withBusy($('saveKey'), async () => {
  const k = $('apiKey').value.trim();
  if (!k) { toast('先填写 key', 'err'); $('apiKey').focus(); return; }
  $('apiKey').value = '';
  await save({ api_key: k }, 'API key 已保存');
});
$('apiKey').onkeydown = e => { if (e.key === 'Enter') $('saveKey').click(); };
$('clearKey').onclick = async () => {
  if (await UI.confirm('清除后识别原谱前要重新填写。', { title: '清除已保存的 API key？', ok: '清除', danger: true })) save({ clear_api_key: true }, '已清除');
};
$('testBtn').onclick = () => api.withBusy($('testBtn'), async () => {
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
