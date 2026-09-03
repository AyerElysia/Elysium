# Minecraft 陪玩链路社区审计与生产验收记录

> 日期：2026-09-03  
> 当前结论：代码生产候选正在收口；自动化专项已通过，现场端到端尚未完成，因此不能标记“已跑通”。

## 用户目标

让同一个爱莉主体拥有专门的 Minecraft 意识，以独立玩家身份和用户处在同一个世界。她要有自己的观察、节奏和行动选择；回不回复游戏聊天是她的决定。长任务不能让她失去说话和重新判断的能力，失败、断线和重试不能被伪装成成功。

## 2026-09-03 现场基线

只读检查得到：

- Elysium 主进程正在运行；
- 没有 Minecraft/Java 进程；
- WSL 的 18765、18766、18767 和 25565 均未监听；
- 当日日志只有 `Minecraft evidence-driven embodiment initialized`，没有 session start、Bridge 认证、body spawn、游戏聊天入站或命令回执。

因此用户此前“游戏里没有反应”不是翻译层故障，而是身体和会话链路没有启动。模块初始化日志不能作为陪玩已就绪的证据。

## 本地 ZIP 审计

审计来源：

- `C:\Users\26652\Downloads\MC集成包.zip`
- `C:\Users\26652\Downloads\MC插件源码.zip`

可取经验：

- Mineflayer 作为独立同服玩家；
- pathfinder 承担连续导航；
- chat/whisper/system 环形缓冲；
- `do_task` 执行多步采集并在最终状态返回；
- 专用 MC chatter 避免挤占普通聊天链；
- 后置游戏状态用于验证任务结果。

没有直接复制源码。旧包以静态 secret、通用 RPC、分散工具和文本式任务状态为主，没有当前 Elysium 所需的 HMAC challenge、不可变 Life Event、严格重放冲突、单一 BodyGate、exact context delivery、content-free World receipt 与明确进程所有权。若源码授权不明，也不能把实现直接搬入仓库。

## 社区实现核对

### N.E.K.O.

公开 `game_agent_minecraft` 插件确实存在，不再沿用“社区没有 MC 插件”的旧结论。它使用：

- 一个 `minecraft_task` 高层工具；
- 每次任务稳定 UUID；
- WebSocket 日志、截图和 `task_finished`；
- busy-skip 与 overwrite；
- 120 秒默认任务超时；
- 约 15 秒的有节制 nudge；
- 断线重连和截图缓存/节流。

资料：

