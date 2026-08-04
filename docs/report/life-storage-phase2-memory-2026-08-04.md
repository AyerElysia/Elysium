# 生命域可选存储阶段 2 / Memory 交付报告

> 日期：2026-08-04
> 范围：Life Memory 领域存储合同、local/MySQL 双适配、并发不变量与公开查询边界
> 安全状态：未激活可选存储、未迁移正式数据、未启动或停止任何运行进程

## 1. 交付结论

Life Memory 已从“业务服务隐式拥有 SQLite 连接”推进为一个内部一致的 `MemoryStorageBundle`。Bundle 按固定顺序提供六个工程 Port：

1. `DocumentIndexProjection`
2. `ExperienceLedgerStore`
3. `WitnessLedgerStore`
4. `LivingMemoryStore`
5. `EpistemicMemoryStore`
6. `LegacyGraphStore`

local adapter 保留既有 SQLite 行为；MySQL adapter 使用显式领域表、版本化 migration 和平台 `StorageBackendRuntime`。同一个 Bundle 必须来自同一 backend generation，禁止按 repository 混搭 local/MySQL。

现役 `LifeMemoryService` 已消费 `LifeEngineService` 唯一拥有的 `StorageBackendRuntime`。当 `[storage].enabled=true` 时，Life Engine 必须先打开 coherent runtime，再将同一个 runtime 注入 Memory；未注入、runtime 未启动或任一 Memory Port 不可用都会 fail closed，禁止静默回退 SQLite。MySQL 模式不会打开或写入 `.memory/memory.db`，Memory 关闭时也不会关闭共享 runtime；统一关闭仍由 Life Engine 负责。`[storage].enabled=false` 保持既有 local SQLite 行为。

文档索引、启动恢复、Experience、Witness、Living Memory、Epistemic 与 legacy graph 的现役权威读写均已改走六个 Port。SQLite FTS 与 Chroma 只保留为 local/可重建投影，不获得跨 backend 的权威地位。

## 2. 领域行为合同

- Document Index 是可重建投影：路径身份规范化，document/chunk/词法索引/向量任务/tombstone 保持确定性，更新由 projection revision 保护。
- Experience 是不可变历史：稳定 occurrence 幂等；同一身份不同 payload 显式冲突；保留 producer identity、sequence 与时间。
- Witness 正文和来源链分离保存；consumer mirror 以 `position + revision` 单调 CAS 推进，不允许陈旧 writer 覆盖新位置。
- Living Memory 的 artifact、interpretation、relation、recall、corecall 只追加；artifact head 使用 expected-revision CAS，幂等重放不增加 revision。
- Epistemic claim/evidence/belief/conflict/state event/retrieval trace 分表保存；检索排名不能改变事实状态。
- Legacy graph 只提供兼容读取与可视化，不获得事实裁决权。

MySQL 写入全部通过 fenced unit-of-work：提交前复核 generation、authority epoch、owner、lease 和 fencing token。稳定身份重放由 payload SHA-256 校验；死锁、锁等待和并发插入竞争只在可判定安全的事务边界重试。

## 3. 本地并发缺口修复

- `memory_artifact_heads` 增加单调 revision；head 更新要求精确 expected revision，陈旧 writer 被拒绝，当前版本幂等重放不抬升 revision。
- `memory_witness_state` 增加 revision；mirror 更新改为 position/revision 双 CAS，并拒绝游标倒退。
- Witness 先提交权威 raw event offset，再协调 mirror；无新事件时也会从权威 offset 修复 mirror，而不是让健康缓存反向决定历史位置。
- Router、文件 lineage 和图谱读取不再穿透 `LifeMemoryService._db`，改为公开、可替换的领域查询边界。

这些 schema 变化是 additive migration，不改写既有主体内容或历史记录。

## 4. Schema 与工程边界

MySQL Memory schema 由六组有序、带 checksum 的 InnoDB migration 构成，对应六个 Port。实现没有把所有记录折叠为通用 JSON envelope；稳定身份、payload hash、版本、frontier、来源和可查询字段均使用明确列与约束。

local 的不可变性继续由既有事务与 SQLite trigger 保护；MySQL 适配器通过 payload hash、行锁、CAS 和 fenced transaction 保护。本阶段不声称 MySQL 已存在等价 UPDATE/DELETE trigger，也不把 FULLTEXT/Chroma 当权威历史。

## 5. 验证范围

自动化合同覆盖：

- 六 Port 的 characterization 与固定顺序；
- local 完整 Bundle 的 document、experience、witness、artifact、claim 与 legacy graph 行为；
- occurrence 幂等、artifact head 陈旧 writer、witness cursor 陈旧 writer和倒退拒绝；
- MySQL migration 顺序、版本与关键领域表；
- 真实 MySQL 的 document、experience、witness、artifact 与 epistemic 基础闭环。
- 服务级 backend 选择：缺少共享 runtime 时 fail closed、MySQL 模式不创建 SQLite、Memory 不关闭共享 runtime、默认 local 跨重启保持兼容。

真实 MySQL 用例是显式 opt-in：只有配置专用 `ELYSIUM_TEST_MYSQL_*` 环境时运行；缺少隔离测试库时必须显示 skipped。该 skipped 不代表真实远程 MySQL 已验收。

本次交付验证结果：

- Memory 定向回归：251 passed、1 skipped；跳过项为未配置隔离测试库的真实 MySQL 用例。
- Life Engine 领域回归：800 passed、4 skipped。
- 全仓回归：3394 passed、10 skipped。
- Ruff、Python compileall 与 `git diff --check`：通过。

## 6. 尚未完成与切换门

本交付不包含正式快照复制、逐记录校验、反向导出、Chroma/FULLTEXT 全量重建、生产 generation 注册/激活或真实运行切换。只有其他生命域 Port、复制校验、恢复演练、全链路行为等价和用户人工切换门全部通过后，才允许考虑启用可选后端。

原 SQLite、Markdown、JSON/JSONL、Chroma 和媒体数据保持原位；任何后续迁移仍必须遵循“复制、校验、可选切换”，不得移动、删除或覆盖源数据。
