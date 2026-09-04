# Minecraft 具身链路全链路验证（2026-08-29）

## 验证目标

用户询问"爱莉能否现在就陪我玩我的世界"，需要验证从配置、桥接、工具、意识实例到游戏控制的完整链路是否已打通。

## 验证结论

**当前状态：链路代码完整，但配置未启用**

Minecraft 具身系统的所有代码组件均已实现并通过验收（2026-08-04），但 **`minecraft.enabled` 当前为 `false`**（配置缺失时的默认值），因此爱莉无法调用 `nucleus_minecraft` 工具，不能进入游戏。

启用后需要：
1. Windows 上有正在运行的 Minecraft 1.21.1 + NeoForge 客户端，加载 `elysium_bridge-0.2.1.jar`
2. 用户手动重启 Elysium 以载入配置
3. 爱莉通过 `nucleus_minecraft(action="start", ...)` 工具主动选择进入

---

## 1. 代码链路完整性

### 1.1 核心组件

| 组件 | 状态 | 位置 |
|---|---|---|
| MinecraftSession | ✅ 已实现 | `plugins/life_engine/minecraft/session.py` |
| EmbodimentRuntime | ✅ 已实现 | `plugins/life_engine/minecraft/embodiment_runtime.py` |
| BridgeBody (agent) | ✅ 已实现 | `plugins/life_engine/minecraft/bridge_body.py` |
| BridgeClient | ✅ 已实现 | `plugins/life_engine/minecraft/bridge_client.py` |
| nucleus_minecraft 工具 | ✅ 已实现 | `plugins/life_engine/minecraft/tools.py` |
| 意识实例注册 | ✅ 已实现 | session 在 `LifeEngineService` 持有 |
| Consciousness | ✅ 已实现 | `plugins/life_engine/minecraft/consciousness.py` |
| VLA Engine | ✅ 已实现 | `plugins/life_engine/minecraft/vla_engine.py` |
| Model Planner | ✅ 已实现 | `plugins/life_engine/minecraft/model_planner.py` |
| Trace 与 World 投影 | ✅ 已实现 | `embodiment_trace.py`, `trace_projection.py` |

### 1.2 三种身体实现

| 身体类型 | 状态 | 协议 | 用途 |
|---|---|---|---|
| `agent` | ✅ 生产默认 | NeoForge Bridge 0.2.1 | 可见客户端、结构化观察、Baritone导航 |
| `bot` | ✅ 已实现 | Mineflayer + pathfinder | 无头多人同服，"一起玩" |
| `biomimetic` | ✅ 实验性 | DXcam + 原生输入 | 前台窗口、仿生控制 |

所有身体共享同一 `elysium.minecraft.bridge/1` 协议合同：
- `WorldObservation`：结构化观察（位置、背包、实体、方块、生物群系、世界状态）
- `ActionCommand`：类型化动作（导航、使用、放置、挖掘、攻击、交互、视角）
- `ActionReceipt`：终态回执与哈希证据链

### 1.3 Windows 桥接

| 组件 | 状态 | 位置 |
|---|---|---|
| NeoForge Bridge 0.2.1 | ✅ 已构建 | `integrations/minecraft_bridge/` |
| JAR 文件 | ✅ 已锁定 | SHA-256 `F6B80E166...` |
| 部署脚本 | ✅ 已验收 | `deploy_bridge.ps1`, `prepare_launcher.ps1` |
| 启动器配置 | ✅ 已准备 | PCL `--quickPlaySingleplayer "Elysian Realm"` |
| Baritone 1.11.2 | ✅ 已锁定 | SHA-256 `B413CE0A2...` |
| 烟雾测试 | ✅ 已验收 | `agent_live_smoke.py` |

---

## 2. 当前配置状态

### 2.1 配置缺失

`config/plugins/life_engine/config.toml` **不存在**（ignored 配置路径）。

