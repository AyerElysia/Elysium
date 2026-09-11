#!/usr/bin/env bash
# Attach only: never start or restart the Elysium main process.
set -euo pipefail
ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
MODE="${1:-main}"
if [[ $# -gt 1 ]]; then
  echo 'Usage: elysia-attach [main|heartbeat|status]' >&2
  exit 2
fi
case "$MODE" in
  --help|-h)
    echo 'Usage: elysia-attach [main|heartbeat|status]'
    echo 'Attach to the running console; detach with Ctrl+B, then D.'
    echo 'This command never starts or restarts Elysium.'
    exit 0 ;;
  heartbeat) exec bash "$ROOT/scripts/elysia_heartbeat_attach.sh" ;;
  main|status) ;;
  *) echo 'Usage: elysia-attach [main|heartbeat|status]' >&2; exit 2 ;;
esac

SERVICE_SOCKET=/run/elysium/tmux.sock
FOUND=0
TMUX_ARGS=()
if tmux -S "$SERVICE_SOCKET" has-session -t '=elysium' 2>/dev/null; then
  FOUND=$((FOUND + 1))
  TMUX_ARGS=(-S "$SERVICE_SOCKET")
fi
if tmux -L elysium has-session -t '=elysium' 2>/dev/null; then
  FOUND=$((FOUND + 1))
  TMUX_ARGS=(-L elysium)
fi
if [[ $FOUND -gt 1 ]]; then
  echo 'Multiple Elysium consoles found; refusing to choose an instance.' >&2
  exit 1
fi
if [[ "$MODE" == status ]]; then
  systemctl show elysium.service --property=LoadState,ActiveState,MainPID,Restart,UnitFileState
  if [[ $FOUND -eq 1 ]]; then
    tmux "${TMUX_ARGS[@]}" list-panes -t '=elysium' -F 'console=#{session_name} pid=#{pane_pid} command=#{pane_current_command}'
  else
    echo 'No running Elysium console.'
  fi
  exit 0
fi
if [[ $FOUND -ne 1 ]]; then
  echo 'Elysium is not running. Start it explicitly; attach does not start it.' >&2
  exit 1
fi
if [[ ! -t 0 || ! -t 1 ]]; then
  echo 'An interactive terminal is required. Use elysia-attach in your Spark terminal.' >&2
  exit 1
fi
exec tmux "${TMUX_ARGS[@]}" attach-session -t '=elysium'
