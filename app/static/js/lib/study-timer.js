// 练琴计时器（曲库页“练琴”视图和练习页顶栏共用）：开始 / 暂停 / 继续 / 结束。计时在服务端（data/study.json），
// 关掉页面、换页面都不会断；这里只负责显示和按钮。显示的秒数 = 上次从服务端取到的秒数 + 取到之后本机走过的时间
// （不用两边的时钟对表）。别的标签页改了计时（BroadcastChannel 'tt-study'）、页面重新可见时重新取一次。
//
// mountTimer(el, {onChange}) → {refresh(timer?)}：el 是一个空的容器；onChange(timer, session) 在计时状态变了之后调用
// （结束计时时 session 是刚记下的那一段，曲库页据此刷新今日进度）。

import { esc, icon } from './html.js';
import * as api from './api.js';
import { toast, prompt } from '/static/ui.js';
import { fmtClock, fmtMin } from '../study-view.js';

const TITLE_PREFIX = /^\d+:\d{2}(:\d{2})? · /;
const LONG = 3 * 3600; // 超过 3 小时才结束：多半是忘了停，问一下记多少分钟
let channel = null;
try { channel = new BroadcastChannel('tt-study'); } catch { /* 不支持就只在页面重新可见时同步 */ }
export const announce = () => { try { channel && channel.postMessage('changed'); } catch { /* 页面正在关 */ } };
export const onAnnounce = fn => { if (channel) channel.addEventListener('message', fn); };

export function timerHTML(t, seconds) {
  if (!t) return `<button class="btn sm timer-start" data-act="start" title="开始计时：不开谱子练的时间也记进今天">${icon('clock', 13)}计时</button>`;
  const run = t.running;
  return `<div class="timer-pill${run ? ' on' : ' paused'}" role="group" aria-label="练琴计时">
    <span class="dot ${run ? 'busy' : 'warn'}" aria-hidden="true"></span>
    <span class="t" role="timer" aria-live="off" title="${run ? '正在计时' : '已暂停'}">${esc(fmtClock(seconds))}</span>
    <button class="icon-btn xs" data-act="${run ? 'pause' : 'resume'}" title="${run ? '暂停' : '继续'}" aria-label="${run ? '暂停计时' : '继续计时'}">${icon(run ? 'pause' : 'play', 12)}</button>
    <button class="icon-btn xs" data-act="stop" title="结束并记下" aria-label="结束计时">${icon('stop', 12)}</button>
  </div>`;
}

export function mountTimer(el, { onChange } = {}) {
  let timer = null, base = 0, at = 0, tick = 0, busy = false;
  const seconds = () => base + (timer && timer.running ? (performance.now() - at) / 1000 : 0);
  function paint() {
    const html = timerHTML(timer, seconds());
    // 每秒只换数字，不重建按钮（焦点、悬停状态都还在）
    const t = el.querySelector('.timer-pill .t');
    if (el._shape === shapeOf() && t) t.textContent = fmtClock(seconds());
    else { el.innerHTML = html; el._shape = shapeOf(); }
    // 浏览器标签上也显示正在计时的时间（练习页的标题是曲名，只加 / 去掉前缀）
    const plain = document.title.replace(TITLE_PREFIX, '');
    document.title = timer && timer.running ? `${fmtClock(seconds())} · ${plain}` : plain;
  }
  const shapeOf = () => (timer ? (timer.running ? 'run' : 'pause') : 'idle');
  function set(t) {
    timer = t || null;
    base = timer ? Number(timer.seconds) || 0 : 0;
    at = performance.now();
    clearInterval(tick);
    if (timer && timer.running) tick = setInterval(paint, 1000);
    paint();
  }
  async function refresh(t) {
    if (t !== undefined) { set(t); return; }
    try { set((await api.get('/api/study/timer')).timer); } catch { /* 取不到就保持原样，下次再取 */ }
  }
  async function act(action) {
    if (busy) return;
    busy = true;
    try {
      const body = { action };
      if (action === 'stop' && seconds() > LONG) {
        const v = await prompt('记多少分钟？', String(Math.round(seconds() / 60)), { title: `已经计时 ${fmtMin(seconds() / 60)}`, ok: '记下', hint: '超过 3 小时，可能忘了停' });
        if (v === null) return;
        const m = Number(v);
        if (!(m > 0 && m <= 600)) { toast('分钟数要在 1–600 之间', 'err'); return; }
        body.minutes = m;
      }
      const r = await api.post('/api/study/timer', body);
      set(r.timer);
      if (action === 'stop') toast(r.session ? `已记下 ${fmtMin(r.session.minutes)}` : '不到 1 分钟，没有记', r.session ? 'ok' : 'info');
      announce();
      if (onChange) onChange(r.timer, r.session || null);
    } catch (e) {
      toast(e.message, 'err', 4000);
      refresh(); // 409（别处已经停了 / 开了）：以服务端为准
    } finally {
      busy = false;
    }
  }
  el.addEventListener('click', e => { const b = e.target.closest('[data-act]'); if (b) act(b.dataset.act); });
  onAnnounce(() => refresh().then(() => onChange && onChange(timer, null)));
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  paint();
  refresh();
  return { refresh, get: () => timer };
}
