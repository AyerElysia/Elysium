# 路由决策模型服务

本地常驻的小模型服务，专门做"此刻要不要开口"的路由判断。

## 为什么要有它

路由判断（`should_respond`）发生在每条消息的关键路径上。此前它走 `sub_actor` 任务，
实际回退到一批**前沿大模型**（grok / claude / gpt-5.6 …）走网络 API，是延迟的主要来源。

换成**本地小模型**后：
- 去掉网络往返与前沿大模型推理延迟（本地裸推理中位 312ms，见下表）
- 本地化，不依赖外部 API 可用性
- **主体性不变**：路由提示词仍然站在主体自己的视角判断"此刻开口是否自然"，而非机械硬规则。
  换模型只是去掉延迟，不牺牲主体性。

## 模型

**Qwen3.5-2B**（注意仓库名没有 `-Instruct` 后缀，写了会 404），bf16 权重约 4.55GB
（单分片）+ vLLM 启动时**现场 fp8 量化**，权重显存约 2.1GB。

从 Qwen3-4B-Instruct-2507 换过来的理由是延迟：同一段 928-token prompt、50 token 输出，

| 模型 | 中位延迟 | 每 token |
| --- | --- | --- |
| Qwen3-4B（GPU 被 SDXL 训练占满时测） | 3869 - 4578ms | 84.5ms |
| Qwen3.5-2B（GPU 空闲时测） | 312.7ms | 6.3ms |

两个数不是同一条件下测的（训练进程后来退了），所以别把 12× 当作纯模型收益。
但即使只看 Qwen3-4B 在空闲时约 1-1.6s 的稳态，2B 仍然快一个量级，
而路由判断只需要"此刻要不要开口"这一个判断，2B 的社交分寸足够。

Qwen3.5-2B 是**混合注意力**：24 层里只有 6 层是 `full_attention`（`full_attention_interval=4`），
其余 18 层是 `linear_attention`，不吃 per-token KV。它也是**多模态**模型
（`config.json` 里有 `vision_config`、`image_token_id=248056`），但 router 只处理文本，
所以启动时用 `--limit-mm-per-prompt '{"image":0,"video":0}'` 直接关掉两个模态，
省掉 profiling 阶段为视觉分支预留的激活显存。日志里会打
`All limits of multimodal modalities supported by the model are set to 0, running in text-only mode.`

**为什么是 fp8 而不是原来的 NF4**：NF4 省的是显存，不是时间。vLLM 里 bitsandbytes 走反量化
路径，每次 forward 都要把 4bit 权重解回 bf16 再算，batch=1 的小模型场景这笔开销占比很大。
fp8 在 Blackwell(sm_120) 上是原生数据类型，w8a8 直接算；权重比 NF4 大一点，但延迟低得多。
本服务的目标是低延迟，不是显存最小。

而且 vLLM 支持对 bf16 权重**加载时现场量化**（`Fp8Config.is_checkpoint_fp8_serialized=False`），
所以不用另外下 FP8 仓库，现有 bf16 目录直接用。

## 显存策略

路由模型**常驻**（不像视觉 embedding 那样按需）——路由在每条消息的关键路径上，
按需加载会给每条首消息增加数秒延迟，违背"最低时延"目标。

`--gpu-memory-utilization` 是**占显卡总显存的比例，不是占空闲显存的比例**。原来硬编码 0.35，
在这张 24463MiB 的卡上就是要 8562MiB；而卡上还有训练等其他进程，空闲常常只剩 7GB，
vLLM 一启动就报 KV cache 不足直接退出。现在 `start.sh` 按 `nvidia-smi` 的**当前空闲显存
反算比例**，并预留 `ROUTER_VRAM_RESERVE_MIB`（默认 512MiB）给别人；不足 3072MiB 时直接
报错退出并提示换更小的模型，而不是让 vLLM 吐一屏栈。

### KV cache 大小是自己算的，不让 vLLM 猜

