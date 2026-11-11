// 曲库页的“练琴”视图：今日进度（圆环 + 连续天数 / 近 7 天 / 今日几首）、今日（今天要练的曲子）、日历和某一天的明细、
// 计划（调顺序、暂停、移出、取消归档）、补记、计时器，以及设置页里的“练琴”三项。拼 HTML 的纯函数在 study-view.js。
// 用词：“今日” = 今天要练的那几首，“计划” = 排好顺序的全部曲子，“日历” = 每天练了多久。
// 计划里有哪些曲子、各自的状态来自曲库列表（GET /api/songs 每项的 study），index.js 每次拿到新列表就调 renderPlan。

import { UI, $, esc, icon, toast } from '/static/ui.js';
import * as api from '/static/js/lib/api.js';
import { mountTimer, announce, onAnnounce } from '/static/js/lib/study-timer.js';
import {
  fmtMin, fmtShort, dayLabel, ringHTML, queueRowHTML, doneRowHTML, planRowHTML, calendarModel, calendarHTML,
  ratingBadge, ratingTip, sessionText, trainerHref, parseDay, dayKey, calMove,
} from '/static/js/study-view.js';

// day：点开明细的那天；focus：日历里能 Tab 到的那一格（键盘走到 / 点过的那天，见 calTabDay）
const S = { today: null, cal: null, day: '', focus: '', songs: [], planOpen: false, onSongs: () => {}, visible: false, timer: null };
const PLAN_FOLD = 6; // 计划超过这么多首时先收起来

// 只在内容变了的时候改 DOM（每次刷新都重画，不能把焦点、悬停冲掉）
function setHTML(el, html) { if (el._html !== html) { el.innerHTML = html; el._html = html; } }

// ---------------- 取数据 ----------------
let loading = null;
export async function refresh({ calendar = true } = {}) {
  if (loading) return loading;
  loading = (async () => {
    try {
      const [today, cal] = await Promise.all([
        api.get('/api/study/today'),
        calendar || !S.cal ? api.get('/api/study/calendar?days=371') : Promise.resolve(S.cal),
      ]);
      S.today = today; S.cal = cal;
      if (S.timer) S.timer.refresh(today.timer);
      render();
      if (S.day) loadDay(S.day, { quiet: true });
    } catch (e) {
      if (!S.today) setHTML($('hero'), `<div class="empty-state"><b>加载失败</b>${esc(e.message)}</div>`);
      else toast(e.message, 'err', 4000);
    } finally {
      loading = null;
    }
  })();
  return loading;
}

function render() {
  const t = S.today;
  if (!t) return;
  $('studyDate').textContent = dayLabel(t.day);
  renderHero(t);
  renderQueue(t);
  if (S.loaded) paintPlan(); // 计划行的“今天开始 / 排队中”跟着今日变
  renderCalendar();
  syncSettings(t.settings);
}

// ---------------- 今日进度 ----------------
function renderHero(t) {
  const m = t.minutes || {}, total = Number(m.total) || 0, goal = Number(m.goal) || t.settings.goal_minutes || 45;
  const left = (t.queue || []).length, done = (t.done || []).length;
  const msg = total >= goal ? '今天的目标完成了' : total > 0 ? `还差 ${fmtMin(goal - total)}` : done ? '还没记时长' : '今天还没开始'; // 打过分、没计时也没在练习页播放（比如脱开电脑练的）：别说“还没开始”
  const sub = left ? `还有 ${left} 首` : done ? '今日的曲子都练完了' : '';
  const week = fmtShort(t.week_minutes);
  const stat = (v, unit, k, ic, cls = '') => `<div class="stat ${cls}"><div class="v">${esc(v)}<small>${esc(unit)}</small></div><div class="k">${ic ? icon(ic) : ''}${esc(k)}</div></div>`;
  $('hero').removeAttribute('aria-busy');
  setHTML($('hero'), `${ringHTML(total, goal)}
    <div class="hero-main">
      <div class="hero-msg">${esc(msg)}${sub ? `<span class="hint">${esc(sub)}</span>` : ''}</div>
      <div class="stats">
        ${stat(t.streak || 0, '天', '连续', 'flame', t.streak > 0 ? 'lit' : '')}
        ${stat(week.n, week.unit, '近 7 天', 'calendar')}
        ${stat(`${done}`, `/ ${done + left} 首`, '今日', 'list')}
      </div>
    </div>`);
}

