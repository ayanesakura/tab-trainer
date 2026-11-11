// 导出 Guitar Pro 文件（.gp）：用 alphaTab 把现在的谱子写成 GP7，在其他软件里继续用。

import { get } from './store.js';
import { getApi, alphaTab, timbreProgram } from './player.js';
import { applyTimbre, scoreChords } from './at-adapter.js';
import { CHORD_PAD } from '../score.js';
import { UI } from './dom.js';

export function exportGp() {
  const api = getApi();
  if (!api || !api.score) { UI.toast('谱子还没加载好，稍后再导出', 'info'); return; }
  applyTimbre(api.score, timbreProgram(), true); // 导出原来的音色，不是练习时临时换的木吉他
  // 和弦名去掉为了区分大小写加的零宽空格（见 score.js 的 CHORD_PAD）：GP 文件里和弦按编号引用，不会再撞车
  const chords = scoreChords(api.score), names = chords.map(c => c.name);
  chords.forEach(c => { c.name = String(c.name || '').replaceAll(CHORD_PAD, ''); });
  let bytes;
  try { bytes = new alphaTab.exporter.Gp7Exporter().export(api.score, api.settings); } catch (e) {
    // alphaTab 写不出这份谱子（某些记号它导出不了）：说清楚，不要点了没反应
    console.warn('导出 GP 失败', e);
    UI.toast('导出 GP 失败：' + ((e && e.message) || e), 'err', 6000);
    return;
  } finally {
    applyTimbre(api.score, timbreProgram());
    chords.forEach((c, i) => { c.name = names[i]; });
  }
  const s = get('song'), a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([bytes], { type: 'application/octet-stream' }));
  a.download = ((s.title || (s.meta && s.meta.title) || 'tab').replace(/[\\/:*?"<>|]/g, '_')) + '.gp';
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}
