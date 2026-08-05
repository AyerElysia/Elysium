# LLM 内核（LLM Kernel）

> 文档状态：权威文档。
> 代码位置：`src/kernel/llm/` 与 `src/kernel/config/models_loader.py`。
> 本文是 LLM 基础设施的权威文档；凡与本文冲突，以本文和当前代码为准。

---

## 0. 一句话定位

LLM 内核是所有模型调用的统一基础设施：负责模型注册与任务路由、请求生命周期管理、负载均衡与故障转移、多模态能力验证、token 预算控制、指标监控，以及训练数据轨迹收集。上层（生命引擎、插件、工具）通过 `LLMRequest` 或快捷 `chat()/stream()` API 使用它，无需关心底层 provider 差异。

---

## 1. 总体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                      上层调用者                                    │
│  life_engine / plugins / tools / chat() / stream()               │
├─────────────────────────────────────────────────────────────────┤
│                      快捷 API 层  (api.py)                        │
│  chat() / stream() → _resolve_model_set() → LLMRequest           │
├─────────────────────────────────────────────────────────────────┤
│                      请求层  (request.py / response.py)           │
│  LLMRequest.send() → Policy Session → Provider Client            │
│  LLMResponse（awaitable + async iterator）                        │
├─────────────────────────────────────────────────────────────────┤
│                      策略层  (policy/)                            │
│  FailoverPolicy / LoadBalancedPolicy / RoundRobinPolicy           │
├─────────────────────────────────────────────────────────────────┤
│                      能力层                                       │
│  media_capabilities.py  token_counter.py  context.py              │
├─────────────────────────────────────────────────────────────────┤
│                      观测层                                       │
│  monitor.py (MetricsCollector)  trajectory_collector.py           │
├─────────────────────────────────────────────────────────────────┤
│                      Provider 客户端  (model_client.py)           │
│  OpenAI-compatible / Anthropic / Gemini / 本地                    │
└─────────────────────────────────────────────────────────────────┘
```

---

## 2. 模型注册与任务路由

### 2.1 单一生产路由权威

`config/models.toml` 是生产环境自动任务路由的唯一权威，定义 Provider、模型注册和每个任务的有序主备链。`config/models.toml.example` 是不含密钥的可提交基线。

旧 `config/model.toml` 只保留给显式迁移工具和独立旧 API，生产启动流程不再读取它；运行时组件、快捷 `chat()/stream()` 以及插件任务 API 全部直接使用 `models.toml`，不得在新注册表出错时静默切换到旧文件。缺文件、TOML 非法、未知字段、Provider/模型引用错误、重复候选、预算非法或缺少生产任务都会让启动显式失败。

### 2.2 解析链

```python
def _resolve_model_set(routing_name: str) -> list[dict]:
    # models.toml task → 按数组顺序返回主备链
    # models.toml registered model → 返回单模型集合
    # 未定义 → 显式失败，并报告安全的 snapshot digest
```

### 2.3 ModelEntry 结构

每个模型注册条目（`types.py`）：

```python
class ModelEntry(TypedDict):
    api_provider: str          # 提供商标识
    base_url: str              # API 端点
    model_identifier: str      # 模型名（如 "mimo-v2.5-pro"）
    api_key: str               # API 密钥
    client_type: str           # 客户端类型
    max_retry: int             # 最大重试次数
    timeout: float             # 超时（秒）
    retry_interval: float      # 重试间隔
    price_in: float            # 输入价格
    price_out: float           # 输出价格
    temperature: float         # 温度
    max_tokens: int            # 最大输出 token
    max_context: int           # 上下文窗口上限
    tool_call_compat: bool     # 工具调用兼容模式
    extra_params: dict         # 额外参数
    media_capabilities: dict   # 模态能力声明
    routing_task: str          # 规范任务名（任务路由时存在）
    routing_model_alias: str   # models.toml 中的模型键
    routing_priority: int      # 原始任务数组下标
    routing_snapshot: str      # 不含密钥的路由快照摘要
