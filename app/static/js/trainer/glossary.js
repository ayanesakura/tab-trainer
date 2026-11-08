// 记号说明页：技巧名和记号取词表（vocab.json），这里只写怎么弹。有示范的可以试听（audio.js 的 DEMOS）。

import { $, esc, UI } from './dom.js';
import { TECHS } from '../vocab.js';
import { playDemo } from './audio.js';
import { stop } from './player.js';
import { get } from './store.js';

const HOW = {
  pm: '右手掌根外侧轻轻搭在琴桥和琴弦交界处再拨弦，声音变短、变闷。',
  sl: '只拨第一个音，左手保持按压滑到目标品，目标音不再拨弦。',
  si: '数字前只有斜线：从低一两品处拨弦，立刻滑进目标品。',
  h: '拨响前一个音后，另一根手指快速有力地砸到目标品发声（也叫捶弦）。',
  p: '手指离弦时往下勾一下，把弦带响。击弦和勾弦连用时整组只拨一次。',
  nh: '左手轻触标注品丝的<b>正上方</b>，不要按下，拨弦后立刻离开。谱上写成 &lt;12&gt;。',
  ah: '左手正常按住（如 2 品），右手食指轻点高 12 品的品丝上方（14 品），拇指或无名指拨弦后食指离开。',
  th: '左手照常按住（谱上 &lt;&gt; 前的数字，0 是空弦），右手手指快速敲击 &lt;&gt; 里那一品的品丝正上方并立刻弹开，不用拨弦。谱上写成 T.H. 0 &lt;19&gt;、5 &lt;22&gt;。',
  roll: '和弦从低音弦到高音弦快速依次拨开，谱上是竖着的波浪线。',
  x: '左手轻搭琴弦不按实，拨出没有音高的“哒”声。',
  vib: '按住后手指来回微微推拉，让音高轻轻起伏。',
  bend: '按住后把弦推起，音高升半音（1/2）或全音（full）。',
};
const DEMO = { pm: 'pm', sl: 'sl', si: 'si', h: 'h', p: 'p', nh: 'nh', ah: 'ah', th: 'th', roll: 'roll' };
// 不是技巧、但谱上常见的记号
const EXTRA = [
  { sym: '3', name: '三连音', how: '一拍平均放三个音，长度要一样。六连音同理。', demo: 'tri' },
  { sym: '(n)', name: '倚音', how: '小一号的音符，在正式音之前极快地带过，随即击弦或滑到正式音。', demo: 'grace' },
  { sym: '⌒', name: '连线（延音）', how: '同音之间的弧线表示延长，后面的音不再拨。' },
];

export function initGlossary() {
  const rows = [...TECHS.map(t => ({ sym: t.sym, name: t.name, how: HOW[t.code], demo: DEMO[t.code] })), ...EXTRA].filter(r => r.how);
  $('glossaryList').innerHTML = rows.map(r => `<div class="row"><code class="sym">${esc(r.sym)}</code><div class="who"><div class="name">${esc(r.name)}</div><div class="sub">${r.how}</div></div>${r.demo ? `<div class="ctl"><button class="btn sm demo" data-demo="${r.demo}" aria-label="试听：${esc(r.name)}">${UI.icon('play')}试听</button></div>` : ''}</div>`).join('');
  $('glossaryList').addEventListener('click', e => {
    const b = e.target.closest('button.demo');
    if (!b) return;
    if (get('play.playing')) stop();
    playDemo(b.dataset.demo);
  });
}
