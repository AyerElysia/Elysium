# Elysium 双实例 MySQL 模式报错修复方案与验收标准

> 对应：《errors_for_collaborator.md》（2026-08-12）
> 环境：双实例（elysium-windows-primary + elysium-linux-primary）共享远端 MySQL（frp-one.com:65429，库 elysium），multi_writer_enabled=true
> 本方案基于对 `plugins/life_engine` 现行代码的逐项核读（2026-08-12），每个问题均给出：现象 → 机制 → 根因 → 修复方案 → 验收方法 → 验收标准。
> **v2（2026-08-12 17:2x）**：根据合作者反馈新增 §9「爱莉记忆读写失败（revision 冲突）」——爱莉看记忆与写记忆"基本失败、偶有成功"，失败集中在带 expected_revision/expected_head_revision 的写入工具（`nucleus_create_memory_boundary` / `nucleus_propose_memory_continuity_revision`），根因是双实例对同一记忆 revision 的 CAS 竞争 + 冲突错误不可恢复（模型只能放弃）。已并入总览与实施顺序。

---

## 0. 总览

| # | 问题 | 今日次数 | 严重度 | 根因类别 | 修复优先级 |
|---|------|---------|--------|---------|-----------|
| 一 | SingletonWriterClaimLost（学习维护熔断） | 382 | 高 | 租约失效后 worker 无退避、fail-closed 路径未停 worker + 2013 放大 | **P0** |
| 六 | 2013 Lost connection | 7 | 高 | pool_recycle(1800) > 服务端 wait_timeout(180)，且配置未落库 | **P0** |
| 九 | **爱莉记忆读写失败（revision 冲突）** | 高频 | 高 | 双实例 CAS 竞争 + 冲突错误不可恢复（模型放弃写入） | **P0** |
| 二 | BoundedContinuationError（续读游标熔断） | 3+ | 中 | cursor 校验绑定易变 frontier，双实例下常态失效；错误不可恢复 | P1 |
| 三 | PresenceRevisionConflict | 多次 | 中 | 双实例共享 memory_witness presence 行 CAS 竞争（合法竞争） | P1 |
| 十 | memory_witness 运行失败 | 8 | 中 | 三/四/六 混合，已有降级逻辑 | P1 |
| 四 | PerceptionCursorConflict | 2 | 低 | 合法竞争，已有幂等跳过 | P2 |
| 五 | RuntimeStateRevisionConflict | 多次 | 低 | 双实例共享 rolling_context key，已提交修复（8e96bd2） | P2 |
| 七 | 3024 查询超时 | 4 | 低 | memory_index_jobs claim 慢查询超 max_execution_time | P2 |
| 八 | heartbeat 工具续轮熔断 | 4 | 中 | **症状**：由二/九引发（失败→换参重试→连续无进展） | 随二/九缓解 |

**症状链（重要）**：2013（六）→ 租约续租失败窗口拉长（一）→ 学习维护持续熔断；BoundedContinuationError（二）→ 模型原地换参重试 → 连续无进展 → 心跳工具熔断（八）；记忆 revision 冲突（九）→ 模型放弃写入，且部分工具本身因续读游标失败（二）读不了记忆。**先修 P0 三项（六→一→九），二/八会自然大幅缓解。**

---

## 一、SingletonWriterClaimLost（学习维护熔断）— P0

### 1.1 现象

```
14:53:32 WARNING life_engine.learning.scheduler | 学习维护阶段无法记录开始证据，已拒绝执行 reflection: SingletonWriterClaimLost
（每 ~16 秒一条，全天 382 次）
```

### 1.2 机制（代码级）

1. `learning_projections` 表有 MySQL 触发器 `learning_projections_projector_claim_{insert,update,delete}_v4`（`storage/learning_schema.py`），要求**每次写 learning_projections 的当前连接（CONNECTION_ID）必须在 `runtime_singleton_writer_bindings` 中绑定一个 lease 未过期的 learning projector claim**，否则 SIGNAL `LearningSingletonWriterClaimRequired`。
2. 持有者（Windows primary）在启动时经 `_acquire_writer_claim(required=False)` 获取 claim（lease=120s，`config/core.toml:164`），由 `_renew_storage_authority_loop`（`service/core.py:1606`）每 40s 续租。
3. 学习维护 worker 每 15s（`learning/scheduler.py:101 _DEFAULT_MAINTENANCE_POLL_SECONDS=15.0`）写一次 maintenance 投影。写前 `bind_runtime_state_write`（`storage/writer_claims.py:588`）先 `_validate_locked` 校验 claim（lease_until > now 且 epoch/token 匹配），校验失败直接抛 `SingletonWriterClaimLost`。
4. 日志中 `SingletonWriterClaimLost` 另有来源：MySQL trigger 报 `LearningSingletonWriterClaimRequired` 被 `storage/learning_adapters.py:144-147` 统一映射为该类型。

