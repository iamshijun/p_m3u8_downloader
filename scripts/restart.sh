#!/usr/bin/env bash
# 重启 m3u8 API 服务
#
# 用法:
#   ./scripts/restart.sh                # 透传 start.sh 的所有环境变量与参数
#   ./scripts/restart.sh --force        # stop 也用 SIGKILL
#
# 任意额外参数/环境变量都会原样转给 start.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> stop"
"$SCRIPT_DIR/stop.sh" "$@" || {
    RC=$?
    if [[ "$RC" -eq 1 ]]; then
        echo "stop 失败, 终止重启" >&2
        exit "$RC"
    fi
    # stop 返回 0 (未运行 / 正常停止) 都允许继续
}

# 短暂等待端口释放
sleep 1

echo "==> start"
exec "$SCRIPT_DIR/start.sh" "$@"
