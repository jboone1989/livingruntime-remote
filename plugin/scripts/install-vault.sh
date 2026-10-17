#!/usr/bin/env bash
# Dedicated OS-identity vault setup. Run as root after reviewing this script.
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root (sudo -n bash plugin/scripts/install-vault.sh <connector-user>)" >&2
  exit 1
fi
CONNECTOR_USER="${1:-}"
if [[ -z "$CONNECTOR_USER" ]] || ! id "$CONNECTOR_USER" >/dev/null 2>&1; then
  echo "An existing Connector OS account is required as the first argument" >&2
  exit 2
fi
if [[ "$CONNECTOR_USER" == root ]]; then
  echo "Refusing a root-owned development Connector" >&2
  exit 2
fi
CONNECTOR_UID="$(id -u "$CONNECTOR_USER")"
CONNECTOR_GROUP="$(id -gn "$CONNECTOR_USER")"
VAULT_USER=livingruntime-vault
SCRIPTS="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if ! getent passwd "$VAULT_USER" >/dev/null; then
  useradd --system --no-create-home --home-dir /nonexistent \
    --shell /usr/sbin/nologin "$VAULT_USER"
fi
if [[ "$(id -u "$VAULT_USER")" == "$CONNECTOR_UID" ]]; then
  echo "Vault and Connector must have distinct uids" >&2
  exit 3
fi
install -d -m 0755 -o root -g root /opt/livingruntime-vault
for file in credentials.py secure_input.py vault_service.py; do
  install -m 0644 -o root -g root "$SCRIPTS/$file" "/opt/livingruntime-vault/$file"
done
if [[ ! -e /opt/livingruntime-vault/venv/bin/python ]]; then
  python3 -m venv /opt/livingruntime-vault/venv
fi
/opt/livingruntime-vault/venv/bin/pip install --disable-pip-version-check 'cryptography>=41,<51'
chmod -R go-w /opt/livingruntime-vault
cat > /etc/systemd/system/livingruntime-vault.service <<EOF
[Unit]
Description=LivingRuntime isolated credential vault
After=local-fs.target
[Service]
Type=simple
User=$VAULT_USER
Group=$CONNECTOR_GROUP
UMask=0077
WorkingDirectory=/opt/livingruntime-vault
ExecStart=/opt/livingruntime-vault/venv/bin/python /opt/livingruntime-vault/vault_service.py --connector-uid $CONNECTOR_UID
Restart=on-failure
RestartSec=5
RuntimeDirectory=livingruntime-vault
RuntimeDirectoryMode=0750
StateDirectory=livingruntime-vault
StateDirectoryMode=0700
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
NoNewPrivileges=yes
CapabilityBoundingSet=
RestrictSUIDSGID=yes
LockPersonality=yes
ProtectControlGroups=yes
ProtectKernelModules=yes
ProtectKernelTunables=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
[Install]
WantedBy=multi-user.target
EOF
chmod 0644 /etc/systemd/system/livingruntime-vault.service
systemctl daemon-reload
systemctl enable --now livingruntime-vault.service
systemctl is-active --quiet livingruntime-vault.service
echo "Isolated vault service running. Check socket permissions and use the plugin UI for synthetic smoke tests."
