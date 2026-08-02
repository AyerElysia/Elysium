# 生命引擎核心（Life Engine Core）

> 文档状态：权威文档；意识 Presence、World Projection 与 Perception Gateway 已落地。
> 代码位置：`plugins/life_engine/core/`（9387 行）+ `plugins/life_engine/service/core.py`（4500+ 行）。
> 本文是生命引擎运行态的权威文档；凡与本文冲突，以本文和当前代码为准。

---

## 0. 一句话定位

生命引擎核心是数字生命的**自主运行循环**：以固定间隔心跳驱动潜意识决策，以事件驱动的主意识表达层（LifeChatter）处理对外交互，两者共享同一主体的身份、记忆和认知，通过统一事件流保持时间连续性。

---

## 1. 总体架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                    LifeEnginePlugin（插件入口）                        │
│  注册工具、Action、Chatter、Router、Service                           │
├─────────────────────────────────────────────────────────────────────┤
│                    LifeEngineService（服务层）                         │
│  心跳循环 / 事件收集 / 状态持久化 / 子系统初始化                       │
├──────────────┬──────────────────┬───────────────────────────────────┤
│  潜意识层     │   主意识层        │   后台子系统                       │
│  (Heartbeat) │   (LifeChatter)  │                                   │
│              │                  │  - 记忆索引 (memory_index_loop)    │
│  定时心跳     │  事件驱动唤醒     │  - 学习系统 (LearningScheduler)    │
│  工具调用     │  多轮对话        │  - 思考流 (ThoughtStreamManager)   │
│  自主意向     │  工具+Action     │  - 冲动引擎 (ImpulseEngine)        │
│  休息/睡眠    │  多模态          │  - 好奇心 (CuriosityEngine)        │
├──────────────┴──────────────────┴───────────────────────────────────┤
│           统一事件流 (LifeEventBus) + SQLite Presence                  │
│  经历追加写入；实例生命周期以事务 outbox 进入同一不可变账本              │
├─────────────────────────────────────────────────────────────────────┤
│                    基础设施                                           │
│  LLM 内核 / 记忆系统 / 适配器(NapCat/Feishu) / 调度器 / 日志         │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. 心跳循环（潜意识层）

### 2.1 运行机制

心跳是生命引擎的自主运行脉搏，由 `LifeEngineService._heartbeat_loop()` 驱动：

```
start() → 初始化子系统 → 启动 _heartbeat_loop() + _memory_index_loop()
                              │
                              ▼ (每 N 秒)
                    ┌── 睡眠时段？──→ 跳过
                    │── 主动休息？──→ 跳过（检查点除外）
                    │
                    ▼
              _run_heartbeat_round()
                    │
                    ├── 记忆日衰减
                    ├── 收集后台智能体结果
                    ├── _prepare_heartbeat_context()  ← 装配 prompt
                    ├── _run_heartbeat_model()        ← LLM 调用 + 工具执行
                    ├── 记录模型回复
                    ├── 触发学习系统快环反思
                    └── 持久化状态
```

### 2.2 关键参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `heartbeat_interval_seconds` | 配置 | 心跳间隔 |
| `heartbeat_timeout_seconds` | 120 | 单次 LLM 调用超时 |
| LLM 总预算 | timeout×2+60 | 覆盖重试+降级 |
| `max_rounds_per_heartbeat` | 配置 | 单次心跳最大工具轮数 |
| `sleep_time` / `wake_time` | 配置 | 睡眠时段（HH:MM） |

### 2.3 心跳 Prompt 装配

`_prepare_heartbeat_context()` 构建注入 LLM 的上下文，包含：

| 段落 | 来源 | 说明 |
| --- | --- | --- |
| 系统人设 | soul.md 模板 | 身份、性格、边界 |
| 世界状态 | World Projection | 带来源 assertion、Presence 存在感和逐实例未确认 change；只进入当前 heartbeat turn |
| 待处理事件 | pending_events | 新消息、系统事件 |
| 好奇心牵引 | CuriosityEngine | 当前探索兴趣 |
| 最近聊天记录 | ChatStream | 近期对话上下文 |
| 最近文件修改 | Trace | workspace 变更 |
| 可发送目标 | SendTargets | 活跃聊天流 |
| life 事件流 | LifeEventBus | 新增显著事件 |
| 学习进展 | LearningScheduler | 技能目录+自我认知 |
| 思考流进展 | ThoughtStreamManager | 活跃思维线程 |
| 冲动/意向 | ImpulseEngine + AutonomyIntent | 待执行的自主计划 |