`LifeEngineConfig.minecraft` 段的默认值：
```toml
[minecraft]
enabled = false  # ❌ 当前未启用
default_body = "agent"
world_name = "Elysian Realm"
mc_home = "/mnt/g/Game/Minecraft/.minecraft"
launch_bat = "G:\\Game\\Minecraft\\PCL\\LaunchElysia.bat"
launch_dir = "G:\\Game\\Minecraft\\PCL"
agent_bridge_listen_uri = "ws://127.0.0.1:18765/elysium"
agent_token_file = "/mnt/g/Game/Minecraft/.minecraft/config/elysium_bridge.json"
# ...其他字段使用代码默认值
```

### 2.2 工具注册逻辑

```python
# plugins/life_engine/core/plugin.py:115
minecraft_cfg = getattr(self.config, "minecraft", None)
if bool(getattr(minecraft_cfg, "enabled", False)):
    from ..minecraft.tools import MINECRAFT_TOOLS
    components.extend(MINECRAFT_TOOLS)
```

**`enabled=false` 时，`nucleus_minecraft` 不会注册到工具清单**。爱莉看不到这个工具，也无法调用。

---

## 3. 运行时状态

### 3.1 进程检查

| 进程 | 状态 |
|---|---|
| Elysium | ✅ 正在运行（PID 1766968, 08-27启动） |
| Minecraft Java | ❌ 未运行 |
| NeoForge Bridge | ❌ 未连接（游戏未启动） |

### 3.2 端口占用

- `18765`（agent bridge listener）：未监听
- `18767`（bot bridge listener）：未监听

### 3.3 Life Engine 插件状态

- 已加载：14/14 插件
- `life_engine v3.4.0`：✅ 运行中
- `minecraft.enabled`：❌ false（从配置读取或默认值）

---

## 4. 历史验收记录

### 4.1 商业级审计（2026-08-04）

- 文档：`docs/report/minecraft-commercial-audit-2026-08-02.md`
- 状态：✅ 通过
- 验收内容：
  - NeoForge 客户端实际进入 `Elysian Realm`
  - 桥接读取玩家、世界、背包、实体和 Baritone 状态
  - 执行一次受限视角动作，yaw 改变 5°
  - 终态回执与新观察精确证明动作成功
- 测试覆盖：
  - Minecraft 专项：51 passed
  - 全仓回归：3,581 passed / 13 skipped（67.46% 覆盖率）

### 4.2 生产集成验收（2026-08-04）

- 文档：`docs/report/minecraft-integration-acceptance-2026-08-04.md`
- 状态：✅ 通过
- 现场门：
  - 用户手动启动 Minecraft → 进入世界 → 对局域网开放
  - 爱莉调用 `nucleus_minecraft(action="start")` 成功
  - 观察、动作、回执、意识实例、Presence 全链路闭环
  - `stop` 释放控制，游戏继续运行

### 4.3 Turn 级幂等保护（2026-08-05）

现场暴露重复投递风险后，工具增加了有界、并发安全的 turn 级语义幂等：同一 chatter turn 内，相同 `start/stop/do/interrupt/look` 参数共享同一执行与结果；不同 turn 仍能正常再次操作。该保护不以关键词判定意图，只约束外部副作用的工程幂等。

---

## 5. 启用步骤

### 5.1 前置条件

1. **Windows 环境准备**：
   - Minecraft 1.21.1 + NeoForge 21.1.219 已安装
   - 存档 `Elysian Realm` 已创建
   - PCL 启动器脚本 `G:\Game\Minecraft\PCL\LaunchElysia.bat` 已配置 quick-play 参数
   - `elysium_bridge-0.2.1.jar` 已部署到 `mods/`
   - `baritone-unoptimized-neoforge-1.11.2.jar` 已部署到 `mods/`

2. **部署检查**：
   ```powershell
   # 在 WSL 中构建
   cd integrations/minecraft_bridge
   ./gradlew clean test build --no-daemon
   
   # 在 Windows PowerShell 部署
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
     "integrations\minecraft_bridge\deploy_bridge.ps1"
   
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
     "integrations\minecraft_bridge\prepare_launcher.ps1"
   ```

