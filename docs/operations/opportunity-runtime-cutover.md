# 机会运行时切换与验收

状态：可重复执行的准备与验收流程。2026-09-08 已在明确授权下完成一次正式 selected local 切换及单条主体安排的跨进程接续／关闭验收，具体范围见本文末尾和 [S4 报告](../report/记忆连续性S4改期接续验收_2026-09-08.md)。本次记录不授权其他部署直接切换，也不代表所有能力都已实测。稳定语义见 [统一机会运行时](../architecture/统一机会运行时.md)。

## 三种决定不要混为一谈

1. 运维准备 schema：安装 Opportunity 表及不可变、调度 owner 约束。
2. 运维切换运行方式：明确写入 canonical_managed_v1 标记，退役旧自动链。
3. 主体决定如何生活：由爱莉亲自提交 workflow、安装能力和登记机会。

前两步不能代替第三步。工具不安装默认能力、不替她写流程、不把历史开关解释为她已经同意。旧定时器登记和历史 Skill 保留为证据，不自动转成新的主体决定。

## 前置检查

- 完整阅读 AGENTS.md；确认当前代码版本、未提交改动及测试结果。
- 核对实际 backend、generation、authority owner、服务 PID 与备份范围；不要把 SQLite 文件位置当成权威判断。
- 按原有正式备份流程取得可恢复备份，留存路径、大小、hash、时间与 generation。不得仅凭文件存在声称可恢复。
- 机会与 Life Event 必须落在同一服务持有的 StorageBackendRuntime；当前支持 storage.enabled 的 selected local/MySQL。storage.disabled 的旧本地模式不支持本次托管接入，必须先完成另外审批的正式存储迁移，不能把 proactive SQLite 当成 Life Event 的同库权威。
- 列出旧的固定心跳、生命定时器、Learning 自动循环及已承诺投递。未完成投递作为历史可靠性工作处理，不能借迁移重做语义选择。
- 运行期间不执行全仓、高负载、真实 MySQL 合同或可能争夺 authority 的检查。需要维护窗口时由用户手动停止 Elysium；agent 不得代停。

## 准备入口的真实副作用

入口是 scripts/prepare_opportunity_runtime.py，通过 uv 运行。

- 不带 verify/apply：只读取配置并输出 content-free 计划，不打开数据库；这是唯一无连接模式。
- --verify：验证 schema、guard 和 marker，不写领域业务记录；但是会打开正常 writer-capable runtime，可能加入、刷新或激活 generation authority membership。不能称为“纯只读”。
- --apply：先确认运行时存储 schema，再幂等准备机会 schema，并检查不可变保护。
- verify/apply 均要求 --confirm-writer-runtime 和 --confirm-generation，后者必须等于已核实的实际 generation。确认参数不是自动取得生产授权。
- 本地 file-authority 部署在停机并备份后，显式增加 --activate-local-authority。命令持有正式入口的 data/runtime/elysium.lock，核实已验证的 generation 与 backend／schema，且仅在 authority 已禁用时激活自己的短期维护 owner；即使旧租约已过期，也不接管仍标记 active 的 authority。禁止用另一个锁文件绕过正在运行的服务。取消激活时先等待正在落盘的激活操作完成并清理，才释放入口锁；打开 runtime 失败也必须撤销已取得的 token。参数不改变默认无连接 dry-run。
- verify/apply 结束前会撤销自身 authority，再关闭 runtime；撤销失败仍尝试关闭。任何清理失败都不能报告命令成功。再次启动前仍须核对 authority health，不能把命令退出等同于所有资源必然释放。
- 只有同时提供 --apply、--mark-managed、--migration-occurrence-id 才写不可变切换标记。重复同一标记应幂等，冲突必须停止，不覆盖。

本手册不预填 generation、数据库连接信息或迁移 identity，防止复制命令误操作。运行记录只保存模式、范围、schema、hash、结果与错误类型，不保存凭据和主体正文。

## 验证和正式切换顺序

