// 曲库页（index.js）的纯函数：一行曲子的 HTML、状态 / 练习 / 花费说明、筛选和排序。不碰 DOM，有 node 测试（tests/js/library.test.mjs）。
// 后端来的字段（包括数字）拼进 HTML 时一律 esc：曲子包、练习记录可能被改过。

import { esc, icon } from './lib/html.js';
import { SOURCES } from './vocab.js';

const SOURCE_ICON = { image: 'image', pdf: 'file', gp: 'score', musicxml: 'score' };
export const isScore = s => !!s.is_score; // GP / MusicXML（后端给的）：没有原谱页面，不需要识别
export const canPlay = s => s.status !== 'corrupt' && (s.bars > 0 || isScore(s));
export const selectable = s => s.status !== 'corrupt'; // 能勾选导出

// “3 天前”：练习时间是本地时间字符串 “YYYY-MM-DD HH:MM”
export function ago(at, now = new Date()) {
  const d = new Date(String(at).replace(' ', 'T'));
  if (Number.isNaN(+d)) return String(at);
  const mins = (now - d) / 60000;
  if (mins < 1) return '刚刚';
  if (mins < 60) return `${Math.floor(mins)} 分钟前`;
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  if (d >= today) return '今天';
  const days = Math.ceil((today - d) / 86400000);
  if (days === 1) return '昨天';
  if (days < 30) return `${days} 天前`;
  return d.getFullYear() === now.getFullYear() ? `${d.getMonth() + 1} 月 ${d.getDate()} 日` : `${d.getFullYear()} 年 ${d.getMonth() + 1} 月`;
}
export function duration(sec) {
  const m = Math.round((sec || 0) / 60);
  if (m < 1) return '不到 1 分钟';
  if (m < 60) return `${m} 分钟`;
  return `${Math.floor(m / 60)} 小时${m % 60 ? ` ${m % 60} 分钟` : ''}`;
}
// 上次练的范围（小节号从 1 开始；后端已经按当前顺序排好 a_n ≤ b_n，小节被删了是 null）
export function rangeText(s) {
  const l = s.practice && s.practice.last;
  if (!l || l.a_n == null || l.b_n == null) return '';
  if (l.a_n === 1 && l.b_n === s.bars && s.bars > 1) return '全曲';
  return l.a_n === l.b_n ? `第 ${l.a_n} 小节` : `第 ${l.a_n}–${l.b_n} 小节`;
}
// 纯文本（ago() 遇到认不出的时间会原样返回），拼进 HTML 时要 esc
export function practiceText(s, now = new Date()) {
  const p = s.practice;
  if (!p || !p.last_at) return '';
  const range = rangeText(s);
  return [ago(p.last_at, now), range && (range === '全曲' ? '练全曲' : `练到${range}`), p.total_seconds > 0 && `累计 ${duration(p.total_seconds)}`].filter(Boolean).join(' · ');
}

// 需要注意的状态（忙、出错、待核对、警告）；一切正常时返回 ''，小节数放在说明行里。
// 下面拼 HTML 的函数里，后端来的字段（包括数字）一律 esc：曲子包、练习记录可能被改过
export function stateHTML(s) {
  const n = s.notice;
  if (s.status === 'corrupt') return `<span class="dot err"></span><span class="err-text">${esc(n && n.msg)}（可在“…”菜单删除）</span>`;
  if (s.busy) {
    const p = s.progress || {};
    const pct = p.total ? Math.round(100 * p.done / p.total) : 6;
    const unit = s.recognizing ? '行' : '批';
    return `<span class="spinner"></span><span>${esc(p.stage || '排队中')}${p.total ? ` · ${esc(p.done)}/${esc(p.total)} ${unit}` : ''}</span><span class="progress" role="progressbar" aria-label="进度" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"><i style="width:${pct}%"></i></span>`;
  }
  if (s.status === 'error') return `<span class="dot err"></span><span class="err-text">${isScore(s) ? '曲谱读取失败' : '识别失败'}${n ? `：${esc(n.msg)}` : ''}</span>`;
  if (s.status === 'cancelled') return `<span class="dot warn"></span><span>已取消${s.bars ? ` · 已识别 ${esc(s.bars)} 小节` : ''}</span>`;
  const review = s.needs_review_bars || 0;
  const parts = [];
  if (review) parts.push(`<span class="badge warn" title="有 ${review} 个小节需要对照原谱核对">${review} 小节待核对</span>`);
  if (n) parts.push(`<span class="badge ${n.kind === 'error' ? 'err' : 'warn'} notice-badge" title="${esc(n.msg)}">${esc(n.msg)}</span>`);
  return parts.join('');
}
export function barsText(s) {
  if (s.status === 'corrupt' || s.busy || s.status === 'error' || s.status === 'cancelled') return '';
  return s.bars ? `${esc(s.bars)} 小节` : isScore(s) ? '已导入' : '';
}

// AI 花费：早期曲子只记了 token，按 Opus 价格估算
export function costText(s) {
  if (!s.tokens) return '';
  const tip = [`共 ${s.tokens.toLocaleString()} token`];
  if (s.cost_estimated) tip.push('早期的曲子只记了 token 数，按 Opus 的价格估算');
  if (s.unpriced_tokens) tip.push(`其中 ${s.unpriced_tokens.toLocaleString()} token 用的是自定义模型，没算进金额`);
  const usd = s.cost_usd >= 0.005 ? '$' + s.cost_usd.toFixed(2) : s.cost_usd > 0 ? '<$0.01' : '';
  const text = usd ? `AI 花费${s.cost_estimated ? '约' : ''} ${usd}` : `AI 用量 ${s.tokens.toLocaleString()} token`;
  return `<span class="cost" title="${esc(tip.join('；'))}">${esc(text)}${s.unpriced_tokens && usd ? ' +' : ''}</span>`;
}

