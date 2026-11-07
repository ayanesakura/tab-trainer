// 练习页的小 store：一棵普通对象树，按路径 set / get / on。
//
// 顶层分几块：
//   song  后端返回的曲子（只读，改动一律 set 新值，不要原地改）
//   view  deriveView(song) 的结果（播放要用的小节、音高、段落……）
//   play  播放状态（ready / playing / bpm / a..b 循环范围 / loops 弹完几遍 / note、next 指板上的音）
//   ui    界面状态（bar 正在看的小节、view 当前页、editMode、pick 谱面上点选的小节、orig 原图弹窗……）
//   rhythm  敲节奏（rhythm-drill.js）：on 打开了（播放键 / 空格开始的是敲节奏），running 正在敲（播放键显示成停止）
//
// 去重：set 时按结构比较（普通对象和数组逐项比，其他按 ===），值没变就不通知。
// 订阅了子路径的（如 on('song.plan')）只在那一部分真的变了时才收到通知；订阅了父路径的，子路径变了也会收到。
// 一次 set 多个路径（set({a: 1, b: 2})）时，同一个订阅者只被调用一次。
// 这个文件不碰 DOM，Node 测试里也能用。

export function createStore(initial = {}) {
  let root = initial;
  const subs = []; // { paths: [[k, k, ...], ...], fn }

  const split = p => (p === '' ? [] : String(p).split('.'));
  const at = (obj, keys) => {
    for (const k of keys) { if (obj == null) return undefined; obj = obj[k]; }
    return obj;
  };
  // 路径上的对象逐层浅拷贝，最后一层换成新值（原来的树不变，订阅者比较新旧时用得到）
  const assoc = (obj, keys, v) => {
    if (!keys.length) return v;
    const [k, ...rest] = keys;
    const base = obj == null ? {} : obj;
    const copy = Array.isArray(base) ? base.slice() : { ...base };
    copy[k] = assoc(base[k], rest, v);
    return copy;
  };
  const isPrefix = (a, b) => a.length <= b.length && a.every((k, i) => k === b[i]);

  function get(path = '') { return at(root, split(path)); }

  function set(path, value) {
    const changes = typeof path === 'object' && path !== null ? Object.entries(path) : [[path, value]];
    const old = root;
    const touched = [];
    for (const [p, v] of changes) {
      const keys = split(p);
      if (equal(at(root, keys), v)) continue;
      root = assoc(root, keys, v);
      touched.push(keys);
    }
    if (!touched.length) return;
    // 订阅的路径和改动的路径有包含关系、并且那一部分前后确实不同，才通知
    const hit = subs.filter(s => s.paths.some(sp => touched.some(tk =>
      (isPrefix(sp, tk) || isPrefix(tk, sp)) && !equal(at(old, sp), at(root, sp)))));
    for (const s of hit) if (subs.includes(s)) s.fn(get(s.paths[0].join('.')), at(old, s.paths[0]));
  }

  /** on('ui.bar', fn) 或 on(['ui.bar', 'song.bars'], fn)；fn(第一个路径的新值, 旧值)。返回取消订阅的函数。 */
  function on(paths, fn) {
    const s = { paths: [].concat(paths).map(split), fn };
    subs.push(s);
    return () => { const i = subs.indexOf(s); if (i >= 0) subs.splice(i, 1); };
  }

  return { get, set, on };
}

const plain = x => x !== null && typeof x === 'object' && (Array.isArray(x) || Object.getPrototypeOf(x) === Object.prototype || Object.getPrototypeOf(x) === null);

/** 结构比较：普通对象和数组逐项比，其他（Set、类实例、函数）只看是不是同一个。 */
export function equal(a, b) {
  if (Object.is(a, b)) return true;
  if (!plain(a) || !plain(b) || Array.isArray(a) !== Array.isArray(b)) return false;
  const ka = Object.keys(a), kb = Object.keys(b);
  if (ka.length !== kb.length) return false;
  return ka.every(k => Object.prototype.hasOwnProperty.call(b, k) && equal(a[k], b[k]));
}

// 练习页用的那一个
export const store = createStore({
  song: null,
  view: null,
  play: { ready: false, playing: false, bpm: 50, a: 0, b: 0, loops: 0, note: -1, next: -1 },
  ui: { bar: -1, view: 'practice', editMode: false, pick: -1, orig: -1, np: false, more: false, drawer: false, fb: true },
  rhythm: { on: false, running: false },
});
export const { get, set, on } = store;
