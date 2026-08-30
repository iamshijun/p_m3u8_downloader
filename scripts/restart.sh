#!/usr/bin/env bash
# 重启 m3u8 API 服务
#
# 用法:
#   ./scripts/restart.sh                  # 后台重启
#   ./scripts/restart.sh foreground       # 前台重启 (透传给 start.sh)
#
# stop.sh 无参数且总是 exit 0; "$@" 原样透传给 start.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> stop"
"$SCRIPT_DIR/stop.sh"

# 短暂等待端口释放
sleep 1

echo "==> start"
exec "$SCRIPT_DIR/start.sh" "$@"
