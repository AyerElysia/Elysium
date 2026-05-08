# 附录 A · 术语表（Glossary）

> **用途**：为报告中出现的核心概念提供标准化定义，确保跨章节术语一致性。  
> **组织形式**：按概念层级分类，每条包含中文术语、英文对照与精确的工程定义。

---

## A.1 哲学层术语

| 中文术语 | English | 工程定义 |
|---------|---------|---------|
| 连续性 | Continuity | 即使在两次 LLM 调用之间，系统仍保持正在演化的内在状态，通过 `life_engine_context.json` 持久化实现 |
| 离散存在 | Discrete Existence | 仅在被调用瞬间存在、调用之间空白的传统 AI Agent 范式 |
| 数字生命体 | Digital Life Entity | 具备连续性、自学习与系统涌现智能的 AI 系统，由 Life Engine 实现 |
| 自下而上的学习 | Bottom-Up Learning | 不依赖外部梯度，基于局部时序相关性的在线学习机制（STDP、Hebbian） |
| 系统涌现智能 | System-Level Emergence | 智能不是单一模型的属性，而是异质子系统（LLM + SNN + 调质 + 记忆）协作的属性 |
| 皮层下系统 | Subcortical System | 类比生物大脑皮层下脑区的低层计算系统，包含 SNN 与调质层 |

---

## A.2 架构层术语

| 中文术语 | English | 工程定义 |
|---------|---------|---------|
| 潜意识 / 生命中枢 | Subconscious / Life Engine | `plugins/life_engine` 插件中的后台系统，负责心跳、SNN/调质/做梦/记忆/ThoughtStream 的统一调度 |
| 社交主意识 | LifeChatter / Social Conscious Mind | `life_chatter` 对话器，负责对外表达、行动选择、上下文组装与内心独白回写 |
| 主意识–潜意识架构 | Dual-Consciousness Architecture | LifeChatter（社交主意识）+ Life Engine（潜意识）异步协同运行的架构设计 |
| 心跳 | Heartbeat | Life Engine 每 30 秒触发的内省+决策循环（`heartbeat_interval_seconds` 配置） |
| 事件流 | Event Stream | `LifeEngineEvent` 的全局有序序列，记录所有消息/工具调用/心跳 |
| 唤醒上下文 | Wake Context | 注入到心跳 LLM 的事件历史 + 状态摘要，通过 `build_wake_context()` 构建 |
| 影子模式 | Shadow Mode | SNN 运行但不注入 prompt 的调试模式（`shadow_only=True`） |
| 组件 | Component | 插件系统的可发现单元，12 种类型（Action/Tool/Chatter 等） |
| 组件签名 | Component Signature | 格式为 `plugin:type:name`，用于全局唯一标识组件（如 `life_engine:tool:search_memory`） |

---

## A.3 SNN 层术语

| 中文术语 | English | 工程定义 |
|---------|---------|---------|
| 脉冲神经网络 | SNN (Spiking Neural Network) | 以离散脉冲（而非连续值）传递信息的神经网络，实现路径：`snn/core.py` |
| 漏电积分发放神经元 | LIF (Leaky Integrate-and-Fire) | 经典脉冲神经元模型，公式：`dv/dt = (-v + I) / τ`，`v ≥ θ → spike` |
| 脉冲时序依赖可塑性 | STDP (Spike-Timing-Dependent Plasticity) | 基于脉冲时序差的局部学习规则，`Δw ∝ exp(-Δt/τ)` |
| 软 STDP | Soft STDP | 用 `sigmoid(膜电位)` 替代二值发放参与学习的工程改造，避免突变 |
| 自稳态 | Homeostasis | 通过动态调整阈值使发放率维持在目标水平的机制（`homeostatic_threshold` 字段） |
| 衰减步 | Decay-Only Step | 仅膜电位泄漏、不学习的低开销 tick，用于降低 SNN 计算成本 |
| 驱动 | Drive | SNN 输出的 6 维生命冲动信号（arousal/valence/social/task/exploration/rest） |
| 动态增益 | Dynamic Gain | 根据输入分布自适应调整的输入缩放因子，维持 SNN 激活在合理范围 |
| 特征窗口 | Feature Window | 从事件历史提取特征的时间窗口（`feature_window_seconds=600`） |

---

## A.4 调质层术语

| 中文术语 | English | 工程定义 |
|---------|---------|---------|
| 调质（神经调质） | Neuromodulation | 影响神经元/网络整体激活水平的化学/动力学过程，对应 `neuromod/engine.py` |
| 调质因子 | Modulator | 项目中实现的 5 个浓度变量：`curiosity/sociability/focus/contentment/energy` |
| 时间常数 | Time Constant (τ) | ODE 回归基线的特征时间尺度，公式：`τ = 1/decay_rate`（约 1000 秒） |
| 基线 | Baseline | 调质因子的稳态目标值，由 `Modulator.baseline` 定义 |
| 边际效应递减 | Headroom | 公式：`h(M) = 1 - 2|M-0.5|`；浓度越极端（接近 0 或 1）越难再变 |
| 昼夜节律 | Circadian Rhythm | 24 小时周期的内在时钟，由 `sleep_time/wake_time` 配置与 `energy` 调质因子实现 |
| 习惯 | Habit | 由 `streak`（连胜天数）与 `strength`（强度）表征的天级行为稳定化机制 |
| 习惯连胜 | Streak | 习惯连续触发的天数，每天首次触发时 `+1`，中断时清零 |