### 2.4 工具执行

心跳中 LLM 可调用所有已注册工具（`nucleus_*` 系列），执行后结果回注 LLM 继续推理，最多 `max_rounds_per_heartbeat` 轮。

### 2.5 空闲检测与休息

- 无工具调用（且无活跃思考流）→ `idle_heartbeat_count += 1`
- 仅调用 `nucleus_rest_heartbeat` → 同样计为空闲
- 空闲超阈值 → 触发警告/临界日志
- LLM 可主动调用休息工具进入 `self_pause`（定时跳过心跳）

### 2.6 睡眠与暂停

- **睡眠时段**：`sleep_time` ~ `wake_time` 期间完全跳过心跳
- **主动休息**：LLM 调用休息工具后设置 `self_pause_until`，期间跳过心跳，到期自动恢复
- **检查点**：长时间休息中每 N 分钟唤醒一次让 LLM 重评估

---

## 3. 主意识表达层（LifeChatter）

**文件**：`core/chatter.py`（4500+ 行）

### 3.1 定位

LifeChatter 是同一主体的**对外运行模式**——当有人发消息来、或自主意向到期要说话时，由 LifeChatter 接管表达。它与心跳共享身份和记忆，但有独立的 LLM 上下文链（长生命周期 payload）。

### 3.2 唤醒机制

```
消息到达 → 适配器 → ChatStream 收集
                        │
                        ▼
              route_should_respond()  ← 路由判断（本地小模型）
                        │
              should_respond = true
                        │
                        ▼
              LifeChatter.execute()  ← 主意识接管
```

路由判断使用 `router` 任务（本地小模型 qwen3-0.6b，低延迟），判断"这批新消息是否值得开口"。熔断器保护：本地模型连续失败后自动跳过，回退到 `agent` 任务。

### 3.3 全局运行态

LifeChatter 维护一个**全局 LLM 上下文**（`_GLOBAL_RUNTIME`），所有聊天流共享同一条 payload 链：

- 多流串行：通过 `_GLOBAL_RUNTIME_LOCK` 保证同一主意识不被并发写入
- 长生命周期：对话历史在 payload 中累积，不每次重建
- 上下文压缩：超过阈值时触发分层压缩（见 §5）

### 3.4 模型任务

```python
def _configured_primary_task_name(self) -> str:
    # 优先: chatter_task_name（独立配置）
    # 回退: task_name（共享配置）
    # 兜底: "expression"
```

当前配置：`chatter_task_name = "expression"`（grok-4.5 打头，情商+智商在线的多模态模型）。

### 3.5 多模态准入

主意识要求模型必须支持多模态：

```python
def _required_primary_modalities(self) -> set[str]:
    # 基础: {"text"}
    # + image（若 native_image/native_emoji 启用）
    # + video（若 native_video 启用）
    # + audio（若 native_audio 启用）
```

不满足模态要求的模型不会被选为主 request 模型。

### 3.6 Action 系统

LifeChatter 通过 Action（而非普通工具）执行对外表达：

| Action | 功能 |
| --- | --- |
| `LifeSendTextAction` | 发送文本消息（支持分段、打字延迟、多目标） |
| `LifeSendFileAction` | 发送文件 |
| `LifePassAndWaitAction` | 选择不回应，等待下一条消息 |
| `LifeThinkAction` | 记录思考快照（不发送） |
| `LifeRecordInnerMonologueAction` | 记录内心独白 |

### 3.7 工具调用

LifeChatter 内部可调用所有 `chatter_allow` 标记的工具，包括：
- 学习系统工具（`nucleus_reflect_now` 等）
- 记忆工具（`nucleus_search_memory` 等）
- 文件工具、Web 工具、子代理工具
- 媒体检视工具（`LifeInspectMediaTool`）

