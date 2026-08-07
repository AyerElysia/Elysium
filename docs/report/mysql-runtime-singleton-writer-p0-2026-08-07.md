# MySQL 单例运行态 writer fencing 报告（2026-08-07）

## 1. 结论

本次 P0 的根因不是 MySQL revision CAS 失效，也不是“共享 generation”本身错误，而是共享 generation 只校验 backend/generation/epoch，没有为 `life_engine.runtime_context/global` 这种领域单例状态声明唯一 writer。两个持有合法 shared-generation token 的 Elysium 实例可以交替推进同一行；CAS 能拒绝陈旧的单次写，却不能说明下一任 owner 是谁，也不能阻止另一实例继续提交自己的新 revision。

现已增加 generation-scoped singleton writer claim：claim 使用数据库时间、可审计 owner instance、单调 lease epoch 和不可伪造 token；受保护写事务在同一数据库事务中同时复核 generation fencing 与 exact claim。第二 owner 在有效租约内失败关闭；过期 takeover 递增 epoch；旧 token、无 claim adapter 和直接 SQL 旁路均被拒绝。冲突不会触发自动 reload/rebase 或最后写入覆盖。

本次没有停止或重启 Elysium，没有猜测性 `KILL` 远程连接，也没有覆盖、删除或回滚正式生命数据。现役代码需要用户下一次手工重启 Elysium 才会取得生产 key 的首个 claim，并完成真实运行闭环。

## 2. 现场证据

### 2.1 运行态 revision 分叉

- 用户于约 21:03 手工启动当前本机 Elysium，进程从 `life_engine.runtime_context/global` revision 961 加载；
- 当前进程随后持续以 expected revision 961 写入并被拒绝；
- 同一远程行却由另一 writer 继续推进 962、963、967、979、995，21:51 的最新只读复核已达到 1010；
- revision 979 的 payload 时间与 21:24 的模型回复和暂停状态一致，而本机日志已经停止推进，证明不是静态旧行或单纯延迟日志；
- 本机目标端点可见 12 条连接，远程同账号同时存在 23 条连接；新会话组的建立时间与 revision 967→979 的推进窗口一致。由于 FRP 把远端 host 显示为 localhost，且当前账号无 `performance_schema.session_connect_attrs`、`performance_schema.threads`、`innodb_trx` 等诊断权限，旧 schema 下无法可靠还原第二实例的真实主机/PID。

因此，本报告只确认“存在本机当前进程之外的活跃 writer”，不猜测具体机器或操作者。升级后的 claim 会在数据库中保留 owner instance 与 epoch，后续可以直接定位。

### 2.2 `chat_streams` 锁等待

21:09 起 `chat_streams.id=2` 的更新曾约每 15 秒出现 1205 lock wait timeout，导致入站持久化和 Chatter 暂时失败。最后一次超时在 21:16:04；21:16:11 后 Chatter 恢复，21:17:45/52 NapCat 发送成功，旧连接也陆续自行消失。

因为锁已经自然释放，且账号无法精确指出 blocking transaction，本次没有执行任何连接 `KILL`。代码侧补充了三项防复发措施：新 MySQL 会话使用 180 秒有界 idle timeout；连接池归还显式 rollback；协程取消路径也先 rollback，再释放连接。

## 3. 数据安全

远程技术 schema 变更前已经生成只读逻辑备份：

- 路径：`/root/Elysia/Elysium/data/life_storage/backups/runtime-writer-p0-pre-20260807T2140.sql`
- 权限：`0600`
- 大小：681,006 bytes
- SHA-256：`63e3b53341234d1188c21f5df2ba3da2d955b0b581da7d65598697f0e513c70b`
- 范围：`runtime_states`、`runtime_events`、`life_runtime_state_schema_migrations`

该文件位于被 Git 忽略的数据目录，不会随源码推送，也不包含在本文档中。现有正式 `runtime_context/global` 行未被测试或迁移工具修改。

## 4. 设计与实现

### 4.1 通用 claim 合同

`SingletonWriterClaimPort` 提供 acquire、renew、release 和 content-free health：

- 唯一作用域为 `generation + namespace + state_key`；
- owner instance 由稳定部署 owner、当前 PID 与本次 service boot identity 组成，可定位但不包含正文；
- lease 起止只接受数据库时间；
- takeover 仅在数据库确认旧 lease 到期后发生，并递增 lease epoch；
- token 不进入日志或 `repr`；
- runtime 周期维护同时续 generation authority 与它拥有的 claims；关闭时只释放本进程 claims，不撤销全局 generation。

