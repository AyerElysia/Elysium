#!/usr/bin/env bash
# 启动 TTS 参考音频工作台。
# 注意：服务默认只监听 127.0.0.1，且没有任何鉴权，不要改成 0.0.0.0 或做端口转发。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$HERE/../.." && pwd)"

if [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
  PY="$PROJECT_ROOT/.venv/bin/python"
else
  PY="$(command -v python3)"
fi

cd "$PROJECT_ROOT"
exec "$PY" -m tools.tts_ref_studio.server "$@"
