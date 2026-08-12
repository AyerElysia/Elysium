#!/bin/bash
# 后台增量同步：本地为主 → 远端 MySQL（仅补缺失，不覆盖）
# 用法：配置到 crontab，每 10 分钟跑一次

cd /root/Elysia/Elysium
export ELYSIUM_MYSQL_PASSWORD="1111"

LOG=/root/Elysia/Elysium/logs/sync_local_to_mysql.log
mkdir -p /root/Elysia/Elysium/logs

echo "=== $(date '+%Y-%m-%d %H:%M:%S') sync start ===" >> "$LOG"
timeout 300 .venv/bin/python scripts/sync_local_to_mysql.py >> "$LOG" 2>&1
echo "exit=$?" >> "$LOG"
