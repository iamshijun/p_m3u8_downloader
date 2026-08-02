#!/usr/bin/env bash
# 把 m3u8 API 注册为 systemd 服务 (Linux only; macOS / 开发机请用 scripts/start.sh)
#
# 用法:
#   sudo ./scripts/install-systemd.sh
#   sudo ./scripts/install-systemd.sh --user=m3u8-downloader --dir=/opt/m3u8_video_fetcher
#   sudo ./scripts/install-systemd.sh --no-start          # 安装但不启动
#
# 行为:
#   1. 检查 root + systemctl + api_server.py
#   2. 检查 fastapi/uvicorn/pydantic 可导入
#   3. 创建系统用户/组 (默认 m3u8-downloader), 把 <INSTALL_DIR>/{run,downloads} chown 给它
#   4. 替换 m3u8-downloader.service 中的占位符, 写入 /etc/systemd/system/m3u8-downloader.service
#   5. 替换 m3u8-downloader.logrotate 中的占位符, 写入 /etc/logrotate.d/m3u8-downloader
#   6. systemctl daemon-reload + enable, 默认 start
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

SERVICE_NAME="m3u8-downloader"
SERVICE_USER="${SERVICE_USER:-m3u8-downloader}"
SERVICE_GROUP="${SERVICE_GROUP:-m3u8-downloader}"
INSTALL_DIR="$PROJECT_DIR"
PYTHON_BIN="${PYTHON:-}"
NO_START=0

usage() {
    sed -n '2,17p' "$0"
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user=*)  SERVICE_USER="${1#--user=}" ;;
        --group=*) SERVICE_GROUP="${1#--group=}" ;;
        --dir=*)   INSTALL_DIR="${1#--dir=}" ;;
        --python=*) PYTHON_BIN="${1#--python=}" ;;
        --no-start) NO_START=1 ;;
        --help|-h) usage ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
    shift
done

if [[ "$EUID" -ne 0 ]]; then
    echo "请用 root 运行: sudo $0" >&2
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "未找到 systemctl, 此脚本仅适用于 systemd 系统" >&2
    exit 1
fi

# 解析 INSTALL_DIR
if [[ ! -f "$INSTALL_DIR/api_server.py" ]]; then
    echo "api_server.py 不在 $INSTALL_DIR" >&2
    exit 1
fi
INSTALL_DIR="$(cd "$INSTALL_DIR" && pwd)"

# 解析 python (优先级: --python=参数 > VIRTUAL_ENV 环境变量 > 系统 python3)
if [[ -z "$PYTHON_BIN" ]]; then
    if [[ -n "${VIRTUAL_ENV:-}" ]]; then
        PYTHON_BIN="$VIRTUAL_ENV/bin/python"
    else
        PYTHON_BIN="$(command -v python3 || true)"
    fi
    if [[ -z "$PYTHON_BIN" ]]; then
        echo "找不到 python3 (可用 --python= 指定路径或激活 venv)" >&2
        exit 1
    fi
fi

# 依赖检查
if ! "$PYTHON_BIN" -c "import fastapi, uvicorn, pydantic" 2>/dev/null; then
    echo "缺少依赖 fastapi/uvicorn/pydantic" >&2
    echo "请先执行: $PYTHON_BIN -m pip install -r $INSTALL_DIR/requirements.txt" >&2
    exit 1
fi

# 系统用户/组
if ! getent passwd "$SERVICE_USER" >/dev/null; then
    echo "==> 创建系统用户: $SERVICE_USER"
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
if ! getent group "$SERVICE_GROUP" >/dev/null; then
    echo "==> 创建系统组: $SERVICE_GROUP"
    groupadd --system "$SERVICE_GROUP"
fi
# 把用户加入组 (兼容 user 已存在但未入组的情况)
usermod -a -G "$SERVICE_GROUP" "$SERVICE_USER" 2>/dev/null || true

# 运行时目录
mkdir -p "$INSTALL_DIR/run" "$INSTALL_DIR/downloads"
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR/run" "$INSTALL_DIR/downloads"
chmod 750 "$INSTALL_DIR/run" "$INSTALL_DIR/downloads"

# 渲染并安装 service
SERVICE_FILE_SRC="$SCRIPT_DIR/m3u8-downloader.service"
SERVICE_FILE_DST="/etc/systemd/system/${SERVICE_NAME}.service"
sed -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
    -e "s|__PYTHON__|$PYTHON_BIN|g" \
    -e "s|__SERVICE_USER__|$SERVICE_USER|g" \
    -e "s|__SERVICE_GROUP__|$SERVICE_GROUP|g" \
    "$SERVICE_FILE_SRC" > "$SERVICE_FILE_DST"
chmod 644 "$SERVICE_FILE_DST"
echo "==> 写入: $SERVICE_FILE_DST"

# 渲染并安装 logrotate
LOGROTATE_SRC="$SCRIPT_DIR/m3u8-downloader.logrotate"
LOGROTATE_DST="/etc/logrotate.d/$SERVICE_NAME"
sed -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
    "$LOGROTATE_SRC" > "$LOGROTATE_DST"
chmod 644 "$LOGROTATE_DST"
echo "==> 写入: $LOGROTATE_DST"

# 重新加载 systemd
systemctl daemon-reload

if [[ "$NO_START" -eq 1 ]]; then
    systemctl enable "$SERVICE_NAME"
    echo ""
    echo "已安装并 enable, 但未启动. 手动: sudo systemctl start $SERVICE_NAME"
else
    systemctl enable "$SERVICE_NAME"
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        systemctl restart "$SERVICE_NAME"
    else
        systemctl start "$SERVICE_NAME"
    fi
    sleep 1
    echo ""
    echo "==> 服务状态:"
    systemctl --no-pager status "$SERVICE_NAME" || true
    echo ""
    echo "==> 常用命令:"
    echo "  sudo systemctl {start|stop|restart|status} $SERVICE_NAME"
    echo "  sudo journalctl -u $SERVICE_NAME -f"
    echo "  tail -f /var/log/m3u8-downloader/server.log"
fi
