# 附录 B · 关键配置参数摘要

> **用途**：保留课程报告所需的可复现配置说明。完整实现以开源仓库为准：<https://github.com/AyerElysia/Neo-MoFox-Soul>。  
> **主要实现位置**：`plugins/life_engine/core/config.py`

---

本附录不再逐项展开所有配置字段。对课程报告而言，配置表的作用是说明 Neo-MoFox-Soul 的连续运行、感知状态、在线学习和离线巩固确实由可调参数控制，而不是只存在于文字描述中。

## B.1 Life Engine 核心配置

| 配置项 | 典型值 | 作用 |
|---|---:|---|
| `enabled` | `true` | 是否启用 Life Engine 后台系统 |
| `heartbeat_interval_seconds` | `30` | 后台心跳周期，决定系统无外部输入时的状态推进频率 |
| `workspace_path` | `data/life_engine_workspace` | 持久化文件目录 |
| `context_history_max_events` | `100` | 事件历史保留上限 |
| `max_rounds_per_heartbeat` | `3` | 单次心跳内允许的工具/推理轮数上限 |
| `idle_pause_after_external_silence_minutes` | `30` | 外部长期静默后暂停高成本 LLM 心跳 |

这些参数对应无人系统中的主控循环、任务缓冲区和低功耗策略。

## B.2 感知与状态层配置

| 子系统 | 关键配置 | 课程含义 |
|---|---|---|
| SNN | `enabled`, `shadow_only`, `tick_interval_seconds`, `feature_window_seconds` | 快速事件响应与本地可塑性 |
| Neuromod | `enabled`, `inject_to_heartbeat`, `habit_tracking` | 慢速情态变量与感知增益调节 |
| Memory | `spread_decay`, `prune_threshold`, `dream_learning_rate` | 记忆衰减、联想扩散和巩固 |
| Dream | `nrem_replay_episodes`, `rem_walk_rounds`, `dream_interval_minutes` | 低活跃期经验整理 |
| ThoughtStream | `max_active_streams`, `dormancy_threshold_hours`, `curiosity_decay_half_life_hours` | 潜意识注意力流与中期兴趣线索 |
| LifeChatter Sync | `sync_to_chatter`, `focus_window_minutes`, `delta_marking` | 主意识读取潜意识运行态的裁剪策略 |

SNN 默认可运行在 shadow 模式，用于收集状态而不直接影响对话输出。这一点对课程报告很重要：它允许系统先验证内部状态是否合理，再决定是否把底层信号注入高层决策。

## B.3 外部能力配置

| 模块 | 作用 |
|---|---|
| `history_retrieval` | 从聊天历史中检索上下文，补充长期记忆 |
| `web` | 可选网页搜索能力，服务于工具型 Agent 行为 |
| `model` | 指定 Life Engine 使用的模型任务名 |
| `thresholds` | 控制空闲、活跃、紧急等状态判断 |

这些配置体现了 Neo-MoFox-Soul 作为 Agent 系统的工程边界：感知不只来自当前消息，也来自历史、工具、网页和内部状态。

## B.4 最小可复现配置示例

```toml
[life_engine.settings]
enabled = true
heartbeat_interval_seconds = 30
workspace_path = "data/life_engine_workspace"
context_history_max_events = 100

[life_engine.snn]
enabled = true
shadow_only = true
tick_interval_seconds = 10.0
feature_window_seconds = 600.0

[life_engine.neuromod]
enabled = true
inject_to_heartbeat = true
habit_tracking = true

[life_engine.dream]
enabled = true
dream_interval_minutes = 90
nap_enabled = true

[life_engine.thought_stream]
enabled = true
max_active_streams = 5
dormancy_threshold_hours = 24
sync_to_chatter = true
```

这个配置足以复现本报告讨论的核心闭环：后台心跳推进状态，SNN 观察事件流，调质层调节状态，ThoughtStream 维护潜意识关注点，做梦系统在低活跃期整理经验。

## B.5 配置与课程概念的对应

| 课程概念 | 配置证据 |
|---|---|
| 持续感知 | `heartbeat_interval_seconds`, `tick_interval_seconds` |
| 状态估计 | `workspace_path`, `context_history_max_events` |
| 自适应感知增益 | `neuromod.enabled`, `inject_to_heartbeat` |
| 在线学习 | `snn.enabled`, `habit_tracking` |
| 离线优化 | `dream.enabled`, `dream_interval_minutes` |
| 注意力同步 | `thought_stream.enabled`, `sync_to_chatter`, `max_active_streams` |

因此，配置文件不是附属材料，而是本报告可复现性的关键证据：报告中的系统行为都可以回到具体参数上检查。
