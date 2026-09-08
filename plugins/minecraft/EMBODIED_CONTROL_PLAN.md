# Minecraft 陪玩具身控制设计

> 状态：生产候选 v2，2026-09-03。本文记录当前可交付边界和扩展接缝，不把今天的任务集合当成最终形态。只有完成真实端到端验收后，才能标记“生产已跑通”。

## 目标

目标不是让一个通用聊天模型远程点按 Minecraft，也不是把核心 heartbeat 加速成游戏 tick。目标是让同一个爱莉主体拥有一个专门的 Minecraft 场景意识、一个独立同服身体和持续的游戏节奏：

- 她能看到当前世界事实，知道用户和其他玩家是否在附近；
- 她能主动说话、跟随、探索、采集、合成、放置、进食或休息；
- 长任务执行时，她仍能听见游戏聊天并重新决定；
- 她可以选择回应或不回应，系统不把每条聊天变成命令；
- 所有成功、失败、中断与重连都有真实证据，不用模型自述冒充完成。

## 当前生产路径

```text
统一主体（SOUL / USER / MEMORY）
        │ 固定 8 KiB 主体投影
        │ 4 KiB / 3 组近期潜意识
        ▼
MinecraftConsciousnessRuntime
        │ speech + 一个高层任务 / wait / end_session
        ▼
MinecraftSession
        │ 决定先落 Life Event；命令写 trace
        ▼
MinecraftTaskEngine（Mineflayer body）
        │ 单一 BodyGate、稳定 task ID、180s 默认期限
        ▼
Minecraft LAN 世界
        │ 聊天 / 玩家 / 生命 / 任务进度与终态
        ▼
身体事件 journal → Life Event → FIFO ACK → 唤醒场景意识
```

默认 `bot` 身体以不同用户名加入用户已经开放到 LAN 的世界。用户保留自己的客户端和键鼠；爱莉不占用用户视角。需要她自己的可见第一人称窗口与 OBS 画面时，显式使用 `agent` 身体；`biomimetic` 仍是会争用前台输入的实验路线。

## 为什么不是“26 个工具让模型一步一步点”

用户保留的两个 ZIP 已做源码级审计。它们证明 Mineflayer 独立玩家、pathfinder、聊天缓冲、后置状态校验和专用 MC chatter 是可行方向，但逐条 RPC 会让模型在移动过程中持续占用推理轮，聊天和环境变化容易被长动作阻塞。

社区 N.E.K.O. 的公开 `game_agent_minecraft` 插件进一步使用单一 `minecraft_task`、稳定 task ID、异步 pending task、busy-skip、技术提醒/超时与 WebSocket 重连。Neuro SDK 的公开协议也强调：

- 给模型一个小而稳定的动作表面；
- 同时只强制一个动作；
- 尽快回送明确结果；
- 上下文只保留当下有意义的事实；
- 重连后重新建立准确的动作契约。

当前设计吸收这些结构，但不复制社区实现里的文本关键词阻塞判断、静态 secret 或无证据的“完成”推断。Elysium 继续使用 HMAC 握手、严格类型、不可变事件、重放冲突和后置世界观察。

## 场景意识与身体的职责

场景意识决定“现在想做什么”：

- 可选 `speech` 是原样发入游戏的文本；
- `pursue` 启动一个身体已经广告的高层任务；
- `wait` 由她选择 2–45 秒后再看，也可被真实事件提前唤醒；
- `end_session` 明确离开场景。

身体任务引擎只负责“连续把这一件事做完”：

- `follow_player`
- `go_to_player`
- `go_to_position`
- `gather_block`
- `craft_item`
- `place_block`
- `eat_item`

它不产生欲望、情绪、优先级或回应决定。新增能力时必须以结构化参数、显式前置条件和可核验终态加入；不能通过关键词把自然语言偷偷映射成固定行为。

外部 `nucleus_minecraft(intent=...)` 保留为专家入口：开放文本意图交给证据驱动 planner，再使用动态低层能力。它与专属意识共享 intent lock 和身体所有权，不与高层任务并发。

## 节奏与不中断

独立 MC 意识有自己的串行模型循环，不改变核心 heartbeat：

1. 获取新鲜观察和有界主体上下文；
2. 形成一个决定并先耐久落账；
3. speech 立即发回游戏，高层任务只等待“已接受”；
4. 身体在后台连续执行任务；
5. 意识按自己选择的时间复思，或被新聊天、玩家变化、生命变化、任务进度/终态提前唤醒；
6. 下一轮结合真实状态，继续、替换、取消、说话、等待或离开。

