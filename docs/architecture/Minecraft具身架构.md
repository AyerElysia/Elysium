# Minecraft 商业级具身架构

## 目标

让爱莉以同一个主体在 Minecraft 中持续感知、形成意图、行动、核验结果并恢复失败。系统不替她规定“应该做什么”，只提供可观察、可中断、可审计的身体能力。

核心原则是“一份意识，两类身体，统一证据契约”：

```text
爱莉的意识与记忆
        │ 自由形成意图
        ▼
模型规划器 ── 动态能力清单、当前世界事实、最近画面
        │ ActionIntent
        ▼
具身运行时 ── 租约、去重、超时、中断、证据链
   ┌────┴─────────────────┐
   │                      │
Agent Body            Biomimetic Body
NeoForge + Baritone    DXcam + SendInput
结构化状态/高层动作       第一人称画面/原生输入
   │                      │
   └────── Minecraft ─────┘
              │
          OBS Game Capture
```

## 统一契约

每次行动包含唯一标识、身体类型、动作名、参数、截止时间和期望的可验证结果。执行结果不是一个布尔值，而是一组终态回执：`succeeded`、`failed`、`timed_out`、`cancelled` 或 `rejected`，并附带行动前后观测序号与证据。

运行时只接受当前动态能力清单中的动作；同一行动重试通过标识去重；身体租约避免两个执行器争抢控制；取消和 `control.release_all` 是终止路径的一部分。每个关键事件写入追加式哈希链，便于复盘直播事故与恢复执行。

这些是操作协议，不是对爱莉思维的分类。系统不使用关键词替她决定目标，也不伪造“已完成”。

## Agent Body：主力商业路线

可见的 NeoForge 1.21.1 客户端内运行 Elysium Bridge：

- 由游戏主线程采集位置、视角、生命、物品栏、附近实体、Baritone 状态和动态能力；
- 通过带共享密钥认证和 HMAC 的反向 WebSocket 主动连接 WSL，避免对外暴露游戏端口；
- 传输层有严格序号、有限观测积压、串行发送和丢帧计数；控制与终态回执不可作为普通观测丢弃；
- 高层移动由 Baritone 执行，低层操作只通过类型化、范围受限的动作完成；不向模型暴露任意 Baritone 命令；
- 死亡时动态暴露 `player.respawn`，恢复后能力随世界状态重新生成。

这条路线在长任务、导航、采集、建造与服务器协作中更稳定，同时保留真实客户端画面、模组兼容性和 OBS 捕捉能力。

### 正式所有权与就绪

Minecraft 是 `LifeEngineService` 独立持有的可选场景，不属于 LearningScheduler。`minecraft.enabled=true` 时才注册 `nucleus_minecraft`；Learning 关闭不会隐藏或接管它。service 的部分初始化失败和 stop 都会幂等关闭 session，失败 owner 保留以便重试，同时继续释放其他资源。

“已经启动”不等于“身体就绪”。Agent Body 必须通过固定版本和摘要预检、共享令牌认证、Bridge/能力匹配、两条连续观察，并明确报告正确单人世界、`world_loaded=true`、`client_paused=false` 和玩家 UUID。标题页、暂停菜单、错误世界、过期桥接、静止观察或多个候选窗口都是显式失败。

### 重放与完成语义

命令账本以 command ID 和规范化载荷摘要去重。完全相同的重试复用已有 ack/终态；若重试发生在原命令仍 pending 时，新连接先得到 ack，并在原执行完成后收到同一份终态，不会重复执行或永久停在“处理中”；相同 ID 配不同载荷被拒绝；pending 不会因有界终态缓存淘汰而丢失。接单回执不代表任务完成，结论必须引用终态回执及其后的新观察。每个 session 将这些证据写成追加式哈希链。

### Perception、Trace 与 World 的单向边界

Minecraft 意图上下文在结构上分为 `durable_context` 与 `transient_prompt_context`。session、stream 和目标等耐久运行身份进入前者；Perception Gateway 准备的世界投影正文只进入后者，并且只有规划器的 `to_prompt()` 可以读取。`to_wire()` 和追加式 trace 不复制瞬态正文，只保存 `minecraft.perception_reference.v1`：delivery identity、内容哈希、版本、游标窗口、frontier、来源 identity 和 UTF-8 字节数。这样既能核验当时使用了哪一份感知，又不会把可替换 Prompt 投影伪装成新的世界事实。

