// 曲库页：上传谱子、曲子列表（识别进度、重新识别、删除）、设置（API key、模型、主题）
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
function setFiles(list) {
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
  if (s.status === 'corrupt') return `<span class="dot err"></span><span class="err-text">${esc(s.error)}（可以在“…”菜单里删除）</span>`;
  if (s.status === 'error') return `<span class="dot err"></span><span class="err-text">${s.kind === 'score' ? '曲谱读取失败' : '识别失败'}：${esc(s.error)}</span>`;
  if (s.status === 'cancelled') return `<span class="dot warn"></span><span>已取消${s.bars ? ` · 已识别 ${s.bars} 小节` : ''}</span>`;
  if (s.kind === 'score' && !s.bars) return `<span class="dot ok"></span><span>已导入，打开练习页读取曲谱</span>`;
  const parts = [`<span class="dot ${s.warn_bars ? 'warn' : 'ok'}"></span><span>${s.bars} 小节</span>`];
  if (s.warn_bars) parts.push(`<span class="badge warn">${s.warn_bars} 处待核对</span>`);
  if (s.error) parts.push(`<span class="badge err">部分行识别失败</span>`);
  return parts.join('');
}

let pollTimer = null;
async function loadSongs() {
  let songs;
  try { songs = await api('/api/songs'); } catch (e) { $('songs').innerHTML = `<div class="empty-state"><b>加载失败</b>${esc(e.message)}</div>`; return; }
  if (!songs.length) {
    $('songs').innerHTML = `<div class="empty-state"><b>还没有曲子</b>上传一份谱子开始练习吧。</div>`;
  } else {
    $('songs').innerHTML = songs.map(s => {
      const busy = s.status === 'parsing' || s.status === 'queued';
      const canPlay = !busy && (s.bars > 0 || s.kind === 'score');
      const sub = [s.artist, s.created].filter(Boolean).map(esc).join(' · ');
      return `<div class="row song-row ${canPlay ? 'click' : ''}" data-id="${s.id}" data-play="${canPlay ? 1 : ''}">
        <div class="tile" style="background:${tileColor(s.title)}">${esc([...s.title][0] || '♪')}</div>
        <div class="who"><div class="name">${esc(s.title)}${s.kind === 'score' ? ' <span class="badge accent">GP</span>' : ''}</div><div class="state">${songState(s)}</div>${sub ? `<div class="sub">${sub}</div>` : ''}</div>
        <div class="ctl">
          ${canPlay ? `<a class="btn primary sm" href="/trainer?id=${s.id}">${icon('play', 12)}练习</a>` : ''}
          <button class="icon-btn sm" data-more="${s.id}" data-busy="${busy ? 1 : ''}" data-corrupt="${s.status === 'corrupt' ? 1 : ''}" title="更多">${icon('more')}</button>
        </div>
      </div>`;
    }).join('');
  }
  clearTimeout(pollTimer);
  if (songs.some(s => s.status === 'parsing' || s.status === 'queued' || s.job)) pollTimer = setTimeout(loadSongs, 2500);
}
$('songs').addEventListener('click', e => {
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
  if (!confirm('整首重新识别会覆盖现有结果（包括手工修改），并再次产生模型调用费用。继续？')) return;
  try { await api(`/api/songs/${id}/parse`, { method: 'POST' }); toast('已开始重新识别', 'info'); } catch (e) { toast(e.message, 'err'); }
  loadSongs();
}
async function cancel(id) {
  try { await api(`/api/songs/${id}/cancel`, { method: 'POST' }); toast('正在取消，进行中的那几行完成后停止', 'info'); } catch (e) { toast(e.message, 'err'); }
  loadSongs();
}
async function del(id) {
  if (!confirm('删除这首曲子（包括上传的谱子图片和识别结果）？')) return;
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
    : '只保存在本机 <code>data/config.json</code>（仅本人可读）。也可以改为启动前设置环境变量 <code>ANTHROPIC_API_KEY</code>。';
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
