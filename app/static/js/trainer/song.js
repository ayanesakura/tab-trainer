// 加载曲子并保持最新：取 /api/songs/<id> 放进 store（set('song')），由它派生 view（deriveView），
// 页面各部分各自订阅。识别 / 讲解进行中时定时轮询（出错退避重试，不会停）。
// 也包括：曲名和副标题、通知（进度 / 出错 / 讲解 / AI 备注 / 接着练）、练习建议页、曲子信息的保存、
// GP / MusicXML 第一次打开时把谱子转换成我们的小节数据。

import { get, set, on } from './store.js';
import { $, UI, esc, songUrl, SONG_ID } from './dom.js';
import { get as apiGet, post, put, withBusy, poll, transient } from '../lib/api.js';
import { SOURCES } from '../vocab.js';
import { deriveView, isScore, songSubtitle, explainState, resumeInfo, planRange, showNote, tuningInput } from './view.js';
import { DERIVE_VERSION, deriveSong } from '../score.js';
import { alphaTab, setLoop, setTempo, setPlayerHooks, revealLoop } from './player.js';
import { notice } from './notices.js';
import { refineConfirm, NO_REFINE, refineScopeLabel } from '../lib/refine.js';


// ---------------------------------------------------------------------
// 取曲子、轮询
// ---------------------------------------------------------------------
let seq = 0, applied = 0, poller = null, polling = false;
async function load() {
  const my = ++seq;
  let s;
  try {
    s = await apiGet(songUrl());
  } catch (e) {
    if (e.name === 'AbortError') throw e;
    if (!transient(e)) { // 404、数据损坏（500 带说明）……重试也没用：停止轮询，说清楚原因
      polling = false; if (poller) poller.stop();
      if (e.status === 404) $('songTitle').textContent = '曲子不存在';
      else {
        if (!get('song')) $('songTitle').textContent = '加载失败';
        notice('offline', { kind: 'err', html: `加载失败：${esc(e.message)}`, actions: [{ label: '重试', onClick: () => refresh() }] });
      }
      return null;
    }
    if (!get('song')) $('songTitle').textContent = '加载失败';
    notice('offline', { kind: 'err', html: `${esc(e.message)}，稍后自动重试。` });
    throw e; // poll 退避重试
  }
  notice('offline', null);
  if (my < applied) return get('song'); // 晚到的旧结果
  applied = my;
  set('song', s);
  return s;
}
/** 写接口返回了最新的曲子：直接用它（比它早发出的读取结果作废） */
export function applySong(s) {
  applied = ++seq;
  set('song', s);
}
/** 写接口报 409（别处改过）：提示并刷新 */
export async function conflict(e) {
  UI.toast(e.message, 'err', 5000);
  await refresh();
}
/** 423：曲子有任务在跑 / 这个小节锁着（不是冲突，内容没在别处改过）：只把服务给的说明告诉用户，等一会再试 */
export const isLocked = e => !!e && e.status === 423;
export const lockedToast = e => UI.toast(e.message, 'info', 5000);
/** 写接口出错的统一处理：409 → conflict（提示并刷新）；423 → 只提示；其他 → prefix + 说明 */
export async function writeFailed(e, prefix = '') {
  if (e.status === 409) await conflict(e);
  else if (isLocked(e)) lockedToast(e);
  else UI.toast(prefix + e.message, 'err', 5000);
}
/** 第一次加载并开始轮询：识别 / 讲解进行中每 2.5 秒取一次，出错时退避重试（不会停） */
export function startLoading() {
  polling = true;
  poller = poll(load, { interval: 2500, while: s => (polling = !!s && s.busy) });
}
/** 马上重新取一次（改谱、开始识别以后）；后台任务又开始了就恢复轮询 */
export async function refresh() {
  let s;
  try { s = await load(); } catch (e) {
    // 网络断了等临时错误：通知里说了“稍后自动重试”，就交给轮询退避重试（轮询可能已经停了）
    if (transient(e) && poller) { polling = true; poller.now(); } else UI.toast(e.message, 'err', 5000);
    return;
  }
  if (s && s.busy && !polling) { polling = true; poller.now(); }
}