// ---------------- 今日 ----------------
function emptyHTML(kind, title, hint, action = '') {
  return `<div class="study-empty"><span class="big ${kind}">${icon(kind === 'ok' ? 'check' : 'target')}</span>
    <div><b>${esc(title)}</b><span class="hint">${esc(hint)}</span></div><span class="grow"></span>${action}</div>`;
}
function renderQueue(t) {
  const q = t.queue || [], done = t.done || [], c = t.counts || {};
  // 右上角是今日以外的：到期但超出每天上限、顺延的复习，还在排队、没轮到今天的新曲
  const kinds = k => q.filter(x => x.kind === k).length;
  const later = Math.max(0, (c.review_due || 0) - kinds('review')), waiting = Math.max(0, (c.new || 0) - kinds('new'));
  const aside = [later && `${later} 首复习顺延`, waiting && `${waiting} 首新曲排队`].filter(Boolean).join(' · ');
  setHTML($('queueAside'), esc(aside));
  let html;
  if (!c.in_plan && c.archived) {  // 归档的不算在计划里
    html = emptyHTML('ok', '计划里的曲子都归档了', '在下面的计划或曲库的“已归档”里取消归档，就会排回今日。');
  } else if (!c.in_plan) {
    html = emptyHTML('', '计划里还没有曲子', '在曲库点“选择”勾几首，或在每首的“…”菜单里加入。', '<a class="btn sm primary" href="#library">去曲库</a>');
  } else if (!q.length && !done.length) {
    html = emptyHTML('ok', '今天没有要练的', c.paused === c.in_plan ? '计划里的曲子都暂停了。' : '复习的曲子还没到期，可以自由练习。');
  } else {
    html = q.map((it, i) => queueRowHTML(it, { primary: i === 0 })).join('');
    if (!q.length) html = emptyHTML('ok', '今天的曲子都练完了', '想多练可以点下面的曲子再弹一遍。');
    if (done.length) html += `<div class="list-sep">${icon('check', 12)}今天练过 · ${done.length}</div>` + done.map(doneRowHTML).join('');
  }
  setHTML($('queue'), html);
}
$('queue').addEventListener('click', e => {
  if (e.target.closest('a,button')) return;
  const row = e.target.closest('.row[data-href]');
  if (row && row.dataset.href) location.href = row.dataset.href;
});

// ---------------- 日历 ----------------
function renderCalendar() {
  const t = S.today, cal = S.cal;
  if (!t || !cal) return;
  const model = calendarModel(cal.days, cal.today || t.day, cal.goal || t.settings.goal_minutes);
  // 累计用后端算好的 total_minutes（有记录以来：计时 + 练习页去掉重叠、补记、早期没日期的），和曲库、今日同一个算法
  const all = Number(cal.total_minutes ?? model.total) || 0, undated = Number(cal.undated_minutes) || 0, sum = fmtShort(all);
  const tip = [`近一年 ${fmtMin(model.total)}`, `有记录以来 ${fmtMin(all)}`, undated > 0 && `含早期未记日期的 ${fmtMin(undated)}（日历上放不进哪一天）`].filter(Boolean).join('；');
  $('calSum').title = all > 0 ? tip : '';
  setHTML($('calSum'), model.active || all > 0 ? esc([model.active && `近一年 ${model.active} 天`, `累计 ${sum.n} ${sum.unit}`].filter(Boolean).join(' · ')) : '');
  S.calModel = model;
  const first = !$('cal')._html, box = $('cal'), had = box.contains(document.activeElement);
  setHTML(box, calendarHTML(model, S.day, S.focus));
  if (first) box.scrollLeft = box.scrollWidth; // 窄屏只露出一部分时，先看到最近的几周
  // 格子是整个重画的：焦点原来在日历里（点了、用键盘走了），放回能 Tab 到的那一格
  if (had && !box.contains(document.activeElement)) focusCell({ preventScroll: true });
}
const focusCell = opts => { const c = $('cal').querySelector('button.cell[tabindex="0"]'); if (c) c.focus(opts); };
$('cal').addEventListener('click', e => {
  const c = e.target.closest('button.cell');
  if (!c) return;
  S.focus = c.dataset.day;
  if (S.day === c.dataset.day) { closeDay(); return; }
  loadDay(c.dataset.day);
});
// 日历只有一格能 Tab 到，格子之间用方向键走（回车 / 空格就是点这个按钮）
$('cal').addEventListener('keydown', e => {
  const c = e.target.closest('button.cell');
  if (!c || !S.calModel || e.altKey || e.ctrlKey || e.metaKey) return;
  const to = calMove(S.calModel, c.dataset.day, e.key);
  if (!to) return;
  e.preventDefault();
  S.focus = to;
  renderCalendar(); // 焦点跟着放到新的那一格
  const cell = $('cal').querySelector('button.cell[tabindex="0"]');
  if (cell) cell.scrollIntoView({ block: 'nearest', inline: 'nearest' }); // 走到露出来的范围外面时，日历跟着滚
});

