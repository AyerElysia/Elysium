#!/usr/bin/env bash
# Attach a dedicated heartbeat viewer on the same tmux socket as
# ``elysia-attach``.  Does not start or restart the Elysium process.
set -euo pipefail
SOCKET="${ELYSIUM_TMUX_SOCKET:-elysium}"
SESSION="${ELYSIUM_HEARTBEAT_SESSION:-elysium-heartbeat}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WATCH="${ROOT}/scripts/watch_heartbeat.sh"

if [[ ! -x "$WATCH" ]]; then
  echo "heartbeat viewer missing: $WATCH" >&2
  exit 1
fi

if ! tmux -L "$SOCKET" has-session -t "$SESSION" 2>/dev/null; then
  tmux -L "$SOCKET" new-session -d -s "$SESSION" -n heartbeat "$WATCH"
fi

exec tmux -L "$SOCKET" attach -t "$SESSION"
