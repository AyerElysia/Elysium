#!/usr/bin/env bash
# Install the visual embedding sidecar without managing the Elysium process.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_NAME="visual-embedding.service"
UNIT_SRC="$SCRIPT_DIR/$UNIT_NAME"
UNIT_DST="/etc/systemd/system/$UNIT_NAME"
LOG_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)/logs"

if [ "$(id -u)" -ne 0 ]; then
    echo "root is required to install $UNIT_DST" >&2
    exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemd is unavailable" >&2
    exit 1
fi
if [ ! -f "$UNIT_SRC" ]; then
    echo "unit file is missing: $UNIT_SRC" >&2
    exit 1
fi

chmod +x "$SCRIPT_DIR/start.sh"
mkdir -p "$LOG_DIR"
install -m 0644 "$UNIT_SRC" "$UNIT_DST"
systemctl daemon-reload
systemctl enable "$UNIT_NAME"

if [ "${1:-}" = "--no-start" ]; then
    exit 0
fi

systemctl restart "$UNIT_NAME"
systemctl status "$UNIT_NAME" --no-pager --lines 0 || true
