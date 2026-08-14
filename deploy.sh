#!/usr/bin/env bash
set -euo pipefail

deployment_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

if command -v python3.11 >/dev/null 2>&1; then
    deployment_python="$(command -v python3.11)"
elif command -v python3 >/dev/null 2>&1; then
    deployment_python="$(command -v python3)"
else
    echo "部署失败: 未找到 Python 3.11+" >&2
    exit 2
fi

exec "${deployment_python}" "${deployment_root}/scripts/deployment.py" "$@"
