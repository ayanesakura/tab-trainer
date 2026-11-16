// 自动跟随的纯逻辑（follow.js 用）：播放到某一行时谱面该滚多少、滚多久、怎么起步和收尾。
// 不碰 DOM：每一行的位置由调用方量好传进来，Node 测试直接导入（tests/js/follow.test.mjs）。
//
// 规则——眼睛要跟得上：
//   - 一次只滚一行的距离。播放到新的一行，就把这一行顶到控制条下面（target）；每次换行页面都往上走一行，
//     新的一行正好落在上一行刚才的位置，视线几乎不用动。以前是等“下一行快看不见”才滚，一滚就把正在弹的那一行
//     从屏幕下方甩到顶上，常常一次跨两三行；
//   - 不用滚就不滚：从这一行到最后一行都看得见（短曲子、快弹完了），或者循环的这一段整段放得下、已经都露着；
//   - 循环的一段整段放得下但没露全：把段首那一行顶上去，之后循环里不再动。

const NEAR = 24; // 离要去的位置不到这么多像素就不动（免得为一点点偏差来回蹭）

/**
 * 播放到第 cur 行时要滚多少像素（正数 = 页面往上走）；不用滚是 0，量不出来是 null。
 * row(i)：第 i 行在视口里的 {y0: 上沿, y1: 下沿}，没有这一行返回 null；last：要弹到的最后一行（全曲 = 最后一行，循环 = 段尾那一行）；
 * loop：循环一段时是 {first, last}（段首、段尾所在的行），全曲是 null；
 * vis：{top, bottom} 看得见的范围（去掉吸顶的控制条和底部指板），target：一行顶到控制条下面时上沿的位置
 */
export function followShift(row, cur, last, loop, vis) {
  const rc = row(cur), rl = row(last);
  if (!rc || !rl) return null;
  const shown = (a, b) => a.y0 >= vis.top && b.y1 <= vis.bottom;
  if (loop) {
    const rf = row(loop.first), rz = row(loop.last);
    if (rf && rz && rz.y1 - rf.y0 <= vis.bottom - vis.target) return shown(rf, rz) ? 0 : rf.y0 - vis.target;
  }
  if (shown(rc, rl)) return 0;
  const d = rc.y0 - vis.target;
  return Math.abs(d) <= NEAR ? 0 : d;
}

/** 滚 dist 像素用多久（毫秒）：一行（两百来像素）大约 0.65 秒，越远越久，最多 1 秒——比浏览器自带的平滑滚动慢，看得清谱面往哪走 */
export const glideMs = dist => Math.round(Math.min(1000, 420 + 1.1 * Math.abs(dist)));

/** 进度 0..1 → 走了多少（0..1）：慢起、中间匀、慢停（三次缓入缓出）。起步慢，眼睛来得及跟上 */
export const ease = k => (k <= 0 ? 0 : k >= 1 ? 1 : k < 0.5 ? 4 * k * k * k : 1 - ((-2 * k + 2) ** 3) / 2);
