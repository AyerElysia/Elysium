# Learning projector writer guard 收口报告（2026-08-09）

## 结论

选择方案 A，但 claim 只保护可变 `learning_projections` 与 maintenance owner，不恢复已经退役的 `learning_events` INSERT singleton guard。这样同一 generation 中的合法实例都能按 occurrence 幂等追加不可变经历事实，而 `life_engine.learning/selected_persistence` 只有一个数据库时间、generation-scoped、带 epoch/token fencing 的 projector owner。

## 技术边界

- schema v3 继续幂等删除旧 v2 全域 guard；
- schema v4 仅为 `learning_projections` 的 INSERT/UPDATE/DELETE 安装 claim-aware trigger；
- v4 trigger 在没有有效 connection binding 时也拒绝写入，避免“尚未出现 claim 行”成为旁路；
- `open_learning_stores(..., writer_claim=claim)` 在 MySQL 上只读核验 v4 guard；
- `learning_events` 的 UPDATE/DELETE 不可变 trigger 保持不变；
- 不增加 runtime、claim 表、配置项或第二套 fencing 协议；
- 本提交不取得正式 claim、不修改正式 Learning 数据、不激活 generation，也不操作 Elysium/NapCat 进程。

## 消费端要求

Learning consumer 应让 evidence append 使用无 projector claim 的写事务，让 selected projection/maintenance commit 使用同一个注入 claim。projection revision 冲突或 claim 丢失必须 fail closed，不得自动 reload/rebase 后覆盖另一 owner 的结果。启动、续租、释放和部分启动清理继续由 `LifeEngineService` 负责。

## 验收

至少覆盖：v2 guard 先安装后由 v3 退役、v4 仅恢复三条 projection guard、无 claim 仍能追加事件、无 claim/旧 epoch/失租连接不能修改 projection、claimed projection commit 成功、迁移命令按 checksum 幂等应用 v1-v4 且不写业务行。

本次实际证据：

- Learning schema/迁移定向：`33 passed`；
- 本机临时隔离 MySQL 8.0.46：`1 passed`，覆盖首次 claim 前无旁路、claimed projection 成功、无 claim 事件追加成功、未绑定 projection 拒绝；退出后 `log_bin_trust_function_creators=0`，临时数据库与用户计数均为 `0`；
- Ruff 确定性检查、变更代码编译、变更范围 diff-check 通过；未格式化两个存在历史格式漂移的整文件；
- Life Engine 串行全集：`1242 passed / 14 skipped / 3 failed`；
- 全仓串行：`4028 passed / 20 skipped / 3 failed / 2 warnings`。

两轮全集的三个失败完全相同，均位于 `test/plugins/life_engine/minecraft/test_vision_injection.py`：主线测试夹具构造的 `_state` 缺少生产代码现已读取的 `body_name`，导致两个 `AttributeError` 和一个派生的空 payload 断言失败。本提交未修改 Minecraft 文件，不将该独立主线缺陷冒充为 Learning 回归通过，也不越权修复其 owner 文件。
