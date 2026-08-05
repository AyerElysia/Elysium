# 生命域可选 MySQL 重构：切换就绪度报告（2026-08-05）

## 结论

可选存储重构的代码主干已经完成，且本地与 MySQL 两套实现已经通过真实隔离 MySQL 合同。Life Event、Life Memory、Presence、World、Subject Document 与 Learning 均消费同一个由 `LifeEngineService` 持有的 `StorageBackendRuntime`；生产配置仍保持 `storage.enabled=false`，当前运行中的 Elysium 仍以既有 SQLite/文件为权威。

目前不能宣称“生产 MySQL 切换已经完成”。剩余阶段不是继续补业务表，而是一次受控的生产切换：用户手动停止 Elysium、生成 `writer_frozen=true` 快照、复制到一个全新的远程 generation 数据库、完成五域同快照校验与反向导出、签署 generation，然后由用户选择后端并手动启动 Elysium 做真实聊天/记忆/重启闭环。

旧远程 `elysium` 数据库只包含 2026-08-04 在线快照的 shadow。它没有按 generation 隔离表名，且 Presence、World、Subject head、Memory projection/head 等可变状态已落有旧值；因此不得通过清表、覆盖或忽略冲突把它原地改造成正式 generation。最终切换必须使用新的空数据库，或由数据库管理员提供等价的独立 schema 和权限边界。

## 计划完成度

| 阶段 | 状态 | 已完成内容 | 仍需完成 |
| --- | --- | --- | --- |
| 0 决策与基线 | 已完成 | 数据域盘点、不可变量、性能与安全边界、复制而非移动 | 最终切换后补 MySQL 生产延迟基线 |
| 1 统一存储基座 | 已完成 | backend factory、单 runtime、UoW、generation/fencing、迁移 runner、copy authority | 无 |
| 2 Presence / World | 已完成实现 | local/MySQL Port、数据库时间 lease、CAS、outbox、World frontier/rebuild、在线影子往返 | 冻结快照的正式复制与启动验收 |
| 3 Life Event | 已完成实现 | local/MySQL ledger、游标 CAS、不可变触发器、复制与反向 SQLite、正式迁移 CLI | 冻结快照正式批次 |
| 4 Subject Document | 已完成实现 | 精确字节版本、head CAS、authority decision、workspace 投影、Witness 写前入账、shadow/production schema 分离 | 冻结快照正式批次与工作区投影验收 |
| 5 Life Memory | 已完成实现 | 六类 Port、32 张显式领域表、现役接线、复制/校验/反向恢复、20 张历史表与 42 个数据库触发器 | 冻结快照正式批次 |
| 6 检索投影 | 已完成代码合同 | 权威 hydration 与 lexical/vector/association 投影分层，Chroma 保持可重建 | 切换后从正式 generation 重建并测延迟 |
| 7 其他耐久状态 | 已完成 Learning 主域 | append-only Learning events、CAS projections、旧 `.life_learning` 精确字节导入与语义反向导出 | 冻结快照正式批次 |
| 8 验证后端与切换 | 工具完成，生产未执行 | 五域汇总审计器、generation 落盘门、今日在线候选与拒绝签署实测 | 新远程库、冻结窗口、正式复制、签署、配置选择、手动重启 E2E |

## 本轮新增的生产门

### 1. Life Event 正式迁移入口

新增 `scripts/migrate_life_events.py`，只接受 manifest 中唯一声明并通过物理 SHA-256 校验的 `life_events.sqlite3`。它支持 fenced candidate copy、批量续跑、消费者游标、反向 SQLite 导出和 content-free copy-run 证据。

在线快照会明确记录 `application-enforced-shadow`，且 `generation_eligible=false`；冻结快照才会要求数据库不可变触发器。

### 2. Learning shadow 与断线恢复

Learning schema 现在区分：

- `CANDIDATE_COPY + writer_frozen=false`：只建立表，不安装生产不可变触发器；
- frozen candidate 或 active writer：必须安装并验证不可变触发器，否则 fail closed。

