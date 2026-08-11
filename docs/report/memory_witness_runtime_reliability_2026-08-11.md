# Memory Witness 运行可靠性修复（2026-08-11）

## 范围

本次只处理 `MemoryWitnessCoordinator` 的两个运行可靠性故障：

1. selected MySQL 查询短暂返回错误码 2013 时，保留当前 Life Event 游标与已落账 Experience，并按 Witness 的短退避重放同一窗口；
2. witness、投影、consumer offset 与 witness-state mirror 已提交后，尾部 Presence activity touch 的 revision conflict 不再把成功轮次改报为失败。

未修改 Presence 存储实现、Memory 权威表、主体文件、日记、运行配置或运行数据，也未操作 Elysium/NapCat 进程。

## 现场证据与提交顺序

证据源为只读日志 `logs/elysium-2026-08-11.log` 与当前代码：

- `20:58:18`：`memory_witness` 在 `prepare_perception -> AsyncConsciousnessRegistry.refresh -> selected MySQL list_instances` 遇到 `(2013, Lost connection to MySQL server during query)`。此时 Experience append 已位于 authoring 之前，而 consumer offset 尚未提交。
- `23:23:08`：`run_once` 尾部 `touch_consciousness_instance` 抛出 `PresenceRevisionConflict(expected=1644, actual=1645)`。代码顺序表明此前 witness/projection、consumer offset 和 witness-state success mirror 已全部提交。

`run_once` 的耐久顺序为：

1. 读取当前 consumer offset；
2. 幂等追加 Experience；重放时同时使用 `inserted + existing`；
3. 生成或复用 witness，并完成主体文档/索引投影；
4. 提交 Life Event consumer offset；
5. CAS 更新 witness-state mirror；
6. 最后执行辅助性的 Presence activity touch。

因此 2013 前不得推进游标；尾部 Presence CAS 也不得反向否定步骤 1–5 已完成的耐久工作。

## 根因与修复

### selected MySQL 2013

现有 worker 只把 LLM 临时错误与 CAS 冲突归入短退避；SQLAlchemy `DBAPIError` 中的 MySQL 2013 被当成未分类永久错误，虽然证据和游标没有丢失，但下一次尝试要等待完整常规周期。

修复只识别已经观测到的 DBAPI 数字码 `2013`，不匹配错误文本，也不把其他数据库错误泛化为可恢复。命中后沿用现有有界短退避、错误聚合和恢复日志；日志只记录异常类型与数字码，不包含 SQL、服务端文本或经历正文。重放继续依赖既有 `append_experiences_detailed(inserted + existing)` 与 projection-path 幂等身份。

### 尾部 Presence revision conflict

辅助 touch 原先直接位于成功提交之后，CAS 冲突会冒泡到 worker 边界，随后写入 `last_error` 并输出“整轮失败”，与真实耐久状态不一致。

修复把这一步收口为 post-commit helper：首次冲突后刷新 Presence 快照并重试一次；若仍冲突，再刷新一次并只保留 content-free warning，`run_once` 仍返回已经完成的成功报告。取消继续传播，非 `PresenceRevisionConflict` 异常仍按原契约显式失败。

后续发生在 `ensure_instance()` 开工前的 Presence CAS 不属于本补丁；它没有已提交 witness 可保护，继续使用既有 fail-closed、刷新与短退避语义。

## 验证

- Witness deadline/recovery 专项：`7 passed`；
- Witness 游标、幂等重放、Memory storage 与 selected Presence 相邻回归：`68 passed`；
- Ruff、Ruff format check、compileall、`git diff --check`：通过。

专项测试显式验证：第一次 2013 后两次读取均从同一 cursor 开始，第二次 authoring 收到同一 Experience occurrence，consumer offset 只提交一次；尾部 Presence 连续两次 CAS 冲突时，witness 已生成、投影已完成、offset/state 已成功提交，`run_once` 仍返回成功。

## 回滚与验收边界

本轮只有工程代码、测试与本报告的未提交 diff。回滚可逐文件丢弃隔离 worktree 的变更，不涉及任何运行数据恢复。代码加载后的真实运行验收需要用户以后手动重启 Elysium；本任务按要求没有执行启动、停止或重启，也没有提交或推送。
