# Learning 大事件回扫与失效事务异常掩盖事故记录（2026-08-10）

## 摘要

2026-08-10 运行现场同时出现两条相互放大的故障链：

1. Learning 反思队列在旧 projection 尚无事件游标时，从全局 Learning 事件位置 0 开始读取，未在存储查询阶段限定 `reflection.enqueued`。现场 1,621 条事件的序列化载荷合计约 274.6 MB，其中包含大量快照；维护循环在游标提交前失败后又从头读取，形成高频远端传输和连接压力。
2. Runtime State 与 Learning SQL adapter 都在 `finally` 中执行 writer binding 清理。正文 SQL 一旦使连接或事务失效，清理阶段仍继续发送 SQL，触发 `PendingRollbackError`，覆盖了更早、更有诊断价值的原始 DBAPI 异常。

外部表现包括 Learning 维护反复失败、远端 MySQL 连接压力增大，以及运行上下文持久化只报告“需要先 rollback”，无法看到真正导致连接失效的首个数据库错误。

## 现场证据

- Learning event frontier 为 1,672；事件共 1,621 条。
- `payload_json` 合计 274,635,294 bytes，单条最大 1,132,823 bytes。
- 目标 `reflection.enqueued` 事件本身仅占很小部分；旧 projection 没有 `reflection_event_cursor_v1`。
- 同类 Learning maintenance `OperationalError` 在本次现场中重复出现 61 次。
- 08:00:06 的可见栈顶位于 `clear_singleton_writer_write()` 内的 `SELECT CONNECTION_ID()`，而不是最初失效的业务 SQL。
- 所有诊断均为只读；未记录或复制 DSN、密码、token、消息正文及 Learning 事件正文。

## 修复合同

### Learning 事件摄取

- `LearningScheduler._ingest_reflection_events()` 在 Store 查询阶段固定过滤 `reflection.enqueued`，让 SQL 使用 event kind/position 索引后再分页和解码。
- 每轮先捕获权威 source frontier；只交付不超过该 frontier 的记录，并发追加留到下一轮。
- 位置是 opaque token：页内只按实际返回位置推进；确定本轮相关事件已耗尽后，才安全推进到捕获的 source frontier。
- 队列满时停在未接纳的相关事件之前，不确认、不丢失。
- 权威事件、快照、projection 内容与排序均不修改。

### Writer binding 与事务清理

- 成功路径保持：bind → operation → clear → Unit of Work commit。
- operation、连接或取消失败时不再执行任何 clear SQL；原异常或 `CancelledError` 直接传播，由 Unit of Work rollback/close 原子撤销同事务 binding。
- clear 自身在成功 operation 之后失败时，整笔事务回滚，不提交领域写入或 binding。
- Runtime Context 在没有 writer claim 时不执行多余的尾部 clear；写入前的既有安全准备保持不变。
- 未新增第二清理事务，未手工 commit/rebase，未放宽 claim、generation、fencing 或 deadlock/lock-timeout 重试合同。

## 验收

- Runtime State 与 Learning storage contract：40 passed。
- Learning maintenance resilience：27 passed。
- Learning 全风险范围与 Runtime State 合同联合回归：125 passed / 1 skipped。
- 覆盖原 DBAPI 异常身份保留、取消传播、失败后 binding/领域写零泄漏、成功 clear 恰好一次、clear 失败整笔回滚、死锁重试重新 bind 且仅成功轮 clear。
- 覆盖旧 cursor 缺失且混有 1 MiB 非目标事件、opaque 稀疏 frontier、并发 frontier 后追加、容量边界、稳定过滤参数。
- Ruff `F/E9/I`、`compileall`、`git diff --check` 通过。

## 发布边界

本修复不操作 Elysium、NapCat 或 MySQL 进程，不改正式数据。已经运行的 Elysium 不会热加载这些代码；须由用户按生命周期规范手动重启后生效，并在新进程中验证首个原始 DBAPI 错误是否可见、Learning 游标是否持续前进、远端读取量是否恢复有界。

`epistemic_opportunity` 的 45 秒超时属于独立的模型路由与性能问题，不在本次数据库事务修复中混改。