`start.sh` 从 `config.json` 直接算每 token 的 KV 字节数，然后用 `--kv-cache-memory-bytes`
**钉死**这个值。Qwen3.5-2B 的混合注意力只有 6 层吃 KV：

    6 层 × 2 KV head × 256 head_dim × 2(K+V) × 2(bf16) = 12288 B/token

比 Qwen3-4B 的 144KiB/token 省了一个数量级（那是 36 层 × 8 KV head）。

**为什么必须钉死**：显卡是共享的（还跑着 TTS 和有时的 SDXL 训练）。vLLM 默认靠
`determine_available_memory()` 跑一遍 profiling 再取显存差值，别的进程在同时分配显存时
这个差值会被算坏，出现过
`Available KV cache memory: -11.35 GiB` → `ValueError: No available memory for the cache blocks`。
钉死之后 vLLM 会打一行「skipped memory profiling」，走确定性路径。

默认再用 `ROUTER_KV_CACHE_MAX_CONTEXTS`（默认 8）把 KV 封顶到 8 个满上下文（约 769MiB）。
router 并发就是 1-2，没必要占着几个 G 不放。想吃满：`ROUTER_KV_CACHE_MAX_CONTEXTS=0`。

**不要开 `--kv-cache-dtype fp8`。** 这张卡上直接起不来：vLLM 会打
`Cannot use FlashAttention backend for FP8 KV cache` 然后退到 xformers 的 Hopper
flash-attn kernel，那个 kernel 在 sm_120 上崩
`CUDA error (flash_fwd_launch_template.h:188): invalid argument`。
fp8 只用在权重上（那条路径没问题），KV cache 保持 bf16。

## 部署

**常驻务必用 systemd。** `start.sh` 是前台 `exec`，跟着终端一起死——这正是之前 8849
莫名连不上、路由报 `Connection error` 的原因。

```bash
cd services/router_model
sudo ./install_service.sh   # 装 systemd unit + 开机自启 + 立刻启动（推荐）
./healthcheck.sh            # 查健康状态并实测一次真实推理延迟
```

手动/调试用法：

```bash
./start.sh                # 前台起服务（终端关了就没了）
./start.sh --setup-only   # 只装环境 + 下模型
./start.sh --print-args   # 只打印显存预算和将要用的 vllm 参数，不起服务
```

常用运维命令：

```bash
systemctl status router-model
journalctl -u router-model -f
tail -f /root/Elysia/Elysium/logs/router_model.log
```

模型下载到 `/root/models/Qwen3.5-2B`。

### 依赖版本注意

**别用 `pip install -r requirements.txt` 一把装。** 照着 `install_env.sh` 跑。
torch 必须是 cu128 轮子、vllm 必须是 cu129 轮子，两者都不在 PyPI 默认索引里。

系统 NVIDIA 驱动为 **CUDA 12.8**（`nvidia-smi` 报 Driver 572.90 / CUDA 12.8，
`cuDriverGetVersion()` 返回 12080）。版本锁定链：

1. `Qwen3_5ForConditionalGeneration` 最早在 **vllm 0.20.2** 注册（旧版直接报架构不支持）
2. vllm 0.20.2 硬性要求 **torch 2.11.0 / torchvision 0.26.0 / torchaudio 2.11.0**
3. 驱动是 12.8，所以 torch 三件套只能用 **+cu128** 本地版本号的轮子
4. vllm 用 GitHub release 里的 **0.20.2+cu129** 轮子，且必须 `--no-deps`

**为什么是 `vllm-0.20.2+cu129` 而不是 PyPI 上的 `vllm-0.20.2`**：PyPI 那个是 CUDA 13 编的，
8 个扩展 `.so` 的 `DT_NEEDED` 写着 `libcudart.so.13`，`import vllm._C` 直接炸
`ImportError: libcudart.so.13: version 'libcudart.so.13' not found`。
软链 `libcudart.so.13 -> libcudart.so.12` **没用**——动态链接器校验 ELF 里记的 SONAME，
不是文件名。改 `DT_NEEDED` 也不该干：即使宿主端符号补齐，CUDA 13 编出来的 device cubin
在 12.8 驱动上仍然加载不了（跨大版本无兼容性保证）。GitHub release 的 `+cu129` 轮子
扩展链的是 `libcudart.so.12`，而 12.9 与 12.8 同属 12.x，受 minor version compatibility 保护。

