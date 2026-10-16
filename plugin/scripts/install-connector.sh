#!/bin/sh
set -eu
# Install a verified-release executable. Pairing is a separate action.
VERSION="${LIVINGRUNTIME_VERSION:-v0.4.67}"
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
printf 'Next: livingruntime-remote install --local --root "$HOME/workspace" --pair YOUR_CODE\n'