async function loadDay(day, { quiet = false } = {}) {
  S.day = day;
  renderCalendar();
  if (!quiet) { $('dayCard').hidden = false; setHTML($('dayCard'), `<div class="card-h"><span class="card-title">${esc(dayLabel(day))}</span><span class="spinner"></span></div>`); }
  try {
    const d = await api.get(`/api/study/day/${encodeURIComponent(day)}`);
    if (S.day !== day) return; // 已经点了别的日子
    renderDay(d);
  } catch (e) {
    if (!quiet) toast(e.message, 'err', 4000);
  }
}
function closeDay() { S.day = ''; $('dayCard').hidden = true; setHTML($('dayCard'), ''); renderCalendar(); }
function renderDay(d) {
  const total = (d.minutes && d.minutes.total) || 0;
  const songs = (d.songs || []).filter(s => s.seconds >= 30 || s.rating);
  const songItems = songs.length ? songs.map(s => `<div class="day-item">
      <span class="lbl"><a href="${trainerHref(s.id)}">${esc(s.title)}</a></span>
      ${s.rating ? ratingBadge(s.rating.rating, ` title="${esc(ratingTip(s.rating))}"`) : ''}
      <span class="num">${s.seconds >= 30 ? esc(fmtMin(s.seconds / 60)) : ''}</span></div>`).join('')
    : '<div class="none">这天没在谱子上练</div>';
  const sessions = d.sessions || [];
  const logItems = sessions.length ? sessions.map(s => {
    const x = sessionText(s);
    return `<div class="day-item"><span class="lbl">${esc(x.label)}${x.sub ? `<span class="hint">${esc(x.sub)}</span>` : ''}</span>
      <span class="num">${esc(fmtMin(s.minutes))}</span>
      <button class="icon-btn sm" data-del="${esc(s.id)}" title="删除这条记录" aria-label="删除这条记录">${icon('trash', 14)}</button></div>`;
  }).join('') : '<div class="none">没有计时或补记</div>';
  setHTML($('dayCard'), `<div class="card-h"><span class="card-title">${esc(dayLabel(d.day))}<span class="total">${total > 0 ? ` · ${esc(fmtMin(total))}` : ' · 没练'}</span></span>
      <button class="icon-btn sm" data-close title="关闭" aria-label="关闭">${icon('x')}</button></div>
    <div class="day-body"><div class="day-col"><h3>曲子</h3>${songItems}</div><div class="day-col"><h3>计时和补记</h3>${logItems}</div></div>`);
  $('dayCard').hidden = false;
}
$('dayCard').addEventListener('click', async e => {
  if (e.target.closest('[data-close]')) { closeDay(); return; }
  const del = e.target.closest('[data-del]');
  if (!del) return;
  if (!await UI.confirm('删掉后这段时间不再算进那一天。', { title: '删除这条记录？', ok: '删除', danger: true })) return;
  try { await api.del(`/api/study/sessions/${encodeURIComponent(del.dataset.del)}`); toast('已删除'); announce(); } catch (err) { toast(err.message, 'err', 4000); }
  refresh();
});