// ---------------------------------------------------------------------
// 顶栏：曲名、来源、副标题
// ---------------------------------------------------------------------
function renderHeader(s) {
  const m = s.meta || {}, v = get('view');
  const title = s.title || m.title || '未命名';
  document.title = title + ' · 吉他谱练习器';
  // 太长会被截断：悬停看全名；来源（图片 / GP …）也只放在悬停提示里，顶栏不再单独占一个标签
  const src = SOURCES[s.source_type];
  $('songTitle').textContent = title; $('songTitle').title = src ? `${title}（来源：${src}）` : title;
  $('songSub').textContent = $('songSub').title = songSubtitle(s, v);
}

// ---------------------------------------------------------------------
// 通知
// ---------------------------------------------------------------------
const dismissed = new Set(); // 这次打开页面里关掉的通知（内容变了会重新出现）
function closable(key) { return dismissed.has(key) ? null : () => dismissed.add(key); }

function progressText(s) {
  const a = s.activity || {}, pr = s.progress || {};
  const unit = { explain: '批', refine: '小节' }[a.kind] || '行';
  const n = pr.total ? `（${pr.done}/${pr.total} ${unit}）` : '';
  if (a.kind === 'explain') return `正在生成讲解${n}，可以照常练习。`;
  if (a.kind === 'refine') {
    const label = esc(refineScopeLabel(a, s.bars.map(b => b.id)));
    if (a.scope === 'bars' && (a.bars || []).length === 1) return `正在精修${label}，可以照常练习。`;
    if (a.scope === 'bars' || a.scope === 'row') return `正在精修${label}${pr.total ? `：已核对 ${pr.done}/${pr.total}` : ''}，可以照常练习。`;
    return `正在精修整首：逐小节放大核对${n}，可以照常练习；改了的小节会标出来。`;
  }
  if (a.kind === 'reparse_row') return `${esc(pr.stage || '正在重新识别')}${n}，其他小节可以照常练习。`;
  if (a.restaging) return `正在重新识别：${esc(pr.stage || '')}${n}，完成前不能改谱。`;
  return `正在识别：${esc(pr.stage || '')}${n}，已识别的小节可以先练。`;
}

async function retryFailed(btn, n) {
  const ok = await UI.confirm('会再调用 AI，只重新识别失败的行；已经识别好的小节不动。', { title: `重试失败的 ${n} 行？`, ok: '重试' });
  if (!ok) return;
  await withBusy(btn, async () => {
    try { await post(songUrl('/retry-failed')); UI.toast('已开始重试失败的行', 'info'); }
    catch (e) { UI.toast(e.message, 'err', 5000); }
  });
  await refresh();
}

// 精修整首：确认框里给出要核对的小节数、预估费用和时间（后端算好的 refine_estimate）
async function startRefine(btn) {
  const s = get('song'), c = refineConfirm(s && s.refine_estimate);
  if (!c) { UI.toast(NO_REFINE, 'info', 5000); return; }
  if (!await UI.confirm(c.text, { title: c.title, ok: c.ok })) return;
  await withBusy(btn, async () => {
    try { await post(songUrl('/refine')); UI.toast('已开始精修整首', 'info'); }
    catch (e) { if (isLocked(e)) lockedToast(e); else UI.toast(e.message, 'err', 5000); }
  });
  await refresh();
}
function renderRefineBtn(s) {
  const btn = $('refineBtn');
  btn.hidden = isScore(s) || !s.bars.length;
  btn.disabled = !!s.busy;
  btn.title = s.activity && s.activity.kind === 'refine' ? `正在精修${refineScopeLabel(s.activity, s.bars.map(b => b.id))}`
    : s.refined ? `上次精修整首：${s.refined.at}，检查了 ${s.refined.checked} 个小节，改了 ${s.refined.changed} 个`
    : '精修整首：每小节放大，让 AI 对照原谱逐拍核对（约 $0.2 / 小节）';
}

async function explain(btn) {
  await withBusy(btn, async () => {
    try { await post(songUrl('/explain')); UI.toast('AI 正在写讲解', 'info'); }
    catch (e) { UI.toast(e.message, 'err', 5000); }
  });
  await refresh();
}