```

### 2.4 当前任务路由（models.toml）

| 任务 | 输出预算 | 模型列表（按主备序） |
| --- | ---: | --- |
| core | 32000 | gpt-5.6-luna, gpt-5.6-terra, gpt-5.6-sol, MiMo-V2.5-Pro, deepseek-v4-flash, gemini-3.5-flash, MiMo-V2.5, claude-sonnet-5 |
| expression | 32000 | gpt-5.6-luna, gpt-5.6-terra, gpt-5.6-sol, deepseek-v4-flash, MiMo-V2.5-Pro, gemini-3.5-flash, MiMo-V2.5, claude-sonnet-5 |
| witness | 16000 | gpt-5.6-luna, gpt-5.6-terra, gpt-5.6-sol, MiMo-V2.5, deepseek-v4-flash, gemini-3.5-flash |
| agent | 32000 | gpt-5.6-luna, gpt-5.6-terra, gpt-5.6-sol, MiMo-V2.5-Pro, deepseek-v4-flash, gemini-3.5-flash, MiMo-V2.5, claude-sonnet-5 |
| utility | 16000 | gpt-5.6-luna, gpt-5.6-terra, gpt-5.6-sol, deepseek-v4-flash, MiMo-V2.5, gemini-3.5-flash |
| vision | 16000 | gpt-5.6-luna, gpt-5.6-terra, gpt-5.6-sol, MiMo-V2.5, gemini-3.5-flash |
| voice | 8192 | sensevoice-small（非生成型，该上限不用于思考） |
| embedding | 8192 | bge-m3（非生成型，该上限不用于思考） |
| router | 8192 | gpt-5.6-luna, gpt-5.6-terra, gpt-5.6-sol, deepseek-v4-flash, MiMo-V2.5, gemini-3.5-flash（云端优先，保留思考） |
| router_context_projection | 16000 | gpt-5.6-luna, gpt-5.6-terra, gpt-5.6-sol, MiMo-V2.5, deepseek-v4-flash, gemini-3.5-flash（权威文件变化时生成派生投影） |
| live | 32000 | gpt-5.6-luna, gpt-5.6-terra, gpt-5.6-sol, deepseek-v4-flash, MiMo-V2.5-Pro, gemini-3.5-flash, MiMo-V2.5, claude-sonnet-5 |

生成型任务的 `tokens` 同时覆盖隐式思考与最终正文；Router 因此不再使用 200 token 的紧缩上限。上下文压缩触发线是输入窗口策略，与输出思考预算分开配置。

GPT 5.6 的生产优先级固定为 **Luna → Terra → Sol**：Luna 成本最低，Terra 与 Sol 依次作为同系列后备。当前三种模型已经通过本地中转站的模型列表、真实 completion、格式遵循、工具调用与重复转发探针，因此位于所有生成型任务链最前；MiMo、DeepSeek、Gemini 与 Claude 继续作为跨模型族回退。中转站内部只启用通过验收的 GPT 渠道做同模型冗余，固定失败或客户端不兼容的渠道不得为了“全部打开”而进入生产路由。

模型注册和一次验收都不代表永久健康。渠道增删、端点迁移或持续 5xx/超时出现后，必须重新执行端到端探针；确认故障的 ability 应退出生产路由，恢复后再按同一准入门槛启用。这样既保留 Luna 的成本优势，也不会让单条坏渠道成为每条生命消息的固定失败前置步骤。

GPT 5.6 的 `ctx=300000` 与 `context_compression_trigger_tokens=200000` 保持不变。触发线占窗口约三分之二，仍留下 100000 token 余量，足以覆盖当前最大 32000 token 的隐式思考/正文预算、工具结果增长和 tokenizer 估算误差。其他百万窗口模型沿用同一 200000 触发线是有意的稳定性取舍：让故障转移前后的输入投影保持在共同安全窗口内，并限制超长上下文的延迟与成本；它不是对模型最大窗口能力的声明。

### 2.5 验证、原子发布与可观测性

加载器先在局部变量中完成整份文件的结构和引用验证，全部通过后才原子替换全局注册表。显式重载失败时，上一代有效快照继续保留；启动阶段首次加载失败则终止启动，不存在部分路由。

快照内部不可变，并生成只依赖已启用任务链、任务预算与温度、模型能力以及 Provider 重试策略的摘要；API Key、端点和未启用模型不进入摘要或路由日志。启动日志输出一次完整任务优先链。每个任务条目携带 `routing_task`、`routing_priority` 和 `routing_snapshot`，选择日志与训练轨迹因此能够回答“配置首选是谁、实际选择谁、哪些模型因冷却被跳过”。启动预检只检查快照中自动任务实际引用的 Provider，不再检查旧配置或未启用 Provider。

---

## 3. 请求生命周期

### 3.1 LLMRequest

`request.py`（860+ 行）是核心请求对象：

```python
@dataclass
class LLMRequest:
    model_set: list[dict]       # 模型列表
    request_name: str           # 请求标识（用于监控/轨迹）
    payloads: list[LLMPayload]  # 消息载荷
    policy: Policy | None       # 重试策略
    trajectory_metadata: dict   # 轨迹元数据
