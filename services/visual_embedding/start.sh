#!/usr/bin/env bash
# Qwen3-VL-Embedding-2B 视觉嵌入服务启动脚本。
#
# 用法：
#   ./start.sh              # 建 venv + 装依赖 + 下载模型 + 起服务
#   ./start.sh --setup-only # 只装环境不下模型不起服务
#
# 环境变量：
#   VISUAL_EMBED_MODEL_PATH  模型路径（默认 /root/models/Qwen3-VL-Embedding-2B）
#   VISUAL_EMBED_PORT        服务端口（默认 8848）
#   TORCH_CUDA_INDEX         torch CUDA wheel 源（默认 cu128，适配 RTX 5090）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"
MODEL_PATH="${VISUAL_EMBED_MODEL_PATH:-/root/models/Qwen3-VL-Embedding-2B}"
PORT="${VISUAL_EMBED_PORT:-8848}"
TORCH_CUDA_INDEX="${TORCH_CUDA_INDEX:-https://download.pytorch.org/whl/cu128}"

echo "==> 视觉嵌入服务目录: $SCRIPT_DIR"
echo "==> 模型路径: $MODEL_PATH"
echo "==> 端口: $PORT"

# 1. 创建独立 venv
if [ ! -d "$VENV_DIR" ]; then
    echo "==> 创建虚拟环境 .venv"
    python3 -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
if python -c "import fastapi, numpy, PIL, torch, uvicorn" 2>/dev/null; then
    echo "==> dependencies ready; skipping installation"
else
    python -m pip install --upgrade pip >/dev/null

# 2. 安装 torch CUDA 版（RTX 5090 / Blackwell 需 cu128）
echo "==> 安装 torch（CUDA: $TORCH_CUDA_INDEX）"
pip install torch torchvision --index-url "$TORCH_CUDA_INDEX"

# 3. 安装其余依赖
echo "==> 安装服务依赖"
pip install -r requirements.txt
fi

if [ "${1:-}" = "--setup-only" ]; then
    echo "==> 环境安装完成（--setup-only）"
    exit 0
fi

# 4. 下载模型
echo "==> 下载/校验模型"
if [ -f "$MODEL_PATH/config.json" ] && find "$MODEL_PATH" -maxdepth 1 -name '*.safetensors' -print -quit | grep -q .; then
    echo "==> model ready; skipping download"
else
    python download_model.py --local-dir "$MODEL_PATH"
fi

# 5. 启动服务
echo "==> 启动服务 (port=$PORT)"
exec python server.py --model-path "$MODEL_PATH" --port "$PORT"
