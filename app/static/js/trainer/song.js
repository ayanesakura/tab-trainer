// 加载曲子并保持最新：取 /api/songs/<id> 放进 store（set('song')），由它派生 view（deriveView），
// 页面各部分各自订阅。识别 / 讲解进行中时定时轮询（出错退避重试，不会停）。
// 也包括：曲名和副标题、通知（进度 / 出错 / 讲解 / AI 备注 / 接着练）、练习建议页、曲子信息的保存、
// GP / MusicXML 第一次打开时把谱子转换成我们的小节数据。

import { get, set, on } from './store.js';
import { $, UI, esc, request, withBusy, SONG_ID } from './dom.js';
import { deriveView, songSubtitle, explainState, resumeInfo, planRange, showNote, tuningInput } from './view.js';
import { DERIVE_VERSION, deriveSong } from '../score.js';
import { alphaTab, setLoop, setTempo, setPlayerHooks } from './player.js';
import { notice } from './notices.js';

const SOURCES = { image: '图片', pdf: 'PDF', gp: 'GP', musicxml: 'MusicXML' };
const isBusy = s => !!(s.busy ?? (s.running || s.status === 'parsing' || s.status === 'queued' || (s.systems || []).some(x => x.status === 'parsing')));
const recognizing = s => !!(s.recognizing ?? (s.status === 'parsing' || s.status === 'queued' || (s.running && s.job !== 'explain')));

// ---------------------------------------------------------------------
// 取曲子、轮询
// ---------------------------------------------------------------------
let pollTimer = null, failures = 0, seq = 0, applied = 0;
export async function refresh() {
  clearTimeout(pollTimer);
  const my = ++seq;
  try {
    const s = await request('GET', `/api/songs/${encodeURIComponent(SONG_ID)}`);
    if (my < applied) return; // 晚到的旧结果
    applied = my; failures = 0;
    notice('offline', null);
    set('song', s);
    if (isBusy(s)) pollTimer = setTimeout(refresh, 2500);
  } catch (e) {
    if (e.status === 404) { $('songTitle').textContent = '曲子不存在'; return; }
    failures++;
    if (!get('song')) $('songTitle').textContent = e.message;
    // 出错（服务重启、网络抖动）不停止轮询：退避重试，最长 30 秒一次
    const wait = Math.min(30000, 2500 * 2 ** Math.min(failures, 4));
    if (!get('song') || isBusy(get('song'))) {
      notice('error', { kind: 'err', lead: 'dot', html: `${esc(e.message)}，${Math.round(wait / 1000)} 秒后重试。`, onClose: () => {} });
      pollTimer = setTimeout(refresh, wait);
    } else UI.toast(e.message, 'err', 5000);
  }
}

// ---------------------------------------------------------------------
// 顶栏：曲名、来源、副标题
// ---------------------------------------------------------------------
function renderHeader(s) {
  const m = s.meta || {}, v = get('view');
  const title = s.title || m.title || '未命名';
  document.title = title + ' · 吉他谱练习器';
  $('songTitle').textContent = title;
  const src = SOURCES[s.source_type];
  $('songSource').hidden = !src; $('songSource').textContent = src || '';
  $('songSub').textContent = songSubtitle(s, v);
}

// ---------------------------------------------------------------------
// 通知
// ---------------------------------------------------------------------
const dismissed = new Set(); // 这次打开页面里关掉的通知（内容变了会重新出现）
function closable(key) { return dismissed.has(key) ? null : () => dismissed.add(key); }

function progressText(s) {
  const pr = s.progress || {}, n = pr.total ? `（${pr.done}/${pr.total} ${s.job === 'explain' ? '批' : '行'}）` : '';
  const kind = s.activity ? s.activity.kind : s.job;
  const restaging = s.activity ? s.activity.restaging : s.restaging;
  if (kind === 'explain') return `正在生成讲解${n}，可以照常练习。`;
  if (kind === 'pages') return `正在识别新加的页${n}，其他小节可以照常练习。`;
  if (kind === 'reparse_row') return `正在重新识别一行${n}，其他小节可以照常练习。`;
  if (restaging) return `正在重新识别：${esc(pr.stage || '')}${n}，完成前不能改谱。`;
  return `正在识别：${esc(pr.stage || '')}${n}，已识别的小节可以先练。`;
}

async function explain(btn) {
  await withBusy(btn, async () => {
    try { await request('POST', `/api/songs/${SONG_ID}/explain`); UI.toast('AI 正在写讲解', 'info'); }
    catch (e) { UI.toast(e.message, 'err', 5000); }
  });
  await refresh();
}

