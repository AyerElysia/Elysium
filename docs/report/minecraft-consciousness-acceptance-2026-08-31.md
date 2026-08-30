# Minecraft 专属意识生产验收记录（2026-08-31）

## 当前结论

状态：**自动化发布候选已通过；真实同服端到端待验收。**

本轮已经把 Minecraft 从“核心 heartbeat 活跃时加速并附带画面”改为同一爱莉主体下独立的 `minecraft` 场景意识。候选代码尚未以真实 Elysium + Minecraft session 跑完现场闭环，因此本报告不把它表述为生产已经跑通，也不授权 AI 启动、停止或重启 Elysium、Minecraft、Java 或 NapCat。

## 已实现契约

- 身体启动前固定并验证 `projection_kind=minecraft` 的统一主体投影；身份缺失时 launch 次数为 0。
- 独立 `MinecraftConsciousnessRuntime` 拥有自己的受管任务、Presence、phase、轮次、失败退避、session 上限与停止信号；核心 heartbeat 周期和载荷不再随游戏变化。
- 每个轮次读取新的结构化观察、可用的第一人称 JPEG 像素、有界近期潜意识和 content-free 最近结果；主体、观察和非空潜意识都要求 exact delivery receipt。
- 模型只通过技术 envelope 自主选择开放文本 `pursue`、自定下一次观察时间的 `wait` 或 `end_session`。代码不使用关键词替主体选择目标、情绪或是否回应玩家。
- `minecraft_consciousness_decision` Life Event 先落账，随后才能进入具身 planner；存储重试复用同一 decision ID，进入物理执行后不自动重放未知动作。
- 高层场景轮次只读取一次近期潜意识；随后产生的 planner 意图不重复注入正文。World 只收到不超过 8 KiB 的 content-free trace receipt，完整证据保留在哈希 trace。
- 专属意识、外部 MC 工具和中断路径使用同一 intent lock；停止按“意识停机信号 → 身体中断 → 等待意识 → 身体关闭 → scene/Presence”顺序清理。

## 自动化证据

| 门 | 结果 |
|---|---|
| Minecraft Python + service 接线 | 105 passed |
| 专属意识与商业 session 精确测试 | 32 passed |
| Life Engine 全集 | 1851 passed / 14 skipped |
| Mineflayer bot | 3 passed |
| NeoForge Bridge | Gradle test build successful |
| 全仓单进程与覆盖率 | 4919 passed / 20 skipped；coverage 72.14%；exit 0 |
| 编译、Minecraft Ruff、跨模块 E9/F/I、format、diff-check | 全部通过 |

全仓门还暴露并修正了两个与 MC 无关但会让验收不确定的基线问题：NapCat HTTP 状态测试误用了带多次重试的外层接口；API v1 把显式空 `environ` 错当成未提供并回退真实进程环境。两处都保留生产安全语义，并已在完整进程顺序中通过。

## 现场端到端矩阵

以下项目必须来自同一次真实 session。现场完成后，在“结果/证据”列填写时间、session ID 和证据位置。

| 验收项 | 当前状态 | 结果/证据 |
|---|---|---|
| 用户手动启动 Elysium；用户世界或固定 25565 LAN 已就绪 | 待现场 |  |
| 正式工具 `preflight → start → status`；身体 playable | 待现场 |  |
| subject reference、instance/session/stream 一致；scene task running | 待现场 |  |
| 用户静默至少 10 分钟，turn 仍推进且核心 heartbeat 不加速 | 待现场 |  |
| 新观察 → 决定落账 → 意图 → 命令 → 终态 → 新观察 → 结论 | 待现场 |  |
| agent 原生 JPEG 或 bot 结构化聊天/玩家证据真实送达 | 待现场 |  |
| 与用户同服连续 15–30 分钟，能自主同行、交流或做事 | 待现场 |  |
| 运行中 interrupt 释放控制、终态可诊断、下一轮可恢复 | 待现场 |  |
| 连续轮次 prompt/World 体积不递归增长，receipt ≤ 8 KiB | 待现场 |  |
| stop 后 Presence/scene 结束；用户游戏保持运行 | 待现场 |  |

现场证据应从这些正式位置提取：

- `nucleus_minecraft(status)` 的 body/consciousness 状态；
- `data/life_engine_workspace/minecraft/traces/<session_id>.jsonl` 的哈希链；
- Life Event 中 `content_type=minecraft_consciousness_decision` 的归属事件；
- World 中 `minecraft.embodied_trace_projection.v1` 的有界 receipt；
- LLM 请求检查记录中的 subject/observation exact delivery 与 agent Image part。

## v1 已知边界与后续扩展点

- bot 身体提供最近 16 条结构化游戏聊天；agent 身体当前通过原生像素看聊天，并通过 `players/entities` 读取同服玩家。未来可以为 NeoForge Bridge 增加同样的结构化聊天事件源，但不得让聊天文本直接拥有命令权。
- 当前节奏由模型选择的 `wait` 与动作完成唤醒组成；后续可加入 Bridge 新聊天、危险状态或玩家接近等事件唤醒，仍须保持单轮串行和模型开放判断。
- v1 最近结果只保留 content-free 有界引用。若以后引入场景长期计划，应落独立可恢复状态并有版本契约，不能重新把完整 prompt、World perception 或像素写回下一轮。
- bot 的离线登录只适用于 LAN 或 `online-mode=false`；正版在线服需要独立授权账号，不能以昵称冒充认证身份。

详细设计见[《Minecraft 专属意识运行时》](../architecture/Minecraft专属意识运行时.md)，现场操作与验收步骤见[《Minecraft 生产运行手册》](../operations/minecraft_production_runbook.md)。