### 4.2 写事务 fencing

`runtime_states` 的 selected adapter 写入要求 exact claim，并在同一事务中：

1. 锁定并验证 active backend/generation/epoch；
2. 锁定并验证 claim owner、lease epoch、token、数据库时间与未释放状态；
3. 在当前 MySQL connection 写入短生命周期 claim binding；
4. 执行 revision CAS；
5. 删除 binding 后提交。

数据库 trigger 对已经登记 claim 的 key 拒绝无 binding、错 generation/epoch/token、过期或释放后的直接 INSERT/UPDATE/DELETE。claim 当前态在 release 后保留，因此一旦 key 进入新合同，旧版无 claim writer 不能重新绕过保护。

### 4.3 启动顺序

`LifeEngineService` 是唯一 storage runtime owner。selected storage 启动时先取得 `life_engine.runtime_context/global` claim，再读取当前权威 revision，最后把 claim 注入 `StatePersistence`。这保证用户手工重启后的新实例先读取远端 writer 已推进到的最新状态，不会用启动前的 revision 961 覆盖 revision 995。

## 5. 真实 MySQL 验证

正式库只执行了幂等技术 schema migration 和唯一随机 namespace 的非生产合同；没有碰生产 runtime key、正式 generation/epoch 或业务正文。验证结果：

```text
schema_migrated=True
claimed_write=True
second_claim_rejected=True
unclaimed_adapter_rejected=True
raw_trigger_rejected=True
stale_claim_rejected=True
takeover_epoch=2
final_revision=2
```

生产库只读复核时：

- `life_engine.runtime_context/global` 在 schema 合同后先读到 revision 995，21:51 的最新现场证据已到 1010；
- 生产 key 尚无 claim 行，符合“等待用户手工重启后首次声明”的计划；
- claim guard trigger 已安装；
- 随机合同留下 1 条已释放、无正文的 claim 审计行，0 live claim、0 transaction binding。

没有预先给生产 key 写一个已释放 claim：那会在运行代码尚未升级时同时冻结新旧 writer，造成未经用户手工重启的服务中断。首次升级实例将在加载状态前原子 claim；如果另一升级实例抢先 claim，预期实例会显示 owner/epoch 并失败关闭，这是正确安全行为。

## 6. 自动化验证

- singleton runtime、service 注入、连接/取消 rollback、baseline migration 定向合同：94 passed、1 skipped；
- Life Engine 全集：1127 passed、13 skipped；
- 真实 MySQL 专用自动化用例在未设置 `ELYSIUM_TEST_MYSQL_RUNTIME_ISOLATED=1` 时明确 skipped；没有把 skip 冒充实库通过；
- 另以正式库唯一随机 namespace 执行上述非生产、无正文的真实合同，全部 8 项通过；
- 新 claim 模块完整 Ruff 通过；变更文件 Ruff `F,E9`、compileall 与 `git diff --check` 通过。

首次全仓组合回归为 3876 passed、19 skipped、4 failed；四个失败均是旧 `test_life_state_integration.py` 使用未配置 storage backend 的 `MagicMock`，与严格的全局 storage 配置合同不兼容。测试桩改为显式 local `CoreConfig` 后，四项定向复验全部通过；最终全仓组合为 **3880 passed、19 skipped、3 warnings**。该结果不替代用户手工重启后的现场 claim/消息闭环。

## 7. 手工激活与验收门

代码和 schema 推送后，用户手工停止并启动唯一预期 Elysium 实例。不得由自动化脚本代为重启。启动后做只读核对：

1. claim owner instance 对应本次启动进程，lease epoch/token 状态有效；
2. `runtime_context/global` 从 revision 1010 或当时更高的最新 revision 加载，而不是从 961 重放；
3. runtime revision 只由 claim owner 推进，外部旧 writer 的无 claim 写被数据库拒绝；
4. claim 周期续租、服务关闭释放和过期 takeover 合同均符合预期；
5. 真实 QQ 入站、Chatter、状态持久化与回复发送闭环成功；
6. 不再出现未定位的 `chat_streams` 长事务锁等待。

完成这六项之前，结论应表述为“代码、schema 与隔离实库合同已完成；现役进程激活验收待用户手工重启”，不能表述为生产闭环已经完成。