function renderNotices(s) {
  const running = !!s.activity;
  const kind = running && s.activity.kind;
  notice('progress', running ? {
    kind: 'info', spinner: true, html: progressText(s),
    actions: [{ label: { explain: '取消', refine: '取消精修' }[kind] || '取消识别', onClick: btn => withBusy(btn, async () => {
      try { await post(songUrl('/cancel')); UI.toast('正在取消…', 'info'); }
      catch (e) { UI.toast(e.message, 'err'); }
      await refresh();
    }) }],
  } : null);

  // 出错 / 警告：只显示后端给的 notice；有识别失败的行时带上“重试失败的 N 行”（只重新识别这几行）
  const n = s.notice;
  const errKey = n ? 'err:' + n.msg : '';
  const scoreFail = isScore(s) && s.status === 'error';
  const failed = !running && !isScore(s) && s.failed_rows > 0 ? s.failed_rows : 0;
  const retry = failed ? [{ label: `重试失败的 ${failed} 行`, icon: 'refresh', primary: true, onClick: btn => retryFailed(btn, failed) }] : [];
  // info：任务做完的说明（精修完成），不是警告
  const head = scoreFail ? '曲谱读取失败：' : n && n.kind === 'error' ? '出错了：' : n && n.kind === 'info' ? '' : '注意：';
  notice('error', n ? (dismissed.has(errKey) ? null : {
    kind: n.kind === 'error' ? 'err' : n.kind === 'info' ? 'ok' : 'warn',
    html: `${head}${esc(n.msg)}`, actions: retry, onDismiss: closable(errKey),
  }) : failed ? { kind: 'warn', html: `有 ${failed} 行识别失败，这几行的小节可能缺失或不完整。`, actions: retry } : null);

  // 让 AI 讲解：有小节缺提示（GP 刚导入、改过音或调弦）或练习建议过时就显示；没有练习建议的提示放在练习建议页
  const ex = explainState(s);
  const exKey = `ex:${ex.missing}:${ex.planStale}`;
  const showEx = !running && s.bars.length && (ex.missing || ex.planStale) && !dismissed.has(exKey);
  notice('explain', showEx ? {
    kind: '',
    html: ex.fresh ? '让 AI 为每个小节写练习提示，并生成练习建议。'
      : [ex.missing ? `${ex.missing} 个小节还没有提示` : '', ex.planStale ? '练习建议可能已过时（谱子改过）' : ''].filter(Boolean).join('；'),
    actions: [{ label: ex.fresh ? '让 AI 讲解这首曲子' : '让 AI 补写', icon: 'sparkle', onClick: explain }], // 不是主按钮：通知区最多一个主按钮（接着练）
    onDismiss: closable(exKey),
  } : null);

  // AI 读谱头时写的备注：关掉后同一段备注不再显示（备注变了才重新出现）
  const m = s.meta || {}, noteKey = 'tt.noteHidden.' + SONG_ID;
  notice('metaNote', m.notes && localStorage.getItem(noteKey) !== m.notes ? {
    // 备注常常很长：默认只显示一行，点一下（或 Tab 到它按回车 / 空格）展开 / 收起（样式 .n-fold，事件在 initSong 里接）
    kind: '', html: `<span class="n-fold" role="button" tabindex="0" aria-expanded="false" title="展开 / 收起"><b>备注</b>${esc(m.notes)}</span>`, onDismiss: () => localStorage.setItem(noteKey, m.notes),
  } : null);
}

let resumeDone = false;
function renderResume(s) {
  if (resumeDone || !s.bars.length) return;
  resumeDone = true;
  const r = resumeInfo(s);
  if (!r) return;
  const go = () => { setLoop(r.a, r.z); revealLoop(); if (r.bpm) setTempo(r.bpm); notice('resume', null); };
  if (new URLSearchParams(location.search).get('resume') === '1') {
    // 从曲库的“接着练”进来：直接接着练，地址里去掉 resume（刷新页面不会再跳）
    const u = new URL(location.href); u.searchParams.delete('resume'); history.replaceState(null, '', u);
    go();
    return;
  }
  notice('resume', {
    kind: 'info', html: `<span title="${esc(r.at)}">上次练到 <b>${r.label}</b> · ${r.bpm} BPM</span>`,
    actions: [{ label: '接着练', primary: true, onClick: go }], onDismiss: () => {},
  });
  // 开始播放了（练别的或已经接着练了）就收起这条提示，让出谱面的地方
  const off = on('play.playing', v => { if (v) { notice('resume', null); off(); } });
}

