# BE 重构交接（refactor/be）—— 最终状态

## 状态
- 全部条目完成，`scripts/check.sh` 通过（ruff、pytest 约 190 个、node 测试）；`uv run pytest -m e2e` 14 个全过（views 里保留了一版兼容字段 kind / job / error / running / warn_bars / last_practiced，协调者会在集成时删掉，没有前端以外的使用者）。
- 已通知 main：API 契约就绪（19ef7a9）、乱码修复覆盖所有来源、兼容字段说明。
- 不动的东西：recognize / llm / ocr / layout / evaluate 只做了外科式修改；提示词和 schema 逐字不变（tests/test_model.py 对比 6957d49 导出的 tests/fixtures/llm_schemas.json）。

## 各条目落点
- Bug 1 duration_ok：evaluate.duration_ok 按警告 code（length / placeholder），rescore 重算并兼容旧报告里的字符串警告（music.warn_code_of）。
- Bug 2：songs.edit_bar 对 GP / MusicXML 抛 Invalid（400），state.bar_locked 给出原因。
- Bug 3：migrate._to_v2 对所有来源的 title / meta.title / artist / arranger 做 fix_mojibake（严格判定）。
- A model.py：Song / Bar / Event / Note… 单一定义；llm.NOTE_SCHEMA / EVENT_SCHEMA 由 model.llm_schema 生成；BarIn.events 用 model.Event；bundles 导入用 model.Song 校验。
- B state.py：status + activity{kind, restaging, systems} + notice{kind, msg, source}；busy / recognizing / can_edit_bars / editable_meta / bar_locked / recover 都在这里。整首重新识别时 status 不变。
- C：派生字段（n、按内容算的警告、meta.title、meta.tuning_midi、plan.stale）在 store.load 时 model.derive 补上、store.save 时 model.strip 去掉；曲名只存 song.title；kind 不再存。
- D staleness.reconcile_bars(s, old, new, policy, setting_changed)：DERIVE（set_derived）、RESTAGE（整首重新识别，保留手改）、ROW（recognize._replace_system_bars：单行重识别、补页、两遍携带拍号）。bar.edited 显式；讲解不再去掉 _orig。
- E：警告 {code, msg}；recognize 的 issues 是 (code, text)，整行统一检查按 code 过滤，发给模型的文字不变。
- F：store（NotFound、锁内改名删除、update 不重建目录、rev、practice.jsonl 追加）、config.py（Settings + 模型设置）、llm.friendly / MissingKey(400) / LLMError(502)、jobs 只剩登记、service.py 编排、main 无 HTTPException、bundles 只用公开函数、校验只在 schemas、上传只把解码错误转 400（songs._decoding）。
- G views.py：SongView / LibraryItem（契约字段 + practice.last.a_n/b_n）、去掉 _ 键、PUT bar/meta 带 rev → 409、GET /api/songs/{sid}/practice/history。
- H practice.py：jsonl 追加、按 mtime/size 缓存、按当前 id 聚合；旧 song.practice 迁移成种子行（store.write_song 写入）；曲子包导出 / 导入带 practice.jsonl。
- I：SCHEMA_VERSION=2，旧格式全部在 migrate.py；启动时 songs.recover_interrupted 把升级过的曲子写回。
- J：music 常量来自 vocab.json；tests/test_vocab.py。vocab.json 只加了字段：instrument.max_fret_written、directions[].prompt_order、warn_codes.empty。
- K：llm.LLM Protocol，Claude / FakeClaude 显式继承；config.llm_factory 供测试替换。
- L：model.add_usage 唯一入口。
- M：删除墓碑测试（test_store）；JS→Python 金标准（tests/fixtures/golden.gp + golden_derived.json，tests/js/fixture.test.mjs + tests/test_golden.py）；补测：设置接口、GP 改小节拒绝、警告 code、practice.jsonl、rev 冲突、v2 迁移。

## 没做 / 留给以后
- recognize.tab_lines 对没存 lines 的旧曲子重新检测整页：需要读图片，不适合放进纯函数的 migrate，且 recognize 不能重组，保留原样。
- views.cost_summary 对只记了 token 的早期曲子按默认模型估价：是显示逻辑，没搬进迁移（迁移成金额会丢掉“估算”标记）。
- 兼容字段（kind / job / error / running / warn_bars / last_practiced）由协调者在集成时删除。
