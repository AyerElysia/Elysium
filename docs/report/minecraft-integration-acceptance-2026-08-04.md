# Minecraft 生产具身集成验收（2026-08-04）

## 1. 结论

二号在 `33152c1b` 基线上审查了四号交付的 `19a1acee`，确认 `INT-MC-001` 的组件注册、service-owned session、正式工具消费、结构化观察、类型化动作、Windows 启动与部署、真实游戏证据链均已形成一条一致链路。集成审查没有改变爱莉的意图、记忆或主体权威；Minecraft 组件只执行主体明确发出的意图，并以观察和回执提供证据。

合入前额外发现并修复一个断线竞态：原命令仍在执行时，新连接重放同一 command ID 虽然不会重复执行，但只能收到 pending ack，原终态仍发往旧连接。Bridge 0.2.1 与 Windows native sidecar 现在都把原执行的同一份终态转交给 pending replay；相同 ID 配不同载荷仍显式拒绝，终态缓存仍有界，pending 不被淘汰。

Bridge 0.2.1 同时将首次生成的认证配置改为临时文件写入后原子替换；Agent Body 的预检允许 NeoForge 在首次启动时创建令牌，读取到短暂不完整配置时会继续有界等待。令牌内容不进入日志、健康信息或本文档。

## 2. 最终发布合同

- Minecraft：1.21.1
- NeoForge：21.1.219
- 世界：`Elysian Realm`
- Bridge：`elysium_bridge-0.2.1.jar`
- Bridge SHA-256：`F6B80E166F8C3EDA683020C8154D817DA3098873AE9ECDF6161F05C8FF8A50DC`
- Baritone：`baritone-unoptimized-neoforge-1.11.2.jar`
- 默认身体：`agent`
- 可选实验身体：`biomimetic`

版本、文件名、摘要、hello 元数据和运行时配置必须整体一致。Bridge 0.2.0 的真实闭环证据仍是有效的前序系统证据，但不能冒充 0.2.1 的最终部署证据。

## 3. 独立复核范围

复核包括：

- `nucleus_minecraft` 仅在功能启用时注册，并消费 `LifeEngineService` 唯一持有的 session；
- start/status/intent/interrupt/stop/close 的幂等、失败清理和 Presence/scene 所有权；
- 精确游戏版本、世界、quick-play、Bridge/Baritone 文件名与摘要预检；
- Bridge HMAC 认证、协议版本、单控制者租约、连续观察序号、有限积压和控制释放；
- 类型化 movement/navigation/mining/interaction/inventory/chat/respawn 操作，未暴露任意 Baritone 命令字符串；
- 同 ID 幂等、异载荷冲突、pending replay 终态转交和缓存上界；
- Windows sidecar 的单实例、固定依赖、精确窗口约束与控制释放；
- 部署、quick-play 修改和第三方冲突模组隔离脚本的精确目录、运行中拒绝和可恢复移动；
- 文档中 Elysium 手动生命周期与 NapCat 独立恢复规范的一致性。

## 4. 验证证据

四号在 Bridge 0.2.0 上已经完成真实 `Elysian Realm` 闭环：读取玩家、世界、背包、实体和 Baritone 状态，执行一次 `movement.input` yaw `+5°`，收到 accepted + completed 终态，并由后续观察精确证明 yaw `+5°`。证据位于：

```text
data/life_engine_workspace/minecraft/traces/20260804T151244_791715Z.jsonl
```

该实机验证还证明同一游戏进程运行约 38 分钟后的重新连接仍可认证、观察、执行和清理；暂停的单人世界会显式拒绝为未就绪。第一次启动由第三方 `InventoryProfilesNext 2.2.5` 自身 NPE 失败，只有该模组及 `libIPN 6.6.3` 在游戏已退出后按固定摘要移动到可恢复隔离目录，没有删除文件或停止进程。

集成方对最终 0.2.1 执行了独立构建、专项测试、静态检查和全仓回归：

- Minecraft + service 专项：51 passed；
- NeoForge `clean test build` 连续两次成功，两次 JAR SHA-256 均为 `F6B80E166F8C3EDA683020C8154D817DA3098873AE9ECDF6161F05C8FF8A50DC`；
- 在本轮提交变基前的主线基线上，全仓单进程回归：3,519 passed / 13 skipped，coverage 67.18%，退出码 0；
- 变基到最新 `c18ba91c` 后，全仓单进程回归：3,581 passed / 13 skipped，coverage 67.46%；唯一失败是已登记且不属于 Minecraft 的 Scheduler `test_recurring_task_with_interval_seconds` 时序偶发，随后同一用例单进程连续 10/10 通过；
- 本轮变更 Python 文件 Ruff、格式检查与全树 `git diff --check` 通过。

0.2.1 的改动仅涉及重放终态转交、令牌原子落盘和首次启动预检；由于用户当前 Minecraft 进程必须保持不动，最终 JAR 没有覆盖正在加载的 0.2.0，也没有冒充完成 0.2.1 实机验证。

## 5. 2026-08-05 现场复验