function renderNotices(s) {
  const running = !!(s.busy ?? (s.running || s.status === 'parsing' || s.status === 'queued'));
  const kind = s.activity ? s.activity.kind : s.job;
  notice('progress', running ? {
    kind: 'info', lead: 'spinner', html: progressText(s),
    actions: [{ label: kind === 'explain' ? '取消' : '取消识别', onClick: btn => withBusy(btn, async () => {
      try { await request('POST', `/api/songs/${SONG_ID}/cancel`); UI.toast('正在取消…', 'info'); }
      catch (e) { UI.toast(e.message, 'err'); }
      await refresh();
    }) }],
  } : null);

  // 出错 / 警告：后端给 notice 就只显示它；旧接口看 error
  const n = s.notice !== undefined ? s.notice
    : s.error ? { kind: s.status === 'error' ? 'error' : 'warn', msg: s.error } : null;
  const errKey = n ? 'err:' + n.msg : '';
  const scoreFail = s.kind === 'score' && s.status === 'error';
  notice('error', n && !dismissed.has(errKey) ? {
    kind: n.kind === 'error' ? 'err' : 'warn', lead: 'dot',
    html: `${scoreFail ? '曲谱读取失败' : n.kind === 'error' ? '出错了' : '注意'}：${esc(n.msg)}`, onClose: closable(errKey),
  } : null);

  // 让 AI 讲解：有小节缺提示（GP 刚导入、改过音或调弦）或练习建议过时就显示；没有练习建议的提示放在练习建议页
  const ex = explainState(s);
  const exKey = `ex:${ex.missing}:${ex.planStale}`;
  const showEx = !running && s.bars.length && (ex.missing || ex.planStale) && !dismissed.has(exKey);
  notice('explain', showEx ? {
    kind: '', lead: 'sparkle',
    html: ex.fresh ? '让 AI 为每个小节写练习提示，并生成练习建议。'
      : [ex.missing ? `${ex.missing} 个小节还没有提示` : '', ex.planStale ? '练习建议是改谱之前写的，可能已过时' : ''].filter(Boolean).join('；') + '。',
    actions: [{ label: ex.fresh ? '让 AI 讲解这首曲子' : '让 AI 补写', icon: 'sparkle', primary: true, onClick: explain }],
    onClose: closable(exKey),
  } : null);

  // AI 读谱头时写的备注：关掉后同一段备注不再显示（备注变了才重新出现）
  const m = s.meta || {}, noteKey = 'tt.noteHidden.' + SONG_ID;
  notice('metaNote', m.notes && localStorage.getItem(noteKey) !== m.notes ? {
    kind: '', lead: 'dot', html: `<b>AI 备注：</b>${esc(m.notes)}`, onClose: () => localStorage.setItem(noteKey, m.notes),
  } : null);
}

let resumeDone = false;
function renderResume(s) {
  if (resumeDone || !s.bars.length) return;
  resumeDone = true;
  const r = resumeInfo(s);
  if (!r) return;
  const go = () => { setLoop(r.a, r.z); if (r.bpm) setTempo(r.bpm); notice('resume', null); };
  if (new URLSearchParams(location.search).get('resume') === '1') {
    // 从曲库的“接着练”进来：直接接着练，地址里去掉 resume（刷新页面不会再跳）
    const u = new URL(location.href); u.searchParams.delete('resume'); history.replaceState(null, '', u);
    go();
    return;
  }
  notice('resume', {
    kind: 'info', lead: 'repeat', html: `上次（${esc(r.at)}）练到 <b>${r.label}</b>，${r.bpm} BPM。`,
    actions: [{ label: '接着练', primary: true, onClick: go }], onClose: () => {},
  });
}

// ---------------------------------------------------------------------
// 练习建议页
// ---------------------------------------------------------------------
function renderPlan() {
  const s = get('song'), v = get('view');
  if (!s || !v) return;
  const p = s.plan, n = v.bars.length;
  if (!p) {
    const can = n && !recognizing(s);
    $('plan').innerHTML = `<div class="list"><div class="empty-state"><b>还没有练习建议</b>${can ? '<p>让 AI 根据全曲生成分段练习建议。</p><button class="btn sm primary" id="planCreate">' + UI.icon('sparkle') + '生成练习建议</button>' : '识别完成后会自动生成。'}</div></div>`;
    if (can) $('planCreate').onclick = e => explain(e.currentTarget);
    return;
  }
  const staleNote = p.stale ? `<div class="notice warn"><span class="dot warn"></span><div class="msg">谱子改过，这些建议可能已过时。</div><button class="btn sm" id="planRefresh">${UI.icon('refresh')}重新生成</button></div>` : '';
  const steps = (p.steps || []).map((st, i) => {
    const r = planRange(st.bars, n);
    const link = r ? `<button class="btn sm" data-a="${r.a}" data-z="${r.z}">${UI.icon('repeat', 13)}循环这段</button>` : '';
    return `<div class="row plan-row"><span class="step">${i + 1}</span><div class="who"><div class="name">${esc(st.title)} <span class="badge">第 ${esc(st.bars)} 小节</span></div><div class="sub">${esc(st.advice)}</div></div><div class="ctl">${link}</div></div>`;
  }).join('');
  $('plan').innerHTML = staleNote + (p.overview ? `<div class="card pad plan-overview">${esc(p.overview)}</div>` : '') + `<div class="list">${steps}</div>`;
  if (p.stale) $('planRefresh').onclick = e => explain(e.currentTarget);
  $('plan').querySelectorAll('button[data-a]').forEach(b => b.onclick = () => { setLoop(+b.dataset.a, +b.dataset.z); location.hash = '#practice'; });
}

