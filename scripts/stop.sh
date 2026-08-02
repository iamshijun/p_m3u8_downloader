#!/usr/bin/env bash
# 停止 m3u8 API 服务 (优雅停止, 超时强杀)
#
# 用法:
#   ./scripts/stop.sh                   # 等待最多 15s
#   ./scripts/stop.sh --force           # 立即 SIGKILL
#   ./scripts/stop.sh --timeout=30      # 自定义优雅停止超时秒数
#
# 退出码:
#   0  正常停止 (或不需停止)
#   1  启动但未停止
#   2  参数错误
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

PID_FILE="$PROJECT_DIR/run/server.pid"

FORCE=0
TIMEOUT=15
for arg in "$@"; do
    case "$arg" in
        --force|-f) FORCE=1 ;;
        --timeout=*) TIMEOUT="${arg#--timeout=}" ;;
        --help|-h)
            sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "未知参数: $arg" >&2; exit 2 ;;
    esac
done

if ! [[ "$TIMEOUT" =~ ^[0-9]+$ ]] || [[ "$TIMEOUT" -lt 1 ]]; then
    echo "--timeout 必须是正整数" >&2
    exit 2
fi

if [[ ! -f "$PID_FILE" ]]; then
    echo "服务未运行 (无 pid 文件: $PID_FILE)"
    exit 0
fi

PID="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -z "$PID" ]] || ! kill -0 "$PID" 2>/dev/null; then
    echo "pid $PID 已不存在, 清理 pid 文件"
    rm -f "$PID_FILE"
    exit 0
fi

# 找出 uvicorn 子进程 (api_server 用 uvicorn.run 启动) 一并 kill
# 避免主进程退出后端口仍被 worker 占用
PIDS_TO_KILL="$PID"
CHILD_PIDS="$(pgrep -P "$PID" 2>/dev/null | tr '\n' ' ' || true)"
if [[ -n "$CHILD_PIDS" ]]; then
    PIDS_TO_KILL="$PIDS_TO_KILL $CHILD_PIDS"
fi

echo "发送 SIGTERM 到: $PIDS_TO_KILL"
kill -TERM $PIDS_TO_KILL 2>/dev/null || true

if [[ "$FORCE" -eq 1 ]]; then
    sleep 1
else
    # 等待优雅退出
    WAITED=0
    while [[ "$WAITED" -lt "$TIMEOUT" ]]; do
        if ! kill -0 "$PID" 2>/dev/null; then
            break
        fi
        sleep 1
        WAITED=$((WAITED + 1))
    done

    if kill -0 "$PID" 2>/dev/null; then
        echo "等待 ${TIMEOUT}s 未退出, 发送 SIGKILL"
        kill -9 $PIDS_TO_KILL 2>/dev/null || true
        sleep 1
    fi
fi

if kill -0 "$PID" 2>/dev/null; then
    echo "停止失败, pid=$PID 仍在运行" >&2
    exit 1
fi

rm -f "$PID_FILE"
echo "服务已停止 (pid=$PID)"
exit 0
