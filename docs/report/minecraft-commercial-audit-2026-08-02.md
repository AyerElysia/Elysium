# Minecraft 商业级具身系统审计与生产交付报告

首次审计：2026-08-02；生产复验：2026-08-04。

## 结论

Minecraft 的 Agent Body 已从“存在实现但正式主体不可调用”修复为可部署、可诊断、可审计的生产链路，并完成真实游戏闭环。当前结果不是标题页截图或模拟成功：NeoForge 客户端实际进入 `Elysian Realm`，桥接读取玩家、世界、背包、实体和 Baritone 状态，执行了一次受限视角动作，收到终态回执，再由新观察精确证明 yaw 改变 5°。

Life Engine 在 `minecraft.enabled=true` 时注册 `nucleus_minecraft` 工具，并由 `LifeEngineService` 独立持有一个 Minecraft session；该能力不再属于 Learning，也不受 `learning.enabled` 影响。正式工具链需要在代码合入后由用户手动重启 Elysium 才能载入；任何 AI 都不得代替用户重启主进程。

默认生产身体是 `agent`。`biomimetic` 已补齐可重建 sidecar、单实例保护、认证与重放账本，但它需要唯一前台窗口且当前机器存在两个旧 sidecar 进程，未在不终止用户进程的前提下重做真实键鼠验收，因此保持可选实验状态，不冒充本轮生产完成项。

## 原阻断与处置

审计确认的主要阻断包括：

1. Minecraft 工具虽已定义，但 Life Engine 未注册或公开，主体无法从正式组件链启动和控制。
2. session 曾经从 LearningScheduler 读取身体，`learning.enabled=false` 会错误隐藏 Minecraft。
3. 启动脚本没有 quick-play 世界参数，标题页可能被误判为就绪。
4. 启动器依赖跨 WSL 的脆弱命令行转义，窗口和进程判断可能不明确。
5. 桥接动作过于自由，缺少同 ID/不同载荷冲突拒绝和有界重放终态。
6. world 未加载、错误世界、旧桥接或能力缺失时，错误不够稳定可诊断。
7. Windows sidecar 环境不可重建，且旧实例可能并发争用键鼠。

本轮分别完成了条件注册、service 独立所有权、精确预检、Windows 原生启动帮助器、类型化动作、命令账本、幂等清理、sidecar 固定依赖和专项测试。

## 生产架构

### Life Engine 所有权

- `minecraft.enabled=false`：不暴露 `nucleus_minecraft`，不创建外部连接。
- `minecraft.enabled=true`：manifest、组件签名和 `get_components()` 一致，service 创建但不提前连接身体的 session。
- session 早于 Learning 初始化，Learning 关闭或初始化失败不影响 Minecraft 的所有权。
- service stop 与部分初始化回滚都会调用幂等 `MinecraftSession.close()`；失败的 owner 保留供重试，同时继续释放其他资源。
- 工具只读取 `service.minecraft_session`，不再回退到 LearningScheduler 或私有字段。

### 启动与就绪

启动前检查精确的 Minecraft 版本、世界目录、PCL 脚本、quick-play 参数、Bridge 与官方 Baritone 文件名和 SHA-256。Windows 帮助器只允许托管目录，多个匹配窗口会明确失败；缺失的标准 WSLInterop 注册项会尝试恢复，不能恢复时返回可操作错误。

真正的 ready 必须有：认证成功、Bridge 0.2.0、必需能力集合、两条连续前进观察、`world_loaded=true`、`client_paused=false`、`singleplayer_name=Elysian Realm` 和玩家 UUID。标题界面、暂停菜单、错误世界、静止观察或断线均不会返回成功。

### 动作与证据

Agent Body 只接受类型化动作：移动/视角、交互、快捷栏、丢弃、聊天、复活、等待、导航、挖掘、停止和释放。任意 Baritone 命令字符串不再是生产接口。

命令账本以命令 ID 和规范化 JSON 摘要识别重试：相同命令返回既有 ack/终态；相同 ID 配不同载荷立即拒绝；终态使用有界 LRU，pending 不会因容量清理而丢失。`accepted` 与 `completed` 分离，模型结论必须引用终态回执和后续新观察。

### 生命周期

start、stop 和 close 均可重复调用。远端游戏先正常关闭或崩溃时，客户端会唤醒等待者并幂等清理，不把“peer 已关闭”升级成伪造的清理失败。Presence、scene 与 perception 回调兼容正式 async port；错误不会用延迟补写或旧 SQLite 回退掩盖。

## 真实环境与固定产物

- Minecraft：1.21.1
- NeoForge：21.1.219
- 世界：`Elysian Realm`
- Elysium Bridge：0.2.0
- Bridge JAR SHA-256：`AB455A1285196A7ACAFD996D32E669F1B865880DA20EE29E25481775F1A624CA`
- Baritone：官方 NeoForge 1.11.2
- 官方 SHA-1：`C72014178D80650DF9BBB57819D7542DA69866C2`
- 本地固定 SHA-256：`B413CE0A2754A3C8484AAE39875CF84BE1F999DEE208E86D41B3D0D329D5CA35`

Gradle 归档关闭时间戳并固定文件顺序；连续构建得到相同 Bridge SHA-256。部署脚本校验锁文件、JAR 内 NeoForge 元数据、官方 Baritone 摘要和唯一选中版本，旧桥接只移动到可恢复目录。