// ---------------------------------------------------------------------
// 练习建议页
// ---------------------------------------------------------------------
function renderPlan() {
  const s = get('song'), v = get('view');
  if (!s || !v) return;
  const p = s.plan, n = v.bars.length;
  if (!p) {
    const can = n && !s.recognizing;
    $('plan').innerHTML = `<div class="list"><div class="empty-state"><b>还没有练习建议</b>${can ? '<p>让 AI 根据全曲生成分段练习建议。</p><button class="btn sm primary" id="planCreate">' + UI.icon('sparkle') + '生成练习建议</button>' : '识别完成后会自动生成。'}</div></div>`;
    if (can) $('planCreate').onclick = e => explain(e.currentTarget);
    return;
  }
  const staleNote = p.stale ? `<div class="notice warn"><span class="dot warn"></span><div class="n-body">谱子改过，这些建议可能已过时。</div><div class="n-acts"><button class="btn sm" id="planRefresh">${UI.icon('refresh')}重新生成</button></div></div>` : '';
  const steps = (p.steps || []).map((st, i) => {
    const r = planRange(st.bars, n);
    const link = r ? `<button class="btn sm" data-a="${r.a}" data-z="${r.z}">${UI.icon('repeat', 13)}循环这段</button>` : '';
    return `<div class="row plan-row"><span class="step">${i + 1}</span><div class="who"><div class="name">${esc(st.title)} <span class="badge">第 ${esc(st.bars)} 小节</span></div><div class="sub">${esc(st.advice)}</div></div><div class="ctl">${link}</div></div>`;
  }).join('');
  $('plan').innerHTML = staleNote + (p.overview ? `<div class="card pad plan-overview">${esc(p.overview)}</div>` : '') + `<div class="list">${steps}</div>`;
  if (p.stale) $('planRefresh').onclick = e => explain(e.currentTarget);
  $('plan').querySelectorAll('button[data-a]').forEach(b => b.onclick = () => { setLoop(+b.dataset.a, +b.dataset.z); location.hash = '#practice'; requestAnimationFrame(revealLoop); });
}

// ---------------------------------------------------------------------
// 曲子信息（调弦页下面）
// ---------------------------------------------------------------------
function fillMeta(s) {
  const m = s.meta || {}, v = get('view');
  const editable = s.editable_meta || ['title'];
  $('mTitle').value = s.title || m.title || '';
  $('mArtist').value = m.artist || '';
  if (m.tuning) m.tuning.forEach((x, i) => { $('mT' + (i + 1)).value = showNote(x); });
  $('mCapo').value = v ? v.capo : m.capo || 0;
  $('mTs').value = (m.time_signature || [4, 4]).join('/');
  const settings = editable.includes('tuning');
  $('mArtistLbl').hidden = !editable.includes('artist');
  document.querySelectorAll('.meta-setting').forEach(el => { el.hidden = !settings; });
  $('metaHead').textContent = editable.length > 1 ? '曲子信息' : '曲名';
  // 改不了调弦等设定的原因：GP / MusicXML 来自原文件；图片曲子还没读出谱头，或者正在识别
  $('metaMsg').textContent = settings ? '音名写法如 D2、F♯3（F#3 也可以）。'
    : isScore(s) ? '调弦、变调夹和拍号以原文件为准。'
    : !Object.keys(s.meta || {}).length ? '还没读出谱头，现在只能改曲名。'
    : '正在识别，完成后可以改调弦、变调夹和拍号。';
}
async function saveMeta() {
  const s = get('song');
  const editable = s.editable_meta || ['title'];
  const body = { title: $('mTitle').value };
  if (editable.includes('artist')) body.artist = $('mArtist').value.trim();
  if (editable.includes('tuning')) body.tuning = [1, 2, 3, 4, 5, 6].map(i => tuningInput($('mT' + i).value));
  if (editable.includes('capo')) body.capo = parseInt($('mCapo').value || '0', 10);
  if (editable.includes('time_signature')) body.time_signature = $('mTs').value.split('/').map(x => parseInt(x, 10));
  body.rev = s.meta_rev; // 乐观锁只看谱头（meta_rev）：问 AI、练习记录、改小节都不影响它
  try {
    applySong(await put(songUrl('/meta'), body));
    UI.toast('已保存');
  } catch (e) {
    await writeFailed(e, '保存失败：');
  }
}