- [N.E.K.O. Minecraft 插件说明](https://github.com/Project-N-E-K-O/N.E.K.O/blob/main/plugin/plugins/game_agent_minecraft/README.md)
- [N.E.K.O. Minecraft 服务实现](https://github.com/Project-N-E-K-O/N.E.K.O/blob/main/plugin/plugins/game_agent_minecraft/service.py)

Elysium 采用“高层任务异步执行、稳定关联、不中断主意识、明确终态和重连”的结构；不采用根据模型回复文本判断卡住、未知状态宽松透传或无后置观察的完成推断。

### Neuro

Neuro SDK 把游戏接入表示为动作注册、动作结果和上下文更新，并要求：

- 动作表面保持小而清楚；
- 同时只强制一个动作；
- 尽快返回成功或错误；
- 对畸形参数做明确校验；
- 大型瞬时状态不要持续塞进上下文；
- 重连后恢复准确的动作集合。

资料：

- [Neuro SDK API specification](https://github.com/VedalAI/neuro-sdk/blob/main/API/SPECIFICATION.md)
- [Neuro SDK best practices](https://github.com/VedalAI/neuro-sdk/blob/main/API/BEST_PRACTICES.md)

### Mineflayer 生态

生产 bot 使用官方生态的 [mineflayer-pathfinder](https://github.com/PrismarineJS/mineflayer-pathfinder) 和 [mineflayer-collectblock](https://github.com/PrismarineJS/mineflayer-collectblock)，依赖已经锁入 `package-lock.json`。

## 当前生产候选设计

### 独立场景意识

`MinecraftConsciousnessRuntime` 是独立的串行模型循环，不改变核心 heartbeat。它可以：

- 发送一段原样游戏 `speech`；
- 启动一个 advertised 高层任务；
- 自己选择等待时间；
- 结束场景。

公共聊天、私聊、玩家进出、生命变化、死亡、spawn、断开与任务进度/终态会提前唤醒它。回复不是自动规则。
已经生成但因 exact delivery 或 JSON 协议失败而不能成为决定的模型轮，也会先以当前 instance/session/stream 身份落入统一 Life Event，再进行有界重试，不会形成只有日志可见的意识黑洞。

### 异步高层任务

bot 当前提供：

- `follow_player`
- `go_to_player`
- `go_to_position`
- `gather_block`
- `craft_item`
- `place_block`
- `eat_item`

任务拥有稳定 ID/摘要、单一 BodyGate、accepted/progress/terminal 事件、显式 cancel/replace 和默认 180 秒技术期限。长任务在身体侧运行，模型轮不被占住。

### 游戏聊天闭环

```text
玩家聊天
  → body event journal
  → MinecraftSession FIFO 消费
  → 当前 minecraft instance 的 Life Event
  → ACK
  → 提前唤醒专属意识
  → 模型自主决定 speech
  → chat.send
  → 游戏内可见
```

未 ACK 的事件在同一认证身体重连后原样重放；已落账但 ACK 丢失的事件只补 ACK，不重复落账或重复唤醒。

### 上下文

Minecraft 不使用 `life_chatter` 大后缀。一次专属请求默认上限：

| 部分 | 默认上限 |
|---|---:|
| 固定主体投影 | 8192 UTF-8 bytes |
| 当前结构化观察 | 8192 UTF-8 bytes |
| 近期潜意识 | 4096 UTF-8 bytes / 3 groups |
| 最近结果 | 4 个 content-free refs |

三段正文分别做 exact delivery。World Perception 不作为跨意识同步默认来源；prompt-only 正文不写入 trace receipt 或 World。
MC 的两条近期潜意识入口都显式使用 3 组/4096 bytes 并设置 `include_tool_payloads=false`，不会退回 producer 的宽松默认值。超大观察采用固定技术通道顺序，保证自身玩家、聊天与任务状态先于大型实体/背包集合进入有界前缀，完整观察仍留在 trace。

## 已完成自动化证据

- Mineflayer：`14 passed`，覆盖七类高层任务适配、协议认证、命令重放、未 ACK 事件重放、跨重连观察序列、BodyGate、任务替换、任务期限、严格 JSON 类型校验，以及接收事件失败时不泄漏身体锁；
- Python Minecraft 全目录：`107 passed`，覆盖 Bridge、专属意识、session、bot、视觉、配置、trace/World 有界投影、失败模型轮落账及 200 实体下核心观察可见性等；
- Minecraft + Console 精确组合：`76 passed`；
- 本轮精确 Python 文件静态检查除 `event_builder.py` 的既有仓库基线告警外没有新增错误；
- JavaScript 所有 `src/*.js` 与 `test/*.js` 通过 `node --check`；
- `git diff --check` 通过。

完整全仓测试未运行：现场 Elysium 主进程仍在运行，按根目录规范不能在此时并发执行高负载或可能争用正式存储的全仓验收。

## 尚未跨过的发布门

### 1. 身体事件服务接线

`MinecraftSession` 对带高层任务能力的 body 明确要求 `record_minecraft_body_event`。当前 `LifeEngineService._create_minecraft_session()` 还没有传入该 callback，所以真实 bot start 会 fail closed；这是当前最直接的软件阻断。

需要在共享服务中：

- 注入 `record_minecraft_body_event=self.record_minecraft_body_event`；
- 用 event ID 缓存完全相同的 `LifeEngineEvent`；
- 原始写入不确定时重试同一对象；
- 同 ID 异载荷拒绝；
- 已成功落账的重放直接复用；
- 增加 service 接线、失败重试、幂等与冲突测试。

### 2. 配置模式默认值一致

运行实例已经显式配置 `default_body="bot"` 和 8192/8192/4096/3/4 的预算；`MCConfig` 默认也已收紧。但共享 `LifeEngineConfig.MinecraftSection` 的仓库默认仍是 agent 与旧预算，需要在同一授权补丁中对齐并更新配置测试。

### 3. 手动重启与真实世界

代码完成后需要用户手动重启 Elysium。用户客户端随后进入目标世界并固定 25565 端口开放 LAN；Elysium session 才能启动它自己拥有的 bot 子进程。AI 不关闭或重启 Elysium，也不关闭用户的 Minecraft/Java。

### 4. 同一次 session 的现场证据

最终必须验证：

1. `preflight → start → status`；
2. bot 出现在真实玩家列表；
3. 游戏聊天落账、唤醒、可见回复；
4. 一次跟随或采集的 accepted/progress/terminal/新观察；
5. 长任务期间仍能听到聊天；
6. 一次重连/未 ACK 重放；
7. 15–30 分钟自主节奏，无请求堆积或上下文增长；
8. interrupt/stop 正确释放，用户客户端不受影响。

缺少任何一项，只能称为“自动化就绪”，不能称为“生产跑通”。
