// 服务模块（player.js）和功能模块之间的钩子：播放模块不 import 曲子、改谱、敲节奏这些模块，
// 它们在 init 时用 provide 挂上自己的处理，播放模块用 invoke 调用（没人挂就什么都不做）。
// 功能模块之间要“叫对方做一件事”（不是同步状态）时也用钩子，不互相 import（tests/js/modules.test.mjs 检查）。
// 只认下面列出的名字（拼错了马上报错）；同一个钩子只能有一个提供者，重复注册时在控制台警告（以后注册的为准），
// 不会像以前 Object.assign 那样悄悄覆盖。这个文件不碰 DOM，Node 测试里也能用。
//
//   score.loaded(score) → Promise<boolean>   谱子（GP / MusicXML）加载好了：song.js 转换成小节数据；false 表示失败，播放模块不再往下走
//   score.error(msg)                         alphaTab 报错 / 曲谱文件取不到：song.js 决定是记成导入失败还是只提示
//   score.editClick(beat, note)              改音模式里点了谱面（note 是 null 表示点在拍上）：editor.js
//   drill.start() / drill.stop()             敲节奏打开时，播放键 / 空格开始和停止的是敲节奏：rhythm-drill.js
//   drill.loopChanged()                      循环范围变了（或者按 Home 回到段首）：敲节奏换成新的段、从头开始
//   practice.markClean() → boolean           这遍干净（控制条的按钮）：practice-log.js；返回标上了没有
//   ai.askForKey()                           没有 API key 时要用 AI（回车提问）：ai-key.js 说明要先填 key，可以去设置

export const HOOK_NAMES = ['score.loaded', 'score.error', 'score.editClick', 'drill.start', 'drill.stop', 'drill.loopChanged', 'practice.markClean', 'ai.askForKey'];

export function createHooks(names = HOOK_NAMES, warn = (...a) => console.warn(...a)) {
  const slots = new Map();
  const check = name => { if (!names.includes(name)) throw new Error(`未知的钩子：${name}`); };
  return {
    /** 挂上 name 的处理；返回取消的函数 */
    provide(name, fn) {
      check(name);
      if (slots.has(name)) warn(`钩子 ${name} 重复注册：后注册的覆盖了前一个`);
      slots.set(name, fn);
      return () => { if (slots.get(name) === fn) slots.delete(name); };
    },
    has(name) { check(name); return slots.has(name); },
    /** 调用 name 的处理，返回它的结果；没人挂时返回 undefined */
    invoke(name, ...args) { check(name); const fn = slots.get(name); return fn ? fn(...args) : undefined; },
  };
}

// 练习页用的那一套
export const { provide, has, invoke } = createHooks();
