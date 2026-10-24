#!/bin/sh
set -eu
# Install a verified-release executable. Pairing is a separate action.
VERSION="${LIVINGRUNTIME_VERSION:-v0.4.69}"
BASE="${LIVINGRUNTIME_RELEASE_BASE:-https://github.com/jboone1989/livingruntime-remote/releases/download}"
DEST="${LIVINGRUNTIME_INSTALL_DIR:-$HOME/.local/bin}"
case "$VERSION" in v[0-9]* ) ;; *) echo "Invalid release version" >&2; exit 2 ;; esac
case "$(uname -s):$(uname -m)" in
 Linux:x86_64|Linux:amd64) ASSET=linux-amd64 ;;
 Linux:aarch64|Linux:arm64) ASSET=linux-arm64 ;;
 Darwin:x86_64) ASSET=macos-amd64 ;;
 Darwin:arm64) ASSET=macos-arm64 ;;
 *) echo "Unsupported OS/architecture" >&2; exit 2 ;;
esac
mkdir -p "$DEST"
TMP="$(mktemp "$DEST/.livingruntime-remote.XXXXXXXX")"
trap 'rm -f "$TMP"' EXIT HUP INT TERM
curl --fail --location --retry 3 --connect-timeout 15 "$BASE/$VERSION/livingruntime-remote-connector-$ASSET" --output "$TMP"
chmod 755 "$TMP"
"$TMP" --version | grep -Fx "${VERSION#v}" >/dev/null || { echo "Downloaded version mismatch" >&2; exit 1; }
mv -f "$TMP" "$DEST/livingruntime-remote"
trap - EXIT HUP INT TERM
printf 'Installed %s to %s\n' "$VERSION" "$DEST/livingruntime-remote"
# curl ... | sh consumes stdin, so guide the user via the controlling TTY.
# CI/automation and existing paired installations remain noninteractive.
if [ -t 1 ] && [ -r /dev/tty ] \
   && [ ! -s "${LIVINGRUNTIME_RELAY_CONFIG:-$HOME/.livingruntime/relay.json}" ] \
   && [ "${LIVINGRUNTIME_INSTALL_NO_SETUP:-0}" != 1 ]; then
  printf 'Pairing code from ChatGPT (XXXX-XXXX): ' >/dev/tty
  IFS= read -r PAIR </dev/tty
  if [ -z "$PAIR" ]; then
    printf 'No code supplied; run %s to pair later.\n' "$DEST/livingruntime-remote"
    exit 0
  fi
  # The verified v0.4.69 binary already understands --pair-file. Keep the
  # one-command installer compatible with an older published binary too.
  PAIR_FILE="$(mktemp "$DEST/.livingruntime-pair.XXXXXXXX")"
  trap 'rm -f "$PAIR_FILE"' EXIT HUP INT TERM
  chmod 600 "$PAIR_FILE"
  printf '%s\n' "$PAIR" > "$PAIR_FILE"
  unset PAIR
  mkdir -p "$HOME/workspace"
  if ! "$DEST/livingruntime-remote" install --local \
      --root "$HOME/workspace" --pair-file "$PAIR_FILE"; then
    printf 'Pairing failed. Create a fresh code in ChatGPT and retry pairing.\n' >&2
    exit 1
  fi
  rm -f "$PAIR_FILE"
  trap - EXIT HUP INT TERM
else
  printf 'To pair later: %s\n' "$DEST/livingruntime-remote"
fi