迁移器只对 MySQL 2006/2013 断线做三次有界重试；权限错误、冲突、校验失败和其他数据库错误不会被重试掩盖。

### 3. Subject shadow 与生产触发器分离

Subject 的 v4 authority 表与数据库不可变触发器已经分离。在线 shadow 可以在没有 trigger 权限时安全建表；正式 generation 必须安装并验证版本、head event 与 authority decision 的 UPDATE/DELETE 保护。

### 4. 触发器不只“安装过”，还要在每次打开时核验

Life Event、Memory、Subject 与 Learning 的 active/frozen 路径现在都不只检查 migration checksum，还会读取 `information_schema.TRIGGERS`，核对触发器名称、目标表、操作类型、执行时机和拒绝语句。触发器被删除、替换或指向错误表时，后端拒绝打开。

### 5. 五域切换汇总审计

新增 `scripts/audit_life_storage_cutover.py`。只有以下条件全部成立时才允许写出 `generation.json`：

- 本地快照独立复验通过且 `writer_frozen=true`；
- Life Event、Memory、Subject、Presence/World、Learning 五个 copy run 来自同一个 manifest 与 source snapshot；
- 五个 run 均为 `verified`，冲突数为 0；
- 各域 verification 明确通过；
- 所有 append-only 域为 `trigger-enforced`；
- generation 数据包含本地 root、五域 MySQL root、frontier 与切换审计哈希。

任一门缺失时退出为不可签署，且不会创建空 generation 目录。

## 真实数据与远程 MySQL 证据

### 2026-08-04 旧在线候选

旧候选快照：

```text
C:\Temp\Data\ElysiumBackups\life-domain-20260804T0615Z-candidate
manifest_sha256 = 77435387f4acc59e48ffe625015d07575398465534b132e428c47e4617124862
source_snapshot_sha256 = d8f800108c71203396f9e6c39e8aa0a386ce7521d2d60ea333ee4ca13ff5a724
writer_frozen = false
```

在既有远程 MySQL shadow 上，本轮补齐并实跑：

- Life Event：`life-event-cli-v1-77435387f4acc59e`，86,094 条事件，冲突 0，源/MySQL/反向 SQLite root 均为 `019dea557c32ce26bd04de97353144baa94270b9d08513dd659ee9a595d3241b`；反向 SQLite `quick_check=ok`，86,094 条事件、1 个消费游标。
- Learning 首批 `life-learning-shadow-v1-...` 在写入前遭遇 MySQL 2013 断线，保留为 `failed`，目标表仍为 0 行；未伪装成功。
- Learning 恢复批次：`life-learning-shadow-v2-77435387f4acc59e`，452 条事件、2 个 ready 投影、10 个源文件、626,186 字节；精确字节导入核验与反向语义投影核验均通过，冲突 0。
- Subject v4 shadow schema：`subject-schema-v4-shadow-77435387f4acc59e`，authority 表建立成功且未安装生产触发器。
- Memory 不可变策略 shadow schema：`life-memory-schema-v9-shadow-77435387f4acc59e`，保持 `application-enforced-shadow`，未对旧影子数据安装生产触发器。

这些 run 的状态必须是 `copied` 而不是 `verified`，因为来源 writer 没有冻结。

### 2026-08-05 当前在线候选

新生成并独立复验：

```text
C:\Temp\Data\ElysiumBackups\life-domain-20260805T0111Z-online-candidate
size ≈ 2.1 GiB
manifest_sha256 = a1f5a9d93ad806303b6d9486035048eb39053799f9c7a9324118904dfc7aef6c
source_snapshot_sha256 = 957155f7af4223a55058417199ee94b1d4b12d4aaa64783dc2883f23ea3f3a8d
verification_root_sha256 = f5769cd2cec8f37492e6b6d90cb3568f6d97c8eb04e4f5ae05119f0ea23632eb
writer_frozen = false
verified items = 4,340
failures = 0
```

将这份新快照与 2026-08-04 的五个远程 run 交给汇总审计器后，审计器正确拒绝签署，原因包括：manifest 不同、snapshot 不同、run 不是 verified、不可变触发器未启用，以及当前快照未冻结。没有生成 generation 文件，也没有修改生产配置。

