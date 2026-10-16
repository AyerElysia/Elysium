#!/usr/bin/env bash
# Elysium one-shot launcher. Starting and restarting the main process is a
# user-owned lifecycle action; an unexpected exit must remain visible.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/.env"
  set +a
fi
exec .venv/bin/python main.py
