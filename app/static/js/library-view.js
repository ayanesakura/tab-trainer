// 曲库页（index.js）的纯函数：一行曲子的 HTML、状态 / 练习 / 花费说明、筛选和排序。不碰 DOM，有 node 测试（tests/js/library.test.mjs）。
// 后端来的字段（包括数字）拼进 HTML 时一律 esc：曲子包、练习记录可能被改过。

import { esc, icon } from './lib/html.js';
import { SOURCES } from './vocab.js';
import { studyBadge } from './study-view.js';

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
    const kind = s.activity && s.activity.kind;
    const unit = s.recognizing ? '行' : kind === 'refine' ? '小节' : '批';
    return `<span class="spinner"></span><span>${esc(p.stage || '排队中')}${p.total ? ` · ${esc(p.done)}/${esc(p.total)} ${unit}` : ''}</span><span class="progress" role="progressbar" aria-label="进度" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"><i style="width:${pct}%"></i></span>`;
  }
  if (s.status === 'error') return `<span class="dot err"></span><span class="err-text">${isScore(s) ? '曲谱读取失败' : '识别失败'}${n ? `：${esc(n.msg)}` : ''}</span>`;
  if (s.status === 'cancelled') return `<span class="dot warn"></span><span>已取消${s.bars ? ` · 已识别 ${esc(s.bars)} 小节` : ''}</span>`;
  const review = s.needs_review_bars || 0;
  const parts = [];
  if (review) parts.push(`<span class="badge warn" title="有 ${review} 个小节需要对照原谱核对">${review} 小节待核对</span>`);
  if (n) parts.push(`<span class="badge ${n.kind === 'error' ? 'err' : n.kind === 'info' ? 'ok' : 'warn'} notice-badge" title="${esc(n.msg)}">${esc(n.msg)}</span>`);
  const r = s.refined; // 精修过整首（逐小节放大核对）
  if (r && !(n && n.kind === 'info')) parts.push(`<span class="badge ok" title="${esc(`${r.at} 精修：检查了 ${r.checked} 个小节，改了 ${r.changed} 个`)}">已精修</span>`);
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

// 识别失败 / 取消、又没有可练的小节时的下一步：模型设置的问题去设置，其他的（图片 / PDF 曲子）重新识别。
// 只认 key / 模型名 / 权限这类改了设置才会好的错误（文字和 app/llm.py 的 friendly() 对应：没设 key、key 无效、403、模型不存在）；
// 429 “额度不足”、过载、连不上 / 连接中断（提到 Base URL）、超时这些稍后重试就可能好，给重试按钮
const SETTINGS_ERR = /API key|模型名|模型设置|（403）/i;
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
  // 每首只占两行：曲名（+ 标签、待核对等徽章），下面一行艺人 · 小节数 · 练习进度；来源、添加时间、AI 花费放在这一行的悬停提示里
  const prac = practiceText(s);
  const sub = [esc(s.artist), barsText(s), prac && `<span class="practice">${esc(prac)}</span>`].filter(Boolean).join(' · ');
  const more = [src && `来源：${src}`, s.created && `添加于 ${esc(s.created)}`, costText(s).replace(/<[^>]+>/g, '')].filter(Boolean).join(' · ');
  const state = stateHTML(s);
  const busyish = s.busy || ['corrupt', 'error', 'cancelled'].includes(s.status); // 进度条 / 出错说明单独一行，徽章跟在曲名后面
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
    <div class="who"><div class="name">${title}${studyBadge(s.study)}${tags}${state && !busyish ? `<span class="badges">${state}</span>` : ''}</div>
      ${state && busyish ? `<div class="state">${state}</div>` : ''}
      ${sub || more ? `<div class="sub"${more ? ` title="${more}"` : ''}>${sub || more}</div>` : ''}</div>
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

// ---------------- 翻页 ----------------
export const PAGE_SIZE = 20;
/** 第 page 页（从 1 开始；超出范围时取最近的一页，比如删掉最后一页唯一的一首之后）。from / to 是这一页第几首到第几首（从 1 开始） */
export function paginate(list, page, size = PAGE_SIZE) {
  const pages = Math.max(1, Math.ceil(list.length / size));
  const p = Math.min(Math.max(1, Math.floor(Number(page)) || 1), pages);
  const start = (p - 1) * size;
  return { items: list.slice(start, start + size), page: p, pages, from: list.length ? start + 1 : 0, to: Math.min(start + size, list.length) };
}
/** 要显示的页码：不超过 7 页全列出；多了保留首页、末页和当前页前后各一页，省略的地方是 null（只省一页时直接列出那一页） */
export function pageNumbers(page, pages) {
  if (pages <= 7) return Array.from({ length: pages }, (_, i) => i + 1);
  const keep = new Set([1, pages, page - 1, page, page + 1]);
  if (page <= 4) [2, 3, 4, 5].forEach(n => keep.add(n));
  if (page >= pages - 3) [pages - 4, pages - 3, pages - 2, pages - 1].forEach(n => keep.add(n));
  const nums = [...keep].filter(n => n >= 1 && n <= pages).sort((a, b) => a - b);
  const out = [];
  nums.forEach((n, i) => {
    const gap = i ? n - nums[i - 1] : 1;
    if (gap === 2) out.push(n - 1);
    else if (gap > 2) out.push(null);
    out.push(n);
  });
  return out;
}
/** 列表下面的翻页条（只有一页时返回 ''）。total：筛选后的曲子数 */
export function pagerHTML({ page, pages, from, to }, total) {
  if (pages <= 1) return '';
  const nav = (dir, ic, label, off) => `<button class="icon-btn sm" data-page="${dir}" data-k="${dir}" title="${label}" aria-label="${label}"${off ? ' disabled' : ''}>${icon(ic, 14)}</button>`;
  const nums = pageNumbers(page, pages).map(n => n === null
    ? '<span class="gap" aria-hidden="true">…</span>'
    : `<button class="pg" data-page="${n}" data-k="p${n}" aria-label="第 ${n} 页"${n === page ? ' aria-current="page"' : ''}>${n}</button>`).join('');
  return `<span class="hint range">${from}–${to} / ${total} 首</span><span class="grow"></span>${nav('prev', 'left', '上一页', page <= 1)}${nums}${nav('next', 'right', '下一页', page >= pages)}`;
}
