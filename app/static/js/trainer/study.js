// 练琴计划在练习页上的部分：顶栏的计时器、“打分”按钮和打分弹层；从今日清单进来（?from=today）时“返回”回到练琴页。
// 这首在计划里才显示“打分”。打分的状态就是 song.study（只订阅这一项；谱面只在 song.bars / song.meta 等变了时才重新派生，
// 改 song.study 不会）。打完分用 setLocal 写回 song.study：之前发出、还没回来的轮询作废，不会把旧的状态盖回来。
// 每次打分记下：1–4 分、勾的问题、卡住的小节（stuck_bars）、备注、当时的速度（play.bpm）；同一天再打一次是改分（后端按天取最后一次）。
//
// 提醒打分（FSRS 靠它排下次什么时候练，但没人提醒就常常忘了点）：这首在计划里、今天还没打分时，点顶栏的“返回”或
// 计时器的“结束”先弹出打分（多一个“跳过”）。从今日清单进来（?from=today）的总是提醒；别的入口要在这次打开里练过
// 至少 1 分钟（只是打开看一眼不打扰）、计划没暂停。一次打开最多提醒一次，跳过 / 关掉以后不再拦。
// 卡住的小节：默认勾上现在循环的这几个小节（循环全曲或太长时不勾），今天打过分的带出上次勾的。

import { get, on } from './store.js';
import { setLocal } from './song.js';
import { $, UI, esc, songUrl, SONG_ID } from './dom.js';
import * as api from '../lib/api.js';
import { icon } from '../lib/html.js';
import { mountTimer, announce } from '../lib/study-timer.js';
import { KINDS, RATING_CLS, ratingName, gapText, dueText } from '../study-view.js';
import { STUDY_RATINGS, STUDY_ISSUES } from '../vocab.js';
import { markBack } from '../lib/lib-return.js';

const RATE_TIPS = { 1: '弹不下来，要停', 2: '能弹完，有几处出错', 3: '基本没错', 4: '很轻松，可以隔久一点' };
const fromToday = new URLSearchParams(location.search).get('from') === 'today';

let layer = null;
const picked = new Set(); // 弹层里勾着的问题
const stuck = new Set(); // 弹层里勾着的卡住的小节（小节 id）
let stuckChoices = []; // 弹层里列出的小节 [{id, n}]
let then = null; // 提醒打分时：打完分 / 跳过以后接着做的事（离开页面、结束计时）
let prompted = false; // 这次打开已经提醒过
let practiced = 0, since = 0; // 这次打开练了多少秒（播放 / 敲节奏）
const STUCK_MAX = 12; // 循环范围超过这么多小节就不默认勾

function paintButton(st) {
  const btn = $('rateBtn');
  btn.hidden = !st;
  if (!st) { if (layer) layer.close(); return; }
  const r = st.rated_today;
  btn.className = `btn sm rate-btn${r ? ` rated ${RATING_CLS[r] || ''}` : ''}`;
  btn.innerHTML = r ? `${icon('check', 13)}${esc(ratingName(r))}` : `${icon('target', 13)}打分`;
  btn.title = r ? '今天已经打过分，点开可以改' : '练完一遍后，给今天的表现打个分，决定下次什么时候再练';
}

function previewText(st, r) {
  const p = st.preview && st.preview[String(r)];
  if (!p) return '';
  if (p.state === 'learning') return '明天再练';
  return p.due_in <= 1 ? '明天复习' : `${gapText(p.due_in)}后复习`;
}

const study = () => get('song.study') || null;

