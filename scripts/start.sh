#!/usr/bin/env bash
# 启动 m3u8 API 服务 (对齐 movie_api: 自动 .venv + 每次同步依赖 + foreground 前台模式)
#
# 用法:
#   ./scripts/start.sh                  # 后台运行 (nohup, 写 run/server.pid + run/server.log)
#   ./scripts/start.sh foreground       # 前台运行 (Ctrl-C 停止, 不写 pid 文件; -f/--foreground 同义)
#
# 监听地址/端口由 api_server.py 自己读取 (优先级: 环境变量 > .env > 代码默认 0.0.0.0:8000).
# 本脚本里的 HOST/PORT 仅用于回显和端口预检 (对 .env 的粗解析, 不拥有阻止启动的权力).
#
# 环境变量:
#   HOST / PORT    仅回显 + 预检用 (真正生效的是 api_server.py 读到的同名配置)
#   MAX_CONCURRENT 同时运行的下载任务数. 仅在显式设置时转发为 MAX_CONCURRENT_DOWNLOADS
#                  (不设置时 api_server.py 直接读 .env 的同名键)
#   PYTHON         创建 .venv 的引导解释器 (默认 python3)
#   SKIP_PIP       设为 1 跳过每次启动的依赖同步
#
# 退出码: 0 启动 / 1 已在运行或启动失败 / 2 参数错误
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

ENV_FILE="$PROJECT_DIR/.env"
PID_FILE="$PROJECT_DIR/run/server.pid"
LOG_FILE="$PROJECT_DIR/run/server.log"

# shellcheck disable=SC1091
source "$SCRIPT_DIR/_common.sh"

# ---- 参数 -----------------------------------------------------------------
RUN_MODE="background"
for arg in "$@"; do
    case "$arg" in
        foreground|-f|--foreground) RUN_MODE="foreground" ;;
        *) echo "[error] 未知参数: $arg (无参=后台, foreground=前台)" >&2; exit 2 ;;
    esac
done

# ---- 回显/预检用的 HOST/PORT (shell 侧粗解析; 真正的解析在 api_server.py) --
HOST="${HOST:-$(env_get HOST)}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-$(env_get PORT)}"
PORT="${PORT:-8000}"
DISPLAY_HOST="$HOST"
[[ "$HOST" == "0.0.0.0" ]] && DISPLAY_HOST="127.0.0.1"

# ---- .venv: 不存在则用 $PYTHON 引导创建 ------------------------------------
PYTHON_BIN="${PYTHON:-python3}"
VENV_DIR="$PROJECT_DIR/.venv"
if [[ -x "$VENV_DIR/bin/python" ]]; then
    VENV_PY="$VENV_DIR/bin/python"
else
    echo "[setup] 创建虚拟环境: $VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    VENV_PY="$VENV_DIR/bin/python"
fi

# ---- 依赖同步: 每次启动都跑 (幂等); 失败仅告警, 网络抖动不应让重启失败 ------
if [[ "${SKIP_PIP:-0}" != "1" ]]; then
    echo "[setup] 同步依赖 (venv 内)"
    "$VENV_PY" -m pip install --upgrade pip >/dev/null 2>&1 \
        || echo "[warn] pip 自身升级失败, 跳过"
    "$VENV_PY" -m pip install -r "$PROJECT_DIR/requirements.txt" \
        || echo "[warn] 依赖同步失败 (网络?), 尝试用 venv 现有依赖启动"
fi
"$VENV_PY" -c "import fastapi, uvicorn" 2>/dev/null || {
    echo "[error] venv 内缺少 fastapi/uvicorn, 请检查网络后重试 (或 SKIP_PIP=1 跳过同步)" >&2
    exit 1
}

# ---- MAX_CONCURRENT: 仅显式设置时转发 --------------------------------------
# 不转发时 api_server.py 读 .env 的 MAX_CONCURRENT_DOWNLOADS;
# 若这里无条件 export 默认值 3, 反而会盖掉 .env 里的同名配置.
if [[ -n "${MAX_CONCURRENT:-}" ]]; then
    export MAX_CONCURRENT_DOWNLOADS="$MAX_CONCURRENT"
fi

# ---- 前台模式: 不写 pid 文件, 不受 stop.sh 管理 (Ctrl-C 停止) ---------------
if [[ "$RUN_MODE" == "foreground" ]]; then
    echo "[run] 前台启动: http://$DISPLAY_HOST:$PORT"
    exec "$VENV_PY" api_server.py
fi

# ---- 后台模式 ----------------------------------------------------------------
mkdir -p "$PROJECT_DIR/run"

# 重复启动守卫: pid 文件存在且进程存活 → 不再拉起
if [[ -f "$PID_FILE" ]]; then
    OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[warn] 服务已在运行 (pid=$OLD_PID)"
        exit 1
    fi
    echo "[setup] 清理残留 pid 文件 (pid=$OLD_PID 已不存在)"
    rm -f "$PID_FILE"
fi

# 端口占用预检: 降级为告警. shell 侧 PORT 是对 .env 的粗解析值, 不拥有
# 阻止启动的权力; 真正判定 = 启动后 sleep 1 的存活检查 + 日志尾部.
if port_in_use "$PORT"; then
    echo "[warn] 端口 $PORT 疑似被占用:"
    if command -v lsof >/dev/null 2>&1; then
        lsof -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | tail -n +2 >&2 || true
    fi
fi

# 后台启动, 日志追加写入; HOST/PORT 由 api_server.py 自己读 .env/环境变量
nohup "$VENV_PY" api_server.py >> "$LOG_FILE" 2>&1 &
PID=$!
echo "$PID" > "$PID_FILE"

# 短暂等待确认进程存活
sleep 1
if ! kill -0 "$PID" 2>/dev/null; then
    echo "[error] 启动失败, 日志尾部:" >&2
    tail -n 20 "$LOG_FILE" >&2 || true
    rm -f "$PID_FILE"
    exit 1
fi

echo "[run] 后台启动: http://$DISPLAY_HOST:$PORT (pid=$PID)"
echo "      日志: $LOG_FILE"
echo "      停止: $SCRIPT_DIR/stop.sh"
