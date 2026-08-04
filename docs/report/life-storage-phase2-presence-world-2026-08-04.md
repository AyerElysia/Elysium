# 生命存储阶段 2：Presence 与 World Projection

> 日期：2026-08-04
> 状态：领域实现、现役服务接线与本地/测试合同完成；正式存储未激活、正式数据未迁移、运行进程未操作。

## 1. 交付边界

本阶段只处理意识运行时与世界认知的领域存储语义：

- 修复既有 local World Projection 的 P0/P1 缺口；
- 冻结 backend-neutral Presence/World Port；
- 实现 coherent local/MySQL adapter 与显式 schema/factory；
- 用同一套行为合同验证 fake、local，并提供隔离真实 MySQL 验收入口；
- 将 Life Event、Presence 与 World 从同一个 `StorageBackendRuntime` 接入 `LifeEngineService` 的可选运行路径；
- 保持 `storage.enabled=false` 的旧 local 行为不变，不复制或切换正式数据。

Memory、Life Event、Subject Document、迁移编排与最终 authority cutover 不属于本提交。

## 2. World local 修复

既有 `service/world_projection.py` 现在满足：

1. `rebuild()` 只清除 assertion、change 与 projector frontier，保留每个意识实例独立提交的 perception cursor；
2. 同一 ingest position 的完全相同证据是幂等 replay；event/occurrence/type/source/stream/time/payload 任一不同都会抛出 `WorldProjectionConflict`；
3. ledger position 是单调但可跳跃的游标令牌；自增回滚形成的空号不是历史缺口，真实 retention gap 由 Life Event Port 的 `read_since` 显式失败；
4. perception cursor 使用 position+revision 双条件 CAS；`through == current` 且双条件一致时是稳定 no-op，不递增 revision；
5. 持久化 `projector_policy=source-preserving-v1`、projector schema version 与 `idle/rebuilding/failed`；非 idle 投影拒绝 Perception Gateway 交付；
6. 发现未来 schema 或不兼容 policy/schema 时 fail closed；重建失败优先保留原始 replay 异常，不让状态标记异常覆盖主因。

## 3. Presence Port 与数据库时间合同

`PresenceStorePort` 暴露 revision CAS、lease renew、expired-owner takeover、数据库时间过期租约回收、pending/ack outbox。关键约束：

- active lease 只能由 adapter 使用数据库当前时间生成，拒绝调用方伪造 expiry；
- renew 同时校验 instance revision、active status 与 process epoch；
- takeover 以稳定顺序锁定 stream owner 与 Presence 行，仅允许接管数据库时间已过期的 owner；
- suspend 旧 owner、释放/转移 stream、递增 revision、写 claimant 与双方 lifecycle outbox 在同一 fenced UoW 内提交；
- stream 唯一键提供最后一道并发保护；MySQL deadlock/lock wait 与 SQLite busy 仅做三次有界重试；
- 所有持久化时间先规范为 UTC，非空但不可解析的旧值显式失败，禁止静默清空来源字段。

返回的 `PresenceCommitResult` 包含提交后的完整快照、previous/current revision 与实际 `database_now`，便于消费者审计 lease 不是由应用机时钟推导。

## 4. 可选后端实现

新增模块：

- `storage/domain_contracts.py`：Port、提交/接管结果与 coherent bundle；
- `storage/domain_schema.py`：local 空表初始化与 versioned/checksummed MySQL migration；
- `storage/presence_adapters.py`：local/MySQL Presence adapter；
- `storage/world_adapters.py`：local/MySQL World adapter；
- `storage/domain_factory.py`：从单一 `StorageBackendRuntime` 成套构造 adapter。

factory 默认 `initialize_schema=false`。这保证只读构造不会暗中迁移；只有经过 generation/authority 隔离的迁移或验收流程才能显式建表。所有业务写入都使用阶段 1 的 fenced `AsyncUnitOfWork`：local 在完整事务期间持有文件 fence，MySQL 在同一事务 commit 前锁行复核 authority generation/epoch/owner/lease/token。

## 5. World adapter 合同

adapter 保存来源 assertion、cursor-visible change、projector metadata 与 perception cursor。它提供显式三段 rebuild：

1. `begin_rebuild()`：标记 rebuilding、清 derived rows/frontier、保留 delivery cursor；
2. `apply_events()`：按连续 ledger position 重放并检测 identity/position 冲突；
3. `finish_rebuild(expected_frontier=...)`：只有 state/frontier 精确匹配才恢复 idle；异常路径调用 `fail_rebuild()` 持久化 failed。