`--no-deps` 是**必须的**：那个轮子把 torch 钉成 `torch==2.11.0`（没带本地版本号约束），
不加 `--no-deps` 的话 pip 会把 `2.11.0+cu128` 换成 PyPI 的 cu130 构建。

### 必须指定 `--attention-backend TRITON_ATTN`

cu129 轮子里的 `_vllm_fa2_C`（flash-attn v2）**只带 sm_80 的 cubin 和 PTX**
（`cuobjdump --list-elf` 只有 sm_80），所以在 sm_120 上跑要靠驱动 JIT 那段 PTX。
但那段 PTX 是 12.9 工具链产的，12.8 驱动拒收：

    torch.AcceleratorError: CUDA error: the provided PTX was compiled with an unsupported toolchain.

`_vllm_fa3_C` 是 sm_90a 独占，也用不上。vllm 自己的主扩展 `_C` 反而带了真正的 sm_120 cubin，
所以除注意力以外的一切都正常——崩的位置很具体，在 `flash_attn_varlen_func`。

解法是换注意力后端：`TRITON_ATTN` 的 kernel 由 Triton 在运行时用 **torch 自带的 ptxas**
（cu128）编译，完全绕开预编译 PTX。它支持 `head_size >= 32`，Qwen3.5-2B 的 256 没问题。
`start.sh` 默认就传 `--attention-backend TRITON_ATTN`，systemd unit 里也钉了同一个值。

**注意 0.20.2 删掉了 `VLLM_ATTENTION_BACKEND` 环境变量**，改成 `--attention-backend`
（或 `--attention-config.backend`）。照旧设环境变量不会报错，只会被静默忽略然后崩在 PTX 上。

### flashinfer

`flashinfer-python 0.6.8.post1` 是 vllm 0.20.2 的硬依赖，启动时会打一行

    Failed to get device capability: SM 12.x requires CUDA >= 12.9.

在 cu128 torch 下这**只是警告**（它比对的是 `torch.version.cuda`，不是驱动），之后仍能正确
识别 `(12, 0)`，不影响启动。**回滚陷阱**：要退回 vllm 0.10.2 的话必须先
`pip uninstall flashinfer-python flashinfer-cubin`——旧版 vllm 下同一条检测会变成致命的
`RuntimeError: FlashInfer requires GPUs with sm75 or higher`。

### 其他两个坑

- **transformers 4.57.6 不认识 `qwen3_5`**，这是正常的：vllm 自带
  `Qwen3_5Config` / `Qwen3_5TextConfig`（在 `vllm/transformers_utils/configs/`），
  不依赖 transformers 认这个 `model_type`。
- **别用 `rm -rf` 清 site-packages 里的包。** 删了 payload 但留下 `.dist-info`，
  pip 会认为「Requirement already satisfied」而拒绝重装（`--force-reinstall` 也静默失效），
  表现为运行时 `ImportError: libxxx.so: cannot open shared object file`。一律用 `pip uninstall`。
- **PyTorch 轮子只有阿里云源能用**：`https://mirrors.aliyun.com/pytorch-wheels/cu128`
  实测 25-38MB/s；`download.pytorch.org` 只有 98KB/s，清华和南大是 404。

### 下载源

`download_model.py` 默认顺序是 **modelscope 优先、hf 镜像（hf-mirror.com）兜底**，
用 `--source` / `ROUTER_DOWNLOAD_SOURCE` 可改。ModelScope 现在是通的，拉 4.55GB 单分片
比走 hf 镜像稳；`huggingface.co` 官方域的 `resolve/main` 依然不可达，别指望它。