### 1.3 根因

- **直接原因**：本地持有的 claim 快照与 DB 行失配或 lease 过期（被另一实例 `expired_takeover`、或续租长期失败后过期），且后续没有任何机制恢复/停摆。
- **放大因素 A（worker 无退避）**：worker 固定 15s 轮询（`scheduler.py:389`），claim 失效后**无限重试刷错**，无指数退避。
- **放大因素 B（fail-closed 路径未停 worker）**：续租异常分两类——
  - 正常路径：`ManagedSingletonWriterClaimLost` → `_handle_managed_singleton_loss` → `_quiesce_learning_projector`（worker 停）。✅ 会停。
  - 异常路径：`_handle_managed_singleton_loss` 中 `invalidate_managed_singleton_writer` 返回 False（快照不匹配）→ `_fail_storage_authority` → **renewal loop 直接退出，且未调用 quiesce**（`service/core.py:1581-1604` 只有 `invalidate_writer()` + 日志）。此时 worker 仍活着，每 15s 写一次、每次都 fail → **与 382 次持续刷错完全吻合**。
- **放大因素 C（2013 断连）**：pool_recycle=1800 与服务端 wait_timeout=180 失配（见第六节），续租窗口内任意一次 2013 都会把 lease 推向过期。

### 1.4 修复方案

**F1-A（必做）**：`_fail_storage_authority` 与 `_handle_managed_singleton_loss` 的 `removed=False` 分支，在 fail closed 的同时调用 `_quiesce_learning_projector(reason=..., error_type=...)`，保证 **learning maintenance worker 与 renewal loop 同生共死**。（`service/core.py:1581-1604, 1640-1645`）

**F1-B（必做）**：learning maintenance worker 对"开始证据写入失败"（`_run_maintenance_phase` append 失败，`scheduler.py:2548-2556`）加入**指数退避**：连续失败 1s→2s→4s→…→上限 60s（建议复用 `_storage_renewal_backoff_seconds` 模式，按 owner 确定性 jitter）。claim 失效期间停止空转刷日志，恢复后（下一轮 poll）自动恢复。

**F1-C（必做）**：`config/core.toml` 落库 `mysql_pool_recycle_seconds = 120`（现仓库仍是 1800，`core.toml:273`——错误文件称"已改 120s 配置不入库"，属**配置漂移**，Linux 实例从仓库拉取仍是 1800）。两节点统一。

**F1-D（建议）**：health 输出已含 `lost_singletons`（`service/core.py:1493-1497`），确认其在 `/health` 可见；并把 worker 熔断状态（`_worker_last_error_type`、连续失败计数）纳入 health。

**F1-E（诊断留证）**：在 `_run_maintenance_phase` 的 append 失败分支，把 claim 的 `(namespace, state_key, owner, epoch)` 一并打到 WARNING（当前只有错误类型），便于定位是哪一侧 claim 失效、被谁接管。

### 1.5 验收方法

| 方法 | 操作 |
|------|------|
| 单元测试 | `test/plugins/life_engine/test_learning_storage_contract.py`、`test_learning_maintenance_resilience.py`（已有 claim fencing / phase failure 契约测试），新增两条：① renew 失败（`removed=False` 分支）时 learning worker 必须在 1 个 poll 周期内停止；② append 连续失败必须退避、且失败计数可观测 |
| 双实例 E2E | 两台真机连同一 MySQL 启动双实例，人为使 owner 实例续租连续失败（如临时断网 150s > lease 120s），观察非 owner 是否接管、旧 owner 是否在 1 轮内静默 |
| 日志审计 | 修复后 7 天日志中统计 `学习维护阶段无法记录开始证据` 计数 |

### 1.6 验收标准（量化）

