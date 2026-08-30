#!/usr/bin/env bash
# 停止由 start.sh 后台模式启动的 m3u8 API 服务 (对齐 movie_api 简化版).
#
# 用法:  ./scripts/stop.sh        (无参数)
#
# 流程: 读 run/server.pid → 先杀子进程再杀主进程 (SIGTERM) → 0.1s × 50 轮询 (5s)
#       → 超时 SIGKILL → 清理 pid 文件. 任何分支都 exit 0 (幂等, 适合 restart/CI).
#
# 注意: start.sh foreground 前台实例不写 pid 文件, 不受本脚本管理 (用 Ctrl-C 停止).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PID_FILE="$PROJECT_DIR/run/server.pid"

if [[ ! -f "$PID_FILE" ]]; then
    echo "[stop] 未找到 $PID_FILE, 服务可能未在运行"
    exit 0
fi

PID="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -z "$PID" ]] || ! kill -0 "$PID" 2>/dev/null; then
    echo "[stop] 服务未在运行 (PID: ${PID:-未知})"
    rm -f "$PID_FILE"
    exit 0
fi

echo "[stop] 停止服务 PID: $PID"

# 先停子进程再停主进程, 避免主进程退出后端口仍被子进程占用
pkill -TERM -P "$PID" 2>/dev/null || true
kill -TERM "$PID" 2>/dev/null || true

for _ in $(seq 1 50); do
    if ! kill -0 "$PID" 2>/dev/null; then
        break
    fi
    sleep 0.1
done

if kill -0 "$PID" 2>/dev/null; then
    echo "[stop] 优雅停止超时 (5s), 强制结束"
    pkill -KILL -P "$PID" 2>/dev/null || true
    kill -KILL "$PID" 2>/dev/null || true
fi

rm -f "$PID_FILE"
echo "[stop] 已停止"
exit 0
