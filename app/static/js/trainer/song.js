// 加载曲子并刷新页面各部分：标题和提示条、段落按钮、练习建议、调弦表、指板、谱面；
// 识别还在进行时定时轮询。也包括“让 AI 讲解”和调弦 / 变调夹 / 拍号的保存。

function applySong(song) {
  if (np) { // 正在改的小节被删掉或内容变了才关面板；别的刷新（比如后台轮询）不打断
    const k = song.bars.findIndex(b => b.id === np.barId);
    if (k < 0 || JSON.stringify(song.bars[k].events) !== np.snap) closeNotePop(); else np.bi = k;
  }
  SONG = song;
  const meta = song.meta || {};
  const tm = meta.tuning_midi || [64, 59, 55, 50, 45, 40];
  OPEN = {}; OPEN_NAME = {};
  tm.forEach((m, i) => { OPEN[i + 1] = m; OPEN_NAME[i + 1] = NN[m % 12]; });
  CAPO = meta.capo || 0;
  const ts = meta.time_signature || [4, 4];
  BLS = song.bars.map(b => { const t = b.time_signature || ts; return t[0] * 4 / t[1]; });
  PAGES = song.pages.map(p => `/files/${song.id}/${p.file}`);
  BARS = song.bars.map((b, bi) => {
    let t = 0;
    const evs = b.events && b.events.length ? b.events : [{ dur: String(barLen(bi)), notes: [] }]; // 空小节按整小节休止显示
    return evs.map(e => {
      const notes = (e.notes || [])
        .filter(n => n.string >= 1 && n.string <= 6 && Number.isFinite(n.fret)) // 非法弦号会让音高变成 NaN，播放卡死
        .map(n => ({ s: n.string, f: n.fret, tech: new Set(n.tech || []), grace: n.grace >= 0 ? n.grace : null, bend: Number.isFinite(n.bend) ? n.bend : 0 }));
      const all = new Set(); notes.forEach(n => n.tech.forEach(k => all.add(k)));
      const d = frac(e.dur), r = { t, d, notes, tech: all };
      t += d;
      return r;
    }).filter(e => e.t < barLen(bi) - 1e-9); // 时值超出小节长度的部分不播放（会在提示里标出）
  });
  TIPS = song.bars.map(b => b.tip);
  REG = song.bars.map(b => (b.region ? [b.page, ...b.region] : null));
  if (song.kind === 'score') beatMap = song.bars.map(b => { let t = 0; return b.events.map(e => { const o = t; t += scoreConv.durToTicks(e.dur); return o; }); });
}

function renderHeader() {
  const m = SONG.meta || {};
  const title = SONG.title || m.title || '未命名';
  document.title = title + ' · 吉他谱练习器';
  $('songTitle').textContent = title;
  const parts = [m.artist, m.arranger && m.arranger + ' 编配', m.tuning && '调弦 ' + [6, 5, 4, 3, 2, 1].map(s => OPEN_NAME[s]).join(' '),
    '变调夹 ' + (CAPO ? CAPO + ' 品' : '无'), m.time_signature && m.time_signature.join('/') + ' 拍' + (SONG.bars.some(b => b.time_signature) ? '（中途有变拍号）' : ''), `共 ${SONG.bars.length} 小节`];
  $('songSub').textContent = parts.filter(Boolean).join(' · ');
  $('metaNote').style.display = m.notes ? '' : 'none';
  $('metaNote').innerHTML = m.notes ? `<span class="dot" style="background:var(--accent)"></span><div><b>AI 读谱时注意到：</b>${esc(m.notes)}</div>` : '';
  const running = SONG.running || SONG.status === 'parsing' || SONG.status === 'queued';
  const pr = SONG.progress || {};
  const explaining = SONG.job === 'explain';
  const what = explaining
    ? `正在生成讲解${pr.total ? `（${pr.done}/${pr.total} 批）` : ''}。可以照常练习，写好的提示会自动出现。`
    : SONG.restaging
      ? `正在重新识别：${esc(pr.stage || '')}${pr.total ? `（${pr.done}/${pr.total} 行）` : ''}。全部完成后才替换现在的结果，这期间不能改谱。`
      : `正在识别：${esc(pr.stage || '')}${pr.total ? `（${pr.done}/${pr.total} 行）` : ''}。已识别的小节可以先练，页面会自动刷新。`;
  $('progressBox').style.display = running ? '' : 'none';
  $('progressBox').innerHTML = running ? `<span class="spinner" style="margin-top:2px"></span><div style="flex:1">${what}</div><button class="btn sm" id="cancelBtn">${explaining ? '取消' : '取消识别'}</button>` : '';
  if (running) $('cancelBtn').onclick = async () => {
    const r = await fetch(`/api/songs/${SONG_ID}/cancel`, { method: 'POST' });
    UI.toast(r.ok ? '正在取消，进行中的那几行完成后停止' : ((await r.json()).detail || '取消失败'), r.ok ? 'info' : 'err');
  };
  $('errorBox').style.display = SONG.error ? '' : 'none';
  $('errorBox').innerHTML = SONG.error ? `<span class="dot err" style="margin-top:4px"></span><div>${SONG.kind === 'score' && SONG.status === 'error' ? '曲谱读取失败' : '出错了'}：${esc(SONG.error)}</div>` : '';
  if (m.tuning) {
    $('mTitle').value = title;
    m.tuning.forEach((n, i) => { $('mT' + (i + 1)).value = n; });
    $('mCapo').value = CAPO; $('mTs').value = (m.time_signature || [4, 4]).join('/');
  }
}