---

## A.5 记忆层术语

| 中文术语 | English | 工程定义 |
|---------|---------|---------|
| 记忆节点 | MemoryNode | 知识图中的实体/事件/概念，由 `memory/nodes.py` 定义 |
| 记忆边 | MemoryEdge | 节点之间的关联，6 种类型（RELATES_TO/PART_OF/CAUSED_BY/CO_OCCURS_WITH/PRECEDES/DEPENDS_ON） |
| Ebbinghaus 衰减 | Ebbinghaus Decay | 记忆强度按指数衰减，公式：`S(t) = S₀ · e^(-λt)`，λ ≈ 0.05 |
| Hebbian 强化 | Hebbian Reinforcement | "一起激活的神经元/边一起强化"，公式：`Δw = α · a_i · a_j` |
| 倒数排名融合 | RRF (Reciprocal Rank Fusion) | 多路检索结果的融合算法，公式：`score = Σ(1/(k+rank))`，k=60 |
| 激活扩散 | Activation Spreading | 沿边权重传播激活的检索增强方法，每跳衰减因子 `spread_decay=0.7` |
| 记忆剪枝 | Memory Pruning | 删除强度低于阈值（`prune_threshold=0.1`）的边，维持记忆图稀疏性 |

---

## A.6 做梦层术语

| 中文术语 | English | 工程定义 |
|---------|---------|---------|
| NREM | NREM Sleep | 非快速眼动睡眠阶段，对应突触稳态缩减过程，实现：`dream/scheduler.py` |
| REM | REM Sleep | 快速眼动睡眠阶段，对应关联整合过程，通过激活扩散游走实现 |
| 突触稳态假说 | SHY (Synaptic Homeostasis Hypothesis) | Tononi & Cirelli 2014 假说，NREM 通过全局缩放维持突触稳态 |
| 种子 | Seed | 做梦的起始记忆/事件，由 `collect_seeds()` 从记忆图中选取 |
| 残影 | Residue | 做梦后留下的可被巩固的产物，通过 `create_residue()` 生成新边 |
| 场景 | Scene | 由场景生成器产出的梦境片段，存储在 `dream_log.json` |
| 小憩 | Nap | 白天空闲时触发的短周期做梦（`nap_enabled=True`） |
| 种子类型 | SeedType | RECENT_MEMORY（最近记忆）/WEAK_NODE（弱节点）/SELF_THEME（自我主题）三种 |

---

## A.7 工程层术语

| 中文术语 | English | 工程定义 |
|---------|---------|---------|
| 运行时内心独白 | Runtime Inner Monologue | LifeChatter 将当前想法回写给 Life Engine 的内部动作，用于闭环同步 |
| 运行态快照 | Runtime Snapshot | Life Engine 输出给 LifeChatter 的状态摘要，包含调质、事件、思考流与梦境残影 |
| 思考流 | ThoughtStream | `plugins/life_engine/streams/` 中维护的持久注意力线索，保存当前焦点与背景在意 |
| 注意力同步 | Attention Sync | 将 ThoughtStream 与运行态摘要同步给 LifeChatter 的机制 |
| 状态序列化 | State Serialization | 完整 SNN/调质/记忆/事件历史的 JSON 持久化，路径：`life_engine_context.json` |
| 事件序列号 | Event Sequence | 全局单调递增的事件编号，保证事件全序（`state.event_sequence`） |
| 流 | Stream | 对话流的抽象，由 `stream_id` 唯一标识（如 `group_12345`） |
| 活跃窗口 | Active Window | 判定外部消息是否"最近活跃"的时间窗口（`external_active_minutes=5`） |
| 工具调用轮数 | Tool Call Rounds | 单次心跳内工具调用的嵌套深度上限（`max_rounds_per_heartbeat=3`） |
| 冲动引擎 | Drive Engine | 将 SNN 驱动信号转化为决策的模块（`drives/impulse.py`） |
| 快照 | Snapshot | 通过 `/api/snapshot` 端点获取的完整系统状态 JSON |

---

## 使用建议

### A.7.1 术语一致性原则

1. **首次出现时明确**：正文中首次使用术语时应给出"中文（English）"形式，之后可只用中文或英文缩写
2. **跨章节统一**：同一概念在不同章节必须使用相同术语（如"心跳"统一，不混用"心跳循环""心跳周期"）
3. **工程定义优先**：当口语表达与工程定义冲突时，以本表工程定义为准

### A.7.2 术语查询建议

- **按层级查找**：概念模糊时从哲学层→架构层→具体实现层逐级查询
- **代码路径追溯**：工程定义中注明了核心文件路径，可据此追溯实现细节
- **术语演化**：部分术语（如 `shadow_only`）在代码中存在多种理解，以本表定义为报告标准

### A.7.3 术语扩展指南

若需补充新术语，请遵循以下格式：

```
| 新术语中文 | New Term | 工程定义（含公式/代码路径/配置参数） |
```

保持定义单句化、可验证性、与代码一致性三原则。

---

**版本信息**  
- 术语数量：43 条  
- 最后更新：2025-01-15  
- 维护人：Neo-MoFox-Soul 报告撰写组
