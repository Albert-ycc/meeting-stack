#!/bin/zsh
# 安装开机自恢复 LaunchAgent：每 5 分钟确认工作台在跑，不在就拉起。
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
REPO_ROOT="${SCRIPT_DIR:h:h}"
LABEL="com.meeting-workbench.bootstrap"
TEMPLATE="$REPO_ROOT/workbench/deploy/$LABEL.plist.template"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -f "$TEMPLATE" ]; then
  echo "找不到 plist 模板：$TEMPLATE" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.meeting-workbench/logs"

# 把模板里的占位符换成本机真实路径
sed -e "s|__REPO_ROOT__|$REPO_ROOT|g" -e "s|__HOME__|$HOME|g" "$TEMPLATE" > "$TARGET"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$TARGET"
launchctl enable "gui/$(id -u)/$LABEL"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "LaunchAgent 已安装：$TARGET"
