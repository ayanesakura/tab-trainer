// 练习页开着的时间（不开播放、只看着谱子练也算）：页面在前台时每 30 秒给服务端一次心跳（POST /api/study/page/{id}），
// 服务端按自己的时钟把这段时间记成一条 kind=page 的练琴记录，和播放、秒表合在一起去重，算进今天、日历和连续天数
// （app/study.py 的 page_beat）。切到别的标签页、最小化、关掉页面时补最后一次（keepalive，页面关了也能发出去），
// 回到前台接着记；隔了一阵（电脑睡了）另起一段，中间不算。不到 1 分钟的不算。
//
// trackPageTime({song}) → {now(), keep(minutes), drop()}：
//   now()：先来一次心跳，返回最新的 {id, minutes, counted}（还没记下是 null）
//   keep(minutes) / drop()：离开前确认记多少分钟 / 这次不算；之后这条不再变（往返缓存恢复回来时换一条新的接着记）
import * as api from './api.js';

const BEAT_MS = 30000;
const newId = () => [...crypto.getRandomValues(new Uint8Array(4))].map(b => b.toString(16).padStart(2, '0')).join('');

export function trackPageTime({ song = null, interval = BEAT_MS } = {}) {
  let id = newId(), tick = 0, last = null, closed = false;
  const url = () => `/api/study/page/${id}`;

  async function beat() {
    try { last = (await api.post(url(), { song })).page || last; } catch { /* 下一次再记 */ }
    return last;
  }
  // 最后一次：只延长、不新建；页面正在关也要发出去
  function endBeat() {
    try {
      fetch(url(), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ song, end: true }), keepalive: true }).catch(() => {});
    } catch { /* 发不出去：最多少记最后不到 30 秒 */ }
  }
  function start() {
    if (tick || closed || document.hidden) return;
    beat();
    tick = setInterval(beat, interval);
  }
  function stop(end = true) {
    if (!tick) return;
    clearInterval(tick); tick = 0;
    if (end) endBeat();
  }
  document.addEventListener('visibilitychange', () => (document.hidden ? stop() : start()));
  addEventListener('pagehide', () => stop());
  // 从往返缓存恢复（点了返回又后退回来）：换一条新的记录接着记
  addEventListener('pageshow', e => { if (e.persisted) { stop(false); id = newId(); last = null; closed = false; start(); } });
  start();

  return {
    async now() {
      if (!closed && !document.hidden) await beat();
      return last;
    },
    async keep(minutes) {
      const cur = last;
      stop(); closed = true;
      if (cur && Math.round(cur.minutes) !== minutes) last = (await api.put(url(), { minutes })).page;
      return last;
    },
    async drop() {
      stop(false); closed = true;
      if (last) await api.del(`/api/study/sessions/${id}`);
      last = null;
    },
  };
}
