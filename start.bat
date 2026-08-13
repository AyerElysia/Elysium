@echo off
rem Elysium 启动入口
rem 先清理本机死进程残留的 writer 租约，避免启动时等待租约过期（最长 60s）
wsl -d Ubuntu-Old bash -c "bash /root/Elysia/Elysium/scripts/cleanup_leases.sh" 2>nul
cd /d %~dp0
uv run main.py