```

### 3.2 send() 流程

```
send()
  │
  ├── 1. 验证 model_set
  ├── 2. 生成 request_id / trace_id
  ├── 3. 提取工具定义（_extract_tools）
  ├── 4. 多模态过滤（filter_model_set_for_media）
  ├── 5. 创建 Policy Session
  │
  ▼ 重试循环
  ├── 6. session.first() / session.next_after_error()
  ├── 7. 上下文预算裁剪（_maybe_trim_payloads_for_model）
  ├── 8. Provider Client 调用
  ├── 9. 成功 → record_success() → 返回 LLMResponse
  │       失败 → 分类异常 → 判断是否可重试 → 继续/抛出
  │
  ▼ 每次尝试
  └── 10. record_attempt() → 轨迹收集器
```

### 3.3 LLMResponse

`response.py` 返回的响应对象：
- **可 await**：`await response` 获取完整文本
- **可异步迭代**：`async for chunk in response` 流式获取
- **工具调用**：`response.call_list` 获取 ToolCall 列表
- **链式追加**：`response.add_payload()` 继续对话
- **自动追加**：`send(auto_append_response=True)` 将回复追加到上下文

### 3.4 上下文管理（LLMContextManager）

`context.py` 提供长生命周期上下文管理：
- **Reminder 机制**：注册动态提示词，每次 send 前自动注入/更新
- **Payload 验证**：确保消息序列合法（system 在前、tool_result 跟随 tool_call）
- **预算管理**：`reminder_bucket` 控制各提醒的字符配额

---

## 4. 策略层（Policy）

### 4.1 策略协议

```python
class Policy(Protocol):
    def new_session(self, *, model_set, request_name) -> PolicySession

class PolicySession(Protocol):
    def first(self) -> ModelStep           # 首次选择
    def next_after_error(self, error) -> ModelStep  # 失败后选择
    def record_success(self, *, latency, tokens)    # 成功反馈
