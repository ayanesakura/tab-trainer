// 小节 AI 教练：针对正在看的小节向 AI 提问，问答记在小节上。回答边生成边显示（流式接口，见 app/main.py 的 ask_bar_stream）。

import { get, on } from './store.js';
import { $, UI, esc, setButton, songUrl } from './dom.js';
import { postStream } from '../lib/api.js';
import { holdBarWhile } from './player.js';
import { setLocal } from './song.js';
import { hasKey, askForKey } from './ai-key.js';

// 正在等的回答（一次只问一个）：{bar: 小节 id, q: 问题, a: 已经收到的回答}
let pending = null;
let shownBar = null; // 问答列表上次画的是哪个小节：换了小节就滚到最底下

// 问答列表不自己滚（不在会滚动的侧栏里再套一层滚动）：回答变长时，侧栏本来就滚在最底下的话跟着滚到底；用户往上翻着看时不打扰
function keepBottom(update) {
  const el = $('side');
  const stick = el.scrollHeight > el.clientHeight && el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  update();
  if (stick) el.scrollTop = el.scrollHeight;
}

const qaHtml = x => `<div class="qa${x.stale ? ' stale' : ''}"><div class="q">${UI.icon('sparkle', 13)}<span>${esc(x.q)}</span>${x.stale ? '<span class="old" title="这一小节后来改过音，回答说的可能是改之前的谱">改谱前</span>' : ''}<span class="t">${esc(x.at || '')}</span></div><div class="a">${esc(x.a)}</div></div>`;
const pendingAnswer = () => pending.a ? esc(pending.a) : '<span class="spinner"></span>思考中…';

function render() {
  const bi = get('ui.bar'), s = get('song');
  if (bi < 0 || !s || !s.bars[bi]) return;
  const bar = s.bars[bi], qa = bar.qa || [];
  const mine = pending && pending.bar === bar.id;
  const list = $('qaList');
  const draw = () => {
    list.innerHTML = qa.map(qaHtml).join('') + (mine
      ? `<div class="qa pending"><div class="q">${UI.icon('sparkle', 13)}<span>${esc(pending.q)}</span></div><div class="a">${pendingAnswer()}</div></div>`
      : '');
    list.hidden = !qa.length && !mine;
  };
  if (shownBar !== bar.id) { shownBar = bar.id; draw(); } else keepBottom(draw);
  const busy = !!pending;
  $('askBtn').disabled = busy;
  const otherN = busy && !mine ? (s.bars.find(b => b.id === pending.bar) || {}).n || '' : '';
  $('askBtn').title = otherN ? `正在回答第 ${otherN} 小节的问题，回答完再问` : '';
  setButton($('askBtn'), mine ? '<span class="spinner"></span>回答中…' : busy ? `<span class="spinner"></span>等第 ${otherN} 小节` : '提问');
}

// 收到一段回答：只改正在回答的那一条（不重画整个列表）；它不在屏幕上（看的是别的小节）就只记下
function showPending() {
  const a = $('qaList').querySelector('.qa.pending .a');
  if (a) keepBottom(() => { a.innerHTML = pendingAnswer(); });
}

async function ask() {
  const bi = get('ui.bar');
  if (bi < 0 || pending) return;
  if (!hasKey()) { askForKey(); return; } // 回车提问不经过按钮：在这里也拦一下
  const q = $('askInput').value.trim();
  if (!q) { $('askInput').focus(); return; }
  const bar = get('song').bars[bi];
  pending = { bar: bar.id, q, a: '' };
  $('askInput').value = ''; // 问题挪到列表里显示；失败时放回来
  render();
  try {
    const d = await postStream(songUrl(`/bars/${encodeURIComponent(bar.id)}/ask/stream`), { question: q }, ev => {
      if (ev.type === 'delta') pending.a += ev.text;
      else if (ev.type === 'reset') pending.a = ''; // 模型服务从头再来：前面收到的作废
      showPending();
    });
    // 回答写进了这个小节的 qa（可能已经换到别的小节看了，按 id 找）：只把 qa 并到 store 里（不用重新取整首曲子）。
    // 问答不改小节的 rev 和曲子的 meta_rev，所以下一次改谱 / 改谱头带的版本号仍然是对的，不会误报 409
    pending = null;
    const k = get('song').bars.findIndex(x => x.id === bar.id);
    if (k >= 0) setLocal(`song.bars.${k}.qa`, d.qa); // 在这之前发出的轮询晚到了也不会把新问答盖掉
  } catch (e) {
    // 没存下来的半截回答不留；问题放回输入框（还停在这个小节、也没有新打字时），改一改就能再问
    pending = null;
    const cur = get('song').bars[get('ui.bar')];
    if (cur && cur.id === bar.id && !$('askInput').value.trim()) $('askInput').value = q;
    const lost = e.status === 0 && /断了/.test(e.message || '');
    UI.toast(lost ? e.message + '。服务还在运行的话，回答写完会存进这一小节，稍后刷新页面可以看到' : (e.message || '提问失败'), 'err', 6000);
  } finally {
    pending = null; render();
  }
}

export function initCoach() {
  $('askBtn').onclick = ask;
  $('askInput').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); ask(); } });
  on(['ui.bar', 'song.bars'], render);
  // 播放中打字提问：侧栏停在正在看的小节，问题不会挂到按回车那一刻光标走到的小节上；
  // Tab 到“提问”（键盘焦点）也停着。鼠标点“提问”时按下到松开之间由 bar-card.js 停住（按在侧栏里）
  holdBarWhile(() => document.activeElement === $('askInput') || $('askBtn').matches(':focus-visible'));
}
