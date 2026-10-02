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

# 路径填进 plist 前先做 XML 实体转义，再转义 sed 替换串里有特殊含义的 \、& 和分隔符 |
plist_sed_value() {
  local value="$1"
  value="${value//'&'/&amp;}"
  value="${value//'<'/&lt;}"
  value="${value//'>'/&gt;}"
  value="${value//'\'/\\\\}"
  value="${value//'&'/\\&}"
  value="${value//'|'/\\|}"
  print -r -- "$value"
}

# 把模板里的占位符换成本机真实路径
sed -e "s|__REPO_ROOT__|$(plist_sed_value "$REPO_ROOT")|g" \
  -e "s|__HOME__|$(plist_sed_value "$HOME")|g" "$TEMPLATE" > "$TARGET"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$TARGET"
launchctl enable "gui/$(id -u)/$LABEL"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "LaunchAgent 已安装：$TARGET"
