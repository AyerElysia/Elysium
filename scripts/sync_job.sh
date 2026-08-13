#!/usr/bin/env bash
# 显式的一次性同步入口。本脚本不安装 cron、不持久化凭据、不自动重试。
set -euo pipefail

if [[ "${1:-}" != "--confirm-explicit-run" ]]; then
    printf '%s\n' \
        '拒绝同步：该操作必须由运维者显式发起。' \
        '用法: scripts/sync_job.sh --confirm-explicit-run --dry-run|--apply --confirm-database <name>' >&2
    exit 64
fi
shift

if [[ $# -eq 0 ]]; then
    printf '%s\n' '拒绝同步：必须显式选择 --dry-run 或 --apply。' >&2
    exit 64
fi

_ELYSIUM_SYNC_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
_ELYSIUM_SYNC_PROJECT_ROOT="$(cd -- "${_ELYSIUM_SYNC_SCRIPT_DIR}/.." && pwd -P)"
cd -- "${_ELYSIUM_SYNC_PROJECT_ROOT}"

exec uv run --frozen --no-sync python "${_ELYSIUM_SYNC_SCRIPT_DIR}/sync_local_to_mysql.py" "$@"
