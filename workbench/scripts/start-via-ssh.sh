#!/bin/zsh
# 经 ssh localhost 启动工作台。
#
# 为什么绕这一圈：macOS TCC 按进程链授予文件访问权。launchd 直接拉起的进程
# （及它启动的 tmux server）常被拦在外置存储和受保护目录之外，而 sshd 通常已在
# 「完全磁盘访问」名单内，经 ssh 链启动可继承该授权。详见 docs/install.md。
#
# 不需要远程访问、也不读外置存储的话，直接跑 remote-bootstrap.sh 即可。
set -euo pipefail

SCRIPT_DIR="${0:A:h}"

ssh -o BatchMode=yes -o ConnectTimeout=10 localhost \
  "$SCRIPT_DIR/remote-bootstrap.sh"