用户结束旧游戏、部署 Bridge 0.2.1 并手动重启 Elysium 后，正式 `nucleus_minecraft` 现场门已经完成。运行中的 Elysium 基线为 `d9612a30`，包含正式工具绑定修复 `f3b577e2` 与 Minecraft 活动恢复过期 Presence 的修复。预检确认：

- 世界为 `Elysian Realm`，quick-play 已配置；
- Bridge 为 0.2.1，JAR SHA-256 为 `F6B80E166F8C3EDA683020C8154D817DA3098873AE9ECDF6161F05C8FF8A50DC`；
- Baritone 为 `baritone-unoptimized-neoforge-1.11.2.jar`，SHA-256 为 `B413CE0A2754A3C8484AAE39875CF84BE1F999DEE208E86D41B3D0D329D5CA35`；
- 既有 Minecraft 进程 PID 54304 与单人世界窗口可用，Elysium 没有替用户启动、停止或重启游戏进程。

第一条正式动作闭环使用 session `20260804T235301_830037Z`：`preflight → start → look → do → look → stop`。动作前观察 `observation_6c20f9f1-1c3d-4be9-8f9e-ea8655086ac3` 位于 `(471.6946, 71, 454.2550)`、yaw `180.9021`；命令 `command_19294465f57b4912994110c4c3d2a448` 收到完成回执 `receipt_6b268093-7d9c-4fab-a958-4aa9ea2a3489`；动作后观察 `observation_56a7efe0-2857-4478-9150-c3ba730bced1` 位置不变、yaw `185.9021`，精确向右转 `5°`，移动、跳跃、攻击与使用物品均未触发，全部控制键为 false。trace 共 7 条记录并通过 `EmbodimentTrace.verify()`，尾哈希为 `e541ef950df0bb67e157eacff4fde6b1967ac664c16e0aa7fb5cb3acf8912203`。随后相隔数分钟出现的玩家移动不属于该命令：受控命令的即时终态观察已经证明位置不变。

Presence 专项复验使用 session `20260805T003635_838383Z`、意识实例 `minecraft_20260805T003635_838383Z` 与游戏实例 `minecraft_335fb687-ecd9-484c-a83f-7c6c17928379`：

1. 08:38:26 注册为 active，revision 1，租约 300 秒；
2. 08:44:08 由正式 heartbeat 按数据库时间转为 suspended，事件 `consciousness.instance_lease_expired`，revision 2；
3. 08:49:23 在不重新 start 的情况下正式调用一次 `look`，得到 `observation_b0fd03e0-78ad-46f0-b4d7-f8e1b82b2243`、sequence 7675、`world_loaded=true`、全部控制键 false；同一实例持久化产生 `consciousness.instance_resumed`，reason `minecraft_look`，revision 3，新租约延至 08:54:23；
4. 08:54:26 正式 `stop` 成功，返回 `game_left_running=true`、`errors=[]`、`cleanup_pending=false`，Presence 以 `minecraft_session_ended` 终止为 revision 5；18765 监听释放，而 Java PID 54304、世界与窗口继续运行并保持可响应；
5. 本 session 的 trace 含 1 条 `body.selected` 记录并通过 `EmbodimentTrace.verify()`，尾哈希为 `b0065cf705bc8fb86ccecdf086067ddb13773d90ab94dc6630434f4579308bbf`。观察本体由 World/Presence 事件链留证，不伪装为动作 trace。

现场同时暴露了一个上层重复投递风险：同一恢复验收 turn 在可见回复发送失败后的延迟 follow-up 中再次选择了 `look`，产生 revision 4 的 `consciousness.instance_seen`。第二次调用没有移动或其他游戏副作用，但证明仅靠模型指令不能保证“只调用一次”。Minecraft 工具因此增加了有界、并发安全的 turn 级语义幂等：同一稳定 unread turn 内，相同 `start/stop/do/interrupt/look` 参数共享同一执行与结果；不同 turn 仍能正常再次操作。该保护不以自然语言关键词判定意图，不改变主体选择，只约束外部副作用的工程幂等。

正式 Life Chatter 首轮上下文约 52 万 token，工具结果 follow-up 约 5.5–7 万 token，导致工具意图端到端延迟达到分钟级。这是通用主体上下文与表达链的生产性能风险，不是 Minecraft Bridge、世界加载或动作协议失败；MC 现场闭环结论与该延迟风险应分别呈现。

### 5.1 2026-08-04 的历史现场门（现已完成）

本轮没有停止、重启或启动 Elysium、NapCat、Minecraft、Java 或 native sidecar，也没有修改运行中客户端的 mods。

最终现场门需要用户自行结束当前 Minecraft 后，按生产手册部署 0.2.1；随后由用户手动重启 Elysium，再从正式 `nucleus_minecraft` 执行 `preflight → start → status → intent → stop`。`stop` 只释放 Elysium 的身体控制、Presence 和 scene，默认保留游戏进程。完成这一步之前，发布状态应表述为“代码与自动化验收完成、0.2.0 真实动作闭环完成、0.2.1 正式工具现场闭环待用户生命周期窗口”，不能表述为 0.2.1 已在运行中。