function renderPop() {
  const st = study();
  if (!st) return;
  const k = KINDS[st.state] || KINDS.new;
  const hint = [k.label, st.last ? `上次${ratingName(st.last.rating)}${st.last.day ? `（${st.last.day.slice(5).replace('-', '/')}）` : ''}` : '第一次打分', st.state === 'learning' && '打“顺”就转入复习']
    .filter(Boolean).join(' · ');
  $('rateHint').textContent = hint;
  $('rateStuck').innerHTML = stuckChoices.map(b => `<button data-bar="${esc(b.id)}" aria-pressed="${stuck.has(b.id)}" title="第 ${b.n} 小节">${b.n}</button>`).join('');
  $('rateStuckRow').hidden = !stuckChoices.length;
  $('rateIssues').innerHTML = Object.entries(STUDY_ISSUES).map(([c, name]) => `<button data-issue="${esc(c)}" aria-pressed="${picked.has(c)}">${esc(name)}</button>`).join('');
  $('rateBtns').innerHTML = Object.entries(STUDY_RATINGS).map(([r, name]) => `<button class="rate r${esc(r)} ${RATING_CLS[r] || ''}" data-r="${esc(r)}" title="${esc(RATE_TIPS[r] || '')}" aria-pressed="${st.rated_today === +r}">
      <b>${esc(name)}</b><span>${esc(previewText(st, r))}</span></button>`).join('');
}

/** 卡住的小节：列出现在循环的几个小节（太长就只列正在看的那一个）和上次勾的；默认勾上循环的（今天打过分的勾上次的） */
function fillStuck(st) {
  const s = get('song'), bars = (s && s.bars) || [], { a, b } = get('play');
  const whole = a === 0 && b === bars.length - 1, n = b - a + 1;
  const loop = !bars.length || whole || n > STUCK_MAX ? [] : bars.slice(a, b + 1);
  const before = (st.stuck_bars || []).map(x => x.id);
  const shown = bars[get('ui.bar')];
  const ids = new Set([...loop.map(x => x.id), ...before, ...(shown ? [shown.id] : [])]);
  stuckChoices = bars.filter(x => ids.has(x.id)).map(x => ({ id: x.id, n: x.n }));
  stuck.clear();
  (st.rated_today ? before : loop.map(x => x.id)).forEach(id => stuck.add(id));
}

/** 打开打分弹层。prompt：提醒打分（标题换成“走之前…”，多一个“跳过”），done 是打完分 / 跳过以后接着做的事 */
function openPop(prompt = null) {
  const st = study(), btn = $('rateBtn'), pop = $('ratePop');
  if (!st) return;
  if (layer) { layer.close(); if (!prompt) return; }
  then = prompt ? prompt.done : null;
  $('rateTitle').textContent = prompt ? prompt.title : '今天弹得怎么样？';
  $('rateFoot').hidden = !prompt;
  $('rateSkip').textContent = prompt ? prompt.skip : '跳过';
  // 今天打过分：带出上次勾的问题和备注，改分时不用重来
  picked.clear();
  const last = st.rated_today && st.last;
  (last ? last.issues || [] : []).forEach(c => picked.add(c));
  $('rateNote').value = last ? last.note || '' : '';
  fillStuck(st);
  renderPop();
  pop.hidden = false;
  const r = btn.getBoundingClientRect();
  pop.style.top = `${r.bottom + 8}px`;
  pop.style.right = `${Math.max(8, innerWidth - r.right)}px`;
  btn.setAttribute('aria-expanded', 'true');
  layer = UI.layer(pop, {
    ignore: [btn], focus: $('rateBtns').querySelector('[aria-pressed="true"]') || $('rateBtns').querySelector('.r3'),
    onClose: () => { layer = null; then = null; pop.hidden = true; btn.setAttribute('aria-expanded', 'false'); },
  });
}

/** 这时候要不要提醒打分（见文件开头） */
function shouldPrompt() {
  const st = study();
  if (!st || st.rated_today || prompted) return false;
  if (fromToday) return true;
  const now = since ? practiced + (performance.now() - since) / 1000 : practiced;
  return !st.paused && now >= 60;
}
/** 提醒打分；不用提醒时返回 false（调用方照常继续） */
function promptRating(done, title, skip) {
  if (!shouldPrompt()) return false;
  prompted = true;
  openPop({ done, title, skip });
  return true;
}
/** 打完分 / 跳过：接着做被拦下的事 */
function proceed() {
  const fn = then;
  then = null;
  if (layer) layer.close();
  if (fn) fn();
}

