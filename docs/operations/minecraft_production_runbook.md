# Minecraft 生产运行手册

## 当前支持范围

生产默认身体是 `agent`：可见的 NeoForge 1.21.1 客户端加载 Elysium Bridge 0.2.1，由游戏主动连接 WSL 中的 Life Engine。它提供结构化世界观察、经过类型校验的动作、Baritone 导航、终态回执和哈希证据链。

`biomimetic` 是可选实验身体，使用 DXcam 与 Windows 原生输入。它依赖唯一的前台 Minecraft 窗口，不能与人同时争用同一桌面的键鼠，也不能在旧 sidecar 仍运行时启动新实例。生产任务应保持 `default_body = "agent"`。

`bot` 是无头共享世界身体：一个 mineflayer 玩家作为独立账号加入服务器或局域网世界，与使用自己客户端的人类处在同一个世界里。它实现与 `agent` 完全相同的 `elysium.minecraft.bridge/1` 协议与操作集（导航由 pathfinder 承担 Baritone 的角色），不需要 Windows 客户端、窗口控制或 quick-play 参数。它的用途是"一起玩"，不是单人托管世界的生产默认路径。

## 固定环境

- Minecraft：1.21.1
- NeoForge：21.1.219
- 世界：`Elysian Realm`
- 游戏目录：`G:\Game\Minecraft\.minecraft`
- 启动脚本：`G:\Game\Minecraft\PCL\LaunchElysia.bat`
- Elysium Bridge：`elysium_bridge-0.2.1.jar`
- Bridge SHA-256：`F6B80E166F8C3EDA683020C8154D817DA3098873AE9ECDF6161F05C8FF8A50DC`
- Baritone：`baritone-unoptimized-neoforge-1.11.2.jar`
- Baritone SHA-256：`B413CE0A2754A3C8484AAE39875CF84BE1F999DEE208E86D41B3D0D329D5CA35`

版本、文件名和摘要属于同一部署契约。只替换 JAR 而不更新锁文件、配置、测试和真实验收会被启动前检查拒绝。

## 首次部署或升级

所有脚本都会校验精确的托管目录。游戏正在运行时，它们会拒绝修改模组，不会自行关闭游戏。

1. 在 WSL 中构建桥接模组：

   ```bash
   cd integrations/minecraft_bridge
   ./gradlew clean test build --no-daemon
   ```

2. 用户手动关闭托管 Minecraft 客户端后，在 Windows PowerShell 中部署桥接：

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
     "integrations\minecraft_bridge\deploy_bridge.ps1"
   ```

   旧 Elysium Bridge 会被移动到 `mods\elysium-disabled\<时间戳>`，不会删除。

3. 准备快速进入专用世界的启动参数：

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
     "integrations\minecraft_bridge\prepare_launcher.ps1"
   ```

   脚本只接受托管的 PCL 启动脚本，并保留 `.pre-elysium-quickplay.bak` 备份。最终参数必须包含 `--quickPlaySingleplayer "Elysian Realm"`。

4. 在 `config/plugins/life_engine/config.toml` 中启用：

   ```toml
   [minecraft]
   enabled = true
   default_body = "agent"
   world_name = "Elysian Realm"
   mc_home = "/mnt/g/Game/Minecraft/.minecraft"
   launch_bat = "G:\\Game\\Minecraft\\PCL\\LaunchElysia.bat"
   launch_dir = "G:\\Game\\Minecraft\\PCL"
   require_quick_play = true
   expected_bridge_version = "0.2.1"
   shared_world_enabled = true
   agent_shared_username = "Elysia"
   consciousness_enabled = true
   consciousness_task_name = "agent"
   consciousness_subject_context_max_bytes = 16384
   consciousness_observation_max_bytes = 16384
   consciousness_subconscious_max_bytes = 8192
   consciousness_subconscious_group_limit = 5
   consciousness_min_wait_seconds = 2
   consciousness_max_wait_seconds = 45
   consciousness_retry_base_seconds = 2
   consciousness_retry_max_seconds = 30
   consciousness_recent_turn_limit = 6
   consciousness_stop_timeout_seconds = 10
   max_session_minutes = 60
   ```

   其余摘要、文件名、监听地址和超时使用代码中的已验证默认值。令牌由桥接首次启动写入 `config/elysium_bridge.json`，不得复制到仓库或日志。

