#!/usr/bin/env bash
# 查看 m3u8 API 服务状态
#
# 退出码:
#   0  正在运行
#   1  pid 文件存在但进程不存在 (残留)
#   3  未运行 (无 pid 文件)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

PID_FILE="$PROJECT_DIR/run/server.pid"
LOG_FILE="$PROJECT_DIR/run/server.log"

ENV_FILE="$PROJECT_DIR/.env"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_common.sh"

if [[ ! -f "$PID_FILE" ]]; then
    echo "状态: 未运行 (无 pid 文件)"
    exit 3
fi

PID="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -z "$PID" ]]; then
    echo "状态: 未运行 (pid 文件为空)"
    exit 3
fi

if ! kill -0 "$PID" 2>/dev/null; then
    echo "状态: 未运行 (残留 pid=$PID, 进程已不存在)"
    exit 1
fi

# 进程信息
PS_LINE="$(ps -o pid,etime,rss,command -p "$PID" 2>/dev/null || true)"
echo "状态: 运行中"
echo "  pid    : $PID"
echo "  cmd    : $(ps -o command= -p "$PID" 2>/dev/null || echo '?')"
echo "  详情   : $PS_LINE"
echo "  日志   : $LOG_FILE"

# 端口探测 (PORT 解析与 start.sh 同一套: 环境变量 > .env > 默认 8000; 真正的监听地址由 api_server.py 决定)
PORT="${PORT:-$(env_get PORT)}"
PORT="${PORT:-8000}"
if port_in_use "$PORT"; then
    echo "  端口 $PORT: LISTEN"
    if command -v lsof >/dev/null 2>&1; then
        lsof -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | tail -n +2 | sed 's/^/    /' || true
    fi
else
    echo "  端口 $PORT: 未监听 (可能仍在启动中, 或 shell 侧 PORT 与实际配置不一致)"
fi

exit 0
