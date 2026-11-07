// 和后端说话的唯一入口（ES 模块）：get / post / put / del / upload / download，出错统一抛 ApiError(detail)；
// withBusy(btn, fn) 给按钮加“忙”状态并防止重复提交；poll(fn, {interval, while}) 轮询，出错退避重试。

export class ApiError extends Error {
  constructor(message, status = 0, data = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status; // 0 = 没连上
    this.data = data;
  }
}

// 后端错误统一是 {"detail": "中文说明"}；万一是 Pydantic 的列表、代理返回的 HTML 或空响应，也给一句能看懂的话
function detailOf(data, text, status) {
  const d = data && data.detail;
  if (typeof d === 'string' && d) return d;
  if (Array.isArray(d) && d.length) return d.map(x => (x && x.msg) || String(x)).join('；');
  const plain = (text || '').trim();
  if (plain && plain.length <= 200 && !plain.startsWith('<')) return plain;
  return `请求失败（${status}）`;
}

async function send(method, path, body, { signal, headers } = {}) {
  const init = { method, headers: { ...headers }, signal };
  if (body instanceof FormData || body instanceof Blob) init.body = body;
  else if (body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
  let r;
  try { r = await fetch(path, init); } catch (e) {
    if (e.name === 'AbortError') throw e;
    throw new ApiError('连不上 tab-trainer 服务，确认它还在运行', 0);
  }
  return r;
}

async function request(method, path, body, opts) {
  const r = await send(method, path, body, opts);
  const text = await r.text();
  let data = null;
  if (text) { try { data = JSON.parse(text); } catch { data = null; } }
  if (!r.ok) throw new ApiError(detailOf(data, text, r.status), r.status, data);
  return data;
}

/** 值得自动重试的错误：连不上服务（status 0），或代理 / 服务暂时不可用（502 / 503 / 504）。
 * 其他（404、曲子数据损坏的 500 等带说明的错误）重试也没用。 */
export const transient = e => !!e && (!e.status || [502, 503, 504].includes(e.status));

export const get = (path, opts) => request('GET', path, undefined, opts);
export const post = (path, body, opts) => request('POST', path, body, opts);
export const put = (path, body, opts) => request('PUT', path, body, opts);
export const del = (path, opts) => request('DELETE', path, undefined, opts);

// 上传文件：files 是 File 列表（字段名默认 files），fields 是其他表单字段；也可以直接传一个 FormData
export function upload(path, files, fields = {}, { field = 'files', ...opts } = {}) {
  let fd = files;
  if (!(files instanceof FormData)) {
    fd = new FormData();
    for (const f of files || []) fd.append(field, f);
    for (const [k, v] of Object.entries(fields)) if (v !== undefined && v !== null) fd.append(k, v);
  }
  return request('POST', path, fd, opts);
}

// 下载文件（导出曲子包等）：→ {blob, filename}，文件名取自 Content-Disposition
export async function download(path, opts) {
  const r = await send('GET', path, undefined, opts);
  if (!r.ok) {
    const text = await r.text();
    let data = null;
    try { data = JSON.parse(text); } catch { /* 不是 JSON */ }
    throw new ApiError(detailOf(data, text, r.status), r.status, data);
  }
  const cd = r.headers.get('content-disposition') || '';
  const m = cd.match(/filename\*=utf-8''([^;]+)/i) || cd.match(/filename="?([^";]+)/i);
  let filename = '';
  if (m) { try { filename = decodeURIComponent(m[1]); } catch { filename = m[1]; } }
  return { blob: await r.blob(), filename };
}

// 按钮忙状态：禁用 + 转圈，结束（成功或失败）后恢复原样；按钮忙的时候再点直接忽略（防止重复提交）。
// 返回 fn 的结果；忙的时候被忽略返回 undefined。btn 可以是 null（只执行 fn）。
export async function withBusy(btn, fn) {
  if (!btn) return fn();
  if (btn.getAttribute('aria-busy') === 'true') return undefined;
  const html = btn.innerHTML, wasDisabled = btn.disabled, w = btn.style.minWidth;
  btn.style.minWidth = btn.offsetWidth + 'px'; // 换成转圈时宽度不跳
  btn.setAttribute('aria-busy', 'true');
  btn.disabled = true;
  const ic = btn.querySelector('.ic');
  if (ic) ic.outerHTML = '<span class="spinner" aria-hidden="true"></span>';
  else btn.insertAdjacentHTML('afterbegin', '<span class="spinner" aria-hidden="true"></span>');
  try {
    return await fn();
  } finally {
    btn.innerHTML = html;
    btn.disabled = wasDisabled;
    btn.style.minWidth = w;
    btn.removeAttribute('aria-busy');
  }
}

// 轮询：立刻执行一次 fn，之后只要 while(结果) 为真就每 interval 毫秒再来一次。
// fn 抛错时不停：间隔翻倍退避（最多 maxInterval），成功一次后恢复。页面在后台时不跑，回到前台立刻补一次。
// → { now(), stop() }：now() 立刻再跑一次（停了的也会重新开始），stop() 彻底停下。
export function poll(fn, { interval = 2500, while: keep = () => true, maxInterval = 30000 } = {}) {
  let timer = null, stopped = false, running = false, again = false, fails = 0, idle = false; // idle：因为页面在后台跳过了一次
  async function tick() {
    clearTimeout(timer); timer = null;
    if (stopped) return;
    if (running) { again = true; return; }
    if (document.hidden) { idle = true; return; } // visibilitychange 时再跑
    running = true; idle = false;
    let delay = interval, cont = true;
    try {
      cont = !!keep(await fn());
      fails = 0;
    } catch (e) {
      fails++;
      delay = Math.min(maxInterval, interval * 2 ** fails);
      console.warn('轮询出错，稍后重试：', e && e.message);
    }
    running = false;
    if (stopped) return;
    if (again) { again = false; tick(); return; }
    if (cont) timer = setTimeout(tick, delay);
  }
  const onVis = () => { if (!document.hidden && idle && !stopped) tick(); };
  let listening = false; // stop() 摘掉监听，now() 重新开始时再挂上（只挂一次）
  const listen = on => { if (on !== listening) { listening = on; document[on ? 'addEventListener' : 'removeEventListener']('visibilitychange', onVis); } };
  listen(true);
  tick();
  return {
    now() { stopped = false; listen(true); tick(); },
    stop() { stopped = true; clearTimeout(timer); timer = null; listen(false); },
  };
}
