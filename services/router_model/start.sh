#!/usr/bin/env bash
# 路由决策模型服务启动脚本（vLLM，常驻）。
#
# 这个服务存在的唯一理由是「低延迟」：路由判定要是走公网大模型，
# 每条消息都得等一个 RTT + 排队。所以下面所有默认值都是按延迟调的，
# 不是按显存占用最小调的。改参数前请先看清注释里的取舍。
#
# 用法：
#   ./start.sh                # 建 venv + 装依赖 + 下载模型 + 前台起服务（端口 8849）
#   ./start.sh --setup-only   # 只装环境 + 下模型，不起服务
#   ./start.sh --print-args   # 只打印将要使用的启动参数与显存预算，不起服务（排查用）
#
# 常驻请用 systemd（前台 exec 跟着终端一起死，这是之前服务莫名消失的原因）：
#   ./install_service.sh      # 装 systemd unit 并开机自启
#
# 环境变量：
#   ROUTER_MODEL_ID     模型 ID（默认 Qwen/Qwen3.5-2B）
#   ROUTER_MODEL_PATH   模型本地路径（默认 /root/models/Qwen3.5-2B）
#   ROUTER_PORT         服务端口（默认 8849）
#   ROUTER_SERVED_NAME  API 模型名（默认 qwen3.5-2b-router，需与 config/model.toml 的 model id 一致）
#   ROUTER_QUANT        量化方式（默认 fp8；可选 bitsandbytes / awq_marlin / none）
#   ROUTER_MAX_LEN      上下文长度（默认 32768；SOUL.md+USER.md+memory 约 1.8 万 token，8192 放不下）
#   ROUTER_MAX_SEQS     最大并发序列（默认 4；路由是 batch=1 场景，小值省显存又省 graph 捕获时间）
#   ROUTER_MAX_BATCHED_TOKENS 每步 token 预算（默认跟 ROUTER_MAX_LEN 相同，且不允许更小）
#   ROUTER_VRAM_BUDGET_MIB  给本服务的显存预算（MiB）。默认按「当前空闲 - 预留」自动算
#   ROUTER_VRAM_RESERVE_MIB 预留给其他进程的显存（MiB，默认 512）
#   ROUTER_KV_CACHE_MIB 直接指定 KV cache 大小（MiB）。默认按「预算 - 权重 - 开销」算
#   ROUTER_KV_CACHE_MAX_CONTEXTS KV cache 封顶为几个满上下文（默认 2；32768 上下文时 2×384MiB=768MiB，与旧 8×96MiB 持平；0=不封顶）
#   ROUTER_OVERHEAD_MIB CUDA context/激活峰值/CUDA graph 的预留（MiB，默认 1536）
#   ROUTER_KV_CACHE_DTYPE KV cache 精度（默认不指定 = bf16；本卡上 fp8 会崩，见下方注释）
#   ROUTER_ATTENTION_BACKEND 注意力后端（默认 TRITON_ATTN；auto = 交给 vLLM 自选，本机会崩，见下方注释）
#   ROUTER_EAGER        设为 1 强制 --enforce-eager（显存实在不够时的兜底，会牺牲延迟）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"
MODEL_PATH="${ROUTER_MODEL_PATH:-/root/models/Qwen3.5-2B}"
PORT="${ROUTER_PORT:-8849}"
SERVED_NAME="${ROUTER_SERVED_NAME:-qwen3.5-2b-router}"
QUANT="${ROUTER_QUANT:-fp8}"
MAX_LEN="${ROUTER_MAX_LEN:-32768}"
MAX_SEQS="${ROUTER_MAX_SEQS:-4}"
VRAM_RESERVE_MIB="${ROUTER_VRAM_RESERVE_MIB:-512}"
ATTN_BACKEND="${ROUTER_ATTENTION_BACKEND:-TRITON_ATTN}"
# 每步能处理的 token 预算。vLLM 要求它 >= max-model-len，否则直接拒绝启动
# （长 prompt 会被切成多段 prefill，反而更慢）。所以跟着 MAX_LEN 走。
BATCHED_TOKENS="${ROUTER_MAX_BATCHED_TOKENS:-$MAX_LEN}"
if [ "$BATCHED_TOKENS" -lt "$MAX_LEN" ]; then
    echo "==> max-num-batched-tokens($BATCHED_TOKENS) < max-model-len($MAX_LEN)，抬到 $MAX_LEN"
    BATCHED_TOKENS="$MAX_LEN"