一个慢 LLM 轮次不会堆积第二个模型请求。一个慢身体任务也不会占住模型轮。空响应和上游失败只进入有界退避，不能伪造决定。

## 身体所有权与任务终态

每个任务记录：

- 稳定 `task_id`；
- kind、arguments、max duration 的规范摘要；
- accepted/started/finished 时间；
- generation、phase、result 或 error。

同 ID 同载荷只返回已有状态；同 ID 异载荷拒绝。单一 BodyGate 阻止 pathfinder、挖掘、合成和低层控制竞争。chat、status、cancel 与 release 不被长任务屏蔽。

默认任务期限是 180 秒，协议允许 5–600 秒。期限到达时 abort 当前任务，尽力取消 CollectBlock、停止挖掘、清除 pathfinder 与控制键，并产生 `failed/timed_out` 事件。接单回执永远不等于完成；完成必须由 terminal event 和更新后的世界观察共同证明。

## 游戏聊天不是“翻译插件”

游戏里的玩家文本不需要经过另一个通用聊天器或文本翻译层：

1. Mineflayer 收到公共聊天或私聊；
2. 形成 `minecraft.chat.received` / `minecraft.whisper.received`；
3. 事件在 body journal 中等待；
4. session 将其归属到当前 Minecraft 意识实例并写入 Life Event；
5. 成功后 ACK 并提前唤醒场景意识；
6. 场景意识自行决定是否回复；
7. 非空 `speech` 通过 `chat.send` 原样发入游戏。

系统消息的本地化只属于事实显示质量，不是聊天是否能通的前置条件。没有 body 进程、18767 监听、LAN 世界或活动 session 时，任何“翻译”都不会让游戏产生回应。

## 上下文预算

Minecraft 不复用 `life_chatter` 后缀。一次专属请求默认包含：

- 固定主体投影：最多 8192 UTF-8 字节；
- 当前结构化观察：最多 8192 字节；
- 近期潜意识：最多 4096 字节、3 个完整因果组；
- 最近结果：最多 4 个 content-free 引用；
- 小型 turn envelope 和可选原生 JPEG。

主体、观察和非空潜意识分别登记 exact delivery。World Perception 不作为跨意识同步默认来源；prompt-only 正文不写入 wire、trace receipt 或 World。完整权威 trace 留在 JSONL，World 只收到 8 KiB 内的 content-free receipt。

## 断线与恢复

反向 Bridge 监听在 body 断线后继续存在。重连必须保持同一 instance ID、能力和 hello 元数据，否则拒绝。body 的 observation/event sequence 按进程生命周期连续，未 ACK 事件按原 ID 和载荷重放；已耐久消费但 ACK 丢失的事件只补 ACK，不能再次写事件或再次唤醒。

进程所有权严格区分：

- Elysium 启动/重启只能由用户手动执行；
- 用户的 Minecraft/Java 客户端不由 AI 关闭；
- MinecraftSession 可以启动和停止自己创建的 bot 子进程；
- stop 必须先结束意识、任务和事件泵，再释放 Bridge、scene 与 Presence。

## 仍需扩展的能力

当前七类任务只是最小陪玩集。后续可以在不破坏上述不变量的前提下加入：

- 容器、熔炉、箱子和交易的结构化事务；
- 可恢复的多阶段建造计划与材料清单；
- 地图/区域记忆和路线复用；
- 战斗风险策略与装备管理；
- bot 的可视化 viewer 或独立可见客户端迁移；
- 多玩家关系、队伍目标与长期场景计划。

扩展不应增加每轮上下文堆叠，也不应回到逐 tick 请求 LLM。长期计划保存 content-free checkpoint；当前 prompt 只取这轮真正需要的片段。

## 发布验收

只有以下同一真实 session 的证据齐全才算跑通：

1. 用户客户端进入世界并以固定 25565 端口开放 LAN；
2. 正式工具完成 `preflight → start → status`，bot 真实出现在玩家列表；
3. 游戏内发一条普通聊天，Life Event 出现相同 event ID/内容，scene 被提前唤醒，爱莉自主选择并在游戏内给出可见 speech；
4. 她启动至少一个跟随或采集任务，观察到 accepted、progress、terminal 和更高序号的新世界状态；
5. 长任务期间新聊天仍能被收到并进入下一模型轮；
6. 模拟一次控制器断连，未 ACK 事件只重放一次且任务不重复执行；
7. 连续 15–30 分钟无模型并发堆积、无上下文递归增长、核心 heartbeat 节奏不改变；
8. interrupt 与 stop 释放身体和 Presence，用户客户端继续运行。

自动化测试、预检或“模块已初始化”都不能替代这组现场证据。