Trace listener 只接收已经 fsync 并带 sequence/hash 的 `TraceRecord`。向 World 发布的是从该 record 白名单派生的 `minecraft.embodied_trace_projection.v1` receipt，而不是原始 payload：回执包含 session/body、trace kind、sequence、hash、相关 observation/intent/command/receipt identity 和内容摘要，禁止包含 `context`、帧正文、facts/parameters 原文或 Perception 正文。每份 receipt 的规范 JSON UTF-8 硬上限为 8 KiB，projection identity 由 session、trace sequence 与 record hash 稳定派生，并作为 World observation 的稳定 `occurrence_id`；相同记录重放复用既有 event/assertion/position，异内容冲突显式失败。World 写入失败不会缓存为已送达，也不会提交 Perception cursor。未知 trace kind、非法字段或超限回执显式失败。

Perception cursor 还必须通过模型传输回执门：世界投影正文以独立 `Text` part 注册 delivery identity，只有最终成功模型 attempt 返回 `exact_present=true`，且 effective UTF-8 字节数与 SHA-256 同 `minecraft.perception_reference.v1` 完全一致，session 才构造 content-free `PerceptionDeliveryReceipt` 并提交。裁剪、缺失、重复、未知回执或旧回执复用都显式失败，游标保持原位。

这条单向边界专门阻止 `World → Perception → Intent → Trace → World` 递归反馈。它不使用关键词过滤：用户或爱莉的合法文本即使包含字段同名词也仍保留在其权威位置，只是不会通过 trace receipt 复制。既有历史 assertion 不由 Minecraft 生产者删除或改写；历史隔离、分页和 superseding projection 由 World/Perception owner 通过可审计契约处理。

## Biomimetic Body：完全仿生路线

Windows 原生身体绑定到精确 Minecraft 窗口：

- DXcam 读取第一人称窗口帧，帧包含不可变序号、时间和摘要；
- `SendInput` 发送键盘、鼠标和视角动作；
- 支持移动、攻击、使用、物品栏、聊天、快捷栏、视角、HUD、调试界面、第三人称和全屏切换；
- 窗口失去前台、目标窗口消失或输入超时都会释放所有按键；
- 模型规划器可以同时接收画面与动态能力，不依赖硬编码关键词路由。

这条路线最接近人类身体，适合检验第一人称视觉能力、特殊 GUI 和没有结构化接口的模组。它依赖前台窗口，不应在同一 Windows 桌面上与人类同时争抢键鼠。

## 一起玩与第三种轻量身体

真正独立的“你和爱莉各有一个角色”需要两个已授权 Minecraft 账号。推荐为爱莉运行第二个可见客户端；若走仿生路线，再给它独立 Windows 会话、虚拟机或实体设备，避免输入焦点互相抢夺。

N.E.K.O 的公开 Minecraft 插件采用 Mineflayer/Mindcraft：机器人账号加入局域网或服务器，具有技能库、任务回执、截图和 Prismarine Viewer。这很适合做低资源、无前台依赖的第三种身体。可以复用其技能分解和 tick 后置条件确认思想，但接入时必须经过本项目的认证、序号、租约和证据契约。它的合成视角不能替代真实客户端作为主直播画面。

## OBS 与直播

OBS 使用 Game Capture。当前 Java 客户端在窗口枚举中不可选时，使用“捕获任何全屏应用”并让 Minecraft 进入全屏，已经验证能稳定得到真实游戏画面。OBS 和游戏应运行在同一 GPU；仿生视觉仍建议保持窗口模式，因为独占全屏会影响 DXcam 访问。

直播生产环境建议拆成两个场景：

1. `Elysia-Agent`：可见第二客户端，全屏 Game Capture，作为主画面；
2. `Elysia-Biomimetic-Lab`：窗口模式，Window/Display Capture，用于第一人称模型实验。

推流密钥、公开服务器地址、白名单和封禁策略不进入仓库。没有明确授权时，验收只录制本地视频，不开启直播。

## 服务器路线

生产服务器应把身份、行为和观测分层：正版认证、白名单、最小权限、分世界/分区保护、速率限制、审计日志、定期备份和一键隔离爱莉角色。游戏内动作失败只影响当前身体会话，不应中断爱莉的意识、记忆或聊天服务。

## 验收门槛

- 契约测试：认证、重放拒绝、序号、去重、租约、超时、中断和恢复；
- 执行测试：真实类型化动作或 Baritone 位移有世界证据，停止和释放有终态回执；
- 仿生测试：真实鼠标视角变化，同时游戏结构化观测交叉确认，前后帧摘要不同；
- 直播测试：OBS 预览无黑屏，并产生可播放的本地录制；
- 故障测试：死亡、窗口丢失、连接重建和观测积压不会留下卡键或伪造成功。

生产部署、固定摘要、真实烟雾测试和故障恢复步骤见[《Minecraft 生产运行手册》](../operations/minecraft_production_runbook.md)，2026-08-04 的实机证据见[《Minecraft 商业级具身系统审计与生产交付报告》](../report/minecraft-commercial-audit-2026-08-02.md)。
