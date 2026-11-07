# tab-trainer 系统性重构 —— 共享契约（所有 agent 必读）

集成分支：`refactor/systematic`（/tmp/wt-refactor），基于 main 6957d49。
各 agent 在自己的 worktree / 分支上工作，**只改自己负责的文件**，完成后由协调者（tab-trainer-21）合并。
**绝对不要**改 /Users/ayane/Documents/tab-trainer（主 checkout，用户的服务和别的会话在用），不要 push，不要动 main 分支。

## 约束（来自其他会话，必须保持）
- 不重组 app/recognize.py、app/llm.py、app/ocr.py、app/layout.py、app/evaluate.py：只做外科式小改动（另一个会话在改识别准确率）。llm.py 的提示词文本和 schema 结构不变。
- 保持且测试继续覆盖：逐小节 time_signature / ts_change / apply_time_signatures；run_parse 的分阶段重识别（restaging）和两遍携带拍号（start_ts）；编辑锁 songs._bar_locked（可改名/搬家，语义不变）；_carry_over 的 difflib 对齐；score.js 的 DERIVE_VERSION（改 deriveSong 必须 +1）；store 的 .bak / CorruptSong / flock；Host/Origin 检查；练习页第一次 refresh 要等 alphaTab 就绪。
- 无前端构建步骤。

## 共享词表 `app/static/js/vocab.json`（已建好）
弦数/标准调弦/品数上限、tick 网格、技巧（code/sym/name）、反复记号（顺序 = alphaTab Direction 枚举；prompt 标记 = llm 提示词里允许的子集）、来源标签、警告代码。
- 后端：music.py 读它得到 TECHS / STANDARD / DIRECTIONS / GRID / INSTRUMENT，llm.py 的 DIRECTIONS = prompt 为 true 的子集。
- 前端：`js/vocab.js` 用 `import vocab from './vocab.json' with { type: 'json' }` 导出常量（Node 24 / Chrome 123+ 都支持）。
- 可以加字段，不要改已有字段的含义；加了字段在合并说明里写清楚。

## API 契约（后端提供，前端只消费，不再自己推断）

### `GET /api/songs/{sid}` —— SongView
现有的公开字段保留（id, title, meta, bars, systems, pages, plan, practice, usage, source_type, status, ...），另外：
- 去掉所有以 `_` 开头的内部字段（含小节上的 `_orig`、`_ocr`）。
- `rev: int` —— 每次保存 +1。
- `busy: bool` —— 这首曲子有任何后台任务（整首识别 / 单行重识别 / AI 讲解）。
- `recognizing: bool` —— 整首识别或单行重识别在进行（讲解不算）。
- `activity: null | {kind: "parse"|"reparse_row"|"explain", restaging: bool, systems: [int]}`。
- `can_edit_bars: bool` —— GP / MusicXML 曲子为 false（后端也拒绝改它们的小节，400）。
- `editable_meta: [str]` —— 允许改的谱头字段（GP/MusicXML 只有 "title"）。
- `notice: null | {kind: "warn"|"error", msg: str}` —— 替代原来 status=ready 但 error 非空的“警告”用法。前端只显示它。
- 每个小节：`warn: [{code, msg}]`（code 取自 vocab.warn_codes），`needs_review: bool`（有 warn 或 uncertain），`locked: str|null`（不能改的原因），`edited: bool`（用户改过）。
- `practice` 保持现在的形状：`{bars: {<bar_id>: {loops, seconds, best_bpm, last}}, last: {a, b, bpm, at}, total_seconds}`（数据来源改成 practice.jsonl 聚合，见下）。

### `GET /api/songs` —— 曲库列表项
现有字段保留，另外：`busy`、`recognizing`、`needs_review_bars: int`（warn 或 uncertain 的小节数，替代 warn_bars；warn_bars 可以保留一版做兼容）、`practice: null | {last_at, total_seconds, last: {a, b, bpm}}`、`notice`。

### 写接口
- 改小节 / 改谱头的 PUT 请求体可带可选 `rev`；和当前不一致 → 409，detail = "曲子在别处改过了，已刷新，请再改一次"。不带 rev 照旧。
- 所有错误响应形状统一 `{"detail": "<中文说明>"}`。
- 新增 `GET /api/songs/{sid}/practice/history` → `{sessions: [{at, a, b, bpm, seconds, loops}]}`（按时间，为以后练习统计用）。