```

`ModelStep.model = None` 表示策略耗尽，停止重试。

### 4.2 三种策略

| 策略 | 行为 | 适用场景 |
| --- | --- | --- |
| `FailoverPolicy` | 按列表顺序，失败切下一个 | 默认策略，简单可靠 |
| `LoadBalancedPolicy` | 评分选最优，失败施加惩罚 | 多模型任务（core/expression） |
| `RoundRobinPolicy` | 轮转 | 均匀分散 |

### 4.3 FailoverPolicy 冷却与恢复

可恢复的网络错误、超时、限流与普通 HTTP 5xx 会按 `request_name + provider + endpoint + model` 进入跨请求冷却。首次冷却为 30 秒，连续失败按 30/60/120/240/300 秒渐进退避，成功探测后立即清零。这使本地中转站的短暂 503 不会造成五分钟假宕机，又不会在上游持续故障时高频重试。鉴权、配置等永久错误不进入冷却，每次请求都会明确暴露。

本地 New API 返回结构化错误码 `system_cpu_overloaded`、`system_memory_overloaded` 或 `system_disk_overloaded` 时，故障范围是整个 `provider + endpoint`，不是某个模型。策略会对该网关建立跨 `request_name` 冷却，跳过所有仍指向同一中转端点的候选；只有配置了不同端点时才继续故障转移。原始状态码和错误码继续向上保留，周期任务据此延迟重试，不把暂时过载伪装成空结果。

正常请求仍从任务数组第一个模型开始。若前置模型处于冷却期，策略按原顺序选择第一个可用候选，并在 INFO 日志中记录任务、快照摘要、配置首选、实际选择、原始优先级和被跳过模型；无跳过的普通选择只记录 DEBUG。所有候选冷却异常同样携带任务与快照摘要，因此“没有从第一模型开始”始终可以追溯到具体路由代次和健康状态。

Conversation Router 的 `router → agent` 技术降级保留任务身份，但传输失败后不会再次请求 Provider、端点和模型 ID 都相同的候选。备用任务只有包含新的传输候选时才继续；如果前一任务只是返回空正文或非法决策结构而非传输失败，仍允许使用备用任务的不同预算重新判断。

开发测试与运行系统可能共处同一台 WSL 主机。默认 pytest worker 固定为 2，避免 `-n auto` 占满全部 CPU 并触发 New API 的资源保护；独立 CI 如需更高并行度可以在命令行显式覆盖。

### 4.4 LoadBalancedPolicy 评分算法

```
score = total_tokens
      + penalty × PENALTY_WEIGHT
      + (usage_penalty + request_count) × USAGE_PENALTY_WEIGHT
      + avg_latency × LATENCY_WEIGHT
```

- **total_tokens**：累计 token 用量（越多越不优先）
- **penalty**：失败惩罚（关键错误 ×3，服务器错误 ×2）
- **usage_penalty**：短期并发惩罚（选中时 +1，完成时 -1）
- **request_count**：历史请求数
- **avg_latency**：平均延迟

评分最低者被选中。无统计数据的模型获得最高优先级（评分 0）。

### 4.5 失败惩罚机制

| 错误类型 | 惩罚倍率 |
| --- | --- |
| NetworkConnectionError / TimeoutError | ×3（关键） |
| HTTPError / ServerError | ×2（服务器） |
| 其他 | ×1（默认） |

惩罚累积后模型被标记为 failed，从候选池移除。`should_retry_same_model()` 判断是否允许同模型重试。

---

## 5. 多模态能力验证

**文件**：`media_capabilities.py`

### 5.1 MediaCapabilities 合约

```python
class MediaCapabilities(TypedDict):
    modalities: list[str]              # 支持的模态 ["text", "image", "audio", "video"]
    accepted_mime_types: dict[str, list[str]]  # 各模态接受的 MIME 类型
    max_item_bytes: int | None         # 单项大小上限
    max_request_bytes: int | None      # 单次请求总大小上限
    max_count: int | None              # 最大附件数
    max_audio_seconds: float | None    # 音频时长上限
    max_video_seconds: float | None    # 视频时长上限
    wire_profile: str | None           # 传输协议
```

### 5.2 验证流程

```
请求含媒体附件
    │
    ├── extract_media_refs(payloads) → 提取所有 MediaRef
    ├── filter_model_set_for_media(model_set, media_refs)
    │       → 只保留声明支持对应模态的模型
    │
    ├── 无兼容模型 → 抛出 MediaLimitError
    │
    ▼ 有兼容模型
    normalize_media_capabilities(model.media_capabilities)
        → 严格验证，缺失能力默认 text-only（fail-closed）
```

---

## 6. Token 计数与预算控制

**文件**：`token_counter.py`

### 6.1 计数策略

| 优先级 | 方法 | 精度 |
| --- | --- | --- |
| 1 | tiktoken（`encoding_for_model`） | 精确 |
| 2 | tiktoken（`cl100k_base` 通用） | 近似 |
| 3 | Fallback（`len(text) // 2`） | 粗糙保底 |

### 6.2 上下文预算裁剪

`LLMRequest._maybe_trim_payloads_for_model()`：
- 计算模型有效上下文预算（`max_context` - 输出预留）
- 若 payload 总 token 超预算，从最早的消息开始裁剪
- 保护 system prompt 和最近消息

---

## 7. 监控与指标

**文件**：`monitor.py`

