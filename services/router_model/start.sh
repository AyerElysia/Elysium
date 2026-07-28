#!/usr/bin/env bash
# 路由决策模型服务启动脚本（vLLM，常驻）。
#
# 用法：
#   ./start.sh                # 建 venv + 装 vLLM + 下载模型 + 起服务（常驻，端口 8849）
#   ./start.sh --setup-only   # 只装环境 + 下模型，不起服务
#
# 环境变量：
#   ROUTER_MODEL_ID     模型 ID（默认 Qwen/Qwen3-4B-Instruct-2507）
#   ROUTER_MODEL_PATH   模型本地路径（默认 /root/models/Qwen3-4B-Instruct-2507）
#   ROUTER_PORT         服务端口（默认 8849）
#   ROUTER_SERVED_NAME  API 模型名（默认 qwen3-4b-router，需与 models.toml 的 model id 一致）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"
MODEL_PATH="${ROUTER_MODEL_PATH:-/root/models/Qwen3-4B-Instruct-2507}"
PORT="${ROUTER_PORT:-8849}"
SERVED_NAME="${ROUTER_SERVED_NAME:-qwen3-4b-router}"

echo "==> 路由模型服务目录: $SCRIPT_DIR"
echo "==> 模型路径: $MODEL_PATH"
echo "==> 端口: $PORT  API 模型名: $SERVED_NAME"

# 1. 创建独立 venv
if [ ! -d "$VENV_DIR" ]; then
    echo "==> 创建虚拟环境 .venv"
    python3 -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip >/dev/null

# 2. 安装 vLLM（自带 torch CUDA）
echo "==> 安装 vLLM（首次较慢）"
pip install -r requirements.txt

# 3. 下载模型
echo "==> 下载/校验模型"
python download_model.py --local-dir "$MODEL_PATH"

if [ "${1:-}" = "--setup-only" ]; then
    echo "==> 环境与模型就绪（--setup-only）"
    exit 0
fi

# 4. 启动 vLLM OpenAI 兼容服务（常驻）
# bitsandbytes NF4 量化：4B 权重压到约 2.7GB，常驻显存友好（与训练共存）
# --enforce-eager 避免 CUDA graph 额外开销；--max-model-len 8192 容纳人设+近期上下文
echo "==> 启动 vLLM 服务 (port=$PORT)"
exec vllm serve "$MODEL_PATH" \
    --served-model-name "$SERVED_NAME" \
    --port "$PORT" \
    --max-model-len 8192 \
    --gpu-memory-utilization 0.35 \
    --enforce-eager \
    --quantization bitsandbytes \
    --load-format bitsandbytes \
    --disable-log-requests
