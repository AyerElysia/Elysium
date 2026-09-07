# Minecraft 陪玩真实联调记录（2026-09-06）

> 当前状态：服务接线和首次现场发现的投影预算撞键已修复；166 项轻量组合回归通过。真实 Console 预检已通过，首次 start 在主体投影校验处明确失败，尚无 bot 进服、游戏回复、同行或采集的通过证据。不得把本记录解释为端到端已完成。

## 授权与操作边界

用户要求将 MC 陪玩真正打通，明确授权控制电脑，并要求优先采用公开项目实现。直接在原仓库工作，不创建 worktree、不委派其他 agent，不覆盖共享工作区其他改动。用户手动运行的 Elysium 不由开发 agent 停止或重启；游戏内操作属于本次授权联调。主体文件和正式不可变历史未被直接修改。

## 时间线与证据

- 现场初始 Elysium PID 275678，仍为修复前进程。游戏原本未运行；检查后启动既有 `G:\Game\Minecraft\PCL\LaunchElysia.bat`，进入 `Elysian Realm`，游戏内人类账号为 `AyerElysia`。
- 补齐 `LifeEngineService` 的 MC 身体事件 callback，MC/服务/Console 组合回归 `135 passed`。检查到用户手动重启后的 Elysium PID 398461；当日日志记录启动成功。
- 通过真实游戏窗口开放局域网，端口为 25565；窗口标题变为多人游戏（局域网），游戏系统消息确认端口。没有修改防火墙或安全设置。
- 使用正式本地 Console 会话调用 `GET /console/api/v1/minecraft/preflight`，结果 `success=true, ready_to_start=true, blockers=[]`。随后同源、带显式 MC action header 调用正式 start；该入口实际执行 `session.start(body_name="bot")`，不是独立测试替身。
- 首次 start 返回 `success=false`，错误为 `Minecraft subject context binding failed: projection manifest profile is incompatible`，失败发生在身体启动之前。
- 只读查询选定本地数据库 `data/life_storage/local.sqlite3` 的投影元数据，找到 `minecraft.v1-22f0e7833b29772a1fcbbce17e9d35b5075cd1718b885e13e6c6d57da161bf15`，`revision=1`、`profile=minecraft`、`max_bytes=16384`。查询没有输出投影正文或凭据。
- 临时数据回归重现：相同 profile/source 的 8 KiB 与 12 KiB 投影在本地文件模式通过，在选定 runtime store 模式触发同一 profile 错误。证明版本键遗漏预算，而不是主体文件损坏。
- 修复共享存储键的预算隔离，保留旧记录，增加兼容历史读取及失败重试校验。投影专项 `31 passed`，与 MC/服务/Console 的最终组合 `166 passed, 1 warning in 16.68s`。唯一 warning 是既有 `websockets.legacy` 废弃提示。
- 已请求用户再次手动重启，以加载投影修复；游戏和局域网保持运行。后续真实验收尚待继续。

## 根因与修复

### 身体事件服务接线缺失

session 要求耐久 recorder 后才允许高层任务身体启动，但共享 service 未注入 callback。现在显式注入，先写统一 Life Event 和 pending checkpoint，再允许 ACK；失败保留原事件，重试不制造新 occurrence。单锁处理并发，缓存上限 256，只淘汰成功项。重启/缓存淘汰后从选定 store 核对原 occurrence 并复用 source sequence；异载荷冲突明确失败。

### MC 配置默认值不一致

共享 `MinecraftSection` 默认仍为 agent/旧预算，已与 `MCConfig` 和现场 bot 配置对齐为 bot 与 8192/8192/4096 字节、3 组、4 个近期引用。

### 主体投影预算撞键

本地投影目录包含预算，但选定存储的键原本只有 profile/version/source digest。更改 MC 预算后，新实例读到了旧预算版本。新键包含预算，例如 `minecraft.bytes-8192.v1-<digest>`；文件路径继续兼容。旧 profile-only 和无 profile 版本只在归属、预算、来源、哈希及包装完整校验后原样复制到新键，不改写或删除旧版本。不同预算另行生成；匹配记录损坏时仍失败。历史 pin、重启和不确定写入走相同严格路径。

## 公开项目复用

实际 bot 依赖为 [Mineflayer](https://github.com/PrismarineJS/mineflayer)、[Pathfinder](https://github.com/PrismarineJS/mineflayer-pathfinder) 和 [CollectBlock](https://github.com/PrismarineJS/mineflayer-collectblock)，版本锁定在 `integrations/minecraft_bot/package-lock.json`，协议连接、连续导航和采集由这些公开实现执行。Elysium 维护高层任务、认证桥、身体锁、统一经历归因和证据回执。

[N.E.K.O. Minecraft 插件](https://github.com/Project-N-E-K-O/N.E.K.O/tree/main/plugin/plugins/game_agent_minecraft) 和 [Neuro SDK](https://github.com/VedalAI/neuro-sdk/blob/main/API/BEST_PRACTICES.md) 仅用于结构对照，没有直接复制其实现。用户提供的旧 ZIP 未发现明确许可证，因此没有搬运源码。

## 验证与剩余验收

执行的组合测试：

```bash
uv run --frozen --no-sync python -m pytest \
  test/plugins/life_engine/minecraft \
  test/plugins/life_engine/test_minecraft_service_integration.py \
  test/plugins/elysium_console \
  test/plugins/life_engine/test_subject_context_projection.py \
  test/plugins/life_engine/test_router_context_projection.py \
  -q --no-cov -n 0
```

新增覆盖：服务 callback、并发重放、append/checkpoint 失败、缓存淘汰、重启、异载荷冲突；投影多预算共存、两种旧键兼容、无当前主体文件的历史 pin、迁移写入前/后失败和损坏拒绝。定向 F/E9 静态检查通过。运行中的主进程未与全仓高负载测试并行。

仍需同一真实 session 的 bot 进服、聊天入账与可见回复、跟随或采集终态与后置观察、长任务期间聊天、重连重放、15–30 分钟自主节奏，以及 interrupt/stop 只释放会话拥有资源。此前的自动化成功不能替代这些现场证据。

## 可逆性与待决归属

没有删除数据或覆盖主体语义，没有提交运行数据库、日志或凭据。投影兼容迁移只新增经过验证的等价键，旧记录保持可读取；不需要回滚或改写权威历史。工程补丁仍在本地原仓库工作区，未推送。主进程加载修复需要用户手动重启；之后由本任务继续正式链路验收。