仓库名是 `Qwen/Qwen3.5-2B`，**没有 `-Instruct` 后缀**，加了会 404。

完整性检查按 `model.safetensors.index.json` 的分片清单逐个核对，不再只看 `config.json`
（下载中断时 `config.json` 早就落地了，权重却缺一半，等 vLLM 起来才报错）：

```bash
python download_model.py --check-only    # 只校验，完整退 0
python download_model.py --force         # 强制重下
```

## 集成

`config/model.toml` 中：
- `[api_providers]` 增加 `LocalRouter`（`http://127.0.0.1:8849/v1`）
- `[[models]]` 增加 `qwen3.5-2b-router`
- `[model_tasks.router]` → `model_list = ["qwen3.5-2b-router"]`

三处名字（`start.sh` 的 `ROUTER_SERVED_NAME`、`[[models]]` 的 `model_identifier` 与 `name`、
`model_list`）必须完全一致，否则请求会以 `LLMAPIError: Connection error.` 的形式失败，
而 `router.py` 会静默回退——只变慢，不报错。

`plugins/life_engine/core/router.py` 的 `route_should_respond` 优先用 `router` 任务，
本地模型不可用时自动回退到 `sub_actor`（保证 robustness）。

路由服务挂了不会让消息处理失败：`router.py` 的所有兜底分支都 `return {"should_respond": True}`。
代价纯粹是延迟——也就是这个服务存在的全部意义。所以"服务没起来"不会报错，只会悄悄变慢，
必须靠 `healthcheck.sh` 主动查。

## 验证

```bash
./healthcheck.sh          # 一把梭：/health + /v1/models + 实测一次推理延迟
```

手动：

```bash
curl http://127.0.0.1:8849/v1/models
curl -X POST http://127.0.0.1:8849/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.5-2b-router","messages":[{"role":"user","content":"hi"}],"max_tokens":20}'
```

## 可调环境变量

`start.sh` 与 systemd unit 共用这些（unit 里改完要 `systemctl daemon-reload`）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ROUTER_MODEL_PATH` | `/root/models/Qwen3.5-2B` | 模型本地路径 |
| `ROUTER_PORT` | `8849` | 服务端口 |
| `ROUTER_SERVED_NAME` | `qwen3.5-2b-router` | API 模型名，**须与 `config/model.toml` 一致** |
| `ROUTER_QUANT` | `fp8` | 量化方式；`bitsandbytes` / `awq_marlin` / `none` |
| `ROUTER_ATTENTION_BACKEND` | `TRITON_ATTN` | 注意力后端。**别改成 `FLASH_ATTN`**，见上文 PTX 那节 |
| `ROUTER_MAX_LEN` | `8192` | 上下文长度 |
| `ROUTER_MAX_SEQS` | `4` | 最大并发序列；路由是 batch=1 场景 |
| `ROUTER_MAX_BATCHED_TOKENS` | = `ROUTER_MAX_LEN` | 单批最大 token 数 |
| `ROUTER_VRAM_RESERVE_MIB` | `512` | 留给其他进程的显存 |
| `ROUTER_VRAM_BUDGET_MIB` | 自动 | 手动指定预算，跳过「空闲 - 预留」反算 |
| `ROUTER_KV_CACHE_MIB` | 自动 | 直接指定 KV cache 大小，跳过「预算 - 权重 - 开销」反算 |
| `ROUTER_KV_CACHE_MAX_CONTEXTS` | `8` | KV 封顶为几个满上下文；`0` = 不封顶、吃满预算 |
| `ROUTER_OVERHEAD_MIB` | `1536` | CUDA context + 激活峰值 + CUDA graph 的预留 |
| `ROUTER_KV_CACHE_DTYPE` | 不指定（bf16） | **本卡上设 `fp8` 会崩**，见上文 KV cache 那节 |
| `ROUTER_EAGER` | `0` | 设 `1` 关掉 CUDA graph（省显存，牺牲延迟） |
