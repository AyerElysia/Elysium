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

## 5. 运行边界与剩余现场门

本轮没有停止、重启或启动 Elysium、NapCat、Minecraft、Java 或 native sidecar，也没有修改运行中客户端的 mods。

最终现场门需要用户自行结束当前 Minecraft 后，按生产手册部署 0.2.1；随后由用户手动重启 Elysium，再从正式 `nucleus_minecraft` 执行 `preflight → start → status → intent → stop`。`stop` 只释放 Elysium 的身体控制、Presence 和 scene，默认保留游戏进程。完成这一步之前，发布状态应表述为“代码与自动化验收完成、0.2.0 真实动作闭环完成、0.2.1 正式工具现场闭环待用户生命周期窗口”，不能表述为 0.2.1 已在运行中。
