#!/usr/bin/env bash
# Follow the dedicated heartbeat panel stream.  Prefer
# ``elysia-attach heartbeat`` (same tmux socket as the main process).
# This script does not start or restart Elysium.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FILE="${HEARTBEAT_PANEL_PATH:-$ROOT/logs/heartbeat.console}"
mkdir -p "$(dirname "$FILE")"
touch "$FILE"
exec tail -n 80 -F "$FILE"