// ---------------------------------------------------------------------
// 曲子信息（调弦页下面）
// ---------------------------------------------------------------------
function fillMeta(s) {
  const m = s.meta || {}, v = get('view');
  const editable = s.editable_meta || (s.kind === 'score' ? ['title'] : ['title', 'tuning', 'capo', 'time_signature']);
  $('mTitle').value = s.title || m.title || '';
  if (m.tuning) m.tuning.forEach((x, i) => { $('mT' + (i + 1)).value = showNote(x); });
  $('mCapo').value = v ? v.capo : m.capo || 0;
  $('mTs').value = (m.time_signature || [4, 4]).join('/');
  const onlyTitle = !editable.includes('tuning');
  document.querySelectorAll('.meta-setting').forEach(el => { el.hidden = onlyTitle; });
  $('metaHead').textContent = onlyTitle ? '曲名' : '曲子信息';
  $('metaMsg').textContent = onlyTitle ? '调弦、变调夹和拍号以原文件为准。' : '音名写法如 D2、F♯3（F#3 也可以）。';
}
async function saveMeta() {
  const s = get('song');
  const editable = s.editable_meta || (s.kind === 'score' ? ['title'] : ['title', 'tuning', 'capo', 'time_signature']);
  const body = { title: $('mTitle').value };
  if (editable.includes('tuning')) body.tuning = [1, 2, 3, 4, 5, 6].map(i => tuningInput($('mT' + i).value));
  if (editable.includes('capo')) body.capo = parseInt($('mCapo').value || '0', 10);
  if (editable.includes('time_signature')) body.time_signature = $('mTs').value.split('/').map(x => parseInt(x, 10));
  if (s.rev !== undefined) body.rev = s.rev;
  try {
    await request('PUT', `/api/songs/${SONG_ID}/meta`, body);
    UI.toast('已保存');
  } catch (e) {
    UI.toast(e.status === 409 ? e.message : '保存失败：' + e.message, 'err', 5000);
  }
  await refresh();
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
  await request('POST', `/api/songs/${SONG_ID}/import-error`, { error: msg }).catch(() => {});
  await refresh();
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
    await request('PUT', `/api/songs/${SONG_ID}/derived`, { meta: d.meta, bars: d.bars, track, derived });
    await refresh();
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
  $('scoreEmpty').hidden = !!n || s.kind === 'score';
  if (!n) { prevIds = v.ids; return; }
  const { a, b } = get('play'), shown = get('ui.bar');
  const wasAll = !prevIds.length || (a === 0 && b === prevIds.length - 1);
  const find = (id, fb) => { const i = v.ids.indexOf(id); return i >= 0 ? i : Math.min(fb, n - 1); };
  const na = wasAll ? 0 : find(prevIds[a], a);
  let nz = wasAll ? n - 1 : find(prevIds[b], b);
  if (nz < na) nz = na;
  const keep = prevIds[shown] ? v.ids.indexOf(prevIds[shown]) : -1;
  prevIds = v.ids;
  setLoop(na, nz, false, keep < 0); // 正在看的小节还在就接着显示它，不跳到别的小节（否则编辑器会被关掉）
  if (keep >= 0) set('ui.bar', keep);
}

export function initSong() {
  setPlayerHooks({
    scoreLoaded: onScoreLoaded,
    error: msg => { const s = get('song'); if (s && s.kind === 'score' && !s.bars.length) reportImportError(msg); else UI.toast('谱面出错：' + msg, 'err', 6000); },
  });
  on(['song.bars', 'song.meta', 'song.pages', 'song.kind', 'song.track', 'song.id'], () => set('view', deriveView(get('song'))));
  on('view', onView);
  on(['song', 'view'], () => renderHeader(get('song')));
  on('song', s => { renderNotices(s); renderResume(s); });
  on(['song.plan', 'song.status', 'song.busy', 'view'], renderPlan);
  on(['song.meta', 'song.title', 'song.editable_meta'], () => fillMeta(get('song')));
  $('metaSave').onclick = () => withBusy($('metaSave'), saveMeta);
}
