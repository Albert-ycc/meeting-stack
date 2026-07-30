#!/bin/zsh
# 可选：把本机工作台暴露到自己的 Tailscale 网络，供手机等设备访问。
# 不需要远程访问就完全不用跑这个脚本。
#
# 安全模型：不在应用层另建账号体系，设备身份边界完全交给 Tailscale ACL。
# 普通局域网端口始终不开放。
set -euo pipefail

PORT="${MEETING_WORKBENCH_PORT:-8765}"
BASE="http://127.0.0.1:$PORT"

curl --fail --silent --show-error "$BASE/api/health" >/dev/null
tailscale serve --bg --yes "$BASE"
tailscale serve status --json | python3 -c 'import json,sys; payload=json.load(sys.stdin); assert payload, "Tailscale Serve 未生效"; print(json.dumps(payload, ensure_ascii=False))'
