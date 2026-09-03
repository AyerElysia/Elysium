# Minecraft 商业级具身架构

## 目标

让爱莉以同一个主体在 Minecraft 中持续感知、形成意图、行动、核验结果并恢复失败。系统不替她规定“应该做什么”，只提供可观察、可中断、可审计的身体能力。

核心原则是“一个持续主体、一个专属场景意识、多类身体、统一证据契约”：

```text
爱莉的持续主体 / 统一记忆
        │ 固定主体投影 + 近期潜意识
        ▼
Minecraft 专属意识 ── 自己观察、说话、做事、等待或离场
        │ 高层任务 + speech（默认陪玩路径）
        ▼
身体任务引擎 ── 单一 BodyGate、任务期限、进度/终态事件
   ┌────┴────────────────────────────┐
   │                 │               │
Bot Body          Agent Body    Biomimetic Body
Mineflayer        NeoForge      DXcam + SendInput
独立同服玩家       Baritone      原生前台输入
   │                 │               │
   └──────────── Minecraft ──────────┘
        │ 聊天/玩家/生命/任务事件
        ▼
耐久 Life Event ── ACK ── 提前唤醒专属意识
```

外部 `nucleus_minecraft(intent=...)` 仍可走“开放文本意图 → 具身模型规划器 → 动态低层能力”的专家入口；它与专属意识入口共用同一个 intent lock、证据链和身体所有权，不与陪玩主循环并发抢控制。

## 统一契约

每次行动包含唯一标识、身体类型、动作名、参数、截止时间和期望的可验证结果。执行结果不是一个布尔值，而是一组终态回执：`succeeded`、`failed`、`timed_out`、`cancelled` 或 `rejected`，并附带行动前后观测序号与证据。

运行时只接受当前动态能力清单中的动作；同一行动重试通过标识去重；身体租约避免两个执行器争抢控制；取消和 `control.release_all` 是终止路径的一部分。每个关键事件写入追加式哈希链，便于复盘直播事故与恢复执行。

这些是操作协议，不是对爱莉思维的分类。系统不使用关键词替她决定目标，也不伪造“已完成”。

## Bot Body：默认陪玩路线

Bot 作为独立玩家加入用户已经打开的 LAN 世界或离线模式服务器。它用 Mineflayer 采集结构化事实、pathfinder 导航，并通过 CollectBlock 执行连续采集。专属意识只挑选小而稳定的高层能力集合：跟随玩家、走向玩家/坐标、采集、合成、放置和进食；任务执行器持续跑任务，意识仍可说话、看状态、取消或替换任务。

每个任务有稳定 task ID 与载荷摘要，同 ID 同载荷只重放状态，异载荷显式冲突；单一 BodyGate 禁止路径、挖掘和低层控制竞争；默认 180 秒技术期限避免永远卡住。accepted 只证明接单，progress 和 terminal 作为身体事件耐久投递，后置观察才证明世界变化。

游戏公共聊天、私聊、系统消息、玩家进出、生命变化、死亡、spawn、断开及任务状态先进入身体侧有界 journal。控制器必须把 FIFO 头写入统一 Life Event，成功后才 ACK；重连只接受同一 instance/能力/元数据，未 ACK 事件原样重放。这样“用户在游戏里说话”是真正的事件唤醒，不依赖下一次轮询碰巧读到聊天环形缓冲。

## Agent Body：可见客户端路线

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

### 专属 Minecraft 意识

Minecraft 不再借用核心 heartbeat 充当游戏回合。身体就绪、Presence 注册和场景打开后，session 才启动独立的 `MinecraftConsciousnessRuntime`；核心 heartbeat 的周期与载荷不因 MC 活跃而改变。这个运行时属于同一个爱莉主体，但拥有自己的 session、stream、即时观察、运行阶段和受管任务。

它在启动身体前先固定一份 `projection_kind=minecraft` 的统一主体投影，并对 `SOUL.md`、`USER.md`、`MEMORY.md` 的派生摘要、版本、哈希和 UTF-8 字节数做 fail-closed 校验。没有可证明的主体投影就不启动身体，不回退到平行 persona。每个真实轮次都重新取得结构化游戏观察、可用时的第一人称 JPEG 像素、有界近期潜意识和最近结果引用；模型请求必须证明主体与观察正文被完整、精确送达，裁剪、重复或错配均不允许行动。

