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

# 只打印提示，不替用户改 .env。Host 白名单只认回环地址、PUBLIC_BASE_URL 的主机名和 ALLOWED_HOSTS，
# 新装的机器不配这一项，经 ts.net 访问会回 400「Host 不在允许列表」。
TS_NAME="$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys
try:
    name = json.load(sys.stdin)["Self"]["DNSName"]
except Exception:
    name = ""
print(name.rstrip(".") if isinstance(name, str) else "")' || true)"
print ""
print "远程访问还差一步：在 ${0:A:h:h}/.env 里写上这一行（已有就核对主机名），然后重启工作台（scripts/stop-web.sh 再 scripts/start-via-ssh.sh）："
print "  MEETING_WORKBENCH_PUBLIC_BASE_URL=https://${TS_NAME:-<本机>.ts.net}"
if [[ -z "$TS_NAME" ]]; then
  print "（没能从 tailscale status 里取到本机名字，<本机>.ts.net 换成 Tailscale 后台里这台机器的完整域名）"
fi