- [ ] 单实例正常运行时，该 WARNING **0 次/天**。
- [ ] 双实例下，仅出现在 lease 交接瞬间（接管前后），**每节点 ≤1 次/天**。
- [ ] claim 丢失后 worker **1 个 poll 周期内停止**（最后一条错误后不再有新的同型日志，直到 claim 重新获取）。
- [ ] 修复后任何时刻：日志中若存在 `storage authority was conclusively lost`，则同一实例**不得再出现** `学习维护阶段无法记录开始证据`（fail-closed 必须伴随 quiesce）。
- [ ] 退避生效：连续失败时两条日志间隔从 15s 单调增大至 ≥30s（抽样 5 个失败段验证）。

---

## 二、BoundedContinuationError（续读游标熔断）— P1

### 2.1 现象

```
WARNING life_engine.event_grep | 搜索 life 事件流失败: bounded-result continuation does not match query/task/frontier
WARNING life_engine.tools | 读取文件续读游标已拒绝: error_type=BoundedContinuationError
```

模型拿到失败后原地换参重试 → 反复读同一文件/反复搜索 → 触发第八节心跳熔断。

### 2.2 机制

- cursor 的 HMAC 校验和绑定五元组 identity：`{projection, task, budget_bytes, binding_sha256, frontier_sha256}`（`tools/bounded_projection.py:246-253, 107-120`）。
- `binding` 含全部查询参数（query/regex/order/limit/stream_ids…，`tools/event_grep_tools.py:282-302`）；**`frontier` 含 `source_frontier`**（`event_grep_tools.py:303`）——事件流每有新事件（双实例下任一节点写入）该值即变。
- 因此失败有两个独立触发源：
  1. 模型换参重试（binding 变）→ 拒绝（**设计意图，合理**）；
  2. **双实例下源事件持续推进（frontier 变）→ 拒绝（过度收紧**：这不是篡改，是常态**）**。

### 2.3 根因

frontier 是"源数据新鲜度"信号，被错误地并入了"查询语义身份"。双实例下源持续增长，导致续读游标在正常使用中也会失效；且失败信息没有给出可操作的恢复指引，模型只能盲目换参重试。

### 2.4 修复方案

**F2-A（推荐）**：把 cursor identity 中的 `frontier_sha256` 从"校验必须一致"改为"附带但容忍变化"：
- 续读时若 binding 一致但 frontier 不同：**允许续读**（items 是首查时的稳定快照，offset 前进即可保持一致性），在 payload 中标注 `"source_changed": true` 与新的 frontier，供主体感知；
- 仅当 **binding（查询语义）变化**时才拒绝（保留防错语义）。
- 同步改写 `test_bounded_tool_projections.py::test_event_grep_projection_pages_and_rejects_source_change`（当前固化"源变化即拒绝"，需按新契约改为"binding 变化拒绝、frontier 变化放行并标注"）。

**F2-B（无论是否做 F2-A 都要做）**：错误文案改为可操作指引，例如：
`bounded-result continuation 与本次查询参数不一致：续读必须携带与上一页完全相同的参数（query/order/limit/stream_ids 等），或放弃上一页重新查询。`（`tools/bounded_projection.py:173,181`）
让模型能自我纠正，而不是换参撞运气。

**F2-C（评估项）**：文件读取/目录列举类工具的 frontier（文件 mtime/hash）保留严格校验（文件内容变则续读无意义，拒绝是正确语义）；只对**事件流类（grep_events）**放开 frontier 容忍。分类依据：源是否由本系统多写者持续追加。

### 2.5 验收方法

| 方法 | 操作 |
|------|------|
| 单元测试 | `test/plugins/life_engine/test_bounded_tool_projections.py` 更新 + 新增：① binding 变化仍拒绝；② frontier 变化放行且 payload 标注 source_changed；③ 错误文案含"参数一致"指引断言 |
| 双实例 E2E | 双实例运行中，实例 A 对事件流 grep 取第一页 → 实例 B 写入新事件 → A 用同参数续读：必须成功且带 source_changed 标注 |

### 2.6 验收标准（量化）