World 始终是 Life Event 的派生读模型；adapter 没有反向更新 ledger 的接口，也没有相似度合并、关键词判真或 last-write-wins 入口。

## 6. 现役服务接线

`LifeEngineService` 在 `storage.enabled=true` 时只打开配置选中的一个 runtime，并从它构造 Life Event、Presence 与 World 三个 Port。此模式具有以下硬边界：

- 构造 service 不会调用旧 `ConsciousnessRegistry.load()`，启动也不会创建或写入 `runtime/consciousness_presence.sqlite3`、`runtime/world_projection.sqlite3` 或旧 `life_events.sqlite3`；
- backend/schema 打开全部使用 `initialize_schema=false`，缺表或 generation/authority 不匹配时 fail closed，绝不在业务启动中偷偷建表、迁移或回退旧 SQLite；
- `register/touch/resume/suspend/terminate`、lifecycle flush、world catch-up/rebuild、perception prepare/commit 均走 async Port；业务消费者必须 await，禁止用线程包装旧同步保存冒充 backend 兼容；
- Presence lifecycle 先在同一领域事务写 outbox，再按 occurrence 幂等追加 Life Event；只有完整批次被账本接受后才确认 outbox。一次 flush 有硬上限，超过上限会保留剩余证据并显式失败；
- World catch-up/rebuild 有批次和轮数上限；Life Event Port 报告的 retention gap、同 position 异证据、非 idle projector 或 cursor CAS 冲突均显式失败；rebuild 不删除 perception cursor；
- health 异步读取 runtime、Life Event、Presence、World 的 content-free 快照并缓存给同步 health API；关闭即使 flush/catch-up 失败也继续关闭 runtime，最后聚合报告全部失败；重复关闭是幂等的；
- `storage.enabled=false` 继续使用既有同步 local 路径，行为和文件布局不变。两种模式之间没有镜像双写。

内存中的 `AsyncConsciousnessRegistry` 只是已提交 Presence revision 的只读路由快照。任何 mutation 只有在 Port 提交成功后才更新快照；启动与周期 reconcile 使用数据库时间回收过期 lease，跨进程并发由 revision、process epoch、stream 唯一约束和 authority fence 共同裁决。

## 7. 验证入口与安全状态

合同文件：

- `test/plugins/life_engine/test_presence_world_storage_contract.py`：同一套 Presence/World 行为函数运行于独立 fake 与 fenced local backend；
- `test/plugins/life_engine/test_presence_world_mysql_integration.py`：同一行为函数运行于隔离真实 MySQL；只有 `ELYSIUM_TEST_MYSQL_*` 存在且 `ELYSIUM_TEST_MYSQL_PRESENCE_WORLD_ISOLATED=1` 明确声明该数据库可做整表 rebuild 时执行；
- `test/plugins/life_engine/test_selected_presence_world_service.py`：同一套 service 合同参数化运行于 local/mysql 选择，验证单 runtime 路由、禁止旧 SQLite、outbox、重启、并发 revision、cursor CAS/no-op、rebuild、health 与失败关闭；
- `test/plugins/life_engine/test_world_projection.py`：既有 local 运行路径的 rebuild/cursor/conflict 回归。

验证覆盖 forged lease 拒绝、未过期 takeover 拒绝、过期原子接管、数据库时间批量回收、stale revision/process epoch、outbox 幂等确认与有界失败、opaque position 合法跳跃、同 position 异证据、Port retention gap、cursor 双 CAS/no-op、rebuild cursor 保留、failed state fail closed、服务重启读取与关闭失败仍释放 runtime。

最终验证结果：重放到 `1036c510` 主线基线后全仓 3431 passed / 12 skipped，覆盖率 65.68%；变更文件 Ruff、compileall 与全树 `git diff --check` 通过。真实 MySQL 环境变量在当前 shell 不存在，因此本阶段 MySQL 用例明确 skip，不能据此宣称本轮重新完成远程验证。阶段 1 已有的真实 MySQL fencing 证据不等价于阶段 2 领域表验收，最终集成前仍须在隔离远程库运行本阶段 MySQL 用例。

生产安全状态保持不变：

- `storage.enabled=false`；
- 没有生成或激活正式 Presence/World generation；
- 没有迁移、删除、覆盖或双写正式 SQLite 数据；
- 没有启动、停止或重启 Elysium、NapCat 或其他运行进程。
