@LESSONS.md

# 给写代码的 agent

- 代码结构、接口约定、怎么跑：`README.md` 的“代码结构”和“开发”。
- 改代码之前读 `LESSONS.md`：以前做错过什么，和本来能拦住它的规则。修复修的是以前合进去的东西时，给它加一条。
- `data/` 是用户真的曲库，不进 git，没有自动备份。批量动它之前先拷一份；脚本删目录只走 `scripts/evalsongs.py`。
- 没让提交、推送，就不提交、不推送。

## 检查

- `scripts/check.sh`：lint、格式、Python 和 JS 测试。碰了 `app/static` 或页面行为，加 `--e2e`。
- 跑整套，不只跑自己碰的模块：守卫测试（`tests/test_guards.py`、`tests/test_exp.py`）查的是别处的约定。
- 修复带一个“去掉修复就失败”的测试。把旧代码放回去跑一次，确认它真的红，再还原。
- 改识别、版面检测、提示词：单元测试不够，在评测曲或本地曲库上跑前后对比（README“识别准确率评测”）。

## 提交

标题照旧：`fix(模块): 用户现在看到的行为`，英文。正文先写为什么和改了什么，然后固定三段，哪段没有就写 none，不省略：

```
Run: scripts/check.sh --e2e at this tree: 611 passed; node 14 files ok.
  Barline detection re-run over the local library: 3 of 1022 rows change.
Red by hand: with the prefix check removed, test_a_target_outside_... fails
  ("DID NOT RAISE SystemExit").
Not tried: a real WeChat screenshot; rows 3 and 5 not compared with the score.
```

- **Run**：跑了什么、在哪个树上、打印了什么（数字，不是“全过”）。
- **Red by hand**：把修复拿掉或把它守着的代码弄坏之后，哪个测试红了、红的是什么。
- **Not tried**：没验证的东西。没跑的检查写在这里，不能不提；写在这里的东西不当成已经可用告诉用户。