fi

MODE="${1:-}"

echo "==> 路由模型服务目录: $SCRIPT_DIR"
echo "==> 模型路径: $MODEL_PATH"
echo "==> 端口: $PORT  API 模型名: $SERVED_NAME"
echo "==> 量化: $QUANT  上下文: $MAX_LEN  最大并发: $MAX_SEQS"

# 1. 创建独立 venv
if [ ! -d "$VENV_DIR" ]; then
    echo "==> 创建虚拟环境 .venv"
    python3 -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# --print-args 只是排查用，不该顺手改环境
if [ "$MODE" != "--print-args" ]; then
    # 2. 安装 vLLM（自带 torch CUDA）
    # 已装好就整段跳过。systemd 拉起服务时这里每一步都要连网，
    # 而 set -e 会让任何一次网络抖动直接变成「服务起不来」。
    # 装过一次之后这些纯属风险，没有收益。
    #
    # 注意：不要在这里写 pip install -r requirements.txt。torch 要 +cu128 轮子、
    # vllm 要 GitHub 上的 +cu129 轮子，都不在 PyPI 默认索引里，硬装会把环境搞坏
    # （PyPI 的 vllm 是 CUDA 13 编的，import vllm._C 直接 ImportError）。
    # 安装逻辑统一放在 install_env.sh 里。
    if python -c "import vllm._C" 2>/dev/null; then
        echo "==> vLLM 已安装，跳过依赖安装（需重装请跑 ./install_env.sh）"
    else
        echo "==> 安装依赖（首次较慢，见 install_env.sh）"
        ./install_env.sh
    fi

    # 3. 校验模型（完整就不连网，缺文件才下载）
    echo "==> 校验模型"
    if python download_model.py --local-dir "$MODEL_PATH" --check-only; then
        :
    else
        echo "==> 模型不完整，开始下载"
        # 下载必须能连外网，这里临时解除 offline
        HF_HUB_OFFLINE=0 python download_model.py --local-dir "$MODEL_PATH"
    fi
fi

if [ "$MODE" = "--setup-only" ]; then
    echo "==> 环境与模型就绪（--setup-only）"
    exit 0
fi

# 4. 算显存预算
#
# 这段之前踩过两个坑，都记下来免得再犯：
#
#   坑 1：--gpu-memory-utilization 是「占总显存的比例」，不是「占空闲显存的比例」。
#         这张卡是和 bot / 训练共用的，别人常驻 15~16GB。原来硬编码 0.35
#         → 0.35 × 24463 = 8562MiB，比当时空闲的还多，vLLM 启动自检直接拒绝。
#
#   坑 2（真正的元凶）：util 算对了也没用。vLLM 用「加载前后的显存差值」推算
#         KV cache 能给多少：
#           non_kv_cache = 权重 + torch 峰值增量 + non_torch 增量
#           可用 KV      = util × 总显存 - non_kv_cache
#         其中 non_torch 增量 = (profile 后的已用显存) - (启动瞬间的已用显存)。
#         源码里写着这么一句假设：
#           "we assume that the other processes using the same GPU did not
#            change their memory usage during the profiling"
#         这张卡上这个假设不成立——加载权重 + torch.compile + profile 要 90 秒，
#         这期间 bot / 训练那边的显存一直在动（实测空闲值在 7226~9085MiB 之间晃）。
#         别人涨的那部分全被算成 vLLM 自己的 non_torch 增量，于是：
#           Available KV cache memory: -11.35 GiB
#           ValueError: No available memory for the cache blocks.
#         调 util 是治不了的：util 再大，free < util × total 又会被自检挡回来。
#
# 所以这里不走 vLLM 的自动推算，直接用 --kv-cache-memory-bytes 把 KV cache 尺寸
# 钉死。这个参数一给，vLLM 就跳过 profiling 那套差值计算（源码 determine_available_memory
# 开头就 return 了），共用显卡上的抖动再也影响不到它。
# 尺寸我们自己按模型几何算，比差值靠谱得多。
read -r VRAM_TOTAL VRAM_FREE VRAM_USED < <(
    nvidia-smi --query-gpu=memory.total,memory.free,memory.used --format=csv,noheader,nounits \
        | head -n1 | tr -d ','
)
echo "==> 显存: 总 ${VRAM_TOTAL}MiB / 空闲 ${VRAM_FREE}MiB / 他人占用 ${VRAM_USED}MiB"

