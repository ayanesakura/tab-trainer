// 读取和本地写入的先后（song.js 用）：晚到的旧读取不能把更新的数据盖掉。
// 每次读取发出时领一个编号；写接口返回了整首（applySong）或本地并进去一部分（setLocal：问答、练习记录）时也领一个，
// 编号比“已经用上的”小的读取结果作废。不碰 DOM，Node 测试直接用（tests/js/trainer.test.mjs）。

export function createFresh() {
  let seq = 0, applied = 0;
  return {
    /** 发出一次读取：返回它的编号 */
    begin: () => ++seq,
    /** 编号 my 的读取回来了：还能不能用（之后发出的读取已经用上了、或者之后本地写过，就作废）；能用就记下用上了 */
    accept(my) {
      if (my < applied) return false;
      applied = my;
      return true;
    },
    /** 本地写进了更新的数据：在这之前发出、还没回来的读取都作废 */
    local() { applied = ++seq; },
  };
}