## 后端内部方向（BE agent 自由实现，但要达到）
- `app/model.py`：Pydantic（或 dataclass）定义 Song/Bar/Event/Note/System/Plan/QA/Practice；`schemas.py` 的请求模型和 `llm.py` 的 NOTE_SCHEMA 尽量从同一套音符定义派生（llm 的 schema 结构不能变，可以只做“生成出来和现在逐字等价”的断言测试）。
- 状态：持久化 `status` + `activity` + `notice`，去掉 job/job_prev/restaging/error_kind 的散落用法；所有“忙/可改/锁定”由一处函数算出。
- 统一的 `reconcile_bars(old, new, policy)`：set_derived、整首重识别替换、单行重识别都用它（按内容 + 位置匹配，迁移 id / tip / qa；练习记录按 id 自然保留）。显式 `bar.edited` 取代“有没有 _orig”来判断改过；_orig 只做撤销基线。
- 结构化警告 `{code, msg}`；evaluate 的 duration_ok 改为按 code（只看 length / placeholder）。
- 分层：store 只做 JSON 读写 + 锁 + 路径 + `store.delete(sid)`（锁内删除，update 遇到目录不存在直接抛 NotFound 不重建）；`store.load` 找不到抛 NotFound，main 里不再预检；曲库摘要和兼容推断搬出 store；`config.py` 管设置读写 / 打码 / llm 配置；`friendly()` 搬到 llm.py；jobs 只做任务登记；bundles 只用公开函数；main 里不再出现 HTTPException 和业务编排；CorruptSong / LLMError 并入 AppError 体系；长度/范围校验只在 schemas 一处（AskIn 与 coach 的 500/1000 冲突取 500）。
- 练习记录：`data/songs/<id>/practice.jsonl` 追加写，不再每次重写 song.json；聚合结果内存缓存（按文件 mtime）；旧 song.practice 在迁移时转成一条聚合种子记录；曲子包导出 / 导入包含它；按 id 聚合，未知 id 忽略（取代 prune_practice 的物理删除）。
- 迁移：SCHEMA_VERSION=2，旧数据兼容（ensure_bar_ids、job_prev、source_type 推断、kind 默认、tab_lines 等）全部收进 `_migrate`，业务代码删掉旧格式分支；顺带修 GP 来源 title/artist 的 GBK→Latin-1 乱码（`s.encode('latin1').decode('gbk')` 成功且得到中日韩字符时替换）。
- 乐器：INSTRUMENT（弦数等）来自 vocab，代码里不再写死 6（llm schema 的 enum 1–6 从它生成，值不变）。
- LLM 替身：定义 `LLM` Protocol，FakeClaude 显式实现；配置（数据目录、并发数、上限）集中到 config/Settings。
- 用量统一 add_usage。

## 前端方向
- 共用基础（FE-A）：`js/lib/api.js`（get/post/put/del，统一 Error(detail)，`withBusy(btn, fn)`，`poll(fn, {interval, while})` 出错退避重试）、`js/vocab.js`、ui.js 里 `UI.layer`（浮层栈：Esc / 点外面 / 焦点移入与还回）、`UI.confirm`（替代原生 confirm）、`UI.notice`、`UI.initShell`、`UI.seg` 加 aria-pressed；ui.css 设计变量（字号 4 档、圆角 3 档、间距 4 档、`--paper-bg`、`--shadow-sm`、指板颜色变量），通用组件（.card-h、.chips、.legend、.grow、按下态 `.btn[aria-pressed=true]`）收进 ui.css；主题：`data-theme` 总是写实际生效值 + matchMedia 监听，CSS 只留一个暗色选择器。ui.js / lib 写成 ES 模块（index 和 trainer 都用 `type="module"`）。
- 练习页（FE-B）：ES 模块 + 小 store（`set/get/on`）+ `deriveView(song)` 纯函数；`window.tt` 调试句柄给 e2e；所有显示/隐藏用 `hidden`；重新布局（见下）。
- 曲库（FE-A）：列表显示练习信息（“3 天前 · 练到 13–15 小节 · 累计 25 分钟”）、“接着练”入口、按最近练习排序选项；去掉无信息量的彩色首字母头像（换成来源图标或去掉）；键盘可上传。
- 文案用词表：曲子 = 曲库里的条目；谱面 = 渲染出来的谱；原谱 = 上传的图片。技巧名统一取 vocab。

### 练习页新布局（FE-B，协调者已定）
- 顶栏：返回、曲名 + 来源徽标、视图切换（练习 / 调弦 / 练习建议 / 记号说明）、主题。曲子信息（艺人、调弦、变调夹、拍号）放到曲名下的副标题。
- ≥1200px 两栏：
  - 左主栏：吸顶的紧凑控制条（一行：播放、状态、速度、循环范围 ‹ › 、只练这一小节、“更多”）；“更多”弹层里放节拍器、预备拍、示范音量、音色、自动提速（带完整标签：“每循环 [2] 遍提速 [4] BPM，最高 [72] BPM”）；控制条下一行是可横向滚动的分段 chips；然后是谱面——**谱面要在第一屏里**。
  - 右侧栏（约 360px，吸顶、独立滚动）：当前小节卡片（编号、技巧标签、提示、原谱截图、问 AI、练习记录、“识别有误？”工具）。
  - 通知（AI 备注、接着练、建议过时、识别进度 / 错误）合并成主栏顶部一条可逐条关闭的通知区；“还没有练习建议”不再占练习页，放到练习建议页。
- 指板：底部停靠条，可折叠（控制条上一个开关，记住状态），扁平风格、颜色走 CSS 变量，跟主题。
- <1200px：侧栏落到谱面下方或做成抽屉。
- 暗色下原图弹窗不能是大白块，`#cropMsg` 等硬编码颜色改变量。

## 警告结构化的具体落点（来自 81 的提醒）
- music.LENGTH_WARN + refresh_length_warn（只重算时值那条、保留 OCR 那条）→ 按 code。
- recognize.transcribe_system 里按 LENGTH_WARN 过滤 issues 的“整行统一”检查 → 按 code。
- evaluate 的 duration_ok → 按 code；app.evaluate.rescore 读旧 report.json 时 pred_warn 可能还是字符串列表，要兼容两种。
- 旧 song.json 里 warn 是字符串列表 → _migrate 里按文字前缀映射到 code（映射不到的 → "other"），SCHEMA_VERSION +1。

## 契约补充（协调中追加）
- 列表项和 SongView 的 practice.last 增加 `a_n`、`b_n`：last.a / last.b（小节 id）当前的 1 起序号，id 不存在为 null，保证 a_n <= b_n。
- 曲库“接着练”链接：`/trainer?id=<sid>&resume=1`。
