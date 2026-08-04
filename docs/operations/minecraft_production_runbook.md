# Minecraft 生产运行手册

## 当前支持范围

生产默认身体是 `agent`：可见的 NeoForge 1.21.1 客户端加载 Elysium Bridge 0.2.0，由游戏主动连接 WSL 中的 Life Engine。它提供结构化世界观察、经过类型校验的动作、Baritone 导航、终态回执和哈希证据链。

`biomimetic` 是可选实验身体，使用 DXcam 与 Windows 原生输入。它依赖唯一的前台 Minecraft 窗口，不能与人同时争用同一桌面的键鼠，也不能在旧 sidecar 仍运行时启动新实例。生产任务应保持 `default_body = "agent"`。

## 固定环境

- Minecraft：1.21.1
- NeoForge：21.1.219
- 世界：`Elysian Realm`
- 游戏目录：`G:\Game\Minecraft\.minecraft`
- 启动脚本：`G:\Game\Minecraft\PCL\LaunchElysia.bat`
- Elysium Bridge：`elysium_bridge-0.2.0.jar`
- Bridge SHA-256：`AB455A1285196A7ACAFD996D32E669F1B865880DA20EE29E25481775F1A624CA`
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
   expected_bridge_version = "0.2.0"
   ```

   其余摘要、文件名、监听地址和超时使用代码中的已验证默认值。令牌由桥接首次启动写入 `config/elysium_bridge.json`，不得复制到仓库或日志。

5. 由用户手动重启 Elysium。AI 和部署脚本均不得替用户停止、重启或拉起 Elysium、NapCat 或已有 Minecraft 进程。

## 启动与就绪语义

`nucleus_minecraft` 只有在 `minecraft.enabled=true` 时暴露。Minecraft session 由 `LifeEngineService` 独立持有，不依赖 Learning 是否启用。

`start` 成功必须同时满足：

- 精确的启动脚本、世界目录、Bridge 与 Baritone 摘要通过预检；
- Windows/WSL 互操作可用，且没有多个匹配的 Minecraft 窗口；
- 桥接完成共享令牌认证，协议版本与必需能力完全匹配；
- 游戏报告 `world_loaded=true`、`client_paused=false`、单人世界名称为 `Elysian Realm`，并提供玩家 UUID；
- 收到至少两条连续前进的完整观察。

标题界面、暂停菜单、错误世界、旧桥接、缺失能力、静止观察或断线都会返回可诊断失败，不能伪装成就绪。已经运行的合规客户端会被复用，不会再启动第三个客户端。

## 操作与证据

Agent Body 只接受显式类型动作：移动/视角、交互、快捷栏、丢弃、聊天、复活、等待、Baritone 导航与挖掘、停止和释放。任意 Baritone 命令字符串不属于生产接口。

命令 ID 与规范化载荷共同进入有界重放账本：完全相同的重试复用已有 ack/终态；相同 ID 配不同载荷会被拒绝。`accepted` 只表示接单，只有终态回执加后续新观察才能支撑完成结论。

每次 session 的证据写入：

```text
data/life_engine_workspace/minecraft/traces/<session_id>.jsonl
```

记录由 `previous_hash`/`record_hash` 串联，至少应包含 `body.selected`、`intent.issued`、`observation`、`command.issued`、`command.receipt` 和 `intent.conclusion`。

## 独立真实烟雾测试

该测试不会启动或停止 Elysium；若游戏未运行会按托管脚本启动，结束时只释放控制并断开桥接，游戏保持运行：

```bash
PYTHONPATH=. .venv/bin/python \
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
