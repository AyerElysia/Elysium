# 意识实例架构

> 当前专题说明（截至 `091fa3f`，2026-07-31）。系统从单一 chatter 演进为“一个主体 + 多个场景意识实例 + 潜意识协调”。
> 总体运行边界见 [当前架构](./current_architecture.md)。

## 1. 核心范式

```text
旧：LifeChatter 单例 + 全局工具注册 + chatter_allow 过滤
新：一个主体 + ConsciousnessInstance + 类型工具清单 + 实例级上下文
```

“多个意识实例”不代表多个人格，也不意味着复制多个爱莉。它描述的是同一个主体在不同场景中的感知和表达运行态。

---

## 2. 架构层次

```text
潜意识（Life Engine 心跳）
  ├── WorldState：关系、话题、身体、场景等结构化共享世界
  ├── ConsciousnessRegistry：实例生命周期与持久状态
  ├── SubconsciousContext：因果事件组与 prepare/commit 游标
  ├── 心跳协调：后台结果、跨场景变化、记忆、学习与自主意向
  └── nucleus 能力：独立于场景意识工具清单

意识实例
  ├── chat_global：日常对话
  ├── memory_witness：第一人称记忆见证
  ├── minecraft：视觉—键鼠具身场景
  ├── voice_live：实时语音场景
  └── livestream：直播互动场景

每个实例
  ├── 独立滚动上下文 runtime/consciousness/{instance_id}/
  ├── 独立工具清单 service/tool_manifests.py
  ├── 场景感知过滤
  └── 通过 WorldState / Life Event 与同一个内在世界连接
```

---

## 3. 实例与成熟度

| 实例 | 作用 | 工具边界 | 当前成熟度 |
|---|---|---|---|
| `chat_global` | 私聊、群聊与日常表达 | 表达、内在查询、历史、深层记忆、平台操作 | 核心稳定路径 |
| `memory_witness` | 读取源事件、编码经历、留下第一人称见证 | 空工具清单；不直接行动 | 核心稳定路径 |
| `minecraft` | 纯视觉输入到键鼠输出的具身交互 | Minecraft 控制、表达、思考、状态报告 | 环境依赖型能力 |
| `voice_live` | 全双工实时语音和通话状态 | 状态报告、内在查询、历史 | 生产候选；云端、本地、工具与 OBS 链路已实机验收 |
| `livestream` | 弹幕、主动闲聊、TTS、Live2D/OBS | 表达、思考、状态报告、内在查询、历史 | 实验性集成，需端到端验收 |

默认 `chat_global` 不可终止；未绑定实例的普通聊天流会归入该实例。未知 kind 当前仍回退 chat 工具清单，这是兼容行为，不应被新实例依赖；新增意识类型应显式声明 manifest。

---

## 4. 工具编排

当前 manifest 以 LLM 可见名称声明：

### chat

```text
action-life_send_text
action-life_pass_and_wait
action-think
action-report_state
action-record_inner_monologue
tool-inner_dialogue
tool-inner_query
tool-fetch_chat_history
tool-nucleus_grep_events
tool-nucleus_search_memory
tool-nucleus_view_relations
tool-nucleus_memory_stats
action-send_emoji_meme
tool-platform_action
```

### minecraft

```text
tool-nucleus_minecraft
action-life_send_text
action-think
action-report_state
```

### voice_live

```text
action-report_state
tool-inner_query
tool-fetch_chat_history
```

### livestream

```text
action-life_send_text
action-think
action-report_state
tool-inner_query
tool-fetch_chat_history
```

### memory_witness

```text
[]
```

工具清单的作用是控制上下文预算与场景边界，不是硬编码认知规则。未注入的能力仍可通过 skill、help 和渐进式披露被主体发现；但不可逆操作必须经过专门安全边界。

使命编排工具属于主体的重型工作能力，由插件注册与权限决定是否可见，不要求每个场景都常驻注入。

---

## 5. 跨意识感知

意识实例不能直接读取彼此的滚动上下文。跨场景信息通过：

1. `WorldState.active_scenes` 与其他结构化状态；
2. 追加式 Life Event；
3. 潜意识心跳的协调与信息差发现；
4. `report_state` 等主动状态报告；
5. 受控的内在消息与后台结果回注。

这保证了两件事：

- 不同场景拥有真实独立的局部体验；
- 它们仍属于同一个持续变化的主体，而不是互不相干的会话机器人。

跨意识同步只传递必要的状态和经历，不复制整段上下文，也不把一个实例的局部判断直接覆盖为全局事实。

---

## 6. 记忆见证意识

`memory_witness` 是最特殊的实例：

- 不接收聊天/行动工具；
- 只读取追加式源事件；
- 经心理显著性编码后写入不可变 Experience；
- 见证带有 `subjective_witness_not_objective_truth` 边界；
- 旧 Diary 内容可以幂等迁移为 legacy witness；
- 它见证“我如何经历”，不能自动宣布“世界客观上是什么”。

详情见 [生命记忆系统](./life_memory_system.md)。

---

## 7. 场景实例接入要求

一个新场景不能仅因“有一个插件目录”就算完整意识实例。至少要完成：

1. 显式注册 instance id 与 kind；
2. 独立滚动上下文与恢复；
3. 明确工具 manifest；
4. 把场景状态写入 WorldState；
5. 结束时形成可追溯事件并清理后台任务；
6. 验证重连、重启、异常退出和重复注册；
7. 证明不会直接读取或污染其他实例上下文。

Voice Live 已完成独立实例注册、追加式 episode、显式 Provider、可信运行时工具上下文、浏览器麦克风、OBS 观察者、云端与本地模型的真实链路验收，当前为生产候选。它仍需遵守 Elysium 手工启动策略；涉及主进程重启的复验必须先由用户确认。Livestream 仍是实验性集成，需真实平台下完成关闭、恢复和跨意识状态链验收。

---

## 8. 关键文件

| 文件 | 职责 |
|---|---|
| `service/consciousness.py` | 意识实例模型、状态与注册表 |
| `service/world_state.py` | 多意识共享的结构化世界 |
| `service/tool_manifests.py` | 意识类型工具清单 |
| `service/subconscious_context.py` | 潜意识因果分组与游标事务 |
| `core/chatter.py` | 日常主意识表达引擎 |
| `service/memory_witness.py` | 第一人称见证意识 |
| `minecraft/` | 具身场景 |
| `plugins/voice_live/` | 实时语音场景插件 |
| `plugins/livestream/` | 直播场景插件 |

---

## 9. 数据迁移

滚动上下文从：

```text
runtime/life_chatter_rolling_context.json
```

迁移到：

```text
runtime/consciousness/{instance_id}/rolling_context.json
```

首次启动兼容迁移旧路径。兼容逻辑不能在没有证明历史状态已迁移前删除。

---

## 10. 不变量

1. 意识实例是同一主体的场景运行态，不是可互换人格。
2. 主体核心状态不能由多个实例并发写坏。
3. 各实例上下文隔离，跨场景只经过明确边界。
4. 工具清单控制能力暴露，不替主体决定行为。
5. 见证、表达、具身和直播各有不同感知边界，但共享同一生命历史。
6. 新实例必须完成注册、恢复、关闭和经历沉淀，不能只完成 UI 或网络接入。