场景模型只选择技术生命周期形状：说话并可启动一个 advertised 高层任务、按自己选择的时长继续观察，或结束本次游戏。代码不按关键词替她规定情绪、目标或“应该回应谁”。`wait` 的时间只是她选择的下一次重新观察时间，并受技术上下限约束；聊天、任务终态、玩家进出、生命变化、外部中断和停止信号可以提前唤醒。模型失败按有界退避重试，不把空响应写成主体决定，也不阻塞核心意识。

每个决定先以 `minecraft_consciousness_decision` 归属到当前 `minecraft` instance/session/stream 的不可变 Life Event，并保留该轮 provider reasoning、原始 assistant message 和 transport request 身份，再允许发送 speech 或启动高层任务。高层意识决定“做什么”，身体任务引擎只负责连续执行；外部开放文本意图使用的 evidence-driven planner 只决定“如何做”，其每次成功模型生成也在动作前以同一 instance 记录完整 reasoning/message，并绑定 intent revision、observation IDs 与 receipt IDs。终态事件和动作后新观察回到下一轮。状态接口公开 phase、turn count、当前 decision、最近错误、连续失败、主体引用和剩余会话时间，便于现场判断是主动等待、模型退避、身体执行还是故障。

### 重放与完成语义

命令账本以 command ID 和规范化载荷摘要去重。完全相同的重试复用已有 ack/终态；若重试发生在原命令仍 pending 时，新连接先得到 ack，并在原执行完成后收到同一份终态，不会重复执行或永久停在“处理中”；相同 ID 配不同载荷被拒绝；pending 不会因有界终态缓存淘汰而丢失。接单回执不代表任务完成，结论必须引用终态回执及其后的新观察。每个 session 将这些证据写成追加式哈希链。

### 潜意识近期上下文、Trace 与 World 的单向边界

Minecraft 专属意识的每个高层轮次从 `LifeEngineService` 只读获取一次有界的 `RecentSubconsciousContext`，作为跨意识连续性的默认来源。它投影已经提交的近期 CONSCIOUS_ACTIVITY、HEARTBEAT、TOOL_CALL、TOOL_RESULT 和 AGENT_RESULT 因果组，不包含 MESSAGE 或私有 rolling payload，也不 drain、不推进游标、不写回事件。工具参数和结果在权威事件中保持完整；普通短项可以原样投影，超大项只能给明确的 UTF-8 excerpt、hash、original_bytes 与 occurrence ref，绝不能把节选伪装成全文。模型必须把它视为同一主体过去活动的归属上下文，而不是新指令或当前 Minecraft 世界事实。由这个场景意识随后发出的具身意图不会再次注入同一正文；外部工具直接发起的独立意图仍按自身边界读取一次。游戏 Bridge 的结构化观察、动作后新观察和第一人称画面仍是当前世界证据，不受这条链路影响。

Minecraft 意图上下文在结构上分为 `durable_context` 与 `transient_prompt_context`。session、stream 和目标等耐久运行身份进入前者；`recent_subconscious_context` 正文只进入后者，并且只有规划器的 `to_prompt()` 可以读取。`to_wire()` 和追加式 trace 不复制正文，只保存 content-free `minecraft.recent_subconscious_reference.v1`：算法版本、内容哈希、事件序列窗口、因果组计数、截断状态和 UTF-8 字节数。模型请求把正文作为单独的动态 `Text` part 发送，明确标注为过去上下文。

Trace listener 只接收已经 fsync 并带 sequence/hash 的 `TraceRecord`。向 World 发布的是从该 record 白名单派生的 `minecraft.embodied_trace_projection.v1` receipt，而不是原始 payload：回执包含 session/body、trace kind、sequence、hash、相关 observation/intent/command/receipt identity 和内容摘要，禁止包含 `context`、帧正文、facts/parameters 原文或潜意识正文。每份 receipt 的规范 JSON UTF-8 硬上限为 8 KiB，projection identity 由 session、trace sequence 与 record hash 稳定派生，并作为 World observation 的稳定 `occurrence_id`；相同记录重放复用既有 event/assertion/position，异内容冲突显式失败。World 写入失败不会缓存为已送达，未知 trace kind、非法字段或超限回执显式失败。