# 每 token 的 KV cache 字节数，从模型 config.json 现算（换模型自动跟着变）：
#   层数 × KV head 数 × head_dim × 2(K+V) × 2(bf16)
KV_BYTES_PER_TOKEN=$("$VENV_DIR/bin/python" - "$MODEL_PATH/config.json" <<'PY'
import json, sys
c = json.load(open(sys.argv[1], encoding="utf-8"))
c = c.get("text_config", c)
layers = c["num_hidden_layers"]
kv_heads = c.get("num_key_value_heads") or c["num_attention_heads"]
head_dim = c.get("head_dim") or c["hidden_size"] // c["num_attention_heads"]
# 混合注意力模型（比如 Qwen3.5 的 full_attention_interval）只有一部分层是全注意力，
# 其余线性注意力层不吃 per-token KV。这里按全注意力层数折算。
interval = c.get("full_attention_interval")
if interval:
    layers = max(1, layers // interval)
print(layers * kv_heads * head_dim * 2 * 2)
PY
)
KV_MIB_PER_1K=$(( KV_BYTES_PER_TOKEN * 1024 / 1048576 ))
echo "==> KV cache: ${KV_BYTES_PER_TOKEN} B/token（每 1K 上下文约 ${KV_MIB_PER_1K}MiB）"

# 权重占用：按 safetensors 实际字节数算，fp8 权重量化后约折半
WEIGHTS_MIB=$("$VENV_DIR/bin/python" - "$MODEL_PATH" "$QUANT" <<'PY'
import pathlib, sys
total = sum(p.stat().st_size for p in pathlib.Path(sys.argv[1]).glob("*.safetensors"))
if sys.argv[2] in ("fp8", "bitsandbytes", "awq", "awq_marlin", "gptq", "gptq_marlin"):
    total *= 0.5 if sys.argv[2] == "fp8" else 0.3
print(int(total / 1048576) + 1)
PY
)

# 除权重和 KV cache 之外还要吃的：CUDA context、激活峰值、CUDA graph、NCCL buffer。
# 实测这个模型 4.19GiB 权重时这部分约 1.2~1.4GiB，给 1536 留点余量。
OVERHEAD_MIB="${ROUTER_OVERHEAD_MIB:-1536}"
# 至少要放得下一个完整上下文，否则长 prompt 直接调度失败
KV_MIN_MIB=$(( MAX_LEN * KV_BYTES_PER_TOKEN / 1048576 + 1 ))

if [ -n "${ROUTER_KV_CACHE_MIB:-}" ]; then
    KV_MIB="$ROUTER_KV_CACHE_MIB"
    echo "==> KV cache 预算: ${KV_MIB}MiB（来自 ROUTER_KV_CACHE_MIB）"
else
    if [ -n "${ROUTER_VRAM_BUDGET_MIB:-}" ]; then
        AVAIL_MIB="$ROUTER_VRAM_BUDGET_MIB"
        echo "==> 本服务显存预算: ${AVAIL_MIB}MiB（来自 ROUTER_VRAM_BUDGET_MIB）"
    else
        AVAIL_MIB=$(( VRAM_FREE - VRAM_RESERVE_MIB ))
        echo "==> 本服务显存预算: ${AVAIL_MIB}MiB（空闲 ${VRAM_FREE} - 预留 ${VRAM_RESERVE_MIB}）"
    fi
    KV_MIB=$(( AVAIL_MIB - WEIGHTS_MIB - OVERHEAD_MIB ))
    echo "==> KV cache 预算: ${KV_MIB}MiB（预算 ${AVAIL_MIB} - 权重 ${WEIGHTS_MIB} - 开销 ${OVERHEAD_MIB}）"

    # 「能给多少」不等于「该给多少」。router 是 batch=1 的短任务，
    # 撑死也就同时跑 MAX_SEQS 条，多出来的 KV cache 一分钱收益都没有，
    # 却是实打实从别人（bot / 训练）手里抢走的显存。
    # Qwen3.5-2B 只有 6 层全注意力，每 token 才 12KiB，按空闲显存算会算出
    # 五十多个满上下文——纯浪费。所以在这里封顶。
    KV_CAP_CONTEXTS="${ROUTER_KV_CACHE_MAX_CONTEXTS:-2}"
    if [ "$KV_CAP_CONTEXTS" -gt 0 ]; then
        KV_CAP_MIB=$(( MAX_LEN * KV_BYTES_PER_TOKEN * KV_CAP_CONTEXTS / 1048576 + 1 ))
        if [ "$KV_MIB" -gt "$KV_CAP_MIB" ]; then
            echo "==> KV cache 封顶到 ${KV_CAP_MIB}MiB（${KV_CAP_CONTEXTS} 个满上下文，够 router 用；余下还给其他进程）"
            echo "==> 想吃满显存：ROUTER_KV_CACHE_MAX_CONTEXTS=0 或直接 ROUTER_KV_CACHE_MIB=${KV_MIB}"
            KV_MIB="$KV_CAP_MIB"
        fi
    fi
fi

if [ "$KV_MIB" -lt "$KV_MIN_MIB" ]; then
    echo "!! KV cache 预算不足：${KV_MIB}MiB < 一个完整上下文所需 ${KV_MIN_MIB}MiB" >&2
    echo "!! 三条路，按推荐顺序：" >&2
    echo "!!   1) 缩上下文（router 判定用不了那么长）：ROUTER_MAX_LEN=4096 ./start.sh" >&2
    echo "!!   2) 换更小的模型：ROUTER_MODEL_ID=Qwen/Qwen3-1.7B ROUTER_MODEL_PATH=/root/models/Qwen3-1.7B ./start.sh" >&2
    echo "!!   3) 先腾显存（当前他人占用 ${VRAM_USED}MiB）" >&2
    exit 1
fi

KV_CACHE_BYTES=$(( KV_MIB * 1048576 ))
KV_TOKENS=$(( KV_CACHE_BYTES / KV_BYTES_PER_TOKEN ))
echo "==> KV cache 钉死为 ${KV_MIB}MiB ≈ ${KV_TOKENS} tokens（约 $(( KV_TOKENS / MAX_LEN )) 个满上下文）"

# util 在钉死 KV cache 后只剩一个作用：过 vLLM 的启动自检
# （free < util × total 就拒绝启动）。所以按空闲显存算，别超。
UTIL=$(awk -v f="$VRAM_FREE" -v r="$VRAM_RESERVE_MIB" -v t="$VRAM_TOTAL" 'BEGIN{
    u = (f - r) / t
    if (u > 0.95) u = 0.95
    if (u < 0.05) u = 0.05
    printf "%.3f", u
}')
echo "==> --gpu-memory-utilization $UTIL（钉死 KV cache 后此值只用于过启动自检）"

# 5. 启动 vLLM OpenAI 兼容服务（常驻）
#
# 这里每个参数都是冲着「低延迟」调的，和原来的配置有几处关键区别：
#
#   量化 fp8（原来是 bitsandbytes NF4）
#     NF4 省的是显存，不是时间：vLLM 里 bnb 走的是反量化路径，每次 forward 都要把
#     4bit 权重解回 bf16 再算，小模型 batch=1 的场景下这笔开销占比很大。
#     fp8 在 Blackwell(sm_120) 上是原生数据类型，w8a8 直接算，权重约 4GB，
#     比 NF4 大一点但快得多。而且 vLLM 支持加载时现场量化 bf16 权重，不用重新下模型。
#
#   不再 --enforce-eager
#     eager 关掉了 CUDA graph。router 是 batch=1 短输出，每步 kernel launch 的固定开销
#     占比极高，CUDA graph 正是消掉这部分的，对小模型收益最大。原注释说 eager 是
#     「避免 CUDA graph 额外开销」，反了：额外开销在启动时（多花几十秒 capture），
#     省的是每一次推理。常驻服务当然该换。
#
#   -cc '{"cudagraph_capture_sizes":[1,2,4]}'
#     只给小 batch 抓图。router 实际并发就是 1，抓那么多档白占显存、白等启动。
#     【0.20.2 变更】老的 --cuda-graph-sizes 1 2 4 被删了，现在要走 --compilation-config
#     （简写 -cc）里的 cudagraph_capture_sizes。写老参数会直接
#       vllm: error: unrecognized arguments: --cuda-graph-sizes 1 2 4
#     同批被删的还有 --swap-space（不再有这个概念）和 --disable-log-requests
#     （改成 --enable-log-requests，本来就 default: False，所以不用写）。
#
#   不要加 cudagraph_mode（也别加 ROUTER_CUDAGRAPH_MODE 这种开关）
#     曾经怀疑默认的 PIECEWISE 是延迟大头，想改成 FULL_DECODE_ONLY。实测 A/B 之后
#     结论相反，默认的反而更快（Qwen3-4B、GPU 被训练占着时测，min/中位/max）：
#       PIECEWISE（默认）      3869.5 / 4226.4 / 4578.0 ms
#       FULL_DECODE_ONLY       4308.3 / 4539.0 / 5160.3 ms
#     那次慢的真正原因是 GPU 在跟 SDXL 训练抢卡，不是抓图模式。训练退掉之后
#     同一套参数中位 312.7ms。所以这里保持默认，别再为此加参数。
#
#   --attention-backend TRITON_ATTN（ROUTER_ATTENTION_BACKEND 可改）
#     这条是本机必需的，不是调优。vLLM 官方只发 +cu129 / cu130 轮子，本机驱动是 12.8。
#     vllm 自己的 _C 扩展里带了真的 sm_120 cubin，所以加载/量化都没事；
#     但捆在里面的 vllm_flash_attn（fa2）只编了 sm_80 的 cubin + sm_80 PTX，
#     跑到 sm_120 上只能让驱动 JIT 那份 PTX，而 12.8 驱动不认 12.9 工具链产的 PTX：
#       torch.AcceleratorError: CUDA error: the provided PTX was compiled with
#       an unsupported toolchain.  (cudaErrorUnsupportedPtxVersion)
#     fa3 更不用想，它只有 sm_90a。
#     TRITON_ATTN 的 kernel 是运行时用 torch 自带 triton 的 ptxas（cu128）编的，
#     绕开了预编译 PTX 这条路，所以能跑。head_size 要求 >= 32，本模型 256 满足。
#     【注意】0.20.2 删掉了 VLLM_ATTENTION_BACKEND 环境变量，改成 --attention-backend。
#     等哪天有了 cu128 轮子或驱动升到 >= 12.9，可以 ROUTER_ATTENTION_BACKEND=FLASH_ATTN 试回去。
#
#   --limit-mm-per-prompt '{"image":0,"video":0}'
#     Qwen3.5-2B 是多模态模型（config 里有 vision_config、image_token_id 248056）。
#     不限制的话每个模态默认 999，vLLM profiling 阶段会按最坏情况给视觉分支预留激活显存，
#     在这张共享卡上纯属浪费。router 只处理文本，直接关掉两个模态。
#
#   --max-num-seqs $MAX_SEQS（默认 4）
#     router 不需要高吞吐，队列开大只会让单条请求排在别人后面变慢。
#
#   --max-num-batched-tokens = max(MAX_LEN, 2048)
#     这是「每一步能处理多少 token」的预算，不是吞吐上限。设得比 max-model-len 小，
#     长 prompt 的 prefill 就得切成好几步做，反而更慢；而且 vLLM 会直接拒绝启动
#     （SchedulerConfig: max_num_batched_tokens is smaller than max_model_len）。
#     router 的 prompt 带着历史，就是长 prompt，所以给到能一次 prefill 完的量。
#
#   KV cache 保持 bf16（不要开 --kv-cache-dtype fp8）
#     试过，这张卡上直接起不来。开 fp8 KV cache 后 vLLM 会打
#     「Cannot use FlashAttention backend for FP8 KV cache」并退到 xformers 的
#     Hopper flash-attn kernel，那个 kernel 在 sm_120 上崩：
#       CUDA error (flash_fwd_launch_template.h:188): invalid argument
#     所以 fp8 只用在权重上（那条路径没问题），KV cache 老老实实 bf16。
#     Qwen3.5-2B 是混合注意力：24 层里只有 6 层是 full_attention（layer_types 里
#     每 4 层一个），其余 18 层是 linear_attention，不吃 per-token KV。
#     所以每 token 只要 6 × 2 KV head × 256 head_dim × 2(K+V) × 2(bf16) = 12KiB，
#     8192 上下文约 96MiB —— 比 Qwen3-4B 的 144KiB/token 省了一个数量级。
#     真要再省这块，用 ROUTER_KV_CACHE_DTYPE 自己指定，但先确认新版 vLLM 修了这个。
ARGS=(
    "$MODEL_PATH"
    --served-model-name "$SERVED_NAME"
    --host 127.0.0.1
    --port "$PORT"
    --max-model-len "$MAX_LEN"
    --gpu-memory-utilization "$UTIL"
    --kv-cache-memory-bytes "$KV_CACHE_BYTES"
    --max-num-seqs "$MAX_SEQS"
    --max-num-batched-tokens "$BATCHED_TOKENS"
    -cc '{"cudagraph_capture_sizes":[1,2,4]}'
    --limit-mm-per-prompt '{"image":0,"video":0}'
    --enable-prefix-caching
)

# 注意力后端。auto 就是让 vLLM 自己挑（在这张卡上会挑到跑不了的 FLASH_ATTN，见上面注释）
if [ "$ATTN_BACKEND" != "auto" ]; then
    ARGS+=(--attention-backend "$ATTN_BACKEND")
fi

# 权重里已经带量化配置（比如直接下的 -FP8 仓库）就别再指定，交给 vLLM 自己认
if [ "$QUANT" != "none" ] && ! grep -q '"quantization_config"' "$MODEL_PATH/config.json" 2>/dev/null; then
    ARGS+=(--quantization "$QUANT")
fi
# KV cache dtype 默认不动（auto = bf16）。原因见上面的注释：本卡上 fp8 KV cache
# 会把 attention 后端从 FlashAttention 换成 xformers 的 Hopper kernel 然后崩。
if [ -n "${ROUTER_KV_CACHE_DTYPE:-}" ]; then
    echo "==> KV cache dtype: $ROUTER_KV_CACHE_DTYPE（手动指定，注意 sm_120 上 fp8 会崩）"
    ARGS+=(--kv-cache-dtype "$ROUTER_KV_CACHE_DTYPE")
fi
# 显存实在不够时的退路：ROUTER_EAGER=1 关掉 CUDA graph 省掉抓图那部分显存
if [ "${ROUTER_EAGER:-0}" = "1" ]; then
    echo "==> ROUTER_EAGER=1，禁用 CUDA graph（省显存，牺牲延迟）"
    ARGS+=(--enforce-eager)
fi

if [ "$MODE" = "--print-args" ]; then
    printf 'vllm serve'
    printf ' %q' "${ARGS[@]}"
    printf '\n'
    exit 0
fi

echo "==> 启动 vLLM 服务 (port=$PORT, quant=$QUANT, max_len=$MAX_LEN)"
exec vllm serve "${ARGS[@]}"