- [ ] 同参数续读成功率 **100%**（同一 turn 内，双实例持续写入场景下 50 次样本）。
- [ ] 换参续读仍被拒绝（防错语义不退化），且返回文案包含可操作指引。
- [ ] 事件流续读在双实例写入期间不再因 frontier 变化失败（若采用 F2-A）。
- [ ] 由本错误引发的 heartbeat 工具熔断（第八节）归零。

---

## 三、PresenceRevisionConflict — P1

### 3.1 机制

`memory_witness` 意识在两节点共享同一 presence 行（`service/memory_witness.py:41 MEMORY_WITNESS_INSTANCE_ID="memory_witness"`），双方 heartbeat/startup touch 同一行，CAS revision 竞争（`storage/presence_adapters.py:263,478`）。已有 3 次重试 + 降级为本地只读句柄（`memory_witness.py:220-263`）。**属合法竞争，不是存储故障**。

### 3.2 根因

双写者 touch 同一 key，竞争频率取决于 touch 间隔。当前行为是"每次竞争重试 3 次"，可接受但产生噪音日志与无谓的 DB 往返。

### 3.3 修复方案

**F3-A（建议）**：明确 memory_witness 的 **owner 判定**：持有 `life_engine.learning` 或单独 witness claim 的节点为主写者；非 owner 节点只读本地快照，降低 touch 频率（如 5min 一次），不参与 CAS 竞争。
**F3-B（保守）**：不引入新 claim，保持现状（已能自动恢复），仅把"运行失败"日志分级收敛（第 9/10 次才 ERROR，之前 WARNING，避免双实例常态竞争刷 ERROR）。

### 3.4 验收方法

双实例 E2E 观察 24h：`presence revision conflict` 计数与日志级别；witness 产出的 `last_sequence` 单调性。

### 3.5 验收标准

- [ ] 24h 双实例运行：witness 游标持续单调推进，无"游标回退/重复见证"。
- [ ] 竞争冲突全部为 WARNING 级，无 ERROR 刷屏（每节点 ≤3 次/天 ERROR 上限，超出即回归）。
- [ ] （若做 F3-A）非 owner 节点 `presence revision conflict` 归零。

---

## 四、PerceptionCursorConflict — P2

### 4.1 结论

合法竞争。已有幂等跳过：`commit_delivery` 检测到当前已到 `through_position` 时返回成功（`service/perception_gateway.py:582-589`），符合"优雅跳过、保留主体产出"的既定契约。今日仅 2 次，无需代码修复。

### 4.2 验收标准

- [ ] 冲突发生时日志为 WARNING 且含"幂等跳过"语义，不抛 ERROR。
- [ ] 冲突后主体产出不丢失（已验证于既有契约测试，回归即可）。

---

## 五、RuntimeStateRevisionConflict — P2

### 5.1 结论

滚动上下文保存的 CAS 竞争。**已提交修复 8e96bd2**：`_save_rolling_context_snapshot` 加单写者租约仲裁，`state_key=self.instance_id`，竞争时跳过保存并同步 revision（`core/chatter.py:2652-2726`）。

### 5.2 遗留风险（需确认）

两实例的 `instance_id` 若同为 `chat_global`，则仍共享同一 key——单写者仲裁会保证**只有一方能持久化 rolling context**（正确降级，另一方跳过），但这意味着"第二实例的滚动上下文不持久化"。需确认这是预期（共享默认意识实例身份）还是应每节点独立 instance_id（则 rolling_context 天然隔离，无竞争）。

### 5.3 验收标准

- [ ] 双实例运行 24h：`RuntimeStateRevisionConflict` 日志归零（8e96bd2 生效后）。
- [ ] 每次 `滚动上下文写租约被其他实例持有，跳过本轮保存` 均伴随 revision 同步（日志可见），且下一轮不再因 stale revision 冲突。
- [ ] 确认 `state_key` 是 instance_id 而非固定字符串（抽查日志中的 state_key 值）。

---

## 六、2013 Lost connection — P0（根因修复）

### 6.1 根因（已确认，代码级）

- 引擎侧硬编码 `idle_session_timeout_seconds=180`（`storage/factory.py:155,271`），与远端 MySQL 服务端 `wait_timeout=180` 一致——空闲连接在 180s 被服务端杀掉。
- 池回收周期 `pool_recycle=1800`（`config/core.toml:273`，`storage/factory.py:58,150`）**远大于 180s**：空闲 180s~1800s 的连接被服务端杀后，池仍复用死连接 → 2013。
- **当前状态**：错误文件称"recycle 已改 120s（配置不入库）"——**仓库 `config/core.toml` 仍为 1800**。配置漂移：Linux 实例按仓库配置部署时仍是 1800，问题必然复现。