工具支持**并行执行**（`tool_parallel.py`）：多个独立工具调用可并发。

### 3.8 强制回复机制

当消息明确 @bot 或直接提问时，即使路由判断犹豫，系统也会强制回复（`_should_force_reply_for_unread_batch`），避免"已读不回"。

---

## 4. 消息路由（Router）

**文件**：`core/router.py`

### 4.1 路由决策链

```
新消息到达
    │
    ├── 强制回复条件？（@bot / 直接提问）──→ 直接唤醒 chatter
    │
    ▼
route_should_respond()
    │
    ├── 尝试 router 任务（本地小模型，2048 ctx）
    │       └── 失败 → 熔断器计数
    │
    ├── 回退 agent 任务（远程模型）
    │
    ▼
返回 SubAgentDecision { should_respond: bool, reason: str }
```

### 4.2 上下文预算控制

路由只需近期语境，严格限制输入：
- history 字符预算：max_context 的 ~15%（2048 → 300 字符）
- 未读消息按模型 max_context 裁剪
- 系统提示词硬守卫：保守 token 估计（1 字符 ≈ 1.5 token），超出直接截断

### 4.3 熔断器

本地 router 模型连续失败 N 次后熔断器打开，后续请求直接跳过 router 走 agent 任务，避免每条消息都等连接超时。

---

## 5. 上下文装配与压缩

### 5.1 三层 Prompt 结构

**文件**：`core/context_assembly.py`

| 层 | 名称 | 生命周期 | 内容 |
| --- | --- | --- | --- |
| Prefix | 前缀 | 稳定 | 身份(soul)、用户画像、记忆、工具规则 |
| Rolling | 滚动 | 累积 | 对话历史、新消息 |
| Suffix | 后缀 | 瞬时 | 本轮运行态（发送前追加，发送后立即剥离） |

### 5.2 上下文压缩

**文件**：`core/context_compaction.py`

当 Rolling 层超过阈值（默认 120K 字符）时触发分层压缩：

```
触发条件: total_chars > 120,000
目标: 压缩到 ~80,000 字符

策略:
1. 保留最近 N 组完整对话（DEFAULT_MIN_RECENT_GROUPS = 2）
2. 旧对话压缩为 summary（最大 12,000 字符）
3. 未闭合工具链始终保留
4. 旧媒体只写 descriptor，不保留 base64
```

压缩后的 summary 用 `<compressed_life_chatter_context>` 标签包裹，作为 USER payload 注入，明确标注"这是背景，不是新消息"。

---

## 6. 多模态处理

**文件**：`core/multimodal.py`

### 6.1 支持的模态

| 模态 | 格式 | 来源 |
| --- | --- | --- |
| 图片 | bmp/gif/jpeg/png/webp | 消息附件、表情包 |
| 语音 | mpeg/mp3/wav/x-wav | 消息附件 |
| 视频 | 配置启用 | 消息附件 |
| 表情 | 同图片 | QQ 表情消息 |

### 6.2 处理流程

```
消息附件 → iter_message_attachments() → plan_media() → build_native_content()
                                              │
                                              ▼
                                    PlannedMedia（去重、预算裁剪）
                                              │
                                              ▼
                                    LLM Content（Image/Audio/Video/Text）
```

- 去重：基于内容 hash（`media_dedup_key`）
- 预算：每类模态有最大数量限制（`max_images_per_payload` 等）
- 降级：模型不支持多模态时，媒体转为文本描述占位

---

## 7. 配置体系

**文件**：`core/config.py`

`LifeEngineConfig` 继承 `BaseConfig`，包含以下配置节：

| 节 | 职责 |
| --- | --- |
| `settings` | 心跳间隔、超时、睡眠时段、workspace 路径 |
| `model` | 潜意识任务名(`task_name`)、主意识任务名(`chatter_task_name`) |
| `chatter` | 对话器开关、模式、历史消息数、子代理、MCP |
| `multimodal` | 多模态开关、各模态预算 |
| `learning` | 学习系统参数（审计间隔、压缩触发等） |
| `streams` | 思考流参数（最大活跃数、衰减半衰期） |
| `drives` | 冲动引擎开关 |
| `curiosity` | 好奇心引擎参数 |
| `memory_index` | 记忆索引 worker 参数 |
| `memory_witness` | 见证意识参数 |
| `web` | Tavily 搜索 API 配置 |
| `media_observer` | 媒体观察者参数 |
| `minecraft` | MC 具身配置 |