## 真实闭环证据

2026-08-04 的生产烟雾 session：`20260804T151244_791715Z`。

- 游戏实例：`minecraft_0812c9b2-b701-46d8-bebd-b121f74e2d9e`
- 身体：`agent`
- Bridge：0.2.0
- 世界：`Elysian Realm`，`world_loaded=true`
- 观察能力：玩家 UUID/位置/视角、生命与饱食、完整物品栏、附近实体、维度、天气、时间、准星、控制状态、Baritone 与传输背压
- 动作：`movement.input`，yaw `+5.0°`
- 回执：`accepted=true`、`completed=true`、`interrupted=false`
- 新观察：yaw 从 `-172.29694` 变为 `-167.29694`
- 结论：`A bounded look action changed observed yaw by 5.000 degrees.`
- 清理：`success=true`、`cleanup_pending=false`、`game_left_running=true`
- 证据链：`data/life_engine_workspace/minecraft/traces/20260804T151244_791715Z.jsonl`

trace 包含 `body.selected → intent.issued → observation → command.issued → command.receipt → observation → intent.conclusion`，每条记录由前后哈希串联。

同一游戏进程持续运行约 38 分钟后再次重连，认证、观察、终态回执和清理仍成功；该轮同时观察到客户端停在暂停菜单。由于暂停的单人世界不能保证导航/移动推进，本轮据此把 `client_paused=true` 收紧为明确未就绪并补充回归测试，不把暂停菜单中的直接视角修改当作完整生产 ready 证据。

## 现场故障与恢复

第一次真实启动成功加载 Elysium Bridge 并认证，但游戏随后自行崩溃。crash report 将首个业务异常精确定位到：

```text
InventoryProfilesNext-neoforge-1.21.1-2.2.5.jar
org.anti_ad.mc.ipnext.config.Features.getENABLE_PROFILES
ENABLE_PROFILES$delegate is null
```

其配套库为 `libIPN-neoforge-1.21.1-6.6.3.jar`。两者属于可选背包整理 UI，不是 Elysium Bridge 或 Baritone。确认托管 Java 进程已因崩溃自行退出后，只将这两份固定摘要文件移动到：

```text
G:\Game\Minecraft\.minecraft\mods\elysium-disabled\incompatible-inventoryprofilesnext-2.2.5
```

没有删除文件、批量禁用其他模组或停止任何进程。隔离后的第二次启动与真实闭环成功，游戏继续稳定运行。

## 参考包审计

用户提供的两份历史包已静态审计：

- `MC集成包.zip` SHA-256：`435939B3F77A264640A76D9C2F085BA8B1BD3FA81E3AA4B4F86D1031EB01F93B`
- `MC插件源码.zip` SHA-256：`53C1D32617C104BAF29BE4311D918AA7F7FCD063803DDA176AB3DE2530E32C8A`

它们提供了技能分解、tick 后置条件确认、Mineflayer/Mindcraft 等有价值思路；但包内未发现许可证文件，且参考桥接存在未认证监听、自由文本命令、时间窗口判定和可伪造完成等问题。本轮没有复制这些高风险实现，只吸收了“类型化技能 + 后置世界证据”的设计思想。

## 自动化验证

- Minecraft 与 service 接线专项：48 passed
- NeoForge：`clean test build` 成功，Java 契约/账本测试通过
- Bridge 产物可复现 SHA-256：`AB455A...F1A624CA`
- 变更 Python 文件 Ruff：通过
- 变更 Python 源文件编译：通过
- sidecar 固定依赖环境：安装与导入检查通过
- 全仓单进程：3513 passed / 13 skipped，覆盖率 67.15%；另有 2 个非 Minecraft 基线超时
- 两个超时用例（Anthropic SDK 首次导入、Scheduler `trigger_at + interval`）随后定向复验：2 passed

并行全仓还复现了团队已登记的 Scheduler recurring 30 秒时序超时，并导致 pytest-xdist 内部退出。报告保留这些失败，不以重跑把全仓描述成“全绿”；Minecraft 专项、NeoForge 构建和真实闭环均在同一提交上独立通过。

专项测试覆盖：组件 enabled/disabled、Learning 独立性、部分初始化回收、认证和元数据、错误世界/标题页、能力与版本拒绝、quick-play、产物摘要/重复 JAR、Windows 启动与歧义窗口、同 ID 重放冲突、start/stop/close 幂等、失败清理重试、正式 service session 工具读取和 native sidecar 单实例账本。

## 尚未冒充完成的边界

1. 当前运行中的 Elysium 主进程尚未载入本次代码。合入后必须由用户手动重启，再通过正式 `nucleus_minecraft` 工具执行一次 start/status/intent/stop 验收。
2. Biomimetic Body 需要用户手动关闭两个遗留 sidecar 后再做真实 DXcam/SendInput 验收；默认 Agent Body 不受影响。
3. “你和爱莉各有一个角色并同时在线”仍需要第二个已授权 Minecraft 账号，以及服务器认证、白名单、权限与备份方案。
4. 公开直播仍需要用户提供平台账号和推流密钥；本轮没有设置密钥或推流。

部署、升级、故障恢复和发布门详见[《Minecraft 生产运行手册》](../operations/minecraft_production_runbook.md)。