### 7.1 MetricsCollector

- **线程安全**：RLock 保护
- **内存 + JSON 双写**：`data/llm_metrics.json` 持久化
- **进程重启恢复**：启动时从 JSON 恢复历史
- **定期刷盘**：后台线程每 5 秒写入

### 7.2 收集的指标

每次请求记录（`RequestMetrics`）：
- model_name / request_name
- latency（延迟）
- tokens_in / tokens_out
- cost
- success / error / error_type
- stream / retry_count / model_index

### 7.3 模型统计（ModelStats）

按模型聚合：total_requests、success_rate、avg_latency、avg_cost、error_types 分布。

---

## 8. 轨迹收集

**文件**：`trajectory_collector.py` + `trajectory_types.py`

### 8.1 设计

- **Append-only JSONL**：`data/training_data_lake/raw/YYYY-MM-DD.jsonl`
- **纯文本**：所有媒体字节/data URL/本地路径一律 redact 为 `[removed]`
- **独立记录**：每行一个 JSON 文档，无跨行依赖
- **异步写入**：后台线程 + 队列，不阻塞请求

### 8.2 记录字段

```
schema_version, trace_id, attempt_id, request_id,
parent_attempt_id, timestamp, request_name, task_name,
task_tags, stream_id, heartbeat_run_id, call_id,
model, model_identifier, api_provider, policy_meta,
messages, response, tool_results, usage,
latency_s, success, error, error_type,
metadata, extensions
```

### 8.3 配置

通过环境变量或内核配置控制：
- 启用/禁用
- 基础路径（默认 `data/training_data_lake`）
- 刷盘间隔
- 队列上限

---

## 9. Embedding 与 Rerank

### 9.1 EmbeddingRequest

```python
@dataclass
class EmbeddingRequest:
    model_set: ModelSet
    inputs: list[str]      # 待向量化文本
    request_name: str
```

用于：记忆索引、语义匹配（学习系统）、向量检索。

### 9.2 RerankRequest

```python
@dataclass
class RerankRequest:
    model_set: ModelSet
    query: str             # 查询
    documents: list[Any]   # 待排序文档
    top_n: int | None      # 返回前 N
```

用于：检索结果重排序。

---

## 10. 快捷 API

**文件**：`api.py`

```python
# 单轮
resp = await chat("你好", model="expression")

# 流式
async for chunk in stream("写一首诗"):
    print(chunk.delta, end="")

# 多轮
resp = await chat([
    {"role": "system", "content": "你是助手"},
    {"role": "user", "content": "你好"},
])

# 带工具
resp = await chat("查天气", tools=[weather_schema])
```

返回 `ChatResponse`（text + tool_calls + usage + model）。

---

## 11. 文件索引

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `api.py` | ~300 | 快捷 API（chat/stream）+ 路由解析 |
| `request.py` | 860+ | LLMRequest 核心（send 循环、轨迹、裁剪） |
| `response.py` | ~420 | LLMResponse（awaitable、流式、链式） |
| `context.py` | ~430 | LLMContextManager（Reminder、验证） |
| `types.py` | ~50 | ModelEntry / RequestType 定义 |
| `monitor.py` | ~400 | MetricsCollector（指标收集+持久化） |
| `media_capabilities.py` | ~300 | 多模态能力验证 |
| `token_counter.py` | ~150 | Token 计数（tiktoken + fallback） |
| `trajectory_collector.py` | ~200 | 训练数据轨迹收集器 |
| `trajectory_types.py` | ~200 | 轨迹记录类型与 redact |
| `embedding_request.py` | ~100 | Embedding 请求 |
| `rerank_request.py` | ~100 | Rerank 请求 |
| `exceptions.py` | ~150 | 异常分类（可重试/不可重试） |
| `policy/__init__.py` | ~50 | 策略工厂 |
| `policy/base.py` | ~40 | Policy/PolicySession 协议 |
| `policy/failover.py` | ~90 | 故障转移策略 |
| `policy/load_balanced.py` | ~370 | 负载均衡策略（评分算法） |
| `policy/round_robin.py` | ~50 | 轮转策略 |
