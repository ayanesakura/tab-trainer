# BE 重构交接（refactor/be）

## 状态
- 已完成：读完 PLAN.md / README / 全部 app/*.py 和 tests，定好设计（见下）。
- 已提交：vocab.json 加字段（只加不改）：`instrument.max_fret_written: 30`（数据里接受的最高品，泛音节点等；`max_fret: 24` 是琴颈）、
  prompt 为 true 的反复记号加 `prompt_order`（llm.py 的 DIRECTIONS 按它排序，保证 BAR_SCHEMA 的 enum 顺序和现在逐字一致）、
  `warn_codes.empty`（"没有任何音符"）。
- 还没开始写代码。测试状态 = main 原样（全绿）。
- 协调者追加的契约：曲库列表项和 SongView 的 practice.last 带 `a_n` / `b_n`（当前 1 起的小节位置，id 不存在为 null，保证 a_n <= b_n，必要时 a/b 一起交换）。

## 设计（按条目）
- J：music.py 读 vocab.json → INSTRUMENT / STRINGS / MAX_FRET(=max_fret_written) / TECHS / STANDARD / DIRECTIONS / PROMPT_DIRECTIONS(按 prompt_order) / GRID(=grid_ticks/ticks_per_quarter=1/48) / WARN_CODES；代码里的 6 / 30 都换成常量。llm.py 的 DIRECTIONS = PROMPT_DIRECTIONS，NOTE 的 string enum 从 STRINGS 生成。tests/test_vocab.py。
- E：check_bar 返回 `[{code, msg}]`，msg 文字不变（返修时发给模型的 issues 文字也必须不变）。派生警告 code = length / off_grid / string_range / empty / placeholder（placeholder 由 bar.placeholder 派生）；持久化的只有 ocr_mismatch / other。
  recognize.transcribe_system 的 issues 改成 (code, text) 元组，整行统一检查按 code=="length" 过滤，problems 传文字。
  evaluate：`duration_ok = not ({length, placeholder} & codes)`，rescore 重算 duration_ok，兼容 pred_warn 是字符串（music.warn_code_of 按文字前缀映射）。
- C/I：新模块 app/migrate.py（SCHEMA_VERSION=2）：kind→source_type（删 kind）、job/job_prev/restaging/error/error_kind/running → status+activity+notice、
  warn 字符串→{code,msg}、`_orig`→edited=True、补小节 id、song.title 与 meta.title 合并成 song.title、GP 来源的 title/artist/arranger 乱码修复
  （latin1→gbk 成功、ASCII 字符数不变、结果含中日文才替换）、旧 song.practice → `_practice_seed`（save 时追加成 practice.jsonl 的种子行，已有种子则跳过）。
  派生字段“读时算、写时去”：model.derive(song)（n、派生警告、meta.tuning_midi、meta.title=song.title、plan.stale）在 store.load / update 返回前做；
  model.strip(song)（去掉上述 + 顶层 `_` 键）在 store.save 时做。这样受约束的 recognize/llm 代码基本不用改。
- B：新模块 app/state.py：begin(s, kind, systems, restaging, progress, status) / finish(s, status, notice, source) / recognizing / bar_locked（语义同旧
  songs._bar_locked：activity.restaging 整首锁、行 status==parsing 锁该行；GP 曲子 locked=原因）/ editable_meta / can_edit_bars / flags(s, running) / recover(s)。
  activity.kind: parse | reparse_row（单行和补页的行都用它）| explain。notice = {kind: warn|error, msg, source}（视图里只给 kind/msg）。
- D：staleness.reconcile_bars(old, new, policy, setting_changed) 取代 carry_over；Policy DERIVE / RESTAGE(keep_edits) / ROW；reconcile_row(s, si, bars)
  给单行重识别、补页、两遍携带拍号用（id / qa / tip 迁移，练习记录按 id 自然保留）。bar.edited 显式；explain 写新提示时只去 _orig 不去 edited。
- F：store：NotFound（不再 KeyError）、delete(sid)（锁内改名成 .trash-* 再删）、update 时目录不在 → NotFound 不重建、rev 每次保存 +1、
  practice.jsonl 追加写 IO。store.DATA/SONGS/CONFIG 仍是模块属性（e2e conftest 会 monkeypatch 它们）。
  config.py：Settings（并发、上传/导入上限、讲解批大小、问答保留数）+ 模型设置读写/打码/llm_config/llm_factory/new_llm/check_llm_ready。
  llm.py：friendly / upstream_reason 搬入；LLMError 继承 errors.Upstream；没 key 抛 MissingKey(LLMError, Invalid) 400；LLM Protocol（FakeClaude 显式继承）。
  jobs.py 只剩登记/取消/spawn。CorruptSong 进 errors（AppError, 500）。errors.Conflict 409（rev 冲突、没有可取消的任务）。
  service.py：main 背后的编排（上传后启动识别、删除、整首重识别、取消、文件路径）。main 无 HTTPException。
  校验只在 schemas：AskIn 500 + 去空白非空、PracticeIn 范围、TagsIn 清洗与上限、MetaIn capo 0–12、BarIn.events 用 model.Event。
  上传解码失败在 songs 里转 Invalid（“文件无法读取”），其他异常不再一律 400。
- G：app/views.py：song_view / library_item / list_library，pydantic 响应模型在 model.py（SongView / LibraryItem / BarView），去掉 `_` 键；
  兼容字段保留一版：error（=notice.msg）、running（=busy）、warn_bars、last_practiced；meta 里派生 tuning_midi 和 title。kind 不再返回。
  PUT bars/meta 带 rev → 不一致 409 "曲子在别处改过了，已刷新，请再改一次"。GET /api/songs/{sid}/practice/history。
- H：app/practice.py：record（校验 id、锁内追加）、aggregate(song)（种子 + 记录，按当前 id 过滤，按文件 mtime/size 缓存解析结果）、history、summary（曲库）、coach 文本。
- M：tombstone 删除 + 测试；tests/fixtures/ 下用 alphaTab Gp7Exporter 生成 GP 文件 + deriveSong 金标准 JSON（tests/js/fixture.test.mjs 校验 JS 输出 == 金标准，
  Python 测试把金标准喂给 set_derived）。

## 下一步
按上面顺序实现，每步提交；最后 scripts/check.sh、`uv run pytest -q -m e2e`（旧前端读 kind/job/error 字符串 warn 的地方会挂，列出来不修）、更新 README 代码结构、给 main 发 "BE API contract ready at <commit>"。
