// 精修（逐小节放大核对，见 app/refine.py）的文字：整首的确认框（练习页和曲库页共用）、小节卡片上“精修这个小节 / 这一行”的预估、
// 进行中的范围。纯函数，有 node 测试。
// est 是后端给的预估（refine_estimate / refine_scopes 里的一项）：{bars, cost_usd, minutes, seconds}
// （费用 = 小节数 × 约 $0.2，时间按几个小节并行估算）。

/** 整首的确认框 → {title, text, ok}；没有可以精修的小节时返回 null */
export function refineConfirm(est, title = '') {
  if (!est || !est.bars) return null;
  const cost = Number(est.cost_usd || 0).toFixed(2);
  return {
    title: title ? `精修整首“${title}”？` : '精修整首曲子？',
    text: `会把 ${est.bars} 个小节逐个放大，让 AI 对照原谱逐拍核对；改了的小节会标出改了什么，可以撤销。手改过、确认过没问题的小节不核对（可以在小节卡片上单独精修）。\n`
      + `预计约 $${cost}，约 ${est.minutes || 1} 分钟。期间可以照常练习，随时可以取消。`,
    ok: '开始精修',
  };
}
export const NO_REFINE = '没有可以精修的小节（手改过、确认过没问题的小节不再核对）';

/** 费用：$0.2、$0.8、$1.25（不补零） */
const usd = x => `$${Number(Number(x || 0).toFixed(2))}`;
/** 时间：75 秒 → “1–2 分钟”，120 秒 → “约 2 分钟” */
export function refineTime(seconds) {
  const s = Number(seconds) || 0, lo = Math.max(1, Math.floor(s / 60)), hi = Math.max(1, Math.ceil(s / 60));
  return lo === hi ? `约 ${lo} 分钟` : `${lo}–${hi} 分钟`;
}
/** 小节卡片：精修这个小节的预估，如“约 $0.2，1–2 分钟” */
export const barRefineHint = est => (est ? `约 ${usd(est.cost_usd)}，${refineTime(est.seconds)}` : '');
/** 小节卡片：精修这一行的预估，如“这一行 4 个小节，约 $0.8，1–2 分钟” */
export const rowRefineHint = est => (est && est.bars ? `这一行 ${est.bars} 个小节，约 ${usd(est.cost_usd)}，${refineTime(est.seconds)}` : '');

/** 正在精修的范围（activity）怎么说：整首 / 第 12 小节 / 第 3 行（4 个小节） / 5 个小节。barIds 是曲子里小节 id 的顺序 */
export function refineScopeLabel(a, barIds = []) {
  const ids = (a && a.bars) || [];
  if (a && a.scope === 'row' && Number.isInteger(a.row)) return `第 ${a.row + 1} 行（${ids.length} 个小节）`;
  if (a && a.scope === 'bars' && ids.length === 1) {
    const i = barIds.indexOf(ids[0]);
    return i >= 0 ? `第 ${i + 1} 小节` : '1 个小节';
  }
  if (a && a.scope === 'bars') return `${ids.length} 个小节`;
  return '整首';
}