// ---------------- 计划 ----------------
const planSongs = () => S.songs.filter(s => s.study).sort((a, b) => a.study.order - b.study.order || String(a.study.added).localeCompare(String(b.study.added)));
export function renderPlan(songs) {
  S.songs = songs || [];
  S.loaded = true;
  paintPlan();
}
function paintPlan() {
  const list = planSongs(), n = list.length;
  const paused = list.filter(s => s.study.paused).length;
  const archived = list.filter(s => s.archived).length;
  setHTML($('planAside'), n ? esc([`共 ${n} 首，按顺序学`, paused && `暂停 ${paused}`, archived && `归档 ${archived}`].filter(Boolean).join(' · ')) : '');
  if (!n) { setHTML($('plan'), '<div class="empty-state"><b>还没有曲子</b>在曲库里把想练的曲子加进来，按顺序一首首学。</div>'); return; }
  const shown = S.planOpen || n <= PLAN_FOLD + 1 ? list : list.slice(0, PLAN_FOLD);
  const inToday = new Set(((S.today && S.today.queue) || []).map(x => x.id));
  let html = shown.map((s, i) => planRowHTML(s, i, n, { today: inToday.has(s.id) })).join('');
  if (n > PLAN_FOLD + 1) html += `<div class="plan-more"><button class="btn ghost sm" data-fold>${S.planOpen ? `${icon('up', 12)}收起` : `${icon('down', 12)}展开全部 ${n} 首`}</button></div>`;
  setHTML($('plan'), html);
}
async function move(id, dir) {
  const ids = planSongs().map(s => s.id), i = ids.indexOf(id), j = i + dir;
  if (i < 0 || j < 0 || j >= ids.length) return;
  [ids[i], ids[j]] = [ids[j], ids[i]];
  // 先在本地换好顺序（按钮跟着走，连点几下不卡），再告诉后端
  const order = new Map(ids.map((x, k) => [x, k]));
  S.songs = S.songs.map(s => (s.study && order.has(s.id) ? { ...s, study: { ...s.study, order: order.get(s.id) } } : s));
  paintPlan();
  const btn = $('plan').querySelector(`.plan-row[data-id="${CSS.escape(id)}"] [data-mv="${dir}"]`);
  (btn && !btn.disabled ? btn : $('plan').querySelector(`.plan-row[data-id="${CSS.escape(id)}"] [data-plan-more]`))?.focus({ preventScroll: true });
  try { await api.put('/api/study/order', { ids }); } catch (e) { toast(e.message, 'err', 4000); }
  changed();
}
$('plan').addEventListener('click', e => {
  if (e.target.closest('[data-fold]')) { S.planOpen = !S.planOpen; paintPlan(); return; }
  const row = e.target.closest('.plan-row');
  if (!row) return;
  const mv = e.target.closest('[data-mv]');
  if (mv) { move(row.dataset.id, +mv.dataset.mv); return; }
  const more = e.target.closest('[data-plan-more]');
  if (more) planMenu(more, S.songs.find(s => s.id === row.dataset.id));
});
function planMenu(btn, s) {
  if (!s) return;
  UI.menu(btn, [
    s.archived && { label: '取消归档', icon: 'archive', onClick: () => unarchive(s) },
    !s.archived && (s.study.paused ? { label: '继续', icon: 'play', onClick: () => setStudy(s, { paused: false }, '已继续') } : { label: '暂停', icon: 'pause', onClick: () => setStudy(s, { paused: true }, '已暂停，不再排进今日') }),
    { label: '移到最前', icon: 'up', onClick: () => toFront(s) },
    'sep',
    { label: '移出计划', icon: 'x', danger: true, onClick: () => setStudy(s, { in_plan: false }, '已移出计划（打分记录还在）') },
  ]);
}
async function unarchive(s) {
  try { await api.post(`/api/songs/${encodeURIComponent(s.id)}/archive`, { archived: false }); toast(`已取消归档“${s.title}”`); } catch (e) { toast(e.message, 'err', 4000); }
  changed();
}
async function toFront(s) {
  const ids = planSongs().map(x => x.id).filter(x => x !== s.id);
  try { await api.put('/api/study/order', { ids: [s.id, ...ids] }); toast('已移到最前'); } catch (e) { toast(e.message, 'err', 4000); }
  changed();
}

