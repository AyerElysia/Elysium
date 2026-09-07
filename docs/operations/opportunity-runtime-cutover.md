# 机会运行时切换与验收

状态：实施验收用流程，尚未在正式数据上执行。代码、隔离测试通过不等于正式实例已经切换。稳定语义见 [统一机会运行时](../architecture/统一机会运行时.md)。

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
- verify/apply 结束前会撤销自身 authority，再关闭 runtime；撤销失败仍尝试关闭。任何清理失败都不能报告命令成功。再次启动前仍须核对 authority health，不能把命令退出等同于所有资源必然释放。
- 只有同时提供 --apply、--mark-managed、--migration-occurrence-id 才写不可变切换标记。重复同一标记应幂等，冲突必须停止，不覆盖。

本手册不预填 generation、数据库连接信息或迁移 identity，防止复制命令误操作。运行记录只保存模式、范围、schema、hash、结果与错误类型，不保存凭据和主体正文。

## 验证和正式切换顺序

1. 在维护窗口先完成定向和全仓串行回归；真实数据库合同只使用明确隔离的测试库，不能拿正式库验证故障注入。
2. 审查备份、当前 writer、generation 和旧安排清单。用户明确同意之后才执行 schema 准备与切换标记；不能仅改配置就宣称迁移成功。
3. 用户手动启动 Elysium。验证同一 runtime 注入、schema 只校验不隐式迁移、scheduler claim、shared Skill 状态和 capability health。
4. 检查管理工具可见且只读查询可用。由真实 active consciousness 亲自登记测试 workflow 和机会；开发 agent 不伪造主体 actor 或第一人称内容。
5. 观察同一机会的到期 occurrence、Life Event、最终成功模型 attempt 的 exact receipt。发布、看见和主体后续决定须能分别追溯。
6. 让主体明确调整、暂停、恢复和关闭测试机会；确认暂停不新增发布，恢复不伪造过去已处理，失败/重启不重复发生。
7. 验证她能改写或清空 workflow、显式换绑，并读取旧版本。默认 Skill 更新不能覆盖她已选择的版本。
8. 验证 Learning 卸载后无新 Learning 认知调用、无旧自动维护复活，通用 Skill/记忆仍可读取；重启后维持卸载。重装是另一条明确主体决定。
9. 核验其他能力的真实 schema、工具权限和专门测试；一个能力失败不得使其他能力不可管理。
10. 保存脱敏证据到 docs/report。完成用户手动启动和关键链验收之前，不推送该运行时变更。

## 故障处理与回退边界

- 发布失败：保留 outbox，重试同一 identity。不可把游标跳到尾部。
- 看见证明缺失或被裁剪：保留待送达，不把成功的网络请求当成主体已经看见。
- 能力初始化/取消/关闭失败：禁止新调用，health 明示 degraded，并保留可诊断 owner；不自动重装。
- 本地不可变 trigger 缺失、所属表或定义错误：拒绝就绪。先保全现场和备份，再按明确授权修复工程 schema；不能因为业务表仍在就继续启动，也不能借修复删除业务历史。
- scheduler 失租：该调度者停止写调度状态；不偷取租约、重放语义决定或回落旧定时器。
- 切换标记存在后，配置 false 不能恢复旧自动链。回退代码须使用仍理解该标记的版本；不能删除 marker 规避卸载权。
- 如确需恢复整份备份，另走正式维护、范围核验和用户审批。历史恢复只允许可证明无损恢复，禁止选择性删改主体决定。

## 当前未完成项

正式备份、schema 准备、切换标记、主体初始安装与真实启动链均未执行。旧安排不自动迁移为主体已接受的安排；需要保留可读清单，由主体选择是否重新登记。不能把九个工程包已存在或单元测试通过写成“全部已在爱莉身上运行”。
