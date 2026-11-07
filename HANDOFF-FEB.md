# FE-B 交接（练习页重构，分支 refactor/feb，worktree /tmp/wt-feb）

契约：/tmp/tt-refactor/PLAN.md。任务清单是协调者给 FE-B 的 1–11 项。

## 当前状态（wip，页面暂时是坏的）
Phase 1 进行中，正在把 js/trainer/ 改成 ES 模块。

已写好（新文件，ES 模块）：
- js/trainer/store.js：createStore / get / set / on，结构比较去重，子路径订阅；slices song / view / play / ui。
- js/trainer/view.js：纯函数 deriveView(song)、sectionsOf、stepSection、practiceText、resumeInfo、barTags（技巧名取 vocab.json）、
  planRange、songSubtitle、explainState、needsReview、warnText、showNote / tuningInput（F♯ 显示）。导入 ../score.js 和 ../vocab.json（JSON 模块）。
- js/trainer/dom.js：$ / esc / SONG_ID / setButton / request（统一错误）/ withBusy（try/finally）。UI 暂取 window.UI（ui.js 还是普通脚本，FE-A 改模块后换成 import）。
- js/trainer/notices.js：通知区（notice(id, spec)，可逐条关闭）。Phase 2 换 UI.notice。
- js/trainer/layer.js：浮层栈（Esc / 点外面 / 焦点）。Phase 2 换 UI.layer。
- js/score.js：加了 gbkFromMojibake / scoreNeedsGbk（GBK 乱码检测），DERIVE_VERSION 4 → 5。

已删除（内容要搬进新模块）：core.js、bar-info.js、synth.js。
还没改（仍是旧的全局变量普通脚本）：player.js、fretboard.js、tuner.js、coach.js、practice-log.js、editor.js、song.js、main.js、trainer.html、trainer.css。
旧代码在 git 历史里：`git show bec7af3:app/static/js/trainer/<文件>`。

## 设计（已定）
- set('ui.bar', i) 代替 showBar；小节卡片、原谱截图、问 AI、练习记录、编辑器、指板各自 on('ui.bar' / 'view' / 'song.xxx')。
- play 切片：ready / playing / bpm / a / b / loops / note / next（指板）。practice-log 订阅 play.playing / play.loops / play.a,b。
- 谱面来源 view.source（{kind:'score', track} 或 {kind:'tex', tex}）放进 store，player 订阅它加载 —— 代替 scoreKey。
- GBK：scoreLoaded 时 scoreNeedsGbk(score) → settings.importer.encoding = 'gbk' 重新 load；make_eval_song.py 的 node 脚本同样处理并把 encoding 传给渲染页。
- window.tt = { get state() 即 play 切片, store, setLoop, play, stop, setTempo, applyAudio, onBeatClick, setEditMode, openNotePop, cursorOutOfView, api, song, editor 状态, ac }，e2e 改用它。
- 新布局（PLAN.md“练习页新布局”）：顶栏曲名 + 副标题；主栏吸顶一行控制条 + “更多”弹层 + 横向 chips + 谱面；右侧 360px 吸顶小节卡；指板底部停靠可折叠；<1200px 侧栏变抽屉。

## 下一步
1. 写 audio.js（原 synth.js，音高用 view）、fretboard.js、bar-card.js（原 bar-info）、orig.js、player.js、coach.js、practice-log.js、editor.js、song.js、main.js、tuner.js 为模块。
2. 新 trainer.html / trainer.css（hidden 属性、无内联样式）。
3. tests/js/trainer.test.mjs（deriveView、sectionsOf、stepSection、practiceText），更新 tests/e2e/test_trainer.py 和 conftest.py 用 window.tt。
4. scripts/make_eval_song.py GBK。
5. Bug：explainBtn / reparse / cancel / metaSave 用 withBusy；轮询出错后继续（退避）；导出 GP 图标 download；状态提示统一“空格播放 · ←→ 换段 · ↑↓ 调速”。
6. Phase 2（等协调者通知 FE-A / BE 就绪后 merge）：见任务 8–11。

## 测试状态
未跑。页面目前不能用（trainer.html 还引用已删除的脚本）。
