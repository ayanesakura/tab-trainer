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
const themeIcon = () => { $('themeBtn').innerHTML = icon(UI.isDark() ? 'moon' : 'sun'); };
$('themeBtn').onclick = () => { UI.setTheme(UI.isDark() ? 'light' : 'dark'); };
const setThemeSeg = UI.seg($('themeSeg'), UI.getTheme(), v => UI.setTheme(v));
document.addEventListener('themechange', e => { setThemeSeg(e.detail); themeIcon(); });
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', themeIcon);

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
  const all = lastSongs.filter(selectable);
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
  if (e.target.checked) lastSongs.filter(selectable).forEach(s => selected.add(s.id));
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
  if (s.status === 'parsing' || s.status === 'queued' || s.job === 'explain') {
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
function renderSongs() {
  const songs = lastSongs;
  if (selecting) updateSelectBar();
  if (!songs.length) {
    $('songs').innerHTML = `<div class="empty-state"><b>还没有曲子</b>上传一份谱子开始。</div>`;
  } else {
    const SOURCE = { image: '图片', pdf: 'PDF', gp: 'GP', musicxml: 'MusicXML' }; // 导入渠道
    $('songs').innerHTML = songs.map(s => {
      const busy = s.status === 'parsing' || s.status === 'queued';
      const canPlay = !busy && (s.bars > 0 || s.kind === 'score');
      const sub = [s.artist, s.created].filter(Boolean).map(esc).join(' · ');
      const pick = selecting && selectable(s), on = pick && selected.has(s.id);
      return `<div class="row song-row ${canPlay || pick ? 'click' : ''} ${on ? 'on' : ''}" data-id="${s.id}" data-play="${canPlay ? 1 : ''}" data-pick="${pick ? 1 : ''}">
        ${selecting ? `<input type="checkbox" class="pick" aria-label="选择 ${esc(s.title)}" ${on ? 'checked' : ''} ${pick ? '' : 'disabled'}>` : ''}
        <div class="tile" style="background:${tileColor(s.title)}">${esc([...s.title][0] || '♪')}</div>
        <div class="who"><div class="name">${esc(s.title)}${SOURCE[s.source_type] ? ` <span class="badge">${SOURCE[s.source_type]}</span>` : ''}</div><div class="state">${songState(s)}</div>${sub ? `<div class="sub">${sub}</div>` : ''}</div>
        <div class="ctl" ${selecting ? 'hidden' : ''}>
          ${canPlay ? `<a class="btn primary sm" href="/trainer?id=${s.id}">${icon('play', 12)}练习</a>` : ''}
          <button class="icon-btn sm" data-more="${s.id}" data-busy="${busy ? 1 : ''}" data-corrupt="${s.status === 'corrupt' ? 1 : ''}" title="更多">${icon('more')}</button>
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
  const more = e.target.closest('[data-more]');
  if (more) {
    e.stopPropagation();
    const id = more.dataset.more;
    if (more.dataset.busy) {
      UI.menu(more, [{ label: '取消识别', icon: 'x', onClick: () => cancel(id) }]);
      return;
    }
    if (more.dataset.corrupt) {
      UI.menu(more, [{ label: '删除', icon: 'trash', danger: true, onClick: () => del(id) }]);
      return;
    }
    UI.menu(more, [
      { label: '整首重新识别', icon: 'refresh', onClick: () => reparse(id) },
      { label: '导出曲子包', icon: 'download', onClick: () => exportSongs([id]) },
      'sep',
      { label: '删除', icon: 'trash', danger: true, onClick: () => del(id) },
    ]);
    return;
  }
  if (e.target.closest('a,button')) return;
  const row = e.target.closest('.row[data-play="1"]');
  if (row) location.href = '/trainer?id=' + row.dataset.id;
});
async function reparse(id) {
  if (!confirm('重新识别会覆盖现有结果，包括手工修改。继续？')) return;
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
  route(); themeIcon();
  api('/api/settings').then(showSettings).catch(() => {});
  loadSongs();
});