5. 由用户手动重启 Elysium。AI 和部署脚本均不得替用户停止、重启或拉起 Elysium，也不得停止或重启已有 Minecraft 进程。NapCat/QQNT 的自动恢复按根目录 `AGENTS.md` 的独立生命周期规范执行，本手册中的 Minecraft 脚本不管理它。

## 专属 Minecraft 意识、共享世界与原生视觉

- `shared_world_enabled = true`（默认）时，agent 身体不再进入本地单人世界，而是通过自动生成的 `LaunchElysiaShared.bat` 以 `--quickPlayMultiplayer "<WSL网关>:<bot_server_port>"` 直连人类玩家开放的局域网世界；游戏内用户名为 `agent_shared_username`（默认 `Elysia`，必须与人类玩家区分）。
- 该模式下 preflight 跳过单人世界与 `--quickPlaySingleplayer` 校验（改为共享世界语义）。
- 身体完成预检和 playable 判定后，session 启动独立 `minecraft` 意识实例。它不是核心 heartbeat 的“加速模式”：核心 heartbeat 始终保持原周期和原载荷，专属实例拥有自己的串行模型轮次、Presence、phase、失败退避、session 上限与停止信号。
- 她拥有自己的客户端窗口，即她自己的眼睛：每个专属意识轮次调用 `session.grab_vision_frame_bytes()` 截取第一人称 JPEG，并作为原生 `Image` part 直接进入多模态请求，不做文字转述。无窗口可截时（如 bot 身体）保留结构化观察并显式没有像素；不会伪造画面。
- 每轮还读取新的 Bridge 结构化观察、同一主体的固定身份投影、有界近期潜意识和 content-free 最近结果。agent 能从像素与 `players/entities` 感知同服玩家；bot 额外提供最近 16 条游戏聊天/私聊/系统/加入/离开事件的结构化环形缓冲。
- 场景模型可以形成开放文本意图、主动等待一段自己选择的时间，或离开本次游戏。新动作结果和外部中断会提前唤醒；无弹幕、无聊天消息时仍会按自己的节奏继续观察与选择。模型空响应只让该 scene 有界退避，不会卡住核心 heartbeat，也不会被伪装成一次有效决定。
- `game_turn_interval_seconds` 与 `consciousness_interval_seconds` 仅为旧配置兼容项，已不改变任何运行节奏。不要再通过缩短核心 heartbeat 获得“连续游玩”。

## 无头 bot 身体（共享世界）

bot 身体用于和人一起玩同一个世界：人类用自己的客户端进入世界，bot 以 `bot_username` 账号加入同一世界。它与 `agent` 共用协议、操作契约、命令账本、trace 证据链与 Presence/scene 投影，只是身体侧实现换成了 mineflayer。

### 部署

1. 安装锁定的 Node 依赖（Node 20.10 及以上；桥接显式依赖 `ws`，不依赖 Node 22 的全局 WebSocket）：

   ```bash
   cd integrations/minecraft_bot
   npm ci
   ```

2. 在 `config/plugins/life_engine/config.toml` 的 `[minecraft]` 段配置目标世界：

   ```toml
   bot_server_host = "auto"
   bot_server_port = 25565
   bot_username = "Elysia"
   ```

   `bot_server_host = "auto"` 会在启动时自动解析 WSL 默认网关（即 Windows 宿主机），WSL 重启后 IP 变化也无需改配置；`bot_username` 必须与人类玩家的游戏名不同。监听地址、观测周期、实体半径和令牌路径使用代码默认值。令牌由 session 首次启动以排他创建方式生成于 `data/life_engine_workspace/minecraft/bot_bridge_token.json`（0600），并发首次启动也不会互相覆盖；令牌不得复制到仓库或日志。启动时监听端口 18767 若被占用，监听器绑定会直接失败并返回可诊断原因，不会抢占已有进程。

   当前随仓库交付的是 Mineflayer `offline` 登录路径，适用于“对局域网开放”的单人世界或 `online-mode=false` 的专用服务器。普通 `online-mode=true` 服务器需要单独购买并交互式登录一个 Microsoft/Minecraft 账号；本实现不会把昵称伪装成已认证账号。

