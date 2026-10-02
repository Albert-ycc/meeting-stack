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

# tmux 把命令交给 shell 解析，路径按单引号转义（路径里的 ' 也能对付）
Q_ROOT="${(qq)ROOT}"
Q_WEB_LOG="${(qq):-$LOGS/web.log}"
Q_WEB_STDIO_LOG="${(qq):-$LOGS/web.stdio.log}"
Q_BACKUP_LOG="${(qq):-$LOGS/backup.log}"
Q_INTEGRITY_LOG="${(qq):-$LOGS/integrity.log}"

# Web 服务的日志由服务自己写进 web.log 并按大小轮转（MEETING_WORKBENCH_LOG_MAX_BYTES、
# MEETING_WORKBENCH_LOG_BACKUP_COUNT 调大小和份数）。shell 这边的标准输出、标准错误另接一个文件：
# 服务起不来时的异常、进度条这类不走日志的输出落在这里，平时几乎是空的；不能和 web.log 指向同一个文件。
# env -u：tmux 服务进程的环境里要是留着调试内存时设的 MallocStackLogging，每个子进程都会往 stderr 打一行，
# 几周写出几百 MB（260924 前的 web.log 就是这样），起服务前先摘掉。
if ! tmux has-session -t '=meeting-workbench' 2>/dev/null; then
  tmux new-session -d -s meeting-workbench \
    "cd $Q_ROOT && exec env -u MallocStackLogging -u MallocStackLoggingNoCompact -u MallocStackLoggingDirectory PYTHONDONTWRITEBYTECODE=1 MEETING_RELAY_CONTROL_ENABLED=1 MEETING_WORKBENCH_LOG_FILE=$Q_WEB_LOG .venv/bin/meeting-workbench serve >> $Q_WEB_STDIO_LOG 2>&1"
fi

if ! tmux has-session -t '=meeting-workbench-backup' 2>/dev/null; then
  tmux new-session -d -s meeting-workbench-backup \
    "cd $Q_ROOT && while true; do PYTHONDONTWRITEBYTECODE=1 .venv/bin/meeting-workbench backup >> $Q_BACKUP_LOG 2>&1; sleep 86400; done"
fi

if ! tmux has-session -t '=meeting-workbench-integrity' 2>/dev/null; then
  tmux new-session -d -s meeting-workbench-integrity \
    "cd $Q_ROOT && while true; do PYTHONDONTWRITEBYTECODE=1 .venv/bin/meeting-workbench verify-audio >> $Q_INTEGRITY_LOG 2>&1; exit_code=\$?; if (( exit_code == 2 )); then sleep 3600; continue; fi; sleep 604800; done"
fi
