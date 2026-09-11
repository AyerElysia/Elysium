#!/usr/bin/env bash
# Install manual controls only. Never stop/start/restart/enable the main process.
set -euo pipefail
ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
EXPECTED_ROOT=/home/ayerelysia/Elysia/Elysium
if [[ "$ROOT" != "$EXPECTED_ROOT" || "$(hostname)" != spark-f3b0 ]]; then
  echo "Refusing installation outside Spark's verified formal repository." >&2
  exit 1
fi
SOURCE="$ROOT/scripts/elysia_attach.sh"
if [[ ! -x "$SOURCE" ]]; then
  echo "Attach helper is not executable: $SOURCE" >&2
  exit 1
fi
if [[ ${1:-} == --user && $# -eq 1 ]]; then
  if [[ "$(id -un)" != ayerelysia ]]; then
    echo 'Run --user as ayerelysia, not root.' >&2
    exit 1
  fi
  DEST=/home/ayerelysia/.local/bin/elysia-attach
  install -d -m 755 /home/ayerelysia/.local/bin
elif [[ $# -eq 0 && $EUID -eq 0 ]]; then
  DEST=/usr/local/bin/elysia-attach
else
  echo 'Usage: bash scripts/install_spark_controls.sh --user' >&2
  echo 'System unit: sudo bash scripts/install_spark_controls.sh' >&2
  exit 1
fi
if [[ -e "$DEST" || -L "$DEST" ]]; then
  if [[ ! -L "$DEST" || "$(readlink -f "$DEST")" != "$SOURCE" ]]; then
    echo "Refusing to overwrite another command: $DEST" >&2
    exit 1
  fi
else
  ln -s "$SOURCE" "$DEST"
fi
if [[ ${1:-} == --user ]]; then
  echo "Installed $DEST; this does not start or restart Elysium."
  exit 0
fi
UNIT_SOURCE="$ROOT/services/elysium/elysium.service"
UNIT_DEST=/etc/systemd/system/elysium.service
if [[ -e "$UNIT_DEST" || -L "$UNIT_DEST" ]]; then
  if [[ -L "$UNIT_DEST" ]] || ! cmp -s "$UNIT_SOURCE" "$UNIT_DEST"; then
    echo "Refusing to overwrite an existing, different service: $UNIT_DEST" >&2
    exit 1
  fi
fi
systemd-analyze verify "$UNIT_SOURCE"
install -m 644 "$UNIT_SOURCE" "$UNIT_DEST"
systemctl daemon-reload
systemctl show elysium.service --property=LoadState,ActiveState,MainPID,Restart,UnitFileState
echo 'Installed manual-only elysium.service. No start, restart or enable was run.'
echo 'If the legacy tmux instance is running, use elysia-attach then Ctrl+C once.'
echo 'After its shutdown, use: sudo systemctl restart elysium'
