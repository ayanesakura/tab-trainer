# FE-B 交接（练习页重构，分支 refactor/feb，worktree /tmp/wt-feb）

契约：/tmp/tt-refactor/PLAN.md。已合并 refactor/fea（FE-A 基础，83cb6b8）。refactor/be 还没合并（等协调者通知）。

## 已完成
1. ES 模块：trainer.html 只加载 theme-boot.js（普通脚本，FE-A 要求）+ `js/trainer/main.js`（module）。alphaTab 在 player.js 里直接 import，模块图加载完才执行 main.js，所以第一次加载曲子时 alphaTab 已就绪。
2. store.js（set/get/on，结构比较去重、子路径订阅、批量 set）；view.js 纯函数 deriveView 等。ui.bar 代替 showBar；view.source 代替 scoreKey；play.note/next 代替 fbKey；song 子路径订阅代替 renderedKey。
3. tests/js/trainer.test.mjs：store、deriveView、stepSection、practiceText、resumeInfo、barTags、planRange、songSubtitle、GBK。
4. window.tt；e2e 全部改用它，新增布局 / 更多弹层 / 指板开关 / resume=1 测试。
5. 只用 hidden；没有内联 style（只剩定位用的 left/top/width/height）。
6. Bug：讲解 / 重新识别 / 取消 / 保存曲子信息都用 api.js 的 withBusy（try/finally、防重复提交）；轮询用 api.poll（出错退避重试）；导出 GP 用 download 图标；状态提示统一“空格播放 · ←→ 换段 · ↑↓ 调速”。
7. GBK：score.js 的 gbkFromMojibake / scoreNeedsGbk；player.js scoreLoaded 时检测到就 importer.encoding='gbk' 重新 load；make_eval_song.py 同样处理并把 encoding 传给渲染页；DERIVE_VERSION 4→5。
8. FE-A 基础：api.js、UI.layer（改音面板、原谱 dialog、更多弹层）、UI.confirm、UI.notice（notices.js 包了一层做排序）、UI.tabs、vocab.js（技巧名、STANDARD、DIRECTIONS、parseDur、STRINGS）、设计变量、aria-pressed、UI.initShell。
10. 新布局（见 PLAN）；截图在 /tmp/feb-shots/final-*.png。
11. 无障碍 / 文案：品位 ± aria-label、自动提速三个输入框有 label、原图→原谱、F♯ 显示（输入框也显示 ♯，保存时转回 #）。

## 第 9 项已完成（已合并 refactor/be 19ef7a9）
兜底已删：用 isScore(source_type)、busy、recognizing、activity、notice、locked、needs_review、can_edit_bars、editable_meta、rev；写接口返回的 SongView 直接 applySong。

## （旧记录）等 BE
代码已按契约字段写（busy / recognizing / activity / can_edit_bars / editable_meta / notice / locked / needs_review / warn{code,msg} / rev + 409），
都带了旧接口的兜底（`s.busy ?? ...` 等）。合并 refactor/be 后：删掉这些兜底（song.js isBusy / recognizing / renderNotices 的 error 兜底，editor.js barLocked 的旧逻辑和 busy，view.js needsReview 的兜底），跑 e2e。

## 测试
scripts/check.sh --e2e 全部通过（合并 BE 前）。
