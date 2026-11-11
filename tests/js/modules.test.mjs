// 练习页模块之间的规矩（README“练习页从 js/trainer/main.js 进入…”）：song.js、player.js 是可以直接调用的服务模块，
// 其余功能模块之间只通过 store（和 hooks.js 的钩子）通信，不互相 import。这里扫一遍 js/trainer/*.js 的 import 检查。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const DIR = fileURLToPath(new URL('../../app/static/js/trainer/', import.meta.url));

// 谁都可以 import 的：值是原因
const ALLOWED = {
  // 服务模块
  'song.js': '服务模块：取曲子、轮询、写接口出错的统一处理',
  'player.js': '服务模块：谱面和播放',
  'gp-export.js': 'player.js 拆出去的导出 GP（控制条的按钮直接调用；不订阅状态）',
  // 基础设施
  'store.js': 'store：模块之间就靠它通信',
  'hooks.js': '钩子：服务模块和功能模块之间、功能模块之间的命令（provide / invoke）',
  'dom.js': 'DOM 小工具',
  'keys.js': '唯一的键盘监听（各模块登记自己的快捷键）',
  'page-images.js': '原谱页面图片的共用缓存',
  'at-adapter.js': 'alphaTab 内部结构的适配层（只碰 alphaTab，不订阅状态）',
  'notices.js': '通知区的绘制工具（song.js 用）',
  'audio.js': '示范音的 WebAudio 合成：调弦页、记号说明、敲节奏共用（不订阅状态）',
  // 纯函数（Node 测试直接用）
  'view.js': '纯函数', 'prefs.js': '纯函数', 'play-machine.js': '纯函数', 'practice-pass.js': '纯函数',
  'fresh.js': '纯函数', 'chords.js': '纯函数', 'click.js': '纯函数',
};
// 入口：按顺序初始化各部分，什么都可以 import
const ENTRY = 'main.js';
// 个别例外：{文件: {被 import 的文件: 原因}}（现在没有）
const EXCEPTIONS = {};

const importsOf = src => [...src.matchAll(/(?:\bfrom\s*|\bimport\s*\(\s*|^\s*import\s+)['"]\.\/([\w.-]+\.js)['"]/gm)].map(m => m[1]);

test('练习页的功能模块之间不互相 import（只通过 store / hooks 通信）', () => {
  const files = readdirSync(DIR).filter(f => f.endsWith('.js'));
  assert.ok(files.includes('coach.js') && files.includes(ENTRY));
  for (const name of Object.keys(ALLOWED)) assert.ok(files.includes(name), `白名单里的 ${name} 不存在了`);
  const bad = [];
  for (const f of files) {
    if (f === ENTRY) continue;
    for (const dep of importsOf(readFileSync(DIR + f, 'utf8'))) {
      if (!ALLOWED[dep] && !(EXCEPTIONS[f] || {})[dep]) bad.push(`${f} → ${dep}`);
    }
  }
  assert.deepEqual(bad, [], `功能模块之间直接 import 了：\n${bad.join('\n')}\n改成通过 store 或 hooks.js 的钩子`);
});

test('import 扫描能认出各种写法', () => {
  const src = `import { a } from './a.js';\nimport * as b from "./b.js";\nimport './c.js';\nconst d = await import('./d.js');\nexport { e } from './e.js';\nimport x from '../lib/x.js';`;
  assert.deepEqual(importsOf(src), ['a.js', 'b.js', 'c.js', 'd.js', 'e.js']);
});
