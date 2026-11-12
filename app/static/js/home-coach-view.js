// 首页 AI 对话（js/home-coach.js）的纯函数：回答里的曲子标记 [[id|曲名]] 换成练习页链接、示例问题、花费说明。不碰 DOM。
import { esc } from './lib/html.js';
import { canPlay } from './library-view.js';

export const EXAMPLES = Object.freeze(['今天先练哪首？', '帮我排一下这周的练习顺序', '哪些曲子该复习了？', '我最近练得怎么样？']);

// 模型提到曲子时写的标记（app/chat.py 的 ref）：[[<曲子 id>|曲名]]；id 和 app/store.py 的 _ID_RE 一致
const MARK = /\[\[([a-z0-9-]{4,64})\|([^\]\n]{0,200})\]\]/g;

/** 流式显示时还没收完的半截标记（结尾的 “[[…” 没有 “]]”）先不显示，免得闪一下乱码 */
export function settled(text) {
  const k = String(text ?? '').lastIndexOf('[[');
  if (k < 0) return String(text ?? '');
  const tail = text.slice(k);
  return tail.includes(']]') || tail.length > 280 || tail.includes('\n') ? text : text.slice(0, k);
}

/** 回答 → 安全的 HTML：全文转义；标记里的 id 是曲库里能练的曲子时换成练习页链接（显示曲库里的曲名），
 * 不认识的 id（编的、已经删了的）、还不能练的只显示文字，不出坏链接。songs：曲库列表（GET /api/songs 的项）。 */
export function answerHTML(text, songs = []) {
  const byId = new Map((songs || []).map(s => [s.id, s]));
  const src = String(text ?? '');
  let out = '', last = 0;
  for (const m of src.matchAll(MARK)) {
    out += esc(src.slice(last, m.index));
    const s = byId.get(m[1]);
    const name = (s && s.title) || m[2].trim() || '这首曲子';
    out += s && canPlay(s)
      ? `<a class="song-link" href="/trainer?id=${encodeURIComponent(s.id)}" title="打开练习页">${esc(name)}</a>`
      : `<span class="song-ref">${esc(name)}</span>`;
    last = m.index + m[0].length;
  }
  return out + esc(src.slice(last));
}

/** 标题上的悬停说明：累计花费（cost：views.cost_summary 的形状） */
export function costTip(cost) {
  if (!cost || !cost.tokens) return '和 AI 聊练习：先练哪首、怎么安排、最近练得怎么样。AI 只给建议，不会改你的计划和数据';
  const usd = cost.cost_usd >= 0.005 ? '$' + cost.cost_usd.toFixed(2) : cost.cost_usd > 0 ? '不到 $0.01' : '';
  const parts = [`AI 对话累计花费 ${usd || `${cost.tokens.toLocaleString()} token`}（新对话不清零）`];
  if (cost.unpriced_tokens) parts.push(`另有 ${cost.unpriced_tokens.toLocaleString()} token 未计价`);
  return parts.join('；');
}