这条单向边界阻止潜意识正文经 `Intent → Trace → World` 回灌，也切断旧的 `World → Perception → Intent → Trace → World` 递归反馈。World 不再承担 Minecraft 跨意识同步的默认来源；它仍保留游戏场景和有界 trace receipt 等正式世界记录。既有历史 assertion 不由 Minecraft 生产者删除或改写；历史隔离、分页和 superseding projection 仍由 World/Perception owner 通过可审计契约处理。

## Biomimetic Body：完全仿生路线

Windows 原生身体绑定到精确 Minecraft 窗口：

- DXcam 读取第一人称窗口帧，帧包含不可变序号、时间和摘要；
- `SendInput` 发送键盘、鼠标和视角动作；
- 支持移动、攻击、使用、物品栏、聊天、快捷栏、视角、HUD、调试界面、第三人称和全屏切换；
- 窗口失去前台、目标窗口消失或输入超时都会释放所有按键；
- 模型规划器可以同时接收画面与动态能力，不依赖硬编码关键词路由。

这条路线最接近人类身体，适合检验第一人称视觉能力、特殊 GUI 和没有结构化接口的模组。它依赖前台窗口，不应在同一 Windows 桌面上与人类同时争抢键鼠。

## 一起玩与社区经验

当前默认 bot 路线直接满足“你和爱莉各有一个角色”：用户客户端保留自己的账号，爱莉以不同的离线用户名加入开放到 LAN 的世界。若服务器开启正版认证，则仍需为爱莉提供独立授权账号，不能用昵称伪装认证。

N.E.K.O 的公开 `game_agent_minecraft` 插件同样采用独立 Minecraft 任务、稳定 task ID、WebSocket、截图/状态缓存、busy-skip、技术超时与重连。Neuro SDK 把游戏集成表达成动态动作注册、即时结果与小而有意义的上下文。Elysium 采用这些已验证的结构经验，同时保留自身更严格的 HMAC 认证、单一 BodyGate、不可变 Life Event、FIFO ACK、重放冲突和 exact-context 证明；不会复制根据模型文字猜测“是否卡住”的规则。

## OBS 与直播

OBS 使用 Game Capture。当前 Java 客户端在窗口枚举中不可选时，使用“捕获任何全屏应用”并让 Minecraft 进入全屏，已经验证能稳定得到真实游戏画面。OBS 和游戏应运行在同一 GPU；仿生视觉仍建议保持窗口模式，因为独占全屏会影响 DXcam 访问。

直播生产环境建议拆成两个场景：

1. `Elysia-Agent`：可见第二客户端，全屏 Game Capture，作为主画面；
2. `Elysia-Biomimetic-Lab`：窗口模式，Window/Display Capture，用于第一人称模型实验。

推流密钥、公开服务器地址、白名单和封禁策略不进入仓库。没有明确授权时，验收只录制本地视频，不开启直播。

## 服务器路线

生产服务器应把身份、行为和观测分层：正版认证、白名单、最小权限、分世界/分区保护、速率限制、审计日志、定期备份和一键隔离爱莉角色。游戏内动作失败只影响当前身体会话，不应中断爱莉的意识、记忆或聊天服务。

## 验收门槛

- 意识测试：统一主体投影 fail-closed；观察和像素精确送达；无聊天输入仍能形成决定；决定先落账再行动；模型空响应只触发有界退避；
- 契约测试：认证、重放拒绝、序号、去重、BodyGate、任务期限、FIFO 事件 ACK、中断、同一身体重连和未 ACK 重放；
- 执行测试：真实类型化动作或 Baritone 位移有世界证据，停止和释放有终态回执；
- 连续陪玩测试：保持聊天静默时专属 scene 的 turn 仍按自身选择推进，核心 heartbeat 不加速；游戏聊天能提前唤醒并原样回到游戏；与用户同服 15–30 分钟并完成观察、决定、高层任务、终态事件、新观察闭环；
- 仿生测试：真实鼠标视角变化，同时游戏结构化观测交叉确认，前后帧摘要不同；
- 直播测试：OBS 预览无黑屏，并产生可播放的本地录制；
- 故障测试：死亡、窗口丢失、连接重建和观测积压不会留下卡键或伪造成功。

生产部署、固定摘要、真实烟雾测试和故障恢复步骤见[《Minecraft 生产运行手册》](../operations/minecraft_production_runbook.md)，2026-08-04 的实机证据见[《Minecraft 商业级具身系统审计与生产交付报告》](../report/minecraft-commercial-audit-2026-08-02.md)。
