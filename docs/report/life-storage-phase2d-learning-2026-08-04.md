# Life Storage Phase 2D：Learning 领域交付报告

> 日期：2026-08-04（Asia/Shanghai）
> 范围：Learning 事件/投影存储、维护韧性、主体候选决定、legacy 无损迁移与现役 `LifeEngineService` 接线
> 安全状态：未修改 `storage.enabled`，未迁移正式 `.life_learning`，未注册或激活正式 generation，未启动、停止或重启任何进程。

## 1. 交付结论

Learning 已从多份 JSON/JSONL/Markdown 直接承担“事实、当前状态和主体自我”的混合结构，收口为三层边界：

1. 不可变 Learning events 保存反思、证据、候选、决定、维护运行和 legacy 原始字节；
2. revision/frontier CAS 投影提供当前洞察、Skill、派生观察、调度状态和候选索引；
3. `SOUL.md + USER.md + MEMORY.md` 仍是唯一主体前缀权威，Learning 无权直接改写。

后台压缩、审计和 gate 只产生来源明确、可质疑的候选。selectable storage 模式下，只有当前 stream 绑定的活动 `ConsciousnessInstance` 能在读取精确候选 revision/hash 与统一 subject revision 后明确接受、拒绝或保持开放。接受最终由 canonical `SubjectAuthorityPort` 在同一个 fenced UoW 中校验证据并提交，Learning 不再维护平行权威接口。legacy local 兼容模式不具备该事务化权威口，因此只保留非权威派生版本，不提供伪安全的主体写入口。

## 2. selectable storage 合同

新增 Learning v1 领域 Port 与 local/MySQL adapter：

- `learning_events` 只追加，occurrence 同字节重放幂等、异字节冲突，数据库 trigger 禁止 update/delete；
- 事件保存 occurrence、source、actor、subject revision、provenance、payload 和规范 SHA-256；
- `learning_projections` 以 revision 与 source frontier 双 CAS 更新，并保存 schema/projector version、rebuild state 与投影 hash；
- `commit()` 原子提交事件和投影；稳定分页使用单调 position；
- health 只输出 frontier、revision、rebuild、pending buffer 和异常类型，不含正文、提示词或凭据；
- portable 限制覆盖标识长度、事件/投影 JSON 大小和查询 kind 数量，避免 local/MySQL 行为漂移。

`LifeEngineService` 仍是唯一 `StorageBackendRuntime` owner。enabled 模式从同一 runtime 打开并注入 Learning 与 Subject store，业务启动固定 `initialize_schema=false`；缺少 runtime/schema/store 时 fail closed，不打开第二个 runtime，不回退或双写 `.life_learning`。关闭时先 flush Learning，再继续释放其他消费者，最后仅一次关闭 runtime。

## 3. 主体决定链

Learning 提供有界 list/read/decide 工具：

- 列表只返回候选身份、状态、目标、hash、revision 与来源摘要；
- 正文通过 UTF-8 安全窗口分页读取，完整原文仍保存在不可变 event；
- actor 从运行 stream 获取，工具参数不能自报 actor；
- accept 必须带 exact candidate revision/hash、读取时的统一 subject revision，以及主体最终选择的完整目标文档；
- reject/kept_open 不接收正文，不调用 Subject Authority；
- `accept_requested` 是意志证据，只有 canonical commit 成功后才成为 `committed`；
- stale revision、inactive actor、occurrence 冲突、候选/决定证据不一致均显式失败，不自动 rebase 或合并。

Subject Authority 成功后先提交不可变文档版本/head/outbox，再由 service 投影工作区并刷新主体上下文缓存。Memory living artifact 只是下游派生镜像，不能反向成为主体权威。

## 4. 调度、队列与隐私

维护心跳拆分为 reflection、epistemic backfill、audit、compression、distillation、metrics、staleness 七个独立阶段。每个 due 阶段有 started/succeeded/failed 记录；一个阶段失败不会阻塞后续阶段，开始证据无法落盘时该阶段拒绝执行。

反思请求先进入有界持久队列，再调用 LLM。失败任务保留并指数退避，重启可恢复；成功后才删除。队列限制任务数量、正文/context 字节和 source ID 数量，拒绝损坏或重复记录。

日志、health、维护 fingerprint 和迁移失败状态只记录异常类型/模块，不复制异常消息，避免私密提示、模型返回或凭据经异常文本外泄。Learning 派生观察与 Skill 目录在 prompt 中被明确标记为“可质疑、非主体权威”，并有独立字节预算、投影算法和 hash 统计。

## 5. legacy 无损迁移与恢复

`scripts/migrate_life_learning.py` 只接受完整生命域快照：

1. 核对 `.life_learning` 文件集合与 snapshot manifest；
2. 把每个旧文件按不超过 1 MiB 的块保存为 immutable events；
3. 写入精确 manifest、完成事件和可重建语义投影；
4. 校验文件大小/hash、每个 chunk hash、重组字节和完成事件；
5. 可选反向导出到此前不存在的新目录，并再次逐字节核对。

迁移固定使用 copy authority 与 `CANDIDATE_COPY` runtime，不能激活 generation。源文件不删除、不移动、不改写；失败导出保留 incomplete 证据。本轮只验证实现和隔离合同，没有对正式数据执行迁移。

## 6. 正式数据只读健康快照

本轮只读审计看到 legacy 正式目录中：

- 88 条洞察：47 archived、15 candidate、26 validated；全部具有 source events 与 evidence；
- 6 条 legacy Skill，均保留历史 `emerging` 标签；无重复或空 ID；
- 洞察审计 422 行、Skill 审计 20 行、指标 36 行，未见无效 JSONL；
- 派生观察当前为 v2，v2 hash 与 `self_knowledge.md` 一致；
- 实际待处理反思队列为 0；
- 未发现真实 `.candidates` 残留目录。

这些数据未被本轮修改。legacy v2 继续保存，但新代码只把它作为非权威派生观察投影，不能覆盖 SOUL/USER/MEMORY。

## 7. 验证证据

- Learning/Subject 定向合同：47 passed，1 skipped；skip 为未显式启用的隔离 Learning MySQL 环境；
- Life Engine 全量：867 passed，7 skipped；
- 仓库全量：3488 passed，13 skipped，coverage 66.11%；
- 变更文件 Ruff `F/E9/I`（`core.py` 按既有基线执行 `F/E9`）：通过；
- compileall：通过；
- `git diff --check`：通过。

真实 Learning MySQL 合同必须在专用隔离库中设置 `ELYSIUM_TEST_MYSQL_LEARNING_ISOLATED=1` 后单独执行。当前未执行，因此本报告不宣称远程 Learning MySQL 已实库验收。

## 8. 后续人工验收门

1. 用户手动重启后观察完整 24 小时维护周期和反思重试恢复；
2. 由真实活动意识实例完成一次 reject/kept_open 无写入闭环；
3. 再完成一次 accept → SubjectAuthority → outbox → workspace/revision 生效闭环，并验证 stale revision 冲突；
4. selectable storage 切换前另行批准冻结快照、Learning 候选复制、反向恢复、隔离 MySQL 合同和 generation activation；
5. 任一门槛未满足时继续保持 `storage.enabled=false`。
