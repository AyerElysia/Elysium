#!/usr/bin/env bash
# 已退役：进程 PID 或 owner 名称不足以证明当前 lease 的精确所有权。
# lease 过期只能由正式 acquire 事务根据数据库时间判定，不允许启动脚本抢占。
set -euo pipefail

printf '%s\n' \
    'cleanup_leases.sh 已退役，拒绝修改任何 writer lease。' \
    '无法同时证明 owner token、epoch、fencing token 和数据库时间时，人工清理同样不安全。' \
    '请保留 claim 并由 Elysium 的原子 acquire 路径在租约自然过期后接管。' >&2
exit 78
