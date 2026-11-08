// 从评测曲的 Guitar Pro 原文件导出“六线谱上画出来的每个数字”和它两边的括号，给 scripts/ocr_bench.py --gp-truth 用。
//   node scripts/ocr_gp_truth.mjs OUT.json eval-amy=/path/amy.gp [eval-r-mother=/path/mother.gp5 ...]
// 每首评测曲找小节数和标准答案一样的 6 弦音轨（几个文件 / 音轨都对得上时取普通音逐小节一致最多的）。
// 画法照 alphaTab 的六线谱（TabNoteGlyph）：鬼音画成 (n)；连音线的终点只在这个声部的第一拍画 (n)（跨小节线 /
// 换行的延续音），小节中间的不画；自然泛音画成 <n>；人工 / 点弦等泛音在品位后面另画一个小的 <品位+harmonicValue>；
// 死音画 x（OCR 不读，不导出）。数据目录：TAB_TRAINER_DATA，默认仓库下的 data/（只读）。
import { readFileSync, writeFileSync } from 'node:fs';
import * as at from '../app/static/vendor/alphatab/alphaTab.core.min.mjs';

const [out, ...pairs] = process.argv.slice(2);
if (!out || !pairs.length) {
  console.error('用法：node scripts/ocr_gp_truth.mjs OUT.json 曲子id=GP文件 [曲子id=GP文件 ...]');
  process.exit(1);
}
const data = process.env.TAB_TRAINER_DATA || new URL('../data', import.meta.url).pathname;
const files = {};
for (const p of pairs) {
  const i = p.indexOf('=');
  (files[p.slice(0, i)] ||= []).push(p.slice(i + 1));
}

function drawn(staff) {
  const nStr = staff.tuning.length;
  return staff.bars.map(bar => {
    const items = [];
    for (const v of bar.voices) for (const b of v.beats) for (const n of b.notes) {
      if (n.isDead) continue;
      const s = nStr + 1 - n.string; // 我们 1 = 最高音弦
      const t = b.displayStart;
      if (n.isTieDestination) {
        if (b.index === 0) items.push({ s, t, f: n.tieOrigin ? n.tieOrigin.fret : n.fret, kind: 'tie', paren: true, angle: false });
        continue;
      }
      const f = n.harmonicType === 1 && n.harmonicValue > 0 ? Math.round(n.harmonicValue) : n.fret;
      items.push({ s, t, f, kind: b.graceType ? 'grace' : 'note', paren: !!n.isGhost, angle: n.harmonicType === 1 && !n.isGhost });
      if (n.harmonicType >= 2 && n.harmonicType <= 6 && n.harmonicValue && Number.isInteger(n.fret + n.harmonicValue)) {
        items.push({ s, t, f: n.fret + n.harmonicValue, kind: 'hmark', paren: false, angle: true });
      }
    }
    return items;
  });
}

const key = (s, f) => `${s}:${f}`;
const result = {};
for (const [sid, list] of Object.entries(files)) {
  const song = JSON.parse(readFileSync(`${data}/songs/${sid}/song.json`, 'utf8'));
  let best = null;
  for (const file of list) {
    const score = at.importer.ScoreLoader.loadScoreFromBytes(new Uint8Array(readFileSync(file)), new at.Settings());
    for (const tr of score.tracks) {
      const staff = tr.staves[0];
      if (staff.bars.length !== song.bars.length || staff.tuning.length !== 6) continue;
      const bars = drawn(staff);
      let same = 0;
      song.bars.forEach((tb, i) => {
        const a = tb.events.flatMap(e => e.notes.filter(n => !n.tech.includes('x')).map(n => key(n.string, n.fret))).sort().join();
        const b = bars[i].filter(x => x.kind === 'note').map(x => key(x.s, x.f)).sort().join();
        if (a === b) same++;
      });
      if (!best || same > best.same) best = { file, track: tr.index, same, bars };
    }
  }
  if (!best) { console.log(`${sid}: 没有小节数对得上的 6 弦音轨`); continue; }
  console.log(`${sid}: ${best.file} 音轨 ${best.track}，普通音逐小节一致 ${best.same}/${song.bars.length}`);
  result[sid] = best;
}
writeFileSync(out, JSON.stringify(result));