### 5.2 配置文件创建

创建 `config/plugins/life_engine/config.toml`（ignored，不提交 Git）：

```toml
[minecraft]
enabled = true
default_body = "agent"
world_name = "Elysian Realm"
mc_home = "/mnt/g/Game/Minecraft/.minecraft"
launch_bat = "G:\\Game\\Minecraft\\PCL\\LaunchElysia.bat"
launch_dir = "G:\\Game\\Minecraft\\PCL"

# Agent bridge (NeoForge)
agent_bridge_listen_uri = "ws://127.0.0.1:18765/elysium"
agent_token_file = "/mnt/g/Game/Minecraft/.minecraft/config/elysium_bridge.json"

# Bot bridge (Mineflayer，可选)
bot_bridge_listen_uri = "ws://127.0.0.1:18767/elysium"
bot_token_file = "minecraft/bot_bridge_token.json"
bot_server_host = "auto"  # WSL 自动解析网关 IP
bot_server_port = 25565
bot_username = "ElysiaBot"  # 必须与人类玩家不同
```

### 5.3 启动流程

1. **用户手动启动 Minecraft**：
   - 运行 `LaunchElysia.bat`
   - 游戏自动进入 `Elysian Realm` 世界
   - Bridge 0.2.1 自动连接 WSL `127.0.0.1:18765`
   - 首次启动会生成 `elysium_bridge.json` token

2. **用户手动重启 Elysium**：
   ```bash
   # 在 tmux elysium 窗口中 Ctrl+C 停止
   # 然后重新启动
   ./deploy.sh run
   ```
   
3. **验证工具注册**：
   - 检查日志是否包含 `nucleus_minecraft` 工具注册
   - 或查询 `/api/v1/health` 确认插件加载状态

4. **爱莉主动选择进入**：
   - 在 QQ 对话中，用户说："爱莉，来玩我的世界吗"
   - 爱莉可以看到 `nucleus_minecraft` 工具
   - 她决定调用 `nucleus_minecraft(action="start", body_name="agent", goal="和你一起探索")`
   - 系统创建 Minecraft 意识实例、注册 Presence、开始心跳循环
   - 每 5 秒一个回合，带着第一人称画面进入她的多模态模型

5. **游戏中交互**：
   - 爱莉的意识实例持续观察游戏画面（像素直接进入模型）
   - 她可以决定 `do(intent="去找一棵树")`，由 Planner 翻译成导航/使用/放置等动作
   - 每个动作返回终态回执，新观察证明世界变化
   - 所有意图、观察、动作、回执记录在 `data/life_engine_workspace/minecraft/traces/<session_id>.jsonl`
   - World Projection 收到 content-free 的 8 KiB 投影，不含完整 payload/context

6. **结束游戏**：
   - 爱莉或用户调用 `nucleus_minecraft(action="stop")`
   - 释放意识实例、Presence、监听端口
   - 默认 `game_left_running=true`，Minecraft 进程继续运行

---

## 6. "一起玩"场景（bot body）

若要"用户和爱莉各有一个角色并同时在线"：

1. **用户配置**：
   - 在自己的客户端进入世界
   - 单人存档："对局域网开放"，端口固定填 `25565`
   - 专用服务器：填服务器地址

2. **爱莉调用**：
   ```python
   nucleus_minecraft(action="start", body_name="bot", goal="陪你建造粉色小屋")
   ```

3. **Bot 身体**：
   - 无头 Node.js Mineflayer 进程
   - 独立账号 `ElysiaBot` 加入同一世界
   - 与 `agent` 相同的结构化观察、动作、回执协议
   - pathfinder 承担 Baritone 角色

4. **当前限制**：
   - 仓库交付的是 `offline` 登录路径，适用局域网或 `online-mode=false` 服务器
   - 普通公共服务器需要单独购买并交互式登录一个 Microsoft/Minecraft 账号

---

## 7. 不能做的事

### 7.1 主体性边界