### 一起玩的操作路径

1. 用户在自己的 Minecraft 客户端进入世界；若是单人存档，先"对局域网开放"，在端口号框里**固定填写 `25565`**（红字为无效端口，白字才能创建；必须与 `bot_server_port` 一致），配置只需设置一次；专用服务器则填服务器地址与端口。
2. 通过正式工具调用 `nucleus_minecraft(action="start", body_name="bot")`。该工具同时注册在 chat 意识清单中，她在聊天对话里就能直接调用，不需要切换到其他意识。session 负责唯一的 bot 进程生命周期：启动 node 子进程、等待桥接认证、等待服务器世界就绪。
3. 就绪判定为 `server_world`：`world_loaded=true`、存在 `world` 事实（mode/server_address）且玩家有 UUID。与 `agent` 不同，它不校验单人世界名称和客户端暂停状态。
4. `stop` 由 session 终止其拥有的 bot 进程并断开桥接；`game_left_running` 对 bot 恒为 `false`，人类的游戏客户端不受任何影响。

### 观测差异

bot 观察 facts 与 `StateCollector` 结构对齐（world/player/players/entities/inventory/crosshair/biome 等），并额外携带有界 `chat` 环形缓冲（最近 16 条公共聊天、私聊、系统、加入、离开事件）、最多 64 名可见玩家、最多 128 个附近实体及 `bot_tasks` 执行状态。桥接出站队列有硬上限；拥塞时优先丢弃过时观察而保留命令回执，并在后续观察报告累计丢弃数。`world.mine` 会先寻路到精确目标，再校验方块未变化且可挖后执行 dig。回执语义不变：只证明接单与派发，导航、挖掘、交互结果必须由后续观察证明。

## 社区实现取舍

- 用户保留的 `MC集成包.zip` / `MC插件源码.zip` 已做源码级对照。其可复用核心是 Mineflayer 独立玩家、pathfinder 长任务、聊天环形缓冲、进程就绪探测和“靠后续状态确认结果”；本实现采用了这些成熟方向。没有直接搬用其静态 secret + 通用 RPC + 分散工具插件链，因为那条链缺少当前 Elysium 所要求的 HMAC 握手、命令幂等账本、统一 Presence/World、content-free durable receipt 和失败后仍可重试的资源所有权。
- Neuro SDK 的公开契约采用“文本状态 + 注册动作 + WebSocket 执行结果”，并明确建议实时游戏把低层操作交给专门执行器。这里沿用这一分层：Life Engine 决定高层意图，Mineflayer/pathfinder 负责连续移动与挖掘，观察而非模型自述证明结果。
- N.E.K.O. 公开仓库展示的是通用插件 SDK/市场与跨场景记忆架构；截至本实现审计时，公开资料中没有可复用、可验证的 Minecraft 专项插件源码。因此只参考其“核心主体与外部插件解耦”的边界，没有把不存在的 MC 实现当作依赖。
- Mineflayer 提供成熟的协议、实体、背包、聊天与物理抽象，适合让爱莉作为独立玩家进入同服；Elysium 额外补上认证桥、幂等命令账本、Presence/World 投影、durable trace 与进程所有权。

