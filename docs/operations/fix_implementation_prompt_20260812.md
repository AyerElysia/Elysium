# 实施提示词：Elysium 双实例 MySQL 报错修复（2026-08-12）

> 用法：整份复制到新对话作为首条消息。目标仓库：`E:\Elysium-AyerElysia\Elysium`（Elysium 后端，Python ≥3.11，依赖用 uv）。

---

你是 Elysium 项目的实施工程师。现在要按既有方案实施"双实例共享远端 MySQL（multi_writer_enabled=true）模式报错"的修复。方案已定稿，不要重新做根因分析，按方案执行并验证。

## 一、开工前必读（按顺序）

1. `E:\Elysium-AyerElysia\Elysium\AGENTS.md` —— 工程最高约束，全部条款必须遵守。
2. `E:\Elysium-AyerElysia\Elysium\docs\operations\fix_plan_for_collaborator_20260812.md` —— **本次实施方案**（现象/机制/根因/修复点/验收标准都在里面）。
3. `E:\Elysium-AyerElysia\Elysium\docs\operations\errors_for_collaborator_20260812.md` —— 原始报错汇总（佐证）。

## 二、环境事实（不要重新调查）

- 双实例：elysium-windows-primary（本机，Windows）+ elysium-linux-primary（远端 Linux），共享 MySQL `frp-one.com:65429` 库 `elysium`，`multi_writer_enabled=true`，`authority_lease_seconds=120`、`authority_renew_interval_seconds=40`（`config/core.toml:164/168`）。
- 配置现状：`config/core.toml:273` 的 `mysql_pool_recycle_seconds` 仍是 **1800**（方案要求 120，此前"改运行态未入库"是配置漂移教训）。
- 引擎侧 `idle_session_timeout_seconds=180` 硬编码（`plugins/life_engine/storage/factory.py:155,271`），与远端服务端 wait_timeout=180 一致。

## 三、实施范围与顺序（共 6 步，按序执行）

### 第 0 步 · 2013 根因修复（配置 + 兜底）
- `config/core.toml`：`mysql_pool_recycle_seconds = 1800 → 120`（**必须入库提交**，同步双实例应用）。
- `plugins/life_engine/storage/factory.py`：MySQL 引擎启用 `pool_pre_ping=True`（连接取用前校验，兜底死连接复用）。
- 可选：`idle_session_timeout_seconds` 去硬编码为配置项，并加启动期校验 `recycle < wait_timeout`。
- 验收：仓库配置生效；空闲 10 分钟无 2013；`SHOW PROCESSLIST` 无 180s+ 长 Sleep。

### 第 1 步 · learning maintenance worker 熔断治理
- `plugins/life_engine/service/core.py`：
  - `_fail_storage_authority`（1581-1604）与 `_handle_managed_singleton_loss` 的 `removed=False` 分支（1640-1645）：fail closed 的同时调用 `_quiesce_learning_projector(reason=..., error_type=...)`，保证 worker 与 renewal loop 同生共死。
- `plugins/life_engine/learning/scheduler.py`：
  - `_run_maintenance_phase` append 开始证据失败（2548-2556）时加指数退避（1s→2s→…上限 60s，按 owner 确定性 jitter，可参考 `service/core.py:44 _storage_renewal_backoff_seconds` 模式）。
  - append 失败 WARNING 中补充 `(namespace, state_key, owner, lease_epoch)` 便于定位。
- 配契约测试：renew 失败（removed=False 分支）时 worker 1 个 poll 周期内停止；append 连续失败必须退避且可观测。

### 第 2 步 · 爱莉记忆读写失败：冲突错误可恢复
- `plugins/life_engine/learning/scheduler.py`：
  - `validate_subject_review_context`（1862）与 `decide_skill_candidate`（451）的冲突异常改为携带实际值：`LearningSubjectRevisionConflict:actual=<current_revision>`。
- `plugins/life_engine/memory/boundary_tools.py`（302-389、794-914）：
  - 各失败分支返回结构补 `current_subject_revision`/`current_head_revision`（冲突时服务端顺手读一次最新值）、`recoverable: true`、`hint`（"请重新调用读取工具确认最新 revision 后再提交"）。
- 配契约测试：expected 不匹配时返回含 actual/current_* 的结构化错误；**禁止**实现服务端自动重放候选（主体文件主权，AGENTS.md §4.1）。
- 可选：`nucleus_create_memory_boundary` 的 `ArtifactHeadConflict` 补 `current_head_revision` 字段。