function renderSections() {
  const host = $('secBtns'); host.innerHTML = '';
  const groups = sections();
  const warnAt = i => { const b = SONG.bars[i]; return (b.warn && b.warn.length) || b.uncertain; };
  const add = (label, a, z) => {
    const btn = document.createElement('button'); btn.textContent = label; btn.dataset.a = a; btn.dataset.z = z; btn.onclick = () => setLoop(a, z);
    if (label !== '全曲' && SONG.bars.slice(a, z + 1).some((_, k) => warnAt(a + k))) { btn.classList.add('warn'); btn.title = '这一段有可能识别有误的小节'; }
    host.appendChild(btn);
  };
  if (SONG.bars.length) add('全曲', 0, SONG.bars.length - 1); // 放最前面：窄屏时也不会被挤出可见范围
  groups.forEach(g => add(g.a === g.z ? `${g.a + 1}` : `${g.a + 1}–${g.z + 1}`, g.a, g.z));
  updateLoopUI();
}

function renderPlan() {
  const p = SONG.plan;
  if (!p) {
    const can = SONG.bars.length && SONG.status !== 'parsing' && SONG.status !== 'queued';
    $('plan').innerHTML = `<div class="list"><div class="empty-state"><b>还没有练习建议</b>${can ? `可以根据现在的谱子生成（只发文字，费用很低）。<div style="margin-top:10px"><button class="btn sm primary" id="planCreate">${UI.icon('sparkle')}生成练习建议</button></div>` : '识别完成后会自动生成。'}</div></div>`;
    if (can) $('planCreate').onclick = () => $('explainBtn').click();
    return;
  }
  const staleNote = p.stale ? `<div class="notice" style="margin-bottom:12px"><span class="dot warn" style="margin-top:4px"></span><div style="flex:1">谱子在生成这些建议之后改过（改音、重新识别或改调弦），下面的内容可能已过时。</div><button class="btn sm" id="planRefresh">${UI.icon('refresh')}重新生成</button></div>` : '';
  const steps = (p.steps || []).map((st, i) => {
    const m = /^(\d+)\s*[-–~]\s*(\d+)$/.exec(st.bars.trim()) || /^(\d+)$/.exec(st.bars.trim());
    const a = m ? +m[1] - 1 : null, z = m ? +(m[2] || m[1]) - 1 : null;
    const link = a != null && a < BARS.length ? `<button class="btn sm" data-a="${a}" data-z="${Math.min(z, BARS.length - 1)}">${UI.icon('repeat', 13)}循环这段</button>` : '';
    return `<div class="row plan-row"><span class="step">${i + 1}</span><div class="who"><div class="name">${esc(st.title)} <span class="badge">第 ${esc(st.bars)} 小节</span></div><div class="sub">${esc(st.advice)}</div></div><div class="ctl">${link}</div></div>`;
  }).join('');
  $('plan').innerHTML = staleNote + (p.overview ? `<div class="card pad" style="margin-bottom:12px;line-height:1.7">${esc(p.overview)}</div>` : '') + `<div class="list">${steps}</div>`;
  if (p.stale) $('planRefresh').onclick = () => $('explainBtn').click();
  $('plan').querySelectorAll('button[data-a]').forEach(b => b.onclick = () => { setLoop(+b.dataset.a, +b.dataset.z); showView('practice'); });
}

