#!/usr/bin/env bash
# scripts/*.sh 共用的公共函数. 被 start.sh / status.sh source, 不单独执行.
# 调用方需先设置 ENV_FILE="$PROJECT_DIR/.env".

# 从项目根 .env 取单个键的值 (仅用于回显/端口探测; 真正的配置解析在 api_server.py).
# 不整文件 source: .env 的值可能含未引号空格 (如 NAME=Yunpan HLS Proxy), source 会当命令执行.
env_get() {
    local key="$1" line val
    [[ -f "${ENV_FILE:-}" ]] || return 0
    # grep 无匹配返回 1, pipefail 下会终止脚本 → 必须 || return 0
    line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=" "$ENV_FILE" | tail -n 1)" || return 0
    [[ -n "$line" ]] || return 0
    val="${line#*=}"; val="${val%%#*}"
    val="$(printf '%s' "$val" | tr -d '\r')"
    val="${val#"${val%%[![:space:]]*}"}"; val="${val%"${val##*[![:space:]]}"}"
    val="${val%\"}"; val="${val#\"}"; val="${val%\'}"; val="${val#\'}"
    printf '%s' "$val"
}

port_in_use() {   # macOS 无 ss, 由 lsof 分支兜住
    local port="$1"
    if command -v ss >/dev/null 2>&1; then
        ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${port}$"
    elif command -v lsof >/dev/null 2>&1; then
        lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
    elif command -v netstat >/dev/null 2>&1; then
        netstat -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${port}$"
    else
        return 1
    fi
}
