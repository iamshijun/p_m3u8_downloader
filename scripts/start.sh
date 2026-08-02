#!/usr/bin/env bash
# 启动 m3u8 API 服务 (后台运行)
#
# 用法:
#   ./scripts/start.sh                  # 使用默认配置
#   PORT=9000 MAX_CONCURRENT=5 ./scripts/start.sh
#
# 环境变量:
#   HOST           监听地址, 默认 0.0.0.0
#   PORT           监听端口, 默认 8000
#   MAX_CONCURRENT 同时运行的下载任务数, 默认 3 (传给 MAX_CONCURRENT_DOWNLOADS)
#   PYTHON         Python 解释器, 默认 python3
#
# 文件:
#   run/server.pid   进程 ID
#   run/server.log   stdout + stderr
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

PID_FILE="$PROJECT_DIR/run/server.pid"
LOG_FILE="$PROJECT_DIR/run/server.log"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
MAX_CONCURRENT="${MAX_CONCURRENT:-3}"
PYTHON="${PYTHON:-python3}"

mkdir -p "$PROJECT_DIR/run"

# 已运行则直接退出
if [[ -f "$PID_FILE" ]]; then
    OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "服务已在运行 (pid=$OLD_PID, port=$PORT)" >&2
        exit 1
    fi
    echo "清理残留 pid 文件 (pid=$OLD_PID 已不存在)"
    rm -f "$PID_FILE"
fi

# 端口占用检测: 任意工具 (ss / lsof / netstat) 命中即直接退出
# ss: Socket Statistics 可认为是 netstat的现代替代品 
port_in_use() {
    local port="$1"
    if command -v ss >/dev/null 2>&1; then
        ss -ltn 2>/dev/null | awk '{print $4}' | grep -E "[:.]${port}$" >/dev/null
    elif command -v lsof >/dev/null 2>&1; then
        lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
    elif command -v netstat >/dev/null 2>&1; then
        netstat -ltn 2>/dev/null | awk '{print $4}' | grep -E "[:.]${port}$" >/dev/null
    else
        return 1  # 没有可用工具, 假定端口空闲
    fi
}

if port_in_use "$PORT"; then
    echo "错误: 端口 $PORT 已被占用" >&2
    if command -v ss >/dev/null 2>&1; then
        ss -ltnp 2>/dev/null | grep -E "[:.]${PORT}\\b" >&2 || true
    elif command -v lsof >/dev/null 2>&1; then
        lsof -iTCP:"$PORT" -sTCP:LISTEN >&2 || true
    fi
    echo "如需覆盖: 改 PORT 环境变量后重试, 或先释放该端口" >&2
    exit 1
fi

# 依赖检查
if ! "$PYTHON" -c "import fastapi, uvicorn" 2>/dev/null; then
    echo "缺少依赖 fastapi/uvicorn, 请先执行: pip install -r requirements.txt" >&2
    exit 1
fi

cd "$PROJECT_DIR"

# 后台启动, 日志追加写入
# 用 uvicorn CLI 而非 python3 api_server.py, 是为了让 $HOST / $PORT 真正生效
# (api_server.py 内部把 host/port 硬编码成 0.0.0.0:8000)
MAX_CONCURRENT_DOWNLOADS="$MAX_CONCURRENT" \
    nohup "$PYTHON" -m uvicorn api_server:app --host "$HOST" --port "$PORT" >> "$LOG_FILE" 2>&1 &

PID=$!
echo "$PID" > "$PID_FILE"

# 短暂等待确认进程存活并监听端口
sleep 1
if ! kill -0 "$PID" 2>/dev/null; then
    echo "启动失败, 请查看日志: $LOG_FILE" >&2
    rm -f "$PID_FILE"
    exit 1
fi

echo "服务已启动 (pid=$PID, host=$HOST, port=$PORT, max_concurrent=$MAX_CONCURRENT)"
echo "日志: $LOG_FILE"
echo "停止: $SCRIPT_DIR/stop.sh"