参考：[Mineflayer](https://github.com/PrismarineJS/mineflayer)、[Neuro SDK](https://github.com/VedalAI/neuro-sdk)、[Project N.E.K.O.](https://github.com/Project-N-E-K-O/N.E.K.O)。

## 启动与就绪语义

`nucleus_minecraft` 只有在 `minecraft.enabled=true` 时暴露。Minecraft session 由 `LifeEngineService` 独立持有，不依赖 Learning 是否启用。

`start` 成功必须同时满足：

- 精确的启动脚本、世界目录、Bridge 与 Baritone 摘要通过预检；
- Windows/WSL 互操作可用，且没有多个匹配的 Minecraft 窗口；
- 桥接完成共享令牌认证，协议版本与必需能力完全匹配；
- 就绪判定按模式区分：单人模式（`shared_world_enabled = false`）要求游戏报告 `world_loaded=true`、`client_paused=false`、单人世界名称为 `Elysian Realm`，并提供玩家 UUID；共享世界模式（默认）采用 `server_world` 语义，只要求 `world_loaded=true`、存在世界标识与玩家 UUID，不校验单人世界名称与暂停状态；
- 收到至少两条连续前进的完整观察。

标题界面、暂停菜单、错误世界、旧桥接、缺失能力、静止观察或断线都会返回可诊断失败，不能伪装成就绪。已经运行的合规客户端会被复用，不会再启动第三个客户端。

## 操作与证据

Agent Body 只接受显式类型动作：移动/视角、交互、快捷栏、丢弃、聊天、复活、等待、Baritone 导航与挖掘、停止和释放。任意 Baritone 命令字符串不属于生产接口。

命令 ID 与规范化载荷共同进入有界重放账本：完全相同的重试复用已有 ack/终态；若重连时原命令仍在执行，新连接会在原执行完成后收到同一份终态，而不会再次执行；相同 ID 配不同载荷会被拒绝。`accepted` 只表示接单，只有终态回执加后续新观察才能支撑完成结论。

每次 session 的证据写入：

```text
data/life_engine_workspace/minecraft/traces/<session_id>.jsonl
```

记录由 `previous_hash`/`record_hash` 串联，至少应包含 `body.selected`、`intent.issued`、`observation`、`command.issued`、`command.receipt` 和 `intent.conclusion`。

`intent.issued` 的 trace 保存完整耐久意图与 content-free `minecraft.recent_subconscious_reference.v1`，不会保存本轮 `recent_subconscious_context` 或其他 `transient_prompt_context` 正文。每条已落盘 trace 向 World 只投影 `minecraft.embodied_trace_projection.v1` 回执；规范 JSON 必须不超过 8192 UTF-8 字节，并且不得出现原始 payload、context、帧、facts、parameters 或潜意识正文。相同 trace record 的 projection ID 必须稳定并作为 World `occurrence_id`；World 写失败时当前意图显式失败，随后按同一 record 重试会复用既有事件/断言/位置而不是重复落账。

每个专属意识高层轮次从 Life Engine 读取一份有界、只读的近期潜意识因果组作为跨意识连续性上下文。正文只进入该轮模型请求；同一高层决定交给具身 planner 时不会再次注入正文，只在耐久意图中留下 decision ID 与 content-free 引用。外部工具绕过专属意识直接发起的独立意图才会在自身轮次读取一次。该读取没有 cursor、commit 或 drain 语义，也不会回写 Life Event。工具调用只携带 tool name/call ID，不携带原始参数。Bridge 的结构化观察和第一人称画面仍是游戏当前状态的权威证据。

专属意识的可观察决定先写入不可变 Life Event，`content_type=minecraft_consciousness_decision`，并归属当前 instance/session/stream；只有落账成功才会执行 `pursue` 意图。重试复用同一 decision ID、事件 sequence 与时间，不会因为存储结果不确定而重复物理动作。trace 中随后出现的 `intent.issued` 应通过 `consciousness_decision_id` 引用这个高层决定。

## 独立真实烟雾测试

该测试不会启动或停止 Elysium；若游戏未运行会按托管脚本启动，结束时只释放控制并断开桥接，游戏保持运行：

```bash
PYTHONPATH=. uv run --frozen --no-sync python \
  integrations/minecraft_bridge/agent_live_smoke.py
```

成功标准是：进入正确世界，读取新鲜状态，执行一次受限的 5° 视角调整，收到完成回执，并由后续观察证明 yaw 发生对应变化。

## 故障处理

- `world_loaded=false`：等待世界完成加载；标题页不能算就绪。
- `client_paused=true`：由用户返回游戏；暂停的单人世界不能宣称导航和移动就绪。
- 世界名称不匹配：修正 quick-play 参数或显式选择 `Elysian Realm`，不要降低校验。
- Bridge/Baritone 摘要不匹配：重新构建并运行部署脚本，不要跳过锁定。
- `WSLInterop` 缺失：Windows 桥会尝试恢复标准 WSL 注册项；失败时返回明确错误。
- 多个匹配窗口或 sidecar：由用户手动关闭多余实例；系统不会猜测、抢占或终止进程。
- 游戏先断开：客户端清理会释放等待者并幂等结束；断线不能被写成动作成功。
- 模组崩溃：以 crash report 的首个业务栈为准，只隔离有直接证据的模组，并保留可恢复副本。不要批量禁用模组。
- life_chatter 后缀突然变大：检查 World 中 `domain=minecraft/predicate=embodied_trace` 的 value schema。新记录必须是 8 KiB 内的 `minecraft.embodied_trace_projection.v1`；若看到完整 `payload.context`、`transient_world_perception` 或 `recent_subconscious_context`，这是旧递归投影或新的边界违约证据。不要删除或重写历史数据库；保留原文并交由 World owner 做有来源隔离、分页或 superseding projection。

2026-08-04 的真实启动发现 `InventoryProfilesNext 2.2.5`/`libIPN 6.6.3` 在渲染阶段自身空指针崩溃。已验证构建可用以下脚本做精确、可恢复隔离：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  "integrations\minecraft_bridge\quarantine_inventoryprofilesnext.ps1"
```

脚本只识别两份已审计文件及其固定 SHA-256，移动到 `mods\elysium-disabled\incompatible-inventoryprofilesnext-2.2.5`，不会删除其他模组。

## 发布验收门

- Minecraft 专项与 service 接线测试通过；
- NeoForge `clean test build` 通过，产物摘要与锁文件一致；
- sidecar 依赖可从固定 requirements 重建并通过导入检查；
- 真实 `观察→意图→动作→终态回执→新观察→证据结论` 闭环通过；
- 用户手动重启 Elysium 后，再从正式 `nucleus_minecraft` 工具执行一次 start/status/intent/stop；
- stop/close 只释放 Elysium 的控制与 Presence/scene，默认不关闭游戏进程。

### 专属意识真实端到端验收

自动化测试不能代替现场陪玩。发布结论必须保存以下同一次 session 的证据：

1. 用户手动启动或重启 Elysium，并用自己的客户端进入目标世界；一起玩使用 bot 时，用户手动以固定端口 `25565` 对局域网开放。AI 不代替用户操作这些进程。
2. 从正式 `nucleus_minecraft` 执行 `preflight → start → status`。`status` 必须显示身体 playable、`consciousness.phase` 正常、固定 subject reference 存在，且 scene/session/stream identity 一致。
3. 用户至少 10 分钟不发聊天指令。期间 `turn_count` 仍增加，模型可以自主 `pursue` 或 `wait`；核心 heartbeat cadence 不变化，也不出现并发堆积。
4. 至少完成一次真实 `新观察 → 高层决定落 Life Event → 意图 → bridge 命令 → 终态回执 → 更高序号新观察 → 结论`。每个物理动作前都能找到对应 decision ID，不能以 accepted 代替完成。
5. 与用户同服连续 15–30 分钟，验证玩家实体或 bot 的结构化聊天可见、能够同行/交谈/做事，并且她的行动不依赖用户逐轮催促。
6. 检查专属请求的主体、观察正文均有 exact delivery receipt；agent 路线有 JPEG 原生图像 part；观察和潜意识投影分别不超过配置预算。
7. 检查 World 仅收到不超过 8 KiB 的 content-free trace receipt，连续多轮不出现 prompt/World 递归增长；完整原始证据仍留在 session trace。
8. 在一个正在执行的意图中调用 `interrupt`，证明释放控制、返回可诊断终态，并能继续下一轮；最后调用 `stop`，Presence/scene 结束而用户游戏保持运行（bot 身体只结束其受管 bot）。

任何一步缺少真实证据都只能标记为“自动化就绪、现场待验收”，不能写成生产已跑通。详细设计与可扩展边界见[《Minecraft 专属意识运行时》](../architecture/Minecraft专属意识运行时.md)。