// 识别失败 / 取消、又没有可练的小节时的下一步：模型设置的问题去设置，其他的（图片 / PDF 曲子）重新识别
const SETTINGS_ERR = /API key|模型设置|模型名|Base URL|余额|额度/i;
export const settingsError = s => s.status === 'error' && s.notice && SETTINGS_ERR.test(s.notice.msg || '');
const retryButton = s => `<button class="btn sm act" data-k="retry-failed" data-retry-failed="${esc(s.id)}" title="只重新识别没识别出来的行">${icon('refresh', 12)}重试失败的 ${esc(s.failed_rows)} 行</button>`;
export function fixAction(s) {
  if (settingsError(s)) return `<a class="btn sm act" data-k="settings" href="#settings">${icon('settings', 12)}去设置</a>`;
  if (isScore(s)) return '';
  return s.failed_rows > 0 ? retryButton(s) : `<button class="btn sm act" data-k="retry" data-retry="${esc(s.id)}">${icon('refresh', 12)}重新识别</button>`;
}

/** 曲库里一行。selecting：选择模式（勾选导出），selected：已勾选的 id */
export function rowHTML(s, { selecting = false, selected = new Set() } = {}) {
  const play = canPlay(s), href = `/trainer?id=${encodeURIComponent(s.id)}`;
  const pick = selecting && selectable(s), on = pick && selected.has(s.id);
  const src = SOURCES[s.source_type] || '';
  const sub = [src, barsText(s), esc(s.artist), s.created && `添加于 ${esc(s.created)}`, costText(s)].filter(Boolean).join(' · ');
  const state = stateHTML(s);
  const prac = practiceText(s);
  const range = rangeText(s), last = s.practice && s.practice.last;
  const tags = (s.tags || []).length ? `<span class="tags">${s.tags.map(t => `<button class="badge tag" data-tag="${esc(t)}" data-k="tag:${esc(t)}" title="只看“${esc(t)}”">${esc(t)}</button>`).join('')}</span>` : '';
  const title = play && !selecting ? `<a class="title" href="${href}" data-k="open" title="${esc(s.title)}">${esc(s.title)}</a>` : `<span class="title" title="${esc(s.title)}">${esc(s.title)}</span>`;
  // 只有“接着练”用主色按钮，其他行的操作用普通按钮，曲库里不会一列全是实心按钮
  let action = '';
  if (play && last) action = `<a class="btn primary sm act" data-k="resume" href="${href}&resume=1" title="${esc(`从${range || '上次的位置'}${last.bpm ? `、${last.bpm} BPM` : ''} 接着练`)}">${icon('repeat', 12)}接着练</a>`;
  else if (play) action = `<a class="btn sm act" data-k="play" href="${href}">${icon('play', 12)}练习</a>`;
  else if (!s.busy && (s.status === 'error' || s.status === 'cancelled')) action = fixAction(s); // 没有可练的内容：给出下一步
  // 有识别失败的行、已经有小节可练：重试失败的行放在练习前面（只识别那几行，比整首重新识别便宜）
  if (play && !s.busy && s.failed_rows > 0 && !isScore(s) && !settingsError(s)) action = retryButton(s) + action;
  return `<div class="row song-row${play || pick ? ' click' : ''}${on ? ' on' : ''}" data-id="${esc(s.id)}" data-play="${play ? 1 : ''}" data-pick="${pick ? 1 : ''}">
    ${selecting ? `<input type="checkbox" class="pick" data-k="pick" aria-label="选择 ${esc(s.title)}" ${on ? 'checked' : ''} ${pick ? '' : 'disabled'}>` : ''}
    <span class="src-ic" title="${esc(src ? `来源：${src}` : '')}">${icon(SOURCE_ICON[s.source_type] || 'music', 18)}</span>
    <div class="who"><div class="name">${title}${tags}</div>
      ${state ? `<div class="state">${state}</div>` : ''}
      ${prac ? `<div class="sub practice">${icon('clock', 12)}<span>${esc(prac)}</span></div>` : ''}
      ${sub ? `<div class="sub">${sub}</div>` : ''}</div>
    <div class="ctl"${selecting ? ' hidden' : ''}>${action}
      <button class="icon-btn sm" data-more="${esc(s.id)}" data-k="more" title="更多" aria-label="${esc(s.title)}：更多操作" aria-haspopup="menu">${icon('more')}</button></div>
  </div>`;
}

const byCreated = (a, b) => (b.created || '').localeCompare(a.created || '');
export const SORTS = ['practiced', 'created', 'title'];
/** 按 sort 排序（原地，返回同一个数组）：最近练习（没练过的按添加时间排在后面）/ 最近添加 / 曲名 */
export function sortSongs(list, sort) {
  if (sort === 'title') list.sort((a, b) => a.title.localeCompare(b.title, 'zh-CN', { numeric: true }));
  else if (sort === 'practiced') {
    const at = s => (s.practice && s.practice.last_at) || '';
    list.sort((a, b) => at(b).localeCompare(at(a)) || byCreated(a, b));
  } else list.sort(byCreated);
  return list;
}
/** 搜索（q 已转小写，匹配曲名、艺人、标签）+ 标签筛选 + 排序，返回新数组 */
export function filterSongs(songs, { q = '', tag = '', sort = 'practiced' } = {}) {
  const hit = s => [s.title, s.artist, ...(s.tags || [])].some(x => (x || '').toLowerCase().includes(q));
  return sortSongs(songs.filter(s => (!tag || (s.tags || []).includes(tag)) && (!q || hit(s))), sort);
}
