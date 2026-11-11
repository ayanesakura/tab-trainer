#!/bin/zsh
# 提交前的完整检查：lint、格式、Python 单元测试、JS 数据转换测试。加 --e2e 再跑浏览器端到端测试（需要 Chrome）。
set -e
cd "${0:A:h}/.."
uv run ruff check app tests scripts exp
uv run ruff format --check app tests scripts exp
uv run pytest -q
node --test tests/js/*.test.mjs
if [[ "$1" == "--e2e" ]]; then
  uv run pytest -q -m e2e
fi
