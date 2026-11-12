// 首页（练琴 / 曲库）的 AI 对话：顶栏右边的“问 AI”打开右侧的对话抽屉（窄屏是底部抽屉）。不是模态：开着时照样能看、能点
// 今日 / 计划 / 曲库列表；Esc 或关闭按钮收起（UI.layer，点外面不收）。对话存在服务器（GET / DELETE /api/coach），
// 回答边生成边显示（POST /api/coach/ask/stream，NDJSON，同小节问答）。回答里的 [[id|曲名]] 换成练习页链接（home-coach-view.js）。
// AI 只给建议，不改数据。
import { UI, $, esc, icon, toast } from '/static/ui.js';
import * as api from '/static/js/lib/api.js';
import { EXAMPLES, answerHTML, settled, costTip } from '/static/js/home-coach-view.js';

const OPEN_KEY = 'tt.chat.open'; // 点回答里的链接去了练习页、再回来时抽屉还开着（只记在这个标签页）
let deps = { songs: () => [], keyOk: () => true, askForKey: () => {} };
let conv = { id: '', turns: [], cost: null };
let pending = null; // 正在等的回答：{q, a}
let layer = null; // 开着时是 UI.layer 的返回值
let loaded = false;

const turnHTML = (t, songs) => `<div class="qa"><div class="q">${icon('sparkle', 13)}<span>${esc(t.q)}</span>${t.at ? `<span class="t">${esc(t.at)}</span>` : ''}</div><div class="a">${answerHTML(t.a, songs)}</div></div>`;
const pendingAnswer = songs => (settled(pending.a) ? answerHTML(settled(pending.a), songs) : '<span class="spinner"></span>思考中…');

// 回答变长时：本来就滚在最底下的话跟着滚到底；用户往上翻着看时不打扰
function keepBottom(update, force = false) {
  const el = $('chatBody');
  const stick = force || el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  update();
  if (stick) el.scrollTop = el.scrollHeight;
}

function render({ toBottom = false } = {}) {
  const songs = deps.songs() || [];
  const empty = !conv.turns.length && !pending;
  $('chatEmpty').hidden = !empty;
  keepBottom(() => {
    $('chatList').innerHTML = conv.turns.map(t => turnHTML(t, songs)).join('') + (pending
      ? `<div class="qa pending"><div class="q">${icon('sparkle', 13)}<span>${esc(pending.q)}</span></div><div class="a">${pendingAnswer(songs)}</div></div>`
      : '');
  }, toBottom);
  $('chatSend').disabled = !!pending;
  $('chatSend').innerHTML = pending ? '<span class="spinner"></span>回答中…' : '发送';
  $('chatNew').disabled = !!pending || !conv.turns.length;
  $('chatNew').title = pending ? '等这个问题回答完' : '清空这段对话，重新开始';
  $('chatTitle').title = costTip(conv.cost);
}

function showPending() {
  const a = $('chatList').querySelector('.qa.pending .a');
  if (a) keepBottom(() => { a.innerHTML = pendingAnswer(deps.songs() || []); });
}

function setConv(d) { conv = { id: d.id || '', turns: d.turns || [], cost: d.cost || null }; }

async function load() {
  if (pending) return;
  try {
    setConv(await api.get('/api/coach'));
    loaded = true;
  } catch (e) {
    if (!loaded) toast(e.message || '读不到对话', 'err', 4000);
  }
  render({ toBottom: true });
}

async function ask(text) {
  if (pending) return;
  const q = (text ?? $('chatInput').value).trim();
  if (!q) { $('chatInput').focus(); return; }
  // 没有 API key（设置里查到的；还没查到时当作有）：先说明去设置填，不发请求
  if (!deps.keyOk()) { deps.askForKey('问 AI 要调用模型，先在设置里填 Anthropic API key。'); return; }
  pending = { q, a: '' };
  if (text == null) $('chatInput').value = ''; // 问题挪到列表里显示；失败时放回来
  render({ toBottom: true });
  try {
    const d = await api.postStream('/api/coach/ask/stream', { question: q }, ev => {
      if (ev.type === 'delta') pending.a += ev.text;
      else if (ev.type === 'reset') pending.a = ''; // 模型服务从头再来：前面收到的作废
      showPending();
    });
    pending = null;
    setConv(d);
  } catch (e) {
    pending = null;
    if (!$('chatInput').value.trim()) $('chatInput').value = q; // 半截回答不留，问题放回输入框
    const lost = e.status === 0 && /断了/.test(e.message || '');
    toast(lost ? e.message + '。服务还在运行的话，回答写完会存进对话，稍后重新打开可以看到' : (e.message || '提问失败'), 'err', 6000);
  } finally {
    pending = null;
    render({ toBottom: true });
  }
}

async function newChat() {
  if (pending) return;
  await api.withBusy($('chatNew'), async () => {
    try { setConv(await api.del('/api/coach')); } catch (e) { toast(e.message || '没能清空', 'err', 4000); }
  });
  render();
  $('chatInput').focus();
}

function paint(on) {
  document.body.classList.toggle('chat-open', on);
  $('chat').inert = !on;
  $('chatBtn').setAttribute('aria-expanded', String(on));
  try { on ? sessionStorage.setItem(OPEN_KEY, '1') : sessionStorage.removeItem(OPEN_KEY); } catch { /* 记不下就算了 */ }
}

export function open() {
  if (layer) { $('chatInput').focus(); return; }
  paint(true);
  layer = UI.layer($('chat'), { outside: false, focus: $('chatInput'), onClose: () => { layer = null; paint(false); } });
  render({ toBottom: true });
  load();
}
export function close() { if (layer) layer.close(); }
/** 曲库列表更新了：开着时重画（回答里的曲子链接按最新的曲库） */
export function refresh() { if (layer) render(); }

/** songs()：曲库列表（回答里的曲子能不能换成链接）；keyOk()：有没有 API key；askForKey(说明)：没有 key 时说明去设置填 */
export function initHomeCoach(opts) {
  deps = { ...deps, ...opts };
  $('chatEmpty').insertAdjacentHTML('beforeend', `<div class="chat-examples">${EXAMPLES.map(q => `<button class="chip" type="button">${esc(q)}</button>`).join('')}</div>`);
  $('chatEmpty').addEventListener('click', e => { const b = e.target.closest('.chat-examples button'); if (b) ask(b.textContent); });
  $('chatBtn').onclick = () => (layer ? close() : open());
  $('chatClose').onclick = close;
  $('chatNew').onclick = newChat;
  $('chatForm').onsubmit = e => { e.preventDefault(); ask(); };
  // 回车发送，Shift+回车换行；输入法选词时的回车不算
  $('chatInput').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(); }
  });
  // 输入法组字时按 Esc 是取消组字，不收起抽屉（浮层栈在 document 的捕获阶段处理 Esc，这里在 window 上先拦下）
  addEventListener('keydown', e => {
    if (e.key === 'Escape' && e.isComposing && $('chat').contains(e.target)) e.stopPropagation();
  }, true);
  paint(false);
  let reopen = false;
  try { reopen = sessionStorage.getItem(OPEN_KEY) === '1'; } catch { /* 读不到就不开 */ }
  if (reopen) open();
}