/** 加入 / 移出 / 暂停（曲库的“…”菜单也用）：body = {in_plan} 或 {paused} */
export async function setStudy(s, body, msg) {
  try { await api.put(`/api/songs/${encodeURIComponent(s.id)}/study`, body); if (msg) toast(msg); } catch (e) { toast(e.message, 'err', 4000); }
  changed();
}
/** 多选加入计划：按给的顺序一首首加（排在计划最后），已经在计划里的跳过 → 加了几首 */
export async function addMany(list) {
  let n = 0;
  for (const s of list) {
    if (s.study) continue;
    try { await api.put(`/api/songs/${encodeURIComponent(s.id)}/study`, { in_plan: true }); n++; } catch (e) { toast(e.message, 'err', 4000); break; }
  }
  changed();
  return n;
}
function changed() { announce(); S.onSongs(); refresh({ calendar: false }); }

// ---------------- 补记 ----------------
let logLayer = null;
$('logAdd').onclick = () => {
  const today = (S.today && S.today.day) || dayKey(new Date(Date.now() - 4 * 3600e3 - new Date().getTimezoneOffset() * 60e3));
  $('logDay').value = today; $('logDay').max = today;
  $('logMin').value = '30'; $('logNote').value = '';
  logLayer = UI.layer($('logDlg'), { modal: true, focus: $('logMin'), onClose: () => { logLayer = null; } });
};
$('logCancel').onclick = () => logLayer && logLayer.close();
$('logForm').onsubmit = async e => {
  e.preventDefault();
  const day = $('logDay').value, minutes = Number($('logMin').value);
  if (!parseDay(day)) { toast('选一个日期', 'err'); $('logDay').focus(); return; }
  if (!(minutes >= 1 && minutes <= 600)) { toast('时长要在 1–600 分钟之间', 'err'); $('logMin').focus(); return; }
  await api.withBusy($('logSave'), async () => {
    try {
      await api.post('/api/study/sessions', { day, minutes: Math.round(minutes), note: $('logNote').value.trim() });
      toast(`已补记 ${fmtMin(minutes)}`);
      if (logLayer) logLayer.close();
      announce();
      refresh();
    } catch (err) {
      toast(err.message, 'err', 4000);
    }
  });
};

// ---------------- 设置页的“练琴”三项 ----------------
let setGoal, setLearn, setRev;
function syncSettings(st) {
  if (!st || !setGoal) return;
  setGoal(String(st.goal_minutes)); setLearn(String(st.max_learning)); setRev(String(st.max_reviews));
}
async function saveSettings(body, msg) {
  try { const st = await api.put('/api/study/settings', body); syncSettings(st.settings || st); toast(msg); announce(); refresh(); } catch (e) { toast(e.message, 'err', 4000); }
}

// ---------------- 入口 ----------------
/** onSongs：计划变了，让曲库重新取列表（列表里有每首的 study） */
export function initStudy({ onSongs } = {}) {
  S.onSongs = onSongs || (() => {});
  S.timer = mountTimer($('timer'), { onChange: (_t, session) => { if (session || S.visible) refresh(); } });
  setGoal = UI.seg($('goalSeg'), '45', v => saveSettings({ goal_minutes: +v }, `每日目标改成 ${v} 分钟`));
  setLearn = UI.seg($('learnSeg'), '2', v => saveSettings({ max_learning: +v }, `同时在学最多 ${v} 首`));
  setRev = UI.seg($('revSeg'), '5', v => saveSettings({ max_reviews: +v }, +v ? `每天最多复习 ${v} 首` : '复习不限数量'));
  onAnnounce(() => { if (S.visible) refresh(); });
  // 页面重新可见（练完一首从练习页回来、过了零点）：重新取今日
  document.addEventListener('visibilitychange', () => { if (!document.hidden && S.visible) refresh(); });
  addEventListener('pageshow', e => { if (e.persisted && S.visible) refresh(); });
  refresh(); // 设置页的三项、计时器也要用
}
export function setVisible(v) {
  const was = S.visible;
  S.visible = v;
  if (v && !was && S.today) refresh();
}