1. 在维护窗口先完成定向和全仓串行回归；真实数据库合同只使用明确隔离的测试库，不能拿正式库验证故障注入。
2. 审查备份、当前 writer、generation 和旧安排清单。用户明确同意之后才执行 schema 准备与切换标记；不能仅改配置就宣称迁移成功。
3. 用户手动启动 Elysium。验证同一 runtime 注入、schema 只校验不隐式迁移、scheduler claim、shared Skill 状态和 capability health。
4. 检查管理工具可见且只读查询可用。由真实 active consciousness 亲自登记测试 workflow 和机会；开发 agent 不伪造主体 actor 或第一人称内容。
5. 观察同一机会的到期 occurrence、Life Event、最终成功模型 attempt 的 exact receipt。发布、看见和主体后续决定须能分别追溯。
6. 让主体明确调整、暂停、恢复和关闭测试机会；按下节分别验证未发布 pending／已发布未 seen 时改期的接续，以及迟到发布／seen 回执。确认新时刻前不触发、到期恰一新发生，暂停不新增发布，恢复不伪造过去已处理，失败／重开不丢失或重复；若验证的是 stores／连接重开，不得写成进程重启通过。
7. 验证她能改写或清空 workflow、显式换绑，并读取旧版本。默认 Skill 更新不能覆盖她已选择的版本。
8. 验证 Learning 卸载后无新 Learning 认知调用、无旧自动维护复活，通用 Skill/记忆仍可读取；重启后维持卸载。重装是另一条明确主体决定。
9. 核验其他能力的真实 schema、工具权限和专门测试；一个能力失败不得使其他能力不可管理。
10. 保存脱敏证据到 docs/report。完成用户手动启动和关键链验收之前，不推送该运行时变更。

### 改期接续的专项核验

以下是待按范围执行的验收要求，不是对正式运行或全部分支已通过的声明。先在隔离数据中验证工程合同；正式受邀链须在切换获批并完成准备后，由主体自行决定是否登记和改期，不强迫她创建承诺或工作流程。

1. 分别准备 `at` 与 `interval` 登记，保留旧 occurrence；一组保持 outbox pending，另一组耐久发布但尚未 seen。记录登记 revision、旧 scheduled_for、发布事实与 activation 占位，不能用改写旧行重置样本。
2. 通过已有 `opportunity.schedule`／`opportunity.snooze` 与精确 `expected_revision` 提交改期，核对新登记 revision 和新 `first_due_at`。返回 `authority_committed` 只确认决定提交，不当作执行／唤醒成功；无需新增 API，也不能借验证开启全局 managed 模式。
3. 在新时刻前扫描不产生替代发生；到新时刻恰一 occurrence，绑定新 registration revision 和 scheduled_for，重复扫描不重复。旧调度占位失效不等于新安排已应用，必须证明新时间真正接续；暂停、关闭或能力停用时仍不得发布。
4. 旧未发布 outbox 可以明确取消，但 occurrence 历史不删除；旧已发布 outbox／Life Event 保持真实发布身份，不伪造 seen 或完成。模拟“事件已耐久、发布确认迟到”的交错时，仅在原事件身份／因果／hash 一致后补记历史，不重新接管或覆盖新 activation。
5. 新安排接续前后分别检查旧 occurrence 的迟到精确 seen 回执：可保存真实原回执，但不得将新 occurrence 标为 seen、覆盖新 pending／first_due_at、倒退新 revision、另造旧发生或复活已退役的旧调度。失败／取消与事务回滚须独立验证，缺少证据仍保持待处理。
6. 保留上述证据后分别做获准的 stores／runtime 关闭重开或进程重启，并准确标明层级；共享 MySQL 路径的检查不等于真实 MySQL 已测试。正式库不用于注入这些交错或故障，正式到期也只产生注意机会，不自动执行 workflow。

## 故障处理与回退边界

