#!/bin/sh
set -eu

RELAY="${LIVINGRUNTIME_RELAY_URL:-https://remote.livingruntime.com}"
PAIR_CODE="${LIVINGRUNTIME_PAIR_CODE:-}"
SSH_HOST="${LIVINGRUNTIME_SSH_HOST:-}"
ROOT="${LIVINGRUNTIME_ROOT:-}"

os="$(uname -s)"
arch="$(uname -m)"

case "$os" in
  Linux) platform="linux" ;;
  Darwin) platform="macos" ;;
  *) echo "Unsupported operating system: $os" >&2; exit 1 ;;
esac

case "$arch" in
  x86_64|amd64) machine="amd64" ;;
  arm64|aarch64) machine="arm64" ;;
  *) echo "Unsupported CPU architecture: $arch" >&2; exit 1 ;;
esac

asset="livingruntime-remote-connector-${platform}-${machine}"
url="https://github.com/jboone1989/livingruntime-remote/releases/latest/download/${asset}"
tmp="${TMPDIR:-/tmp}/${asset}.$$"

cleanup() { rm -f "$tmp"; }
trap cleanup EXIT INT TERM

if [ -z "$PAIR_CODE" ]; then
  printf "Pairing code from ChatGPT (XXXX-XXXX): " >/dev/tty
  IFS= read -r PAIR_CODE </dev/tty
fi
if [ -z "$SSH_HOST" ]; then
  printf "SSH host or user@host: " >/dev/tty
  IFS= read -r SSH_HOST </dev/tty
fi
if [ -z "$ROOT" ]; then
  printf "Allowed workspace root (for example /home/ubuntu): " >/dev/tty
  IFS= read -r ROOT </dev/tty
fi

echo "Downloading LivingRuntime Remote Connector..."
curl -fL --retry 3 --connect-timeout 15 "$url" -o "$tmp"
chmod 700 "$tmp"

echo "Checking SSH, pairing this computer, and enabling autostart..."
"$tmp" install --pair "$PAIR_CODE" --host "$SSH_HOST" --root "$ROOT" --relay "$RELAY"

echo
echo "LivingRuntime Remote is ready."
echo "Return to ChatGPT and run: connection_status"