// ---------------------------------------------------------------------
// GP / MusicXML：第一次打开（或转换规则、alphaTab 升级过）时把谱子转成我们的小节数据存起来
// ---------------------------------------------------------------------
let importErrorSent = false;
async function reportImportError(msg) {
  // 第一次打开就解析失败：记到曲库里（不再显示成可以练习），页面上也说清楚
  UI.toast('读取曲谱失败：' + msg, 'err', 8000);
  if (importErrorSent) return;
  importErrorSent = true;
  try { applySong(await post(songUrl('/import-error'), { error: msg })); } catch { await refresh(); }
}
async function onScoreLoaded(score) {
  const s = get('song');
  const six = score.tracks.findIndex(t => t.staves[0] && t.staves[0].tuning.length === 6);
  const track = s.bars.length ? (s.track || 0) : Math.max(0, six);
  $('trackName').textContent = score.tracks[track] ? `音轨：${score.tracks[track].name}` : '';
  // 转换规则或 alphaTab 升级过：重新转换一次（内容没变的小节会保留讲解和练习记录）
  const derived = { version: DERIVE_VERSION, alphatab: (alphaTab.meta && alphaTab.meta.version) || '' };
  const outdated = !s.derived || s.derived.version !== derived.version || s.derived.alphatab !== derived.alphatab;
  if (s.bars.length && track === (s.track || 0) && !outdated) return true;
  try {
    const d = deriveSong(score, track);
    applySong(await put(songUrl('/derived'), { meta: d.meta, bars: d.bars, track, derived }));
    return true;
  } catch (e) {
    if (!s.bars.length) reportImportError(e.message); else UI.toast('读取曲谱失败：' + e.message, 'err', 6000);
    return false;
  }
}

// ---------------------------------------------------------------------
// 曲子换了：重新派生 view，循环范围和正在看的小节按小节 id 保留（重新识别一行后编号可能整体移动）
// ---------------------------------------------------------------------
let prevIds = [];
function onView(v) {
  const s = get('song'), n = v.bars.length;
  $('scoreEmpty').hidden = !!n || isScore(s);
  if (!n) { prevIds = v.ids; return; }
  const { a, b } = get('play'), shown = get('ui.bar');
  const wasAll = !prevIds.length || (a === 0 && b === prevIds.length - 1);
  const find = (id, fb) => { const i = v.ids.indexOf(id); return i >= 0 ? i : Math.min(fb, n - 1); };
  const na = wasAll ? 0 : find(prevIds[a], a);
  let nz = wasAll ? n - 1 : find(prevIds[b], b);
  if (nz < na) nz = na;
  const keep = prevIds[shown] ? v.ids.indexOf(prevIds[shown]) : -1;
  prevIds = v.ids;
  // 范围真的变了才重设（问 AI 写了问答、识别中轮询到新数据都会走到这里：范围没变时不能把暂停的位置跳回段首）
  if (na !== a || nz !== b) setLoop(na, nz, false, keep < 0);
  else if (keep < 0) set('ui.bar', na);
  if (keep >= 0) set('ui.bar', keep); // 正在看的小节还在就接着显示它，不跳到别的小节（否则编辑器会被关掉）
}

export function initSong() {
  setPlayerHooks({
    scoreLoaded: onScoreLoaded,
    error: msg => { const s = get('song'); if (s && isScore(s) && !s.bars.length) reportImportError(msg); else UI.toast('谱面出错：' + msg, 'err', 6000); },
  });
  on(['song.bars', 'song.meta', 'song.pages', 'song.source_type', 'song.track', 'song.id'], () => set('view', deriveView(get('song'))));
  on('view', onView);
  on(['song', 'view'], () => renderHeader(get('song')));
  on('song', s => { renderNotices(s); renderResume(s); renderRefineBtn(s); });
  $('refineBtn').onclick = e => startRefine(e.currentTarget);
  const fold = f => f.setAttribute('aria-expanded', String(f.getAttribute('aria-expanded') !== 'true'));
  $('notices').addEventListener('click', e => { const f = e.target.closest('.n-fold'); if (f) fold(f); });
  $('notices').addEventListener('keydown', e => {
    const f = e.target.closest('.n-fold');
    if (f && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); e.stopPropagation(); fold(f); } // 空格不能再当播放键
  });
  on(['song.plan', 'song.status', 'song.busy', 'view'], renderPlan);
  on(['song.meta', 'song.title', 'song.editable_meta'], () => fillMeta(get('song')));
  $('metaSave').onclick = () => withBusy($('metaSave'), saveMeta);
}