let renderedKey = '';
let renderedIds = [];
function render(force) {
  renderHeader();
  updateBarTools();
  const key = JSON.stringify([SONG.bars, SONG.meta, SONG.systems.map(s => s.status), SONG.plan]);
  if (!force && key === renderedKey) return;
  renderedKey = key;
  // 按小节 id 记住循环范围和正在看的小节：重新识别某一行后小节编号可能整体移动
  const prevIds = renderedIds, prevA = state.a, prevB = state.b;
  const wasAll = !prevIds.length || (prevA === 0 && prevB === prevIds.length - 1);
  const idA = prevIds[prevA], idB = prevIds[prevB], idShown = prevIds[shownBar];
  buildAll(); renderTune(); renderFretboard(); renderSections(); renderPlan();
  const isScore = SONG.kind === 'score';
  $('cropCard').hidden = isScore; $('infoGrid').classList.toggle('nocrop', isScore);
  // 讲解按钮：有小节缺提示（GP 刚导入、改过音或调弦）或练习建议缺失 / 过时就显示；生成中由进度条提示
  const missing = SONG.bars.filter(b => !(b.tip || '').trim()).length, planOld = !SONG.plan || SONG.plan.stale;
  const busyNow = SONG.running || SONG.status === 'parsing' || SONG.status === 'queued';
  $('explainBox').hidden = busyNow || !SONG.bars.length || (!missing && !planOld);
  const fresh = isScore && missing === SONG.bars.length;
  setButton($('explainBtn'), UI.icon('sparkle') + (fresh ? '让 AI 讲解这首曲子' : '让 AI 补写提示和练习建议'));
  $('explainHint').textContent = fresh
    ? '根据谱子数据给每个小节写练习提示，并生成练习建议（只发文字，费用很低）。'
    : `${[missing ? `${missing} 个小节还没有提示` : '', !SONG.plan ? '还没有练习建议' : planOld ? '练习建议是改谱之前写的，需要更新' : ''].filter(Boolean).join('，')}（只发文字，费用很低）。`;
  $('fixBox').hidden = isScore;
  $('editModeBtn').hidden = isScore;
  if (isScore && editMode) setEditMode(false);
  loadScore();
  if (!imgs.length || imgs.length !== PAGES.length) loadImages();
  renderedIds = SONG.bars.map(b => b.id);
  if (!BARS.length) {
    if (!isScore) $('score').innerHTML = '<div class="empty-state"><b>还没有识别出的小节</b>识别完成后会出现在这里。</div>';
    return;
  }
  const n = BARS.length - 1, find = (id, fallback) => { const i = renderedIds.indexOf(id); return i >= 0 ? i : Math.min(fallback, n); };
  let a = wasAll ? 0 : find(idA, prevA), z = wasAll ? n : find(idB, prevB);
  if (z < a) z = a;
  const keep = idShown ? renderedIds.indexOf(idShown) : -1;
  shownBar = -1;
  setLoop(a, z, false, keep < 0); // 正在看的小节还在就直接显示它，中途不跳到别的小节（否则编辑器会被关掉）
  if (keep >= 0) showBar(keep);
}

let importErrorSent = false;
async function reportImportError(msg) {
  // GP / MusicXML 第一次打开就解析失败：记到曲库里（不再显示成可以练习），页面上也说清楚
  UI.toast('读取曲谱失败：' + msg, 'err', 8000);
  if (importErrorSent) return;
  importErrorSent = true;
  await fetch(`/api/songs/${SONG_ID}/import-error`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ error: msg }) }).catch(() => {});
  await refresh(false);
}

async function fetchSong() {
  const r = await fetch(`/api/songs/${encodeURIComponent(SONG_ID)}`);
  if (!r.ok) throw new Error(r.status === 404 ? '曲子不存在' : '加载失败 ' + r.status);
  return r.json();
}
let pollTimer = null;
async function refresh(force) {
  try {
    const song = await fetchSong();
    applySong(song); render(force);
    if (song.bars.length) renderResume();
    const busy = song.running || song.status === 'parsing' || song.status === 'queued' || song.systems.some(s => s.status === 'parsing');
    clearTimeout(pollTimer);
    if (busy) pollTimer = setTimeout(() => refresh(false), 2500);
  } catch (e) {
    $('songTitle').textContent = e.message;
  }
}

function initSong() {
  $('explainBtn').onclick = async () => {
    $('explainBtn').disabled = true;
    const r = await fetch(`/api/songs/${SONG_ID}/explain`, { method: 'POST' });
    if (!r.ok) UI.toast((await r.json()).detail || '请求失败', 'err', 5000);
    else UI.toast('AI 正在写讲解，完成后提示会出现在每个小节里', 'info');
    $('explainBtn').disabled = false;
    await refresh(true);
  };
  $('metaSave').onclick = async () => {
    const tuning = [1, 2, 3, 4, 5, 6].map(i => $('mT' + i).value.trim());
    const ts = $('mTs').value.split('/').map(x => parseInt(x, 10));
    const body = { title: $('mTitle').value, tuning, capo: parseInt($('mCapo').value || '0', 10), time_signature: ts };
    const r = await fetch(`/api/songs/${SONG_ID}/meta`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (r.ok) UI.toast('已保存'); else UI.toast('保存失败：' + ((await r.json()).detail || r.status), 'err', 4000);
    if (r.ok) await refresh(true);
  };
}