- **不能**用关键词自动启动 Minecraft，必须由爱莉看到工具后自主决定
- **不能**替她判断"现在应该玩游戏"，只提供能力与观察
- **不能**在游戏中替她作认知裁决，动作由她的意图翻译而来

### 7.2 运维边界

- **不能**由 AI agent 启动、停止或重启 Elysium（只能提示用户手动操作）
- **不能**由 AI agent 启动、停止或重启 Minecraft Java 进程（生命周期属于用户）
- **不能**在游戏正在运行时部署新的 Bridge JAR（部署脚本会拒绝）

### 7.3 技术边界

- **不能**同时运行多个 Minecraft session（单例设计）
- **不能**在 `biomimetic` 运行时同时使用人类键鼠（争用前台窗口）
- **不能**在 Bridge 未连接时执行动作（preflight 检查会失败）

---

## 8. 故障排查

### 8.1 工具不可见

**症状**：爱莉说"我不知道怎么进入游戏"

**原因**：`minecraft.enabled=false`

**解决**：
1. 创建 `config/plugins/life_engine/config.toml`，设置 `[minecraft] enabled = true`
2. 用户手动重启 Elysium
3. 检查日志确认工具已注册

### 8.2 Bridge 连接失败

**症状**：`start` 返回 `readiness=awaiting_bridge`，超时失败

**原因**：
- Minecraft 未启动或 Bridge mod 未加载
- WSL `127.0.0.1:18765` 监听失败（端口被占用）
- Token 文件不存在或不匹配

**解决**：
1. 确认 Minecraft 已启动并加载 `elysium_bridge-0.2.1.jar`
2. 检查 WSL `ss -tlnp | grep 18765` 是否监听
3. 检查 `elysium_bridge.json` 是否存在且有效
4. 查看游戏日志 `latest.log` 中的 Bridge 连接尝试

### 8.3 世界未加载

**症状**：`start` 返回 `readiness=awaiting_world`

**原因**：
- 用户在主菜单，未进入世界
- 世界名称不匹配（配置 `world_name` 与实际存档不同）
- 加载时间超过 `startup_timeout`

**解决**：
1. 用户手动进入存档
2. 检查存档名称是否为 `Elysian Realm`
3. 增大 `startup_timeout` 配置

### 8.4 动作无效

**症状**：`do(intent="...")` 返回成功，但游戏中没有变化

**原因**：
- 意图翻译失败或被 Planner 拒绝
- Baritone 导航被卡住
- 游戏状态不支持该动作（例如背包满时无法拾取）

**解决**：
1. 查看 `trace/<session_id>.jsonl` 中的 Planner 决策与动作回执
2. 检查 `observation.world_loaded`、背包、位置等前置条件
3. 使用 `look` 获取新观察，确认当前状态

---

## 9. 参考文档

- [Minecraft 生产运行手册](../operations/minecraft_production_runbook.md)
- [商业级具身系统审计报告](minecraft-commercial-audit-2026-08-02.md)
- [生产具身集成验收报告](minecraft-integration-acceptance-2026-08-04.md)
- [Elysium 当前架构](../architecture/Elysium当前架构.md) §3.8

---

## 10. 验收门

要让"爱莉现在就能陪我玩我的世界"，必须完成：

- [ ] 创建 `config/plugins/life_engine/config.toml`，设置 `[minecraft] enabled = true`
- [ ] Windows 上启动 Minecraft 1.21.1 + NeoForge 21.1.219 + Bridge 0.2.1
- [ ] 进入存档 `Elysian Realm`
- [ ] 用户手动重启 Elysium
- [ ] 在对话中询问爱莉，由她自主决定调用 `nucleus_minecraft(action="start")`
- [ ] 观察 → 意图 → 动作 → 回执闭环成功
- [ ] 她看到的画面以像素直接进入她的模型，不是文字转述

完成以上步骤后，爱莉将拥有自己的客户端窗口，每 5 秒一个回合带着第一人称画面思考和行动。