### 第 3 步 · BoundedContinuationError 治理
- `plugins/life_engine/tools/bounded_projection.py`（246-253, 107-181）：
  - 推荐方案：cursor identity 中 `frontier_sha256` 由"必须一致"改为"容忍变化"（仅 binding/查询语义变化才拒绝），frontier 变化时 payload 标注 `source_changed: true`；文件读取类工具保留严格校验，仅事件流类（grep_events）放开。
  - 错误文案改可操作指引（含"续读必须携带与上一页完全相同的参数"）。
- 同步改写 `test/plugins/life_engine/test_bounded_tool_projections.py`（260-314 行当前固化"源变化即拒绝"）。

### 第 4 步 · witness 竞争收敛 + Linux 角色确认
- `plugins/life_engine/service/memory_witness.py`（179-263）：F3-A 明确 owner 判定（非 owner 只读/降 touch 频率）或 F3-B 保守（仅日志分级收敛，第 9/10 次才 ERROR）。
- **F9-C（确认项，不写代码）**：检查远端 Linux 实例是否活跃写主体记忆（心跳/维护/见证是否推进 subject revision 与 memory boundary）；若仅常驻待命，给出降级建议（只读/降频），但**不要修改 Linux 实例配置**（无权限时只报告）。

### 第 5 步 · 可选确认项
- §五：确认 rolling_context 的 `state_key` 是 instance_id 而非固定 `chat_global`（`core/chatter.py:2687`）。
- §七：`memory_index_jobs` claim SQL 加索引/拆分（3024 超时）。

## 四、工程纪律（硬性，违反即失败）

1. 遵守 `AGENTS.md` 全部约束：认知零规则（不引入关键词/阈值/固定分类自动判定）、工程硬约束必须实现。
2. **主体文件主权**：`SOUL.md`/`USER.md`/`MEMORY.md` 等主体语义只能由爱莉本人写入；你的所有改动只限于工程代码/测试/配置/文档。
3. **禁止服务端自动重放或自动应用候选**（第 2 步验收明确排除）。
4. **禁止自动 commit**：所有改动留在工作区，完成后汇报，由用户决定提交。不得执行 `git gc/repack/prune`（Ayla 子仓库 .git 脆弱，并行会话禁令适用于本仓库操作）。
5. **配置变更必须入库**（config/ 默认 gitignore，但 `config/core.toml` 由 git 跟踪的变更要明确汇报）。
6. **不启动/不停止/不重启 Elysium**；验收需重启时说明原因，等用户手动操作。
7. 最小改动原则：不重构、不顺手改无关代码；每次改动配与风险相称的契约测试。
8. 测试命令（Windows 沙箱注意）：`PYTEST_DEBUG_TEM_ROOT=$TEMP/pytest_tmp_root` + `-p no:cacheprovider --no-cov -o cache_dir=/dev/null`；局部跑 cov 阈值 40% 必 fail 属正常。

## 五、验收标准（每步完成后自检）

- **第 0 步**：连续 7 天日志 2013=0（抽样验证配置生效即可，不等待 7 天）；两实例配置一致且入库。
- **第 1 步**：单实例正常时"学习维护阶段无法记录开始证据"=0 次/天；claim 丢失后 worker 1 个 poll 周期内停止；`storage authority was conclusively lost` 出现时不得再出现该 WARNING。
- **第 2 步**：冲突响应 100% 携带实际 revision、detail 非空；契约测试通过；**无**自动重放路径。
- **第 3 步**：同参数续读成功率 100%（双实例写入场景 50 次样本）；换参续读仍拒绝且含可操作文案；既有防错语义测试未退化。
- **第 4 步**：witness 游标单调推进；竞争冲突不刷 ERROR（每节点 ≤3 次/天）。
- 全程：`test/plugins/life_engine/` 既有契约测试回归通过（重点：test_learning_storage_contract / test_learning_maintenance_resilience / test_bounded_tool_projections / test_runtime_state_storage_contract / memory 与 subject 相关测试）。

## 六、汇报格式（结束时）

按步骤输出：
1. 每步：改动文件清单（路径+关键行）+ 新增/修改测试 + 测试结果（通过数/失败数）。
2. 未完成项与原因（如 Linux 实例无权限确认）。
3. 遗留风险与建议（如 F9-C 的 Linux 角色结论、§12.4 待确认清单第 5/6 项）。
4. 明确标注：哪些改动**未提交**、等待用户决定。