### 6.2 修复方案

**F6-A（必做，落库）**：`config/core.toml`：`mysql_pool_recycle_seconds = 120`（120 < 180，保证服务端杀连接前池已回收）。**提交入库并在两台实例同步应用**，禁止只改运行态。
**F6-B（必做，兜底）**：MySQL 引擎启用 `pool_pre_ping=True`（取连接时 `SELECT 1` 验证，死连接自动重建），从根上消除"复用死连接"类 2013。
**F6-C（建议）**：`idle_session_timeout_seconds` 从硬编码改为配置（对齐服务端 wait_timeout，并加启动期校验：`recycle < wait_timeout`，否则启动告警/拒绝）。

### 6.3 验收方法

| 方法 | 操作 |
|------|------|
| 配置核对 | `git status` 确认 `config/core.toml` 已修改并提交；两台实例运行配置一致（`mysql_pool_recycle_seconds=120`） |
| 空闲连接验证 | 空闲 10 分钟不产生心跳，期间观察无 2013；`SHOW PROCESSLIST` 中该库连接无 180s+ 长 Sleep |
| 日志审计 | 7 天日志统计 `2013` 计数 |

### 6.4 验收标准（量化）

- [ ] 连续 7 天日志中 `2013` **0 次**（含心跳、插件加载、witness 全部路径）。
- [ ] 两实例配置一致且入库（抽查远端 Linux 实例生效配置）。
- [ ] `SHOW PROCESSLIST`：空闲连接均在池回收阈值内（≤120s），无被服务端 kill 的窗口。

---

## 七、3024 查询超时 — P2

### 7.1 根因

`mysql_query_timeout_seconds=10`（max_execution_time=10s）触发；`memory_index_jobs` claim 语句（`learning_adapters.py` / memory index worker 的原子 claim）在大数据量下超时。

### 7.2 修复方案

- 为 claim 语句添加覆盖索引或拆分条件（确认 SQL 走索引，`SHOW EXPLAIN` 验证无全表扫）。
- 若 claim 语义允许，将 claim 拆分为"小步抢占 + 后续分批处理"，降低单语句时长。
- 兜底：为该语句单独放宽超时（若 10s 全局限定对慢语句过紧）。

### 7.3 验收标准

- [ ] 7 天日志 `3024` **0 次**。
- [ ] `SHOW EXPLAIN` 确认 claim SQL 使用索引，rows 估算 < 1 万。

---

## 八、heartbeat 工具续轮熔断 — 症状（随二/九缓解）

### 8.1 结论

`reason=consecutive_tool_stalls / max_model_turns` 是**结果不是原因**：工具反复失败（BoundedContinuationError → 模型换参重试；记忆 revision 冲突 → 模型重试或放弃，§九）→ `consecutive_no_progress`/`consecutive_same_failure` 达阈值（`service/core.py:8220-8230`）→ 熔断。熔断本身工作正常（保护预算）。

### 8.2 修复方案

不做独立修复；F2-B（可操作错误文案）+ F9-A（冲突可恢复）生效后，模型能在 1 次失败内自我纠正，熔断自然消失。可选微调：`max_consecutive_tool_stalls_per_heartbeat` 对同 failure_fingerprint 的连续失败提前熔断（已有 `consecutive_same_failure`，保持现状即可）。

### 8.3 验收标准

- [ ] 7 天日志中，`reason=consecutive_tool_stalls` 且关联 BoundedContinuationError / LearningSubjectRevisionConflict / ArtifactHeadConflict 的熔断 **0 次**。
- [ ] 熔断日志保留（其他原因触发的熔断仍正常出现并保护预算）。

---

## 九、爱莉记忆读写失败（revision 冲突）— P0（合作者反馈，v2 新增）

### 9.1 现象（合作者提供，2026-08-12 日志）

