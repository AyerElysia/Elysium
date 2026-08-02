# 意识实例架构

> 当前实现：一个主体、多个场景意识窗口、潜意识统一协调。Presence、事件来源、World Projection 与逐实例 Perception Gateway 均已接入。

## 1. 身份模型

`ConsciousnessInstance` 是同一主体在一个场景中的局部运行窗口。不同实例可以拥有不同的即时输入、私有滚动上下文、session、回复目标和工具 manifest，但不能被解释成不同人格，也不能互相读取私有上下文。

当前声明的实例 kind：

| kind | 作用 | 当前协调闭环 |
|---|---|---|
| `chat` / `chat_global` | 私聊、群聊与日常表达 | Presence、事件、heartbeat/chatter 感知 |
| `memory_witness` | 第一人称经历编码与见证 | 独立 consumer、请求级世界感知 |
| `minecraft` | 视觉—键鼠具身场景 | session/lease、trace observation、意图级感知 |
| `voice_live` | 全双工实时语音 | session/lease、listening frontier 动态感知 |
| `livestream` | 弹幕和直播表达 | room Presence、请求级感知、状态 observation |

`chat_global` 是默认实例，不可终止；没有独立 stream owner 的普通聊天归入它。kind 是开放技术标识，不是认知类别。未知 kind 必须显式声明 manifest，系统不会回退到 chat 能力。

## 2. 运行层次

```text
潜意识 / Life Engine
  ├─ immutable Life Event：完整经历与归因
  ├─ SQLite Presence：实例、lease、revision、stream owner
  ├─ World Projection：带来源、可重建、允许矛盾的 assertion
  ├─ Perception Gateway：逐实例 prepare/commit cursor
  ├─ heartbeat / memory / learning / thought stream
  └─ nucleus tools

场景意识实例
  ├─ 独立即时输入和滚动上下文
  ├─ 显式工具 manifest
  ├─ 原始授权回复目标
  ├─ transient world perception
  └─ 带 source_instance_id 的 observation
```

系统传递的是带来源的运行存在和世界观察，不复制其他实例的对话历史。Presence 始终只是技术事实；Projection 中的局部观察也不会被代码自动判真。

## 3. Presence 生命周期

权威存储是 `runtime/consciousness_presence.sqlite3`。一次状态事务同时提交实例 revision、active stream owner 和 lifecycle outbox。outbox 在不可变账本接受同一 occurrence 后才确认。

短生命周期场景声明 session 与 lease，并在真实活动时续租；异常消失后 lease reconciliation 会 suspend 实例并释放 stream。陈旧 revision 不能覆盖新状态，同一 active stream 只能有一个 owner。

`runtime/consciousness_registry.json` 是旧数据导入源和兼容导出，不是当前权威。

## 4. 跨实例感知

每个实例拥有独立 World Projection cursor。每轮 `prepare` 提供：

- 全部 active 窗口的最小存在感；
- 全部带来源 assertion，包括矛盾与已撤回记录；
- 自该实例上次成功确认以来的相关投影 change。

这些内容只作为当前轮 transient context。模型/provider 成功接受后才 `commit`；失败、超时或执行异常不推进 cursor。Presence 最小存在感不依赖增量 cursor，因此实例持续知道彼此当前存在。

潜意识 heartbeat、`chat_global`、Voice、Minecraft、`memory_witness` 和 Livestream 均使用同一 prepare/commit 契约。具体数据流、迁移和恢复见[世界状态与意识实例协调](./world_state_coordination.md)。

## 5. 工具边界

工具 manifest 只控制能力暴露与授权，不决定主体想做什么：

- chat：表达、思考、状态报告、内在查询、历史和获授权平台能力；
- minecraft：具身控制、表达、思考、状态报告；
- voice_live：状态报告、内在查询、历史；
- livestream：表达、思考、状态报告、内在查询、历史；
- memory_witness：空 manifest，只见证不直接行动。

`report_state` 追加 `world.observation_reported`，不修改 JSON；`inner_query` 返回完整、可归因投影，不使用关键词匹配、固定类别或代码截断替当前实例判断意义。

## 6. 新场景接入要求

新场景至少必须完成：

1. 声明显式 instance ID、开放 kind、session 和稳定 stream ID；
2. 原子注册 Presence，短生命周期场景配置 lease；
3. 声明显式工具 manifest，不依赖未知 kind fallback；
4. 保持私有滚动上下文隔离和原始授权回复目标；
5. 生命周期与重要观察写入带实例归属的 Life Event；
6. 在每个真实模型/动作 frontier 使用 Perception Gateway；
7. 仅在上下文被成功接受后 commit cursor；
8. 实现幂等 start/stop、恢复、资源关闭和失败重试；
9. 验证重复注册、stream 冲突、revision 冲突、lease 过期、失败不确认和重启恢复。

通道桥只负责协议适配，不拥有身份、记忆或世界真相。

## 7. 关键文件

| 文件 | 职责 |
|---|---|
| `service/consciousness.py` | 实例模型、Presence 生命周期、lease、outbox 发布 |
| `service/presence_store.py` | SQLite Presence、stream 唯一约束、revision CAS |
| `service/event_bus.py` | 不可变 Life Event 账本与 consumer cursor |
| `service/world_projection.py` | 事件来源 World Projection、重建、感知 cursor |
| `service/perception_gateway.py` | transient 感知 prepare/commit/query |
| `service/tool_manifests.py` | 显式实例能力边界 |
| `service/memory_witness.py` | 独立第一人称见证消费 |
| `minecraft/session.py` | 具身实例与意图级感知 |
| `plugins/voice_live/` | 实时语音实例与 provider 感知注入 |
| `plugins/livestream/` | 直播实例与请求级感知注入 |

## 8. 不变量

1. 所有实例属于同一主体，不是可互换人格。
2. 私有上下文隔离，跨场景只经过明确、可归因边界。
3. 完整经历先进入不可变账本；投影可以重建，账本不可被投影反向修改。
4. Presence 不自动成为信念，局部 observation 不自动成为客观事实。
5. 矛盾观察并存，只有显式、可审计事件能建立撤回或修订关系。
6. 模型请求失败不得伪装成已感知，cursor 只能在成功后推进。
7. system prompt 保持流无关，场景与世界运行态只进入当前 turn。
