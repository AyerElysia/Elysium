#!/usr/bin/env bash
# 把路由模型服务装成 systemd unit（常驻 + 崩了自动拉起）。
#
# 为什么必须走 systemd：start.sh 是前台 exec，直接在终端里跑，
# 终端一关（或者 ssh 断线）服务就跟着没了。之前 8849 老是连不上、
# 路由 LLM 一直报 Connection error，就是这个原因。
#
# 用法：
#   ./install_service.sh            # 安装 + 开机自启 + 立刻启动
#   ./install_service.sh --no-start # 只安装，不启动
#
# 装完之后：
#   systemctl status router-model
#   journalctl -u router-model -f          # unit 自身日志
#   tail -f ../../logs/router_model.log    # vLLM 输出
#   systemctl restart router-model

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_NAME="router-model.service"
UNIT_SRC="$SCRIPT_DIR/$UNIT_NAME"
UNIT_DST="/etc/systemd/system/$UNIT_NAME"
LOG_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)/logs"

if [ "$(id -u)" -ne 0 ]; then
    echo "!! 需要 root（写 /etc/systemd/system）" >&2
    exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
    echo "!! 这台机器没有 systemd，改用 nohup 或 tmux 常驻" >&2
    exit 1
fi
if [ ! -f "$UNIT_SRC" ]; then
    echo "!! 找不到 unit 文件: $UNIT_SRC" >&2
    exit 1
fi

chmod +x "$SCRIPT_DIR/start.sh" "$SCRIPT_DIR/healthcheck.sh" 2>/dev/null || true
# unit 里用的是 append: 重定向，目录不存在 systemd 会直接启动失败
mkdir -p "$LOG_DIR"

echo "==> 安装 $UNIT_DST"
install -m 0644 "$UNIT_SRC" "$UNIT_DST"
systemctl daemon-reload
systemctl enable "$UNIT_NAME"

if [ "${1:-}" = "--no-start" ]; then
    echo "==> 已安装并设为开机自启（--no-start，未启动）"
    echo "==> 手动启动: systemctl start $UNIT_NAME"
    exit 0
fi

echo "==> 启动服务（冷启动要载 4.5GB 权重 + 现场 fp8 量化 + 编 triton kernel + 抓 CUDA graph）"
echo "==> 实测 init engine 约 110s（其中编译 47s），有编译缓存后会快很多"
systemctl restart "$UNIT_NAME"
sleep 3
systemctl status "$UNIT_NAME" --no-pager --lines 0 || true

echo
echo "==> 看日志:   journalctl -u $UNIT_NAME -f"
echo "==> 看 vLLM:  tail -f $LOG_DIR/router_model.log"
echo "==> 等就绪:   $SCRIPT_DIR/healthcheck.sh --wait"
