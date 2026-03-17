#!/bin/bash
set -e

PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/com.user.stickies-sync.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.user.stickies-sync.plist"
STATE_DIR="$HOME/.local/share/stickies-sync"
LABEL="com.user.stickies-sync"

echo "==> 创建状态目录"
mkdir -p "$STATE_DIR"

echo "==> 安装 LaunchAgent"
cp "$PLIST_SRC" "$PLIST_DST"

echo "==> 卸载旧任务（若存在）"
launchctl unload "$PLIST_DST" 2>/dev/null || true

echo "==> 加载 LaunchAgent"
launchctl load "$PLIST_DST"

echo "==> 验证任务已加载"
launchctl list | grep "$LABEL" && echo "✓ 安装成功" || echo "✗ 安装失败，请检查日志"
