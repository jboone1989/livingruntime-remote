#!/usr/bin/env bash
set -euo pipefail

RUN_USER="ubuntu"
INSTALL_DIR="/opt/livingruntime/remote-mcp-gateway"
PORT="8765"
ALLOWED_ROOTS=()
SYSTEMD_UNITS=()

usage() {
  cat <<'EOF'
Usage:
  sudo ./install.sh [--user ubuntu] [--port 8765] \
    [--allowed-root /home/ubuntu]... [--unit content-agent.service]...

The service binds to 127.0.0.1 only. Use a trusted MCP tunnel or reverse proxy
with TLS if a remote MCP client must reach it.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) RUN_USER="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --allowed-root) ALLOWED_ROOTS+=("$2"); shift 2 ;;
    --unit) SYSTEMD_UNITS+=("$2"); shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this installer with sudo/root." >&2
  exit 1
fi

if ! id "$RUN_USER" >/dev/null 2>&1; then
  echo "Unknown user: $RUN_USER" >&2
  exit 1
fi

HOME_DIR="$(getent passwd "$RUN_USER" | cut -d: -f6)"
if [[ ${#ALLOWED_ROOTS[@]} -eq 0 ]]; then
  ALLOWED_ROOTS=("$HOME_DIR")
fi

for root in "${ALLOWED_ROOTS[@]}"; do
  [[ -d "$root" ]] || { echo "Allowed root does not exist: $root" >&2; exit 1; }
done

for unit in "${SYSTEMD_UNITS[@]}"; do
  [[ "$unit" =~ ^[A-Za-z0-9_.@:-]+\.service$ ]] || {
    echo "Invalid systemd unit: $unit" >&2
    exit 1
  }
done

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install -d -m 0755 "$INSTALL_DIR"
install -m 0644 "$SOURCE_DIR/server.py" "$INSTALL_DIR/server.py"
install -m 0644 "$SOURCE_DIR/policy.py" "$INSTALL_DIR/policy.py"
install -m 0644 "$SOURCE_DIR/smoke_test.py" "$INSTALL_DIR/smoke_test.py"
install -m 0644 "$SOURCE_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"

python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --disable-pip-version-check -r "$INSTALL_DIR/requirements.txt"

TOKEN="$(openssl rand -hex 32)"
ROOTS_JOINED="$(IFS=:; echo "${ALLOWED_ROOTS[*]}")"
UNITS_JOINED="$(IFS=,; echo "${SYSTEMD_UNITS[*]}")"
AUDIT_DIR="$HOME_DIR/.local/state/remote-mcp-gateway"
install -d -o "$RUN_USER" -g "$RUN_USER" -m 0700 "$AUDIT_DIR"

cat >/etc/remote-mcp-gateway.env <<EOF
MCP_GATEWAY_TOKEN=$TOKEN
MCP_GATEWAY_ALLOWED_ROOTS=$ROOTS_JOINED
MCP_GATEWAY_SYSTEMD_UNITS=$UNITS_JOINED
MCP_GATEWAY_AUDIT_LOG=$AUDIT_DIR/audit.jsonl
MCP_GATEWAY_MAX_OUTPUT_BYTES=262144
MCP_GATEWAY_DEFAULT_TIMEOUT=30
MCP_GATEWAY_BIND_HOST=127.0.0.1
MCP_GATEWAY_PORT=$PORT
EOF
chmod 0600 /etc/remote-mcp-gateway.env

cat >/etc/systemd/system/remote-mcp-gateway.service <<EOF
[Unit]
Description=LivingRuntime Remote MCP Gateway
After=network.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=/etc/remote-mcp-gateway.env
ExecStart=$INSTALL_DIR/.venv/bin/uvicorn server:app --host 127.0.0.1 --port $PORT --workers 1 --no-access-log
Restart=on-failure
RestartSec=2
TimeoutStopSec=10
PrivateTmp=true
ProtectSystem=full
ProtectHome=false
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
LockPersonality=true
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

SUDOERS="/etc/sudoers.d/remote-mcp-gateway-systemd"
if [[ ${#SYSTEMD_UNITS[@]} -gt 0 ]]; then
  SYSTEMCTL="$(command -v systemctl)"
  {
    printf 'Cmnd_Alias REMOTE_MCP_SYSTEMD = '
    first=1
    for unit in "${SYSTEMD_UNITS[@]}"; do
      for action in start stop restart; do
        if [[ $first -eq 0 ]]; then printf ', '; fi
        printf '%s %s %s' "$SYSTEMCTL" "$action" "$unit"
        first=0
      done
    done
    printf '\n%s ALL=(root) NOPASSWD: REMOTE_MCP_SYSTEMD\n' "$RUN_USER"
  } >"$SUDOERS"
  chmod 0440 "$SUDOERS"
  visudo -cf "$SUDOERS" >/dev/null
else
  rm -f "$SUDOERS"
fi

systemctl daemon-reload
systemctl enable --now remote-mcp-gateway.service

python3 - "$PORT" <<'PY'
import sys
import time
import urllib.request

port = int(sys.argv[1])
url = f"http://127.0.0.1:{port}/healthz"
last = None
for _ in range(20):
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            body = response.read().decode()
            if response.status == 200 and '"ok":true' in body:
                print(f"health check OK: {url}")
                raise SystemExit(0)
    except Exception as exc:
        last = exc
        time.sleep(0.25)
raise SystemExit(f"health check failed: {last}")
PY

MCP_GATEWAY_TOKEN="$TOKEN" "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/smoke_test.py" \
  --url "http://127.0.0.1:${PORT}/mcp"

echo "Installed remote-mcp-gateway.service"
echo "Local MCP endpoint: http://127.0.0.1:${PORT}/mcp"
echo "Bearer token is stored root-only in /etc/remote-mcp-gateway.env"
echo "Audit log: $AUDIT_DIR/audit.jsonl"
