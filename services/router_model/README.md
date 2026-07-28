# 路由决策模型服务

本地常驻的小模型服务，专门做"此刻要不要开口"的路由判断。

## 为什么要有它

路由判断（`should_respond`）发生在每条消息的关键路径上。此前它走 `sub_actor` 任务，
实际回退到一批**前沿大模型**（grok / claude / gpt-5.6 …）走网络 API，是延迟的主要来源。

换成**本地小模型**后：
- 去掉网络往返与前沿大模型推理延迟（裸推理稳态约 1-1.6s）
- 本地化，不依赖外部 API 可用性
- **主体性不变**：路由提示词仍然站在主体自己的视角判断"此刻开口是否自然"，而非机械硬规则。
  换模型只是去掉延迟，不牺牲主体性。

## 模型

**Qwen3-4B-Instruct-2507**（2026 同级别最强小模型之一，中文优秀），bitsandbytes NF4 量化后权重约 2.7GB。

选 4B 而非更小：路由需要社交分寸判断（是否被叫到、话题是否相关、此刻开口是否自然），
4B 的分寸判断明显优于 1.7B/0.6B，同时在 5090 上依然很快。

## 显存策略

路由模型**常驻**（不像视觉 embedding 那样按需）——因为路由在每条消息的关键路径上，
按需加载会给每条首消息增加数秒延迟，违背"最低时延"目标。

NF4 量化后约 2.7GB 权重 + 约 3-4GB KV cache，`--gpu-memory-utilization 0.35`，
即使与训练等其他 GPU 任务共存也无压力。

## 部署

```bash
cd services/router_model
./start.sh                # 建独立 venv + 装依赖 + 下载模型 + 起服务（常驻，端口 8849）
./start.sh --setup-only   # 只装环境 + 下模型
```

模型下载到 `/root/models/Qwen3-4B-Instruct-2507`（modelscope 优先）。

### 依赖版本注意（重要）

系统 NVIDIA 驱动为 **CUDA 12.8**，因此：
- 必须用 **cu128** 的 torch（vllm 0.26 默认拉 cu13，需 CUDA 13 驱动，不兼容）
- 锁定 **vllm 0.10.2**（配 torch 2.8 cu128）
- **transformers 必须 4.x**（vllm 0.10.2 与 transformers 5.x 的 tokenizer API 不兼容）
- bitsandbytes 做 NF4 量化

这些已在 `requirements.txt` 锁定。

## 集成

`config/model.toml` 中：
- `[api_providers]` 增加 `LocalRouter`（`http://127.0.0.1:8849/v1`）
- `[[models]]` 增加 `qwen3-4b-router`
- `[model_tasks.router]` → `model_list = ["qwen3-4b-router"]`

`plugins/life_engine/core/router.py` 的 `route_should_respond` 优先用 `router` 任务，
本地模型不可用时自动回退到 `sub_actor`（保证 robustness）。

## 验证

```bash
curl http://127.0.0.1:8849/v1/models
curl -X POST http://127.0.0.1:8849/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3-4b-router","messages":[{"role":"user","content":"hi"}],"max_tokens":20}'
```
