#!/bin/zsh
# 幂等拉起工作台的三个常驻 tmux session：Web 服务、每日备份、每周音频完整性核验。
# 已在跑的不会重复启动，可安全反复执行（LaunchAgent 每 5 分钟调用一次）。
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
ROOT="${SCRIPT_DIR:h}"          # <repo>/workbench
STATE="${MEETING_WORKBENCH_DATA_DIR:-$HOME/.meeting-workbench}"
LOGS="$STATE/logs"

# ssh localhost 是非交互 shell，PATH 里没有 Homebrew。ffprobe/ffmpeg 等外部命令
# 依赖它，缺了会让长录音在时长探测处静默卡住，这里显式补上。
PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH
mkdir -p "$LOGS"

if ! tmux has-session -t '=meeting-workbench' 2>/dev/null; then
  tmux new-session -d -s meeting-workbench \
    "cd '$ROOT' && exec env PYTHONDONTWRITEBYTECODE=1 MEETING_RELAY_CONTROL_ENABLED=1 .venv/bin/meeting-workbench serve >> '$LOGS/web.log' 2>&1"
fi

if ! tmux has-session -t '=meeting-workbench-backup' 2>/dev/null; then
  tmux new-session -d -s meeting-workbench-backup \
    "cd '$ROOT' && while true; do PYTHONDONTWRITEBYTECODE=1 .venv/bin/meeting-workbench backup >> '$LOGS/backup.log' 2>&1; sleep 86400; done"
fi

if ! tmux has-session -t '=meeting-workbench-integrity' 2>/dev/null; then
  tmux new-session -d -s meeting-workbench-integrity \
    "cd '$ROOT' && while true; do PYTHONDONTWRITEBYTECODE=1 .venv/bin/meeting-workbench verify-audio >> '$LOGS/integrity.log' 2>&1; exit_code=\$?; if (( exit_code == 2 )); then exit 2; fi; sleep 604800; done"
fi