配置文件：`config/plugins/life_engine/config.toml`

---

## 8. 子系统初始化

`LifeEngineService.start()` 按以下顺序初始化：

```
1. 加载持久化状态（_load_runtime_context）
2. 记忆集成（MemoryIntegration）
3. DFC 集成（DFCIntegration）
4. 思考流管理器（ThoughtStreamManager）
5. 冲动引擎（ImpulseEngine）
6. 三环自学习系统（LearningScheduler）
7. 启动心跳循环（_heartbeat_loop）
8. 启动记忆索引循环（_memory_index_loop）
```

---

## 9. 事件流与状态

### 9.1 统一事件流

兼容运行层仍使用 `LifeEngineEvent`，持久经历边界统一转换为 `LifeEvent` 并通过 `LifeEventBus` 写入 SQLite 追加式账本：

- 账本 ingest position 是耐久顺序，producer sequence 只作为来源字段保留；
- `occurrence_id` 提供幂等写入；
- 消息保留完整原文，兼容心跳展示文本可以单独缩短；
- `source_instance_id`、`causation_id`、`correlation_id` 和 `recorded_at` 保留实例归因与因果边界；
- 心跳通过 cursor 追踪已处理事件，新事件在下次 prepare 中形成固定快照；
- 显著性只负责技术注意预算，不能删除或改写原始经历。

意识实例生命周期先在 `consciousness_presence.sqlite3` 中原子提交 Presence、stream owner 和 outbox，再幂等写入 Life Event。账本写入失败时 outbox 不确认，后续可重试。

### 9.2 状态持久化

`StatePersistence` 负责将运行态写入 workspace：
- 心跳计数、空闲计数
- 事件游标、待处理事件
- 对话历史
- 自主意向状态

意识实例运行状态不再由 `StatePersistence` 或 JSON 负责。`SQLitePresenceStore` 单独保存：

- instance/session/process epoch；
- active/suspended/terminated 与 lease；
- active stream 唯一 owner；
- revision/CAS 与生命周期 outbox。

`runtime/consciousness_registry.json` 仅是迁移期兼容导出；`runtime/consciousness_presence.sqlite3` 才是权威 Presence。`runtime/world_projection.sqlite3` 是从不可变 Life Event 重建的带来源派生投影；旧 `world_state.json` 只作为迁移源保留。

---

## 10. 文件索引

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `core/plugin.py` | ~120 | 插件入口，注册所有组件 |
| `core/config.py` | ~400 | 配置定义（Pydantic） |
| `core/chatter.py` | 4500+ | 主意识表达层（LifeChatter + Action） |
| `core/router.py` | ~350 | 消息路由判断 + 熔断器 |
| `core/context_assembly.py` | ~170 | 三层 Prompt 装配 |
| `core/context_compaction.py` | ~300 | 上下文分层压缩 |
| `core/multimodal.py` | ~400 | 多模态兼容层 |
| `core/chat_history.py` | ~200 | 对话历史管理 |
| `core/send_targets.py` | ~150 | 可发送目标管理 |
| `core/tool_parallel.py` | ~200 | 工具并行执行 |
| `core/sub_agent_tool.py` | ~300 | 子代理工具 |
| `core/compat_tools.py` | ~200 | 兼容旧接口工具 |
| `service/core.py` | 4500+ | 服务层（心跳循环、事件、状态） |
| `service/event_bus.py` | — | 不可变 Life Event SQLite 账本、消费游标与兼容镜像 |
| `service/consciousness.py` | — | 意识实例模型、Presence 生命周期、lease 与 outbox 发布 |
| `service/presence_store.py` | — | SQLite Presence、stream 唯一约束与 revision/CAS |
| `service/world_projection.py` | — | 带来源 assertion、投影 change、重建与逐实例 cursor |
| `service/perception_gateway.py` | — | active 窗口与世界变化的 transient prepare/commit/query |
