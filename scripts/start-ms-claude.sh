#!/usr/bin/env bash
# 启动 ms-claude 独立实例
# 与原版 Claude 并存，各自使用不同的代理

set -e

# 配置
MS_CLAUDE_HOME="${MS_CLAUDE_HOME:-$HOME/.config/ms-claude}"
MS_CLAUDE_PORT="${MS_CLAUDE_PORT:-8081}"
CLIENT_CMD="${CLIENT_CMD:-claude}"

cd "$(dirname "$0")/.."

echo "=========================================="
echo "启动 ms-claude 独立实例"
echo "=========================================="
echo "Home 目录: $MS_CLAUDE_HOME"
echo "代理端口: $MS_CLAUDE_PORT"
echo "客户端命令: $CLIENT_CMD"
echo "=========================================="

# 设置环境变量
export MS_CLAUDE_HOME
export MS_CLAUDE_PORT

# 启动
./ms-claude --connect --client-cmd "$CLIENT_CMD" --port "$MS_CLAUDE_PORT"