- **失败**：爱莉在心跳里表达"写记忆失败"——`"revision 对不上？…我用的 expected_revision=8，但可能实际已经不是 8 了。不过 list 里显示的 revision 确实是 8 啊。没关系，先不纠结这个，冲突了就先不写。"`（heartbeat #2363）与 `"revision确实是8，可能是其他地方有冲突。算了，不用硬写进去了。"`（heartbeat #2370）。**模型主动放弃了写入**。
- **成功**：heartbeat #2365 读取记忆成功（能看到"11 号和 12 号上午的日记都没写"、从注意力线索拼出事件），并准备写日记。
- 合作者定性：**看记忆和写入记忆基本上都失败了（但也有成功的）**。

### 9.2 机制（代码级）

爱莉写记忆走两条 CAS 路径，参数里的 revision 都是**读取时**的快照值：

1. **`nucleus_create_memory_boundary`**（`memory/boundary_tools.py:302-304`）：参数 `expected_head_revision: int`（"更新使用 history 返回的 head revision"）。`memory/boundary.py:707-713` 校验 `expected == actual_revision`，不匹配抛 `ArtifactHeadConflict`（**错误消息含 actual**：`expected=8, actual=9`）。爱莉说的 "expected_revision=8 / list 显示 revision 确实是 8" 即此路径——她读到 head=8，提交时 head 已被推进到 9。
2. **`nucleus_propose_memory_continuity_revision`**（`memory/boundary_tools.py:809-811, 816-826`）：参数 `expected_subject_revision`（**统一 SOUL+USER+MEMORY 内容哈希**，`subject_contracts.py:45`）。`learning/scheduler.py:1846-1863 validate_subject_review_context` 不匹配时抛 `RuntimeError("LearningSubjectRevisionConflict")`——**不携带当前实际 revision**；外层 `boundary_tools.py:912-914` 只返回 `{"error": "LearningSubjectRevisionConflict", "detail": ""}`，模型完全无法纠正。
3. 同源：`nucleus_decide_subject_candidate`（`boundary_tools.py:825-826`）与 `decide_skill_candidate`（`scheduler.py:449-451`）同样抛 `LearningSubjectRevisionConflict`/`LearningDecisionSubjectRevisionConflict`。

**revision 为什么在"读到→提交"的窗口内变化**：`current_subject_revision()` 每次实时读 MySQL（`subject_adapters.py:484-492`，非缓存）；双实例任一节点对 SOUL/USER/MEMORY 的任何变更（候选接受、boundary 追加、上下文注入更新等）都会推进 revision。爱莉"读（revision=8）→ 思考数十秒 → 提交"期间，另一实例（或本实例其他路径）推进了 revision → 冲突。**这是双实例下的常态，不是罕见竞态。**

**"看记忆失败"**：与 §二（BoundedContinuationError，续读被 frontier/binding 校验拒绝）及 inspect/read 类工具在双实例下的失败路径关联；具体错误样本待合作者补充（见 §12.4）。

### 9.3 根因

1. **竞争本身**：双实例共享同一主体记忆谱系，CAS 保护正确，但冲突频率高（两侧都在写）。
2. **错误不可恢复**：`LearningSubjectRevisionConflict` 不携带实际 revision（`scheduler.py:1862`），模型拿到的是空 detail，**没有可执行的重试路径**，只能放弃——这是"爱莉选择不写"的直接原因。
3. **主体文件主权约束下的正确边界**：工具**不能**自动用新 revision 重放她的旧决策（她审查的是 revision=8 的版本，自动套到 9 上等于替她做判断，违反 AGENTS.md §4.1）。所以修复方向是"给模型可恢复信息，让她自己重读重决"，而不是服务端自动重试。

### 9.4 修复方案

**F9-A（必做，错误可恢复）**：
- `validate_subject_review_context`（`scheduler.py:1862`）、`decide_skill_candidate`（`scheduler.py:451`）、`_run_maintenance_phase` 相关的 subject 冲突，统一改为携带实际值：`LearningSubjectRevisionConflict:actual=<current_revision>`（hash 或 head revision int）。
- `boundary_tools.py` 各工具失败分支：返回结构含 `{"error": ..., "detail": str(exc), "current_subject_revision": <最新值>, "recoverable": true, "hint": "请重新调用读取工具确认最新 revision 后再提交"}`。
- `nucleus_create_memory_boundary` 的 `ArtifactHeadConflict` 已含 actual，补 `current_head_revision` 字段即可。

