// 歌词：alphaTab 只看六线谱时把歌词画在谱表上方（和弦图上面），看着像是上一行的；这里自己画在每行六线谱的下方，
// 对准各拍的位置，多段歌词一段一行（每行下面留多少空在 player.js 的 onScoreLoaded 里按歌词段数设）。

import { on } from './store.js';
import { $ } from './dom.js';
import { getApi, lyricLines, LYRIC_LINE } from './player.js';
import { lyricSpots } from './at-adapter.js';

function drawLyrics() {
  let layer = $('lyricLayer');
  if (!layer) { layer = document.createElement('div'); layer.id = 'lyricLayer'; $('score').appendChild(layer); }
  layer.replaceChildren();
  const api = getApi();
  if (!api || !api.score || !lyricLines(api.score)) return;
  for (const { text, x, y, k } of lyricSpots(api, $('score'))) {
    const el = document.createElement('span');
    el.className = 'lyric';
    el.textContent = text;
    Object.assign(el.style, { left: x + 'px', top: y + 2 + k * LYRIC_LINE + 'px' });
    layer.appendChild(el);
  }
}

export function initLyrics() {
  on('ui.renderGen', drawLyrics); // 谱面画完（小节位置才有）
}
