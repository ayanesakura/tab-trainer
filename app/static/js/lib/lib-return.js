// 从曲库点进练习页、再回到曲库：曲库恢复成离开前的样子（页码、标签、归档筛选、搜索词、滚动位置）。
// 曲库点开一首时（index.js）记两份：这条浏览记录的 history.state（浏览器“后退”回来用），和这里的 sessionStorage——
// 练习页的“返回”按钮、打分后返回、“返回曲库”链接是新开一条浏览记录，history.state 是空的，只能从这里取。
// sessionStorage 这份只有练习页走这几个返回（markBack：同一首、去的是曲库）时才算数，曲库取一次就删：
// 新开网址、点品牌链接、在练习页地址栏里输 / 都是默认的曲库。网站存储被禁用时读写会抛错：当没记过。
// 不碰 DOM、不读全局 history，Node 里传一个假的 storage 测（tests/js/library.test.mjs）。
const KEY = 'tt.libReturn';

function session() {
  try { return globalThis.sessionStorage || null; } catch { return null; }
}
function read(s) {
  try { const v = JSON.parse(s.getItem(KEY)); return v && typeof v === 'object' ? v : null; } catch { return null; }
}
function write(s, v) {
  try { s.setItem(KEY, JSON.stringify(v)); return true; } catch { return false; }
}

/** 记下的状态整理成能直接用的值（缺的、类型不对的用默认值） */
export function cleanReturn(v) {
  v = v && typeof v === 'object' ? v : {};
  return {
    page: Math.max(1, Math.floor(Number(v.page)) || 1),
    tag: typeof v.tag === 'string' ? v.tag : '',
    archived: v.archived === true,
    q: typeof v.q === 'string' ? v.q : '',
    y: Math.max(0, Number(v.y) || 0),
  };
}

/** 曲库：点开 id 这首之前记下当前状态 {page, tag, archived, q, y} */
export function saveReturn(id, state, s = session()) {
  if (s) write(s, { ...cleanReturn(state), id, back: false });
}

/** 练习页：通过页内的返回去 href 之前调用。记录是从曲库点开的同一首（id）、去的是曲库（/ 或 /#library）才打上“回去”的标记 → 是否打上 */
export function markBack(id, href, s = session()) {
  if (!s || !id) return false;
  const v = read(s);
  if (!v || v.id !== id) return false;
  let u;
  try { u = new URL(href, 'http://localhost/'); } catch { return false; }
  if (u.pathname !== '/' || u.search || (u.hash && u.hash !== '#library')) return false; // 从今日进来的回练琴页（/#study）：不恢复曲库
  return write(s, { ...v, back: true });
}

/** 曲库：新开一条浏览记录时调用（history.state 是空的）。练习页打过“回去”标记的 → 离开前的状态，否则 null；记录都删掉（只用一次） */
export function takeReturn(s = session()) {
  if (!s) return null;
  const v = read(s);
  if (!v) return null;
  try { s.removeItem(KEY); } catch { /* 删不掉：下次 saveReturn 会覆盖 */ }
  return v.back === true ? cleanReturn(v) : null;
}