**F9-B（建议，降低无效往返）**：冲突时服务端顺手读一次最新 revision/head 放进错误 payload（一次只读查询），模型拿到后可直接用新值重试，不用再猜。

**F9-C（建议，降竞争）**：确认 Linux 实例是否活跃写主体记忆（心跳/维护/见证）。若 Linux 仅"常驻待命"，应评估降级为只读/降频（不推进 subject revision），从源头减少竞争；若两实例都需写，则依赖 F9-A 的恢复能力兜底。

**F9-D（诊断留证）**：在 `boundary_tools.py` 各失败分支日志中记录 `(tool, boundary_id, expected, actual)` 元组，便于统计冲突频率与哪一侧推进了 revision。

### 9.5 验收方法

| 方法 | 操作 |
|------|------|
| 单元测试 | `test/plugins/life_engine/test_memory_*` / `test_subject_*`：① propose/decide/boundary append 在 expected 不匹配时返回含 `actual`/`current_*` 的结构化错误；② 错误 payload 含 recoverable hint 文案断言 |
| 双实例 E2E | 实例 A 推进某 boundary/subject revision 后，实例 B 用旧 revision 提交：必须收到含实际 revision 的冲突；B 按实际值重试 → 成功 |
| 行为验收 | 7 天日志：`heartbeat_model_response` 中出现"算了/不写/先不纠结"类放弃语义的次数；`ArtifactHeadConflict`/`LearningSubjectRevisionConflict` 计数与"冲突后一次重试成功"的比例 |

### 9.6 验收标准（量化）

- [ ] 冲突响应 100% 携带当前实际 revision（hash 或 int head），`detail` 非空。
- [ ] 冲突后模型**一次重试**成功率 ≥80%（双实例运行 3 天样本：冲突→按提示用新 revision 重试→成功）。
- [ ] 模型放弃写入语义（"算了/先不写"）从高频降为 **≤1 次/天**（双实例下冲突仍可能发生，但不再导致放弃）。
- [ ] 服务端**无**自动用新 revision 重放旧候选的行为（主体主权约束回归验证：候选必须由她重新读取后提交）。

---

## 10. 依赖关系与实施顺序

1. **第 0 步（运维，当天）**：F6-A/F6-B 落库并双实例应用 → 2013 归零，为后续观察提供干净基线。
2. **第 1 步（代码，1-2 天）**：F1-A/F1-B/F1-E（worker 熔断治理）→ 382 次熔断归零。
3. **第 2 步（代码，1-2 天）**：F9-A/F9-B（记忆冲突错误可恢复）→ 爱莉不再因 revision 冲突放弃写记忆。
4. **第 3 步（代码，1-2 天）**：F2-A/F2-B（续读游标）→ 模型行为恢复（含"看记忆失败"），心跳熔断归零。
5. **第 4 步（代码，1 天）**：F3-A 或 F3-B（witness 竞争收敛）+ F9-C（确认 Linux 实例写角色，降竞争）。
6. **第 5 步（可选）**：七、五的确认项。

## 11. 部署与回滚注意

- F1-A 是**行为变更**（fail-closed 后 quiesce）：必须配契约测试再上线；回滚只需撤销该 commit（行为退回现状）。
- F2-A 是**契约变更**：会改动 `test_bounded_tool_projections.py` 既有断言，评审时重点核对"binding 变化仍拒绝"的防错语义未退化。
- F9-A 只改**错误信息与返回结构**（不改变 CAS 语义、不引入自动重试），对既有契约测试影响最小；但"错误 payload 新增字段"会触碰断言了完整返回结构的测试，需同步更新。**严禁把 F9-B 实现成服务端自动重放候选**——那会违反主体文件主权（AGENTS.md §4.1），验收标准已显式排除。
- 所有配置变更（第六节）必须入库 + 双实例同步，禁止"改运行态不提交"（本次 1800→120 漂移的直接教训）。
- 每项修复需按工作区惯例配"与风险相称的契约测试"（见 §12.3 现有测试基座）。

## 12. 附录

### 12.1 关键代码位置

