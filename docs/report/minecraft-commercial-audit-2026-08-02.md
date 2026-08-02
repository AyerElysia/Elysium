# Minecraft 商业级具身系统审计与交付报告

日期：2026-08-02

## 结论

原实现展示了“感知—意图—行动”的正确方向，但不是可发布系统：运行时只接入了关键词驱动的会话控制器，视觉/VLA 链路没有进入正式会话；目标完成可以被模拟；每次输入都启动新的 PowerShell；启动器可能误报就绪，并能误伤其他 Java 进程；整个 Minecraft 模块没有测试。

本次已把它重构为统一具身运行时，并完成两个真实身体的端到端验证：

- Agent Body：NeoForge 1.21.1 + 认证反向 WebSocket + 结构化状态 + Baritone；
- Biomimetic Body：精确窗口绑定 + DXcam 第一人称画面 + Windows 原生输入；
- 两者共享动态能力、行动终态、取消、租约、去重和追加式证据链；
- OBS 已捕捉真实游戏并完成本地录像，没有开启直播。

## 原实现的关键缺口

1. `do_intent` 用关键词选择动作，会把语言表象误当成主体意图。
2. 会话默认只创建 `ConversationalMotorController`，已有视觉、VLA 和执行代码实际上不在生产链路中。
3. 部分目标可以在没有世界证据时返回成功。
4. Windows 输入链路按动作创建 PowerShell 进程，时序、焦点、卡键释放与恢复均不可靠；挖掘动作存在鼠标按键映射错误。
5. 启动逻辑使用固定等待与宽泛进程判断，停止逻辑可能结束不属于本项目的 Java。
6. 不存在认证、重放保护、背压、终态回执、可验证观测序号和 Minecraft 专项测试。

## 本次实现

### 统一运行时

新增严格数据契约、动态能力清单、身体租约、行动幂等、超时/取消、终态证据和哈希链追踪。模型输出必须是可校验 JSON；未知动作直接拒绝，不猜测、不回退到关键词规则。

### NeoForge 桥与 Agent Body

桥接模组针对现有 NeoForge 21.1.219 构建，连接由 Windows 游戏端反向发往 WSL。握手使用共享密钥，消息使用 HMAC，客户端序号与动作标识防止重放。观测串行发送且有有限背压，控制结果不随普通帧丢弃。

Baritone 作为确定性执行器处理导航类意图；每次执行的开始、进度、停止和释放都有回执。死亡状态会暴露明确的复活动作，而不是在死亡画面上假装继续执行。

### 原生仿生身体

原生 sidecar 不再按动作启动脚本，而是持久运行。它绑定精确窗口和进程，捕捉真实第一人称帧并批量发送键鼠输入；异常、取消或窗口丢失都会执行 `release_all`。模型可操作移动、鼠标、物品栏、聊天、快捷栏，以及 F1/F3/F5/F11/Tab 等客户端控制。

### N.E.K.O 参考审计

Project N.E.K.O 仓库中的 `game_agent_minecraft` 插件使用独立正版账号、Mineflayer、任务/聊天/物品栏消息和截图；其下载包内是 MIT 许可的 Mindcraft 源码。v0.1.1 的技能实现加入 tick 后置条件确认与重试，这一点值得吸收。

它当前仍是实验性方案：WebSocket 监听 `0.0.0.0` 且没有认证，任务协议以自由文本和时间窗口为主，画面是 800×512、视距 6 的 Prismarine 合成渲染，也没有本系统的序号、租约和证据链。因此将它定位为可选的第三种轻量服务器身体，而不是替换可见 NeoForge 客户端。

本地仅下载并静态审计，未执行第三方代理包：

- v0.1.0 SHA-256：`1808165269075F152F12E95BE712930964488EFB7366437331EA32C9C7167D53`
- v0.1.1 SHA-256：`6C785ED34A0957933DBCCE214D167D5F70D397C75460D964D4394DB9C79D6C81`

## 真实验收证据

### Agent Body

- Minecraft 版本：NeoForge 1.21.1，NeoForge 21.1.219；
- 桥接 JAR SHA-256：`AC01B0EA16FCCC66575117DA93C3FCFA96C628C35F5889AC3D246A6AB82E3566`；
- 官方 Baritone 1.11.2 JAR SHA-256：`B413CE0AC8061D954E671492E4D826BD4539C8936641F7321A76F0679697CA35`；
- 动态能力包含 `baritone.command`、`chat.send`、`control.release_all`、`native.input_batch`、`player.respawn`；
- 观测序号从 1 前进到 4；
- 真实世界坐标从 `[458.4646843141369, 68, 433.6767815407236]` 变为 `[458.48419306071474, 68, 434.2355443608967]`；水平位移 `0.5591032824092985`；
- Baritone pathing 状态被观察为真；停止和释放均收到终态回执。

### Biomimetic Body

- 绑定窗口标题 `Minecraft NeoForge* 1.21.1 - 单人游戏`；
- 原生输入批次包含 3 个事件；
- 游戏侧交叉观测到 yaw 变化 `-10.350001700000007` 度；
- 输入前 JPEG：154,380 字节，SHA-256 `51315760AC1AB02BB6FC33D646D7692C893A4AF56412D4DC30D7E21EE03FFA16`；
- 输入后 JPEG：153,247 字节，SHA-256 `CA97C644E5761E648E36FC543D40882372600B1E2082B48855696068C5A896F7`；
- 最后执行释放全部输入，测试终态为完成。

