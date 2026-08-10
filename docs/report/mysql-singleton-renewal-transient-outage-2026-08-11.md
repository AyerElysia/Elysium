# MySQL 单例 writer 续租瞬断平台契约报告（2026-08-11）

## 1. 结论

现场单实例在 storage authority renewal 遇到一次数据库异常后，服务层立即调用全局 `runtime.invalidate_writer()`，随后 Learning 持续报告 `SingletonWriterClaimLost`。现有数据库写事务本来已经逐次执行 generation 与 exact claim fencing，因此把一次连接异常直接解释为“所有权已丢失”既缺少证据，也扩大了故障域。

本次平台修复建立了结构化、可消费的边界：连接异常保持原始异常，表示续租结果未知；只有 claim store 明确拒绝 exact claim 时，才产生带精确 scope 的 `ManagedSingletonWriterClaimLost`。领域层可以据此只停止对应 singleton projector，而无需让整个 storage runtime 永久失效。

## 2. 根因

`StorageBackendRuntime._renew_managed_singleton_writers()` 原先直接传播底层异常，调用方只能看到一次统一的 `renew_authority()` 失败。服务层没有稳定类型来区分：

- MySQL 瞬时不可达；
- exact claim 已过期、被接管或 token/epoch 不再有效；
- 基础 generation authority 已失效。

因此消费者采用了过宽的全局 fail-closed。它保证没有无 fence 写入，却会在数据库恢复且原 claim 仍有效时永久停止 Learning，并连带影响无关 singleton scope。

## 3. 已实现的平台合同

改动范围限定在通用 storage runtime、公开导出和合同测试：

- 新增 `ManagedSingletonWriterClaimLost`，携带 generation、namespace、state key、owner instance、lease epoch、failure type 和原始 cause；
- 仅捕获底层 `SingletonWriterClaimLost` / `SingletonWriterClaimConflict` 并转换为结构化失租；
- SQLAlchemy/DBAPI 连接错误、超时与取消不包装、不清理 claim，原样传播；
- 新增 `invalidate_managed_singleton_writer(claim)`，只从本地管理表移除 exact 当前快照；
- 失效操作不访问数据库、不 release、不 acquire、不 takeover、不修改 generation authority；
- fencing token 不进入异常字符串、repr 或文档示例。

## 4. 消费端必须遵守的后续合同

本提交不修改 `LifeEngineService` 或 Learning 领域代码。消费端需独立完成并验证：

- 瞬时连接异常进入有界、可取消退避，健康状态为 `renewal_unknown/degraded`；
- 写事务继续逐次数据库时间 exact-claim 校验；
- 结构化失租只 quiesce 匹配 namespace/state key 的 projector/maintenance；
- 只使用异常中的最新 `claim` 执行精确本地失效，不解析文本；
- 不自动重新 acquire、rebase、takeover 或覆盖投影 revision；
- 基础 generation authority 确证失效仍使整个 runtime fail closed；
- Learning 未取得 projector claim 时保持 event-only，禁止 legacy local projection fallback。

## 5. 验证与安全边界

定向合同测试：`15 passed`。覆盖瞬时连接异常身份保持、Lost/Conflict 结构化转换、token 不泄露、精确失效幂等、旧快照不能移除新 claim、取消传播及既有 runtime state fencing。

完整 Life Engine 回归：`1361 passed / 14 skipped`；编译检查、变更生产代码与测试的 Ruff 检查、测试格式检查、全树 `git diff --check` 均通过。`plugins/life_engine/storage/__init__.py` 在本次修改前已有 I001/RUF022 与两处长导入格式债，本次只加入新异常公开导出，未扩大既有告警或重排其他 owner 的导出表。

全仓串行回归最终结果：`4198 passed / 20 skipped / 2 warnings`。首次全仓运行的唯一失败是独立 worktree 缺少被 `.gitignore` 排除的本机 `config/models.toml`；其余 `4197 passed / 20 skipped`。使用不含凭据、同样被忽略且未提交的最小测试夹具复跑该用例通过，随后完成上述单次全仓全绿；测试后移除临时夹具。

本次没有 schema 变更、配置变更、正式数据读写、进程操作或自动激活动作。Elysium 仍必须由用户手动启动；在领域消费端合入并由用户下一次手动启动前，当前运行实例不会自动获得新行为。