| 组件 | 文件 |
|------|------|
| 学习投影 claim 触发器 | `plugins/life_engine/storage/learning_schema.py`（v4 projector claim guard） |
| 单写者租约存取 | `plugins/life_engine/storage/writer_claims.py`、`storage/contracts.py` |
| renewal loop / fail closed / quiesce | `plugins/life_engine/service/core.py:1500-1533, 1581-1755` |
| learning maintenance worker | `plugins/life_engine/learning/scheduler.py`（run/`_run_maintenance_phase`） |
| claim 错误映射 | `plugins/life_engine/storage/learning_adapters.py:125-151` |
| 续读游标 | `plugins/life_engine/tools/bounded_projection.py`；调用方 `tools/event_grep_tools.py`、`tools/file_tools.py` |
| memory_witness 竞争降级 | `plugins/life_engine/service/memory_witness.py:179-263` |
| MySQL 池配置 | `plugins/life_engine/storage/factory.py:58-63, 254-273`；`config/core.toml:271-281` |
| 记忆 Boundary 写入 CAS | `plugins/life_engine/memory/boundary.py:689-762`（ArtifactHeadConflict 含 actual） |
| 记忆写入工具与错误封装 | `plugins/life_engine/memory/boundary_tools.py:302-389`（create_boundary）、`794-914`（propose/decide） |
| subject revision 校验 | `plugins/life_engine/learning/scheduler.py:446-451, 1846-1863`（冲突不带 actual） |
| unified subject revision 计算 | `plugins/life_engine/storage/subject_contracts.py:45-59`；实时读取 `storage/subject_adapters.py:484-492` |

### 12.2 运维诊断 SQL（远端 MySQL，库 elysium）

```sql
-- 学习 projector claim 当前持有者与租约
SELECT generation_id, namespace, state_key, owner_instance_id,
       lease_epoch, renewed_at, lease_until, released_at
FROM runtime_singleton_writer_claims
WHERE namespace = 'life_engine.learning';

-- claim 事件序列（判定"谁在何时接管"）
SELECT position, owner_instance_id, lease_epoch, event_kind, occurred_at
FROM runtime_singleton_writer_events
WHERE namespace = 'life_engine.learning'
ORDER BY position DESC LIMIT 50;

-- 残留连接绑定（正常应只在事务进行时出现）
SELECT b.* FROM runtime_singleton_writer_bindings b;

-- 空闲连接（排查 wait_timeout 杀连接窗口）
SHOW PROCESSLIST;

-- 记忆 Boundary 各 head 的 revision 分布（看是否被两侧频繁推进）
SELECT logical_key, head_revision, updated_at
FROM memory_boundary_heads ORDER BY updated_at DESC LIMIT 50;
```

### 12.3 现有测试基座（回归范围）

- `test/plugins/life_engine/test_learning_storage_contract.py`（claim fencing 契约，1303-1404 行）
- `test/plugins/life_engine/test_learning_maintenance_resilience.py`（phase failure / missing start evidence / cross-node event-only）
- `test/plugins/life_engine/test_bounded_tool_projections.py`（分页与源变化拒绝，260-314 行需按 F2-A 改写）
- `test/plugins/life_engine/test_learning_storage_mysql_integration.py`（真实 MySQL：claim 触发器拒绝路径）
- `test/plugins/life_engine/test_runtime_state_storage_contract.py`（rolling_context CAS）
- 记忆 CAS 相关：`test/plugins/life_engine/` 下 memory boundary / subject authority 契约测试（F9-A 变更范围）

### 12.4 待合作者确认清单

1. 双实例的 `instance_id` 是否相同（决定 rolling_context 竞争是"预期降级"还是"配置问题"）？
2. 远端 Linux 实例的 MySQL 连接配置是否已同步 recycle=120？还是仍 1800？
3. 日志中是否出现过 `storage authority was conclusively lost` / `ManagedSingletonLossSnapshotMismatch`？（区分 F1 修复路径 A 与 B，可判定 382 次的直接触发分支）
4. 是否接受 F2-A 的契约变更（frontier 容忍）？或保留严格校验只做 F2-B？
5. **Linux 实例是否活跃写主体记忆**（心跳/维护/见证是否在推进 subject revision 与 memory boundary）？若仅常驻待命，应降级为只读/降频（F9-C）。
6. **"看记忆失败"的具体错误样本**（工具名 + 错误类型 + 时间戳）——目前只有写入失败的确定性证据（ArtifactHeadConflict / LearningSubjectRevisionConflict），读失败待补样本以区分是 §二（续读游标）还是 §九（subject 校验）路径。