### OBS

OBS Studio 32.2.1 portable 的下载 SHA-256 与官方发布值一致：`DB64A2934F8261F85B1410B84BE011207A0AFDA5400D008289F1F1E211BCC7DE`。在同一 GPU 上使用 Game Capture 的全屏应用模式，预览已显示实时 Minecraft，并录制 21 秒、1280×720、16,686,545 字节的本地 MP4。未设置或使用推流密钥。

## 自动化验证

- Minecraft 专项测试：15 个通过；
- Life Engine 相关回归：110 个通过；
- 修改/新增 Python 文件 Ruff 检查通过；
- Windows sidecar Python 编译检查通过；
- NeoForge 模组 Gradle 构建通过，启用 Java 编译警告即错误。

## 商业上线前仍需用户提供的外部条件

1. 爱莉的第二个已授权 Minecraft 账号；否则无法和人类角色真正同时在线。
2. 公共服务器的域名/主机、正版验证、白名单、权限、备份和治理选择。
3. 直播平台账号与推流密钥。密钥应只放在 OBS 的本机配置或秘密管理服务中。
4. 仿生身体若要与人在同一时刻独立操作，需要独立 Windows 会话、虚拟机或另一台设备。

在这些外部条件到位前，本地单人世界、两类身体、真实动作核验和 OBS 录像链路已经闭环。

## 代码交付边界

本次工作落在以下生产边界中：

- `plugins/life_engine/minecraft/embodiment_contracts.py`：身体、能力、意图、观测和终态回执契约；
- `embodiment_runtime.py`、`embodiment_trace.py`：身体租约、幂等、超时/取消和追加式哈希证据链；
- `bridge_client.py`、`bridge_body.py`：带认证、HMAC、序号和背压的持久桥接；
- `model_planner.py`、`session.py`：动态能力驱动的模型规划和商业会话生命周期；
- `launcher.py`、`tools.py`、Life Engine 配置与服务接线：显式身体选择、已有客户端复用和启动就绪检查；
- `integrations/minecraft_bridge/`：NeoForge 1.21.1 客户端模组、状态采集、Baritone/原生操作执行和真实烟雾测试；
- `integrations/windows_native_body/`：DXcam、精确窗口绑定、SendInput、异常释放和画面/输入交叉验证；
- `test/plugins/life_engine/minecraft/`：认证、序号、重放、租约、恢复、模型输出和启动生命周期测试；
- `docs/architecture/minecraft_embodiment.md`：生产架构、直播、服务器与多人共玩约束。

旧 Minecraft 实验模块中 27 条未使用导入和无效 f-string 告警也已机械清理。最终整个 `plugins/life_engine/minecraft`、专项测试和 Windows sidecar 均通过 Ruff，避免新生产链路通过而旧目录仍保持静态检查红灯。

## 本机部署产物与运行状态

- 构建后的桥接模组已安装到现有 Minecraft `mods` 目录；
- 官方 Baritone 1.11.2 已安装并按发布摘要复核；
- Windows 原生 sidecar 使用独立虚拟环境和游戏配置中的随机令牌；
- OBS Studio 32.2.1 portable 已按官方摘要复核并完成本地录制；
- 第三方 N.E.K.O/Mindcraft 下载包只做静态审计，没有执行；
- 推流密钥、账号凭据和桥接令牌均未写入仓库或报告。

## 研究来源与取舍

- Neuro SDK：https://github.com/VedalAI/neuro-sdk
- Project N.E.K.O：https://github.com/Project-N-E-K-O/N.E.K.O
- Mindcraft：https://github.com/mindcraft-bots/mindcraft
- Mineflayer：https://github.com/PrismarineJS/mineflayer
- Baritone：https://github.com/cabaletta/baritone
- Voyager：https://github.com/MineDojo/Voyager
- STEVE-1：https://arxiv.org/abs/2306.00937
- JARVIS-1：https://arxiv.org/abs/2311.05997
- MineStudio：https://github.com/CraftJarvis/MineStudio
- OpenHA：https://github.com/CraftJarvis/OpenHA
- JARVIS-VLA：https://github.com/CraftJarvis/JarvisVLA
- MCCTP：https://github.com/lucasoyen/MCCTP
- OBS Game Capture：https://obsproject.com/kb/game-capture-source
- Minecraft Java 捕捉排障：https://obsproject.com/kb/minecraft-java-edition-troubleshooting

Neuro-sama 的内部 Minecraft 系统是闭源的；社区讨论只能作为混合式架构的线索，不能当作已证实实现。本次取舍依赖可复现的公开 SDK、论文和源码：由模型保留意图与选择权，把高频、确定性的动作交给可验证执行器，同时保留完全第一人称的仿生身体作为通用控制面。

OpenHA/MineStudio 等视觉代理是后续离线研究的重要候选，但其现有模拟器版本和现有 NeoForge 1.21.1 客户端不构成可直接替换关系。本次没有为了模型规模牺牲版本兼容、直播可见性和可验证性。