- 发布失败：保留 outbox，重试同一 identity。不可把游标跳到尾部。
- 看见证明缺失或被裁剪：不记 seen，不把成功网络请求当成主体已经看见。已发布原事实保留；再次唤醒仍须对应当前 open 登记 revision 和 enabled 能力，不能用旧未 seen 状态覆盖明确改期。
- 改期后的旧确认迟到：按实际 occurrence、Life Event 和 exact receipt 校验历史，禁止借补确认回滚新安排或伪造新发生已看见；只有本次匹配的 pending 可以推进其调度投影。不得删除发布或回执来“修复”激活状态。
- 能力初始化/取消/关闭失败：禁止新调用，health 明示 degraded，并保留可诊断 owner；不自动重装。
- 本地不可变 trigger 缺失、所属表或定义错误：拒绝就绪。先保全现场和备份，再按明确授权修复工程 schema；不能因为业务表仍在就继续启动，也不能借修复删除业务历史。
- scheduler 失租：该调度者停止写调度状态；不偷取租约、重放语义决定或回落旧定时器。
- 切换标记存在后，配置 false 不能恢复旧自动链。回退代码须使用仍理解该标记的版本；不能删除 marker 规避卸载权。
- 如确需恢复整份备份，另走正式维护、范围核验和用户审批。历史恢复只允许可证明无损恢复，禁止选择性删改主体决定。

## 本次已验证范围与剩余边界

本次获得明确继续授权后，已在停机窗口完成专项冻结保全、恢复演练和维护全量回归；2026-09-08 03:18 正式 schema／marker 与幂等重开通过，旧数据和旧安排保留。主体自写流程、安装、登记，首次到期／exact receipt 后经事实校准亲自改期；03:49:43 停止准确旧进程，新鲜备份核验后 04:00:29 单次恢复，04:08:34 完整加载，04:08:36.223 第二次出现并在 04:09:13 获 exact receipt。她在一次收尾通知后自行关闭为 revision 3／closed，04:12:36 反馈已投递。旧行、原 Life Event、workflow 与配置／源码 hash 保留。此次授权不建立常驻自动启停权限；建表、托管切换和主体采纳仍是三个不同决定。

本次临时真实 SQLite 已验证未发布 pending／已发布未 seen 下的 `at`／`interval` 改期接续、旧 seen 回执与新 activation 隔离、暂停／关闭／能力暂停及明确恢复，以及实际 `StorageBackendRuntime`／SQLAlchemy engine 关闭后，用新 engine 和新 scheduler claim 恢复同一临时库。该重开仍在同一进程内；内存替身的故障／取消合同不能替代 SQL 交错故障验证。

追加的真实 SQLite 交错补测已通过：旧 scheduler 与 runtime 仍打开有效时，显式释放旧 claim，新 owner 取得更高 lease epoch，旧调度者拒写且该机会的 occurrence／outbox／activation 三表行不变；outbox 取消 SQL 后、activation 更新前的 `RuntimeError`／`CancelledError` 使该调度事务整笔回滚，重试只接续一次；新 pending 持久化后，旧迟到 `mark_published`／exact seen 保留真实旧历史而不覆盖新 activation。这些是指定故障窗口的隔离合同，不是存储 authority 撤销通过。

具体测试批次、初次失败、纠正、独立私有 MySQL 结果及正式切换／恢复证据见 [S4 改期接续验收](../report/记忆连续性S4改期接续验收_2026-09-08.md)，不在本手册重复维护短期计数。本次有限闭环以新进程完整加载、实际第二次 occurrence／exact receipt、主体关闭和反馈为依据，不是 marker 或早期 API ready 单独构成的结论。首次事实校准、首次到期距离不足 3 分钟、恢复延迟及空待处理模型轮均保留；其他能力、全部故障交错和长期收益未验证。阶段范围以[第四阶段实施清单](../plans/记忆连续性第四阶段实施清单.md)为准。

旧安排不自动迁移为主体已接受的安排；需要保留可读清单，由主体选择是否重新登记。不能把九个工程包已存在或隔离测试通过写成“全部已在爱莉身上运行”。