## 真实隔离 MySQL 验证

本轮在本机 MySQL 8.0.46 的全新隔离数据库中执行，测试库与专用账号在确认无活动连接后已删除；`log_bin_trust_function_creators` 仅在触发器测试期间由 0 临时设为 1，结束后已恢复为 0。

- 生命域实库合同：`8 passed`。覆盖 runtime、copy authority、Life Event、Memory 42 triggers、Presence/World、Subject、Learning 与通用触发器漂移检测。
- 通用 MySQL、核心 SQLite→MySQL、同步 ledger 与 memory archive：`10 passed`。
- 本轮相关 local/fake/审计定向：`44 passed`。
- 最新主线单进程全仓：`3619 passed / 14 skipped`，覆盖率 `67.56%`，无失败；Life Engine 全域为 `936 passed / 8 skipped`。

首次触发器测试在默认 binlog 设置下以 MySQL 1419 拒绝创建触发器；这不是被跳过的噪声，而是最终远程权限门的真实证据。调整本机隔离环境后全部通过；远程共享库没有调整该全局设置。

## 当前远程阻断

最终生产切换还缺两个外部条件：

1. 一个全新的空 MySQL generation 数据库，授予现有应用账号完整的该库读写权限；不得复用或清空当前旧 shadow 表。
2. 数据库管理员允许创建触发器。若服务器启用 binary log，可由管理员按其运维策略授予所需能力、预创建并核验触发器，或安全配置 `log_bin_trust_function_creators`；应用账号当前直接创建会得到 MySQL 1419。

数据库连接凭据只通过环境变量注入，未写入仓库、报告或 manifest。

## 最终切换步骤

外部条件就绪后按以下顺序执行，禁止跳步：

1. 用户手动停止 Elysium；确认所有已知本地 writer 已停止，记录 PID/端口/文件句柄证据。
2. 生成新的 `writer_frozen=true` 快照并独立复验。
3. 向全新的远程 generation 数据库运行五个迁移器；每个域使用同一 manifest，输出独立反向恢复目录。
4. 运行只读域审计与 `audit_life_storage_cutover.py`；只有 eligible 才落盘 verified generation。
5. 保留全部原 SQLite、Markdown、JSON、JSONL、Chroma 和旧 shadow，不删除、不覆盖。
6. 用户明确选择 MySQL generation 后修改配置；Elysium 仍由用户手动启动。
7. 完成真实聊天、消息入账、记忆写入/检索/联想、Subject 投影、Presence/World、Learning、进程重启恢复和后端健康闭环。
8. 若任一项失败，停止新 writer，依据反向导出与保留的本地 generation 回切；不得双主运行。

在上述步骤 1—7 完成前，最准确的状态仍是：**重构代码与迁移工具已完成，生产 MySQL 切换尚未完成。**

## 2026-08-06 AttentionThread 补充（当前口径）

本报告前文的“五域”是 2026-08-05 当时口径。当前切换汇总门已扩展为六域：Life Event、Life Memory、Subject Document、Presence/World、Life Learning、AttentionThread。`audit_life_storage_cutover.py` 新增必填 `--attention-run`，旧的五域 run 集合不再足以签署新 generation。

AttentionThread 的旧源 `thoughts/streams.json` 是状态快照而不是事件账本。迁移器只做原字节 archive、snapshot-only candidate import、逐行哈希校验和原字节 reverse export；旧快照始终不可激活，也不会产生 canonical 事件。只有 frozen copy run、trigger-enforced、legacy 非激活证明和全新的 canonical event/head/focus 空域证明同时成立，AttentionThread canonical 域才可进入 generation。

2026-08-06 已在本机隔离 MySQL 8.0.46 完成 Attention canonical 与 legacy migration 两项真实合同，并用 2026-08-05 在线候选快照跑通正式 CLI。在线批次按预期保持 `copied` 且不可签署；没有修改生产配置、正式数据或远程共享库。生产切换仍需新的冻结快照、新的空 MySQL generation 数据库和用户维护窗口。
