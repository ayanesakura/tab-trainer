// 精修（逐小节放大核对，见 app/refine.py）的确认框文字：练习页和曲库页共用。纯函数，有 node 测试。
// est 是后端给的 refine_estimate：{bars, cost_usd, minutes}（费用 = 小节数 × 约 $0.055，时间按几个小节并行估算）。

/** → {title, text, ok}；没有可以精修的小节时返回 null */
export function refineConfirm(est, title = '') {
  if (!est || !est.bars) return null;
  const cost = Number(est.cost_usd || 0).toFixed(2);
  return {
    title: title ? `精修“${title}”？` : '精修这首曲子？',
    text: `会把 ${est.bars} 个小节逐个放大，让 AI 对照原谱逐拍核对；改了的小节会标出改了什么，可以撤销。手改过、确认过没问题的小节不核对。\n`
      + `预计约 $${cost}，约 ${est.minutes || 1} 分钟。期间可以照常练习，随时可以取消。`,
    ok: '开始精修',
  };
}
export const NO_REFINE = '没有可以精修的小节（手改过、确认过没问题的小节不再核对）';
