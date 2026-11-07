#!/bin/zsh
# 启动 tab-trainer 并打开浏览器。在访达里双击即可（会开一个终端窗口，关掉窗口就停止服务）；
# 也可以在终端里运行 ./start.command。端口默认 8765，可用 PORT=9000 ./start.command 修改。
cd "${0:A:h}" || exit 1
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
PORT=${PORT:-8765}
URL="http://127.0.0.1:$PORT/"

if curl -s -o /dev/null --max-time 2 "$URL"; then
  echo "tab-trainer 已经在运行：$URL"
  open "$URL"
  exit 0
fi
if ! command -v uv >/dev/null; then
  echo "需要先安装 uv：https://docs.astral.sh/uv/getting-started/installation/"
  read -k1 "?按任意键关闭…"
  exit 1
fi

uv sync -q || { read -k1 "?安装依赖失败，按任意键关闭…"; exit 1; }
# 服务起来后再打开浏览器
( for _ in {1..40}; do curl -s -o /dev/null --max-time 1 "$URL" && { open "$URL"; exit 0; }; sleep 0.5; done ) &
echo "tab-trainer：$URL （关掉这个窗口或按 Ctrl+C 停止）"
exec uv run python -m app.main --port "$PORT"
