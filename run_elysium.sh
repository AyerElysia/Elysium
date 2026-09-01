#!/usr/bin/env bash
# Elysium one-shot launcher. Starting and restarting the main process is a
# user-owned lifecycle action; an unexpected exit must remain visible.
set -euo pipefail

cd /root/Elysia/Elysium
exec .venv/bin/python main.py
