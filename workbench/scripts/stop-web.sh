#!/bin/zsh
# 停止 Web 服务。转写与派单守护是独立进程，不受影响。
set -euo pipefail

TMUX_BIN="$(command -v tmux || echo /opt/homebrew/bin/tmux)"

ssh -o BatchMode=yes -o ConnectTimeout=10 localhost \
  "$TMUX_BIN kill-session -t '=meeting-workbench' 2>/dev/null || true"
