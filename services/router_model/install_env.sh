#!/usr/bin/env bash
# 路由模型服务的环境安装脚本。
#
# 为什么不能直接 `pip install -r requirements.txt`：
#   torch 三件套要的是 +cu128 本地版本号的轮子，只在 PyTorch 索引/镜像里有；
#   vllm 要的是 GitHub release 里的 +cu129 轮子，PyPI 上那个是 CUDA 13 编的，
#   在本机 12.8 驱动上 `import vllm._C` 直接炸。两者都得指定来源，
#   requirements.txt 表达不了，所以单独写成脚本。背景见 requirements.txt 顶部注释。
#
# 用法：
#   ./install_env.sh            # 装齐（已装对版本会跳过）
#   ./install_env.sh --force    # 无视已装版本，重装
#
# 环境变量：
#   ROUTER_TORCH_INDEX  torch 三件套的索引地址（默认阿里云 cu128 镜像）
#   ROUTER_VLLM_WHEEL   vllm cu129 轮子地址（默认 GitHub release）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"
PY="$VENV_DIR/bin/python"

# 官方 download.pytorch.org 在本机只有 ~98KB/s（下 torch 要 2 小时），
# 阿里云镜像实测 25MB/s。清华和南大的 pytorch-wheels/cu128 都是 404，别改回去。
TORCH_INDEX="${ROUTER_TORCH_INDEX:-https://mirrors.aliyun.com/pytorch-wheels/cu128}"
VLLM_WHEEL="${ROUTER_VLLM_WHEEL:-https://github.com/vllm-project/vllm/releases/download/v0.20.2/vllm-0.20.2+cu129-cp38-abi3-manylinux_2_31_x86_64.whl}"

TORCH_VER="2.11.0+cu128"
VISION_VER="0.26.0+cu128"
AUDIO_VER="2.11.0+cu128"
VLLM_VER="0.20.2+cu129"

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

if [ ! -d "$VENV_DIR" ]; then
    echo "==> 创建虚拟环境 .venv"
    python3 -m venv "$VENV_DIR"
fi

# 装过就跳过。这个脚本可能被 start.sh 调用，而 start.sh 会被 systemd 拉起——
# 每次启动都联网重装等于把网络抖动变成「服务起不来」。
have_version() {
    "$PY" - "$1" "$2" <<'PY' 2>/dev/null
import importlib.metadata as md, sys
try:
    sys.exit(0 if md.version(sys.argv[1]) == sys.argv[2] else 1)
except md.PackageNotFoundError:
    sys.exit(1)
PY
}

need_torch=1
if [ "$FORCE" = "0" ] \
    && have_version torch "$TORCH_VER" \
    && have_version torchvision "$VISION_VER" \
    && have_version torchaudio "$AUDIO_VER"; then
    need_torch=0
fi

if [ "$need_torch" = "1" ]; then
    echo "==> 安装 torch 三件套 (cu128, 来源 $TORCH_INDEX)"
    # torch 轮子有 820MB，pip 直连经常在中途断（IncompleteRead），所以给足重试。
    "$PY" -m pip install \
        --index-url "$TORCH_INDEX" \
        --retries 10 --timeout 120 \
        "torch==$TORCH_VER" "torchvision==$VISION_VER" "torchaudio==$AUDIO_VER"
else
    echo "==> torch 三件套已是目标版本，跳过"
fi

if [ "$FORCE" = "1" ] || ! have_version vllm "$VLLM_VER"; then
    echo "==> 安装 vllm $VLLM_VER (cu129 轮子)"
    # --no-deps：vllm 的 metadata 写着 torch==2.11.0（不带本地版本号），
    # 让 pip 自己解会把我们装好的 +cu128 换成 PyPI 上的 cu130 版，
    # 而 cu130 torch 在 12.8 驱动上会报 "The NVIDIA driver on your system is too old"。
    # 所以先手工装好 torch，再 --no-deps 装 vllm。
    "$PY" -m pip install --no-deps --retries 10 --timeout 120 "$VLLM_WHEEL"
    echo "==> 补 vllm 的其余依赖（不含 torch 三件套）"
    "$PY" -m pip install --retries 10 --timeout 120 \
        "transformers>=4.56.0,!=5.0.*,!=5.1.*,!=5.2.*,!=5.3.*,!=5.4.*,!=5.5.0" \
        huggingface_hub modelscope hf-transfer
else
    echo "==> vllm 已是 $VLLM_VER，跳过"
fi

echo "==> 自检"
"$PY" - <<'PY'
import torch
print(f"torch {torch.__version__} cuda {torch.version.cuda}")
if not torch.cuda.is_available():
    raise SystemExit("!! torch 看不到 CUDA 设备")
# cu130 轮子装错时 is_available() 仍是 True，要真去建 context 才会暴露
# "The NVIDIA driver on your system is too old"，所以这里必须实际分配一次。
torch.zeros(8, device="cuda")
print(f"device {torch.cuda.get_device_name(0)} capability {torch.cuda.get_device_capability(0)}")

import vllm
print(f"vllm {vllm.__version__}")
import vllm._C  # noqa: F401  # cu13/cu12 装错的话这行会 ImportError
from vllm.model_executor.models.registry import ModelRegistry
arch = "Qwen3_5ForConditionalGeneration"
if arch not in ModelRegistry.get_supported_archs():
    raise SystemExit(f"!! {arch} 未注册，vllm 版本不对")
print(f"{arch} 已注册")

# 注意力后端自检。本卡是 sm_120，而 vllm 预编译的 fa2 扩展只带 sm_80 的 cubin/PTX，
# 那份 PTX 又是 CUDA 12.9 工具链编的，12.8 驱动会拒收：
#   CUDA error: the provided PTX was compiled with an unsupported toolchain
# 所以必须走 TRITON_ATTN（Triton 运行时用 torch 自带的 cu128 ptxas 现编）。
from vllm.v1.attention.backends.registry import AttentionBackendEnum
if not hasattr(AttentionBackendEnum, "TRITON_ATTN"):
    raise SystemExit("!! 这个 vllm 没有 TRITON_ATTN 后端，start.sh 会崩在 PTX 上")
cap = torch.cuda.get_device_capability(0)
if cap >= (12, 0) and (torch.version.cuda or "").startswith("12.8"):
    print("TRITON_ATTN 可用（sm_120 + cu128 必须用它，别用 FLASH_ATTN）")
else:
    print("TRITON_ATTN 可用")
PY

echo "==> 环境就绪"
echo
echo "提醒：启动必须带 --attention-backend TRITON_ATTN（start.sh 默认已带）。"
echo "      0.20.2 删掉了 VLLM_ATTENTION_BACKEND 环境变量，设它不会报错、只会被忽略。"
