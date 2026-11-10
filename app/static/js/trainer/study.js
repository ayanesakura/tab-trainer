// 练琴计划在练习页上的部分：顶栏的计时器、“打分”按钮和打分弹层；从今日清单进来（?from=today）时“返回”回到练琴页。
// 这首在计划里才显示“打分”。打分的状态就是 song.study（只订阅这一项；谱面只在 song.bars / song.meta 等变了时才重新派生，
// 改 song.study 不会）。打完分用 setLocal 写回 song.study：之前发出、还没回来的轮询作废，不会把旧的状态盖回来。
// 每次打分记下：1–4 分、勾的问题、备注、当时的速度（play.bpm）；同一天再打一次是改分（后端按天取最后一次）。

import { get, on } from './store.js';
import { setLocal } from './song.js';
import { $, UI, esc, songUrl } from './dom.js';
import * as api from '../lib/api.js';
import { icon } from '../lib/html.js';
import { mountTimer, announce } from '../lib/study-timer.js';
import { KINDS, RATING_CLS, ratingName, gapText, dueText } from '../study-view.js';
import { STUDY_RATINGS, STUDY_ISSUES } from '../vocab.js';

const RATE_TIPS = { 1: '弹不下来，要停', 2: '能弹完，有几处出错', 3: '基本没错', 4: '很轻松，可以隔久一点' };
const fromToday = new URLSearchParams(location.search).get('from') === 'today';

let layer = null;
const picked = new Set(); // 弹层里勾着的问题

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
  $('rateIssues').innerHTML = Object.entries(STUDY_ISSUES).map(([c, name]) => `<button data-issue="${esc(c)}" aria-pressed="${picked.has(c)}">${esc(name)}</button>`).join('');
  $('rateBtns').innerHTML = Object.entries(STUDY_RATINGS).map(([r, name]) => `<button class="rate r${esc(r)} ${RATING_CLS[r] || ''}" data-r="${esc(r)}" title="${esc(RATE_TIPS[r] || '')}" aria-pressed="${st.rated_today === +r}">
      <b>${esc(name)}</b><span>${esc(previewText(st, r))}</span></button>`).join('');
}

function openPop() {
  const st = study(), btn = $('rateBtn'), pop = $('ratePop');
  if (!st) return;
  if (layer) { layer.close(); return; }
  // 今天打过分：带出上次勾的问题和备注，改分时不用重来
  picked.clear();
  const last = st.rated_today && st.last;
  (last ? last.issues || [] : []).forEach(c => picked.add(c));
  $('rateNote').value = last ? last.note || '' : '';
  renderPop();
  pop.hidden = false;
  const r = btn.getBoundingClientRect();
  pop.style.top = `${r.bottom + 8}px`;
  pop.style.right = `${Math.max(8, innerWidth - r.right)}px`;
  btn.setAttribute('aria-expanded', 'true');
  layer = UI.layer(pop, {
    ignore: [btn], focus: $('rateBtns').querySelector('[aria-pressed="true"]') || $('rateBtns').querySelector('.r3'),
    onClose: () => { layer = null; pop.hidden = true; btn.setAttribute('aria-expanded', 'false'); },
  });
}

async function rate(r) {
  const body = { rating: r, issues: [...picked], note: $('rateNote').value.trim() };
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
    if (layer) layer.close();
  } catch (e) {
    UI.toast(e.message, 'err', 4000);
    btns.forEach(b => { b.disabled = false; });
  }
}

export function initStudy() {
  mountTimer($('timer'));
  if (fromToday) { const b = $('backBtn'); b.href = '/#study'; b.title = '返回练琴'; b.setAttribute('aria-label', '返回练琴'); }
  $('rateBtn').onclick = openPop;
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