async function rate(r) {
  const body = { rating: r, issues: [...picked], note: $('rateNote').value.trim(), stuck_bars: stuckChoices.map(b => b.id).filter(id => stuck.has(id)) };
  const bpm = Math.round(get('play.bpm') || 0);
  if (bpm > 0) body.bpm = bpm;
  const btns = [...$('rateBtns').querySelectorAll('button')];
  btns.forEach(b => { b.disabled = true; });
  try {
    const res = await api.post(songUrl('/study/rate'), body);
    setLocal('song.study', res.study || null);
    const st = res.study || {};
    const next = st.state === 'learning' ? '明天再练' : st.due_in != null ? `下次：${dueText(st.due_in)}` : '';
    UI.toast(`已记下：${ratingName(r)}${next ? ` · ${next}` : ''}`);
    announce();
    proceed();
  } catch (e) {
    UI.toast(e.message, 'err', 4000);
    btns.forEach(b => { b.disabled = false; });
  }
}

export function initStudy() {
  mountTimer($('timer'));
  if (fromToday) { const b = $('backBtn'); b.href = '/#study'; b.title = '返回练琴'; b.setAttribute('aria-label', '返回练琴'); }
  $('rateBtn').onclick = () => openPop();
  $('rateSkip').onclick = proceed;
  $('rateStuck').addEventListener('click', e => {
    const b = e.target.closest('[data-bar]');
    if (!b) return;
    const id = b.dataset.bar;
    if (stuck.has(id)) stuck.delete(id); else stuck.add(id);
    b.setAttribute('aria-pressed', String(stuck.has(id)));
  });
  // 这次打开练了多久（提醒打分的条件之一）
  on(['play.playing', 'rhythm.running'], () => {
    const busy = !!get('play.playing') || !!get('rhythm.running');
    if (busy && !since) since = performance.now();
    else if (!busy && since) { practiced += (performance.now() - since) / 1000; since = 0; }
  });
  // 点“返回”：先提醒打分，打完 / 跳过再走。真要走了才告诉曲库是从这里回去的（恢复点进来之前的样子，lib-return.js）；
  // 打分弹层关掉、留在这页的不算
  $('backBtn').addEventListener('click', e => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return; // 新标签页打开：不拦
    const href = $('backBtn').href;
    if (promptRating(() => { markBack(SONG_ID, href); location.href = href; }, '走之前给今天打个分？', '跳过，直接返回')) e.preventDefault();
    else markBack(SONG_ID, href);
  });
  // 计时器的“结束”：先提醒打分，打完 / 跳过再结束计时（再点一次结束按钮，这次不拦）
  $('timer').addEventListener('click', e => {
    const b = e.target.closest('[data-act="stop"]');
    if (!b) return;
    if (promptRating(() => b.click(), '结束前给今天打个分？', '跳过，直接结束')) { e.preventDefault(); e.stopPropagation(); }
  }, true);
  $('rateIssues').addEventListener('click', e => {
    const b = e.target.closest('[data-issue]');
    if (!b) return;
    const c = b.dataset.issue;
    if (picked.has(c)) picked.delete(c); else picked.add(c);
    b.setAttribute('aria-pressed', String(picked.has(c)));
  });
  $('rateBtns').addEventListener('click', e => { const b = e.target.closest('[data-r]'); if (b && !b.disabled) rate(+b.dataset.r); });
  $('rateNote').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); const p = $('rateBtns').querySelector('[aria-pressed="true"]'); if (p) rate(+p.dataset.r); } });
  // 曲子（轮询）带来的 study：计划在曲库里改了、别的标签页打了分，这里跟着变；曲子的其他部分变了（练完一遍、问答）不重画
  on('song.study', () => { paintButton(study()); if (layer) renderPop(); });
  paintButton(study());
}
