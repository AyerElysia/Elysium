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
| Minecraft Python + service 接线 | 107 passed |
| 专属意识与商业 session 精确测试 | 33 passed |
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

### 现场尝试记录：03:17 的 status-only 假启动

- 用户客户端已进入 `Elysian Realm` 并以固定端口 `25565` 对局域网开放；宿主端口 owner 为用户的 Minecraft 进程，WSL 连通检查成功。
- KOOK 邀请进入表达层后，请求检查器记录到 `tool-nucleus_minecraft` 与 `action-life_send_text` 同轮调用。Minecraft 参数实际是 `action=status`，回执明确为 `active=false`、`readiness=idle`、`bridge_connected=false`、`consciousness.phase=not_started`；当时只有用户的一个 Java 进程，18765/18766/18767 均未监听。
- 同轮可见消息却说“我这就启动身体”，随后表达层因已经发送可见回复而结束本轮，没有让模型读取 status 回执并继续 `start`。因此该次尝试判定为失败，不能计入端到端验收。
- 已增加 Minecraft 专用因果门禁：同轮 Minecraft 调用与可见消息不能并行闭合；可见消息会被延后，模型必须先读取完整回执再行动或回复。status 回执也显式标记 `status_query_only=true`、`started_by_this_call=false`，inactive 时给出正式 bot start 提示；工具说明禁止用 status 代替 start 或提前宣称成功。
- 新增回归证明 status 会执行而同轮假承诺不会发送，运行态进入 follow-up；Minecraft 工具定向测试共 8 项通过。03:30 之后的 Elysium 实例已加载该修复，真实 `start → playable → 专属意识` 仍按下表继续取证。

### 现场尝试记录：03:55 的主体投影逐字节拒绝

- 用户再次发送明确的 `action=start, body_name=bot` 后，请求检查器证明正式 `nucleus_minecraft` start 已执行；因果门也正确延后了同轮“身体启动中”文本，没有伪报成功。
- 工具终态为 `success=false`，精确错误是 `Minecraft subject context binding failed: Minecraft subject projection byte count does not match its text`。当时 session 仍 inactive，bot、Bridge 与 sidecar 均未启动，因此游戏内没有反应是启动前置门的真实结果。
- 根因是共享投影渲染器按规范保留末尾换行并据此计算 UTF-8 字节数与 SHA-256，而 Minecraft 绑定层先对正文调用 `strip()`，删除换行后再与原元数据比较。生产投影因此稳定相差 1 字节；旧测试使用无末尾换行的 ASCII 假快照，未覆盖真实格式。
- 修复后绑定层保留快照原始正文，只用 `text.strip()` 判断“是否全空白”，预算、字节数、哈希、来源清单和 profile 校验仍全部 fail closed。新增中文 UTF-8 + 末尾换行回归，并把商业 session 夹具改成生产同形快照。该修复必须在用户手动重启 Elysium 后重新执行真实 start 才能计入现场闭环。

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
