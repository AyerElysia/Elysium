#!/usr/bin/env bash
# Elysium 启动包装：在 tmux 会话内运行，退出后 10 秒自动重拉
cd /root/Elysia/Elysium
while true; do
    .venv/bin/python main.py
    code=$?
    echo ""
    echo "[run_elysium] Elysium 退出（code=$code），10 秒后自动重启..."
    sleep 10
done
