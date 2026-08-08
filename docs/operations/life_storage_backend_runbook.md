# 生命域存储快照、共享写入与权威切换运行手册

本手册服务于 Elysium 生命域可选本地/MySQL 存储。它不包含真实主机、用户名、密码或 fencing token。Life Event、Memory、Subject Document、Presence、World、Learning 的 Port/adapter、`LifeEngineService` 单 runtime 接线、未冻结候选复制、反向恢复、数据库不可变保护与真实隔离 MySQL 合同均已实现。MySQL runtime 支持多个合法进程加入同一个 verified generation 并发读写；这里的“单一权威”指单一 active backend/generation/epoch，不表示整个 generation 只能有一个进程。与此同时，`runtime_context/global` 等领域单例状态必须取得数据库时间 writer claim，不能由多个 shared writer 同时持有。正式冻结复制、五域同快照 generation 签署和用户手动启动后的跨领域验收仍是**验收门**，不是自动启停指令。

## 1. 当前安全状态

- `config/core.toml [storage].backend` 是 Core 与 Life Engine 的唯一后端选择，只接受 `local` 或 `mysql`；
- Life Engine 插件配置不再拥有 `enabled` 或 `authoritative_backend` 开关；
- 本机配置虽可选择 `mysql`，但正式 generation、authority 与 fencing 未通过时必须失败关闭，不得退回混合的 Core=MySQL、Life=local；
- 新的存储 runtime 不会自动创建、注册或切换 generation；MySQL registry 尚未激活时，首个业务进程可以激活已登记且 verified 的目标 generation，后续进程只能加入该 generation；
- `LifeEngineService` 是唯一 runtime owner；各子域只消费注入对象，禁止自行再次打开或关闭 runtime；
- 后端打开失败时 fail closed，不会从 MySQL 静默回退到 local，也不会反向回退；
- 全局 MySQL 模式不打开或双写旧 Life Event、Presence、World SQLite，也不回写 `.life_learning`；全局 local 模式保持原本地行为；
- 快照、迁移和校验器没有删除、移动、截断或覆盖源数据的权限；
- Elysium 只能由用户手动启动。脚本不会停止或重启 Elysium。

## 2. 只读盘点

```bash
uv run python scripts/audit_life_storage.py \
  --data-root /root/Elysia/Elysium/data
```

输出只包含数据库结构、行数、索引、完整性结果、扫描耗时和文件聚合大小，不输出正文或凭据。盘点本身不是备份，也不产生 generation。

## 3. 创建不可覆盖快照

### 3.1 在线候选快照

```bash
uv run python scripts/backup_life_data.py \
  --data-root /root/Elysia/Elysium/data \
  --output /absolute/backup/life-domain-<UTC>
```

该命令使用 SQLite Online Backup API，并对非数据库权威文件逐字节复制。SQLite 先在本地临时副本完成一致性复制和逻辑根计算，备份副本规范化为 `journal_mode=DELETE` 后再顺序写入目标，避免跨文件系统逐页写入与 WAL checkpoint 改变已封存主文件。输出目录必须不存在；任一源文件在复制中发生变化、任一哈希不一致或任一必需数据库缺失都会失败并保留 `SNAPSHOT_INCOMPLETE`。如果复制完成但独立复核失败，则写入 `VERIFICATION_FAILED.json`。在线快照即使校验通过，也只能是 candidate，不能成为可写权威。

### 3.2 冻结写入者后的可验证快照

只有用户已经手动停止 Elysium，并确认没有其他写入者持有这些本地文件时，操作者才可以添加：

```bash
--writer-frozen
```

该参数仅声明“人工冻结已经完成”。脚本会比较 SQLite 主文件、WAL、SHM 在备份前后的 stat 和 SHA-256，但不会代替人工识别所有写入者。任何变化都会让命令失败。

## 4. Manifest 与 generation

每个完整快照包含：

- `manifest.json` 及其规范化 SHA-256；
- 六个 SQLite 源的物理备份哈希、逐表 schema/行数/逻辑根和 frontier；
- 日记、生命工作区、媒体缓存与表情媒体的逐文件路径、大小与 SHA-256；
- 明确排除的可重建 Chroma 投影和被保留但不递归复制的旧备份目录；
- `writer_frozen` 事实。

只有“独立校验通过 + `writer_frozen=true`”的快照才能生成 `verified` generation。完全相同的 generation 重复注册是幂等成功；相同 ID 的不同 manifest 是显式冲突。

## 5. Authority、共享写入与 fencing

权威实现由全局后端选择自动派生，用户不再单独配置；同时必须区分“一个权威 generation”和“一个写入进程”：

- 单一权威意味着同一 registry 在一个 epoch 内只承认一个 active backend 和 generation，禁止 local/MySQL 双主或两个 generation 独立接收业务写入；它不限制同一 MySQL generation 内合法进程的数量；
- `local` 后端仍使用 `authority_provider = "file"` 和进程级单实例保护。SQLite、权威 Markdown/JSON 与本地投影不支持多个 Elysium 进程安全共享写入；
- `mysql` 后端使用 `authority_provider = "mysql"`。首个进程在 registry 尚未激活时激活 verified generation；后续进程通过 `join_generation()` 加入当前 generation，不会因已有合法 writer 而拒绝启动；
- 每个进程都必须配置非空且可审计的 `authority_owner_id`。该值标识调用者和诊断来源，不赋予独占权，也不能替代数据库账号、权限和业务 actor 授权；
- shared token 绑定 `registry_id + backend + generation + epoch`。每个耐久写事务在提交前，仍须于**同一个数据库事务**中锁定 registry 行并复核这四项；generation/epoch 改变后，旧进程立即被 fence；
- 领域并发冲突继续由 InnoDB 行锁、唯一键、稳定 occurrence/idempotency identity、revision CAS 和事务 outbox 处理。共享 generation 不允许最后写入覆盖、跳过 parent/revision 或吞掉重复身份冲突；
- shared writer 的周期维护是重新验证 generation/epoch，不延长或夺取一个进程独占租约；任一 shared writer 正常退出只释放自己的连接与本地 token，不撤销全局 generation，也不影响其他进程；
- shared generation 只解决“哪些进程可以写这个 generation”，不解决“谁拥有某个单例状态”。被领域声明为 singleton 的 `namespace/state_key` 必须额外取得数据库时间 writer claim；claim 包含可审计 owner instance、单调 lease epoch、不可伪造 token 与到期时间；
- claim 的 acquire/renew/release 与受保护写入都在事务内复核 active generation。第二 owner 在有效租约内失败关闭；租约过期 takeover 递增 epoch，旧 owner 立即被 fence。冲突后禁止自动 reload/rebase、无界重试或最后写入覆盖；
- MySQL 已登记 claim 的 runtime state 同时受应用层 exact-claim 校验和数据库 trigger 保护。连接池事务必须临时绑定当前连接与 claim token，提交前移除绑定；释放 claim 后保留登记身份，旧版无 claim writer 不能绕过保护；
- 需要同样 trigger 保护的领域 adapter 只消费 runtime 公共接口：在 `unit_of_work(writer_claim=claim)` 内调用 `bind_singleton_writer_write(session, claim)`，受保护语句结束后于 `finally` 调用 `clear_singleton_writer_write(session)`。领域层不得访问 `_singleton_writer_claims`、复制 token 摘要算法或另建平行租约表；
- generation 的激活、切换、封存与审计事件继续形成哈希链。审计损坏、generation 未验证、backend/generation/epoch 不匹配时 fail closed；
- schema migration 仍使用 MySQL advisory lock 串行执行。多个业务进程可并发启动，但不能并发执行互相冲突的 DDL；业务启动也不得把缺表或 checksum 漂移当作成功。

健康输出中的 `writer_mode = "shared"` 表示多进程共享 generation；`exclusive` 表示旧的 file authority 合同。健康信息不得暴露数据库密码或真实 fencing secret。

## 6. 配置形状

MySQL 的后端选择、连接、generation 与 authority 只配置在 Core 全局文件：

```toml
# config/core.toml
[storage]
backend = "local"  # 日常切换只改这一项；另一个合法值是 "mysql"
backend_generation = "<VERIFIED_MYSQL_GENERATION_ID>"  # local 自动忽略
schema_version = 1
registry_id = "life-domain"
authority_owner_id = "<UNIQUE_WRITER_ID>"
require_verified_generation = true
authority_lease_seconds = 120
authority_renew_interval_seconds = 40

[database]
mysql_host = "127.0.0.1"
mysql_port = 3306
mysql_database = "elysium"
mysql_user = "elysium"
mysql_password = "${ELYSIUM_MYSQL_PASSWORD}"
mysql_ssl_mode = "disabled"
connection_pool_size = 10
connection_timeout = 10
```

Life Engine 插件文件不再包含 `[storage]` 或 `[storage_mysql]`。它只保留 local 模式所需的 `[storage_local]` 路径；MySQL 模式会直接复用上述全局配置。

只有 MySQL generation、连接和 authority 控制面验收完成后，才允许把全局 `backend` 改为 `mysql`。日常 local/MySQL 切换只改这一字段：`local` 自动使用 file authority 并忽略保留在配置中的 MySQL generation，`mysql` 自动使用 MySQL authority 并严格校验该 generation。旧 `authority_provider` 字段由配置迁移自动移除，不再允许人工联动。每个 worktree/部署实例仍须提供稳定且唯一的 `authority_owner_id`；不要把真实用户名、机器绝对路径或密钥放进公共配置示例。连接密码仍只能通过配置中的环境变量提供。首个进程负责在尚未激活时创建 generation epoch；后续进程只加入同一 epoch。shared writer 不需要把 epoch、lease 或 fencing token 写入配置或环境变量，也不会通过周期维护夺取其他进程资格。异常和健康输出使用不含密码的 backend identity。

MySQL 模式不使用入口级 `data/runtime/elysium.lock`；不同 worktree 可以同时打开同一 generation，并并发处理互不冲突的领域数据。每个完整 Elysium 进程仍需拥有不冲突的 HTTP 端口、临时目录和不可共享外部适配器会话；若它们都要拥有同一个 `runtime_context/global`，只有取得 writer claim 的进程能启动到可写状态，其他进程必须显示 owner/epoch 诊断并失败关闭。认证失败、TLS/权限错误、schema 漂移、generation 不匹配、单例 claim 冲突、端口冲突和外部会话争用都不能用静默 local 回退或空实现伪装成功。

正常关闭和插件启动回滚都必须在 `finally` 路径释放当前进程已经取得的 singleton writer claims；运行上下文保存冲突、消费者关闭失败或领域 store 尚未完成挂载，均不得跳过 claim 撤销与 runtime 关闭。`LifeEngineService` 启动时，`runtime_context` 与 `learning` 两个 claim 共用一个最长为一个 lease 周期加单次短轮询余量的 monotonic deadline：上一实例异常退出但 lease 尚未自然到期时，新实例只轮询调用原子 acquire，由数据库时间判定到期并 takeover；若真实 owner 持续 renew，则到期保留 owner/epoch 诊断并 fail closed。两个 claim 不得各自重置 deadline；取消、非冲突错误和第二 claim 失败都必须立即传播并释放已取得的第一 claim。遇到 `SingletonWriterAlreadyClaimed` 时，不得删除 claim 行、强制改 epoch、解析 owner PID 后抢占或自动修改 token：可只读核对 owner、数据库时间的 `lease_until` 与只追加 claim events，但 lease 是否过期只由 acquire 事务内的数据库时间裁决。正常退出应产生 `released` 事件，异常退出则只能在租约自然过期后 takeover。

### 6.1 Memory 隔离合同验证

Memory 的本地合同测试不需要外部服务；真实 MySQL 合同测试必须显式指向专用测试库，并通过以下环境变量提供连接：

- `ELYSIUM_TEST_MYSQL_HOST`
- `ELYSIUM_TEST_MYSQL_PORT`
- `ELYSIUM_TEST_MYSQL_DATABASE`
- `ELYSIUM_TEST_MYSQL_USER`
- `ELYSIUM_TEST_MYSQL_PASSWORD`
- `ELYSIUM_TEST_MYSQL_SSL_MODE`
- `ELYSIUM_TEST_MYSQL_MEMORY_ISOLATED=1`

未提供必要变量时，真实 MySQL 用例必须明确显示为 skipped；不得借用正式数据库，也不得把 skipped 记作真实远程验收通过。`ELYSIUM_TEST_MYSQL_MEMORY_ISOLATED=1` 是允许安装破坏性 trigger 合同并保留随机测试历史的明确声明，绝不能指向正式库。测试会创建 Memory v1-v8 schema、安装独立 checksum 的不可变 trigger、注册隔离 generation 并取得短租约 authority；权威测试行不会用清理 `DELETE` 绕过不可变合同。

### 6.2 Presence/World 隔离合同验证

本地与 fake 合同验证覆盖数据库时间 lease、过期回收/takeover、revision/stream 并发、lifecycle outbox、World frontier/rebuild/cursor；service 级合同还会分别模拟 local/mysql 选择，证明单 runtime 注入、禁止旧 SQLite、重启恢复与失败关闭。真实 MySQL Presence/World 合同除通用 `ELYSIUM_TEST_MYSQL_*` 外，还必须显式设置：

```text
ELYSIUM_TEST_MYSQL_PRESENCE_WORLD_ISOLATED=1
```

该标志表示目标是允许整表 rebuild/cleanup 的专用隔离库，不得指向正式库。业务启动一律以 `initialize_schema=false` 打开 adapter；缺表必须失败，不能让 Elysium 启动流程代替迁移器建表。

### 6.3 Runtime State 技术 schema 升级

`runtime_states` 保存 Chatter 滚动上下文、心跳/游标、好奇状态、自治状态、Router 投影健康等可覆盖运行态；`runtime_events` 保存自治生命周期、Life Trace 与主体叙事等幂等只追加事件。MySQL 缺少任一表时业务启动必须 fail closed，禁止回读本地 JSON/JSONL/Markdown。

已经激活 generation 的现有部署新增该技术 schema 时，必须在 Elysium 停止状态下显式执行：

```bash
uv run python scripts/adopt_life_mysql_baseline.py \
  upgrade-runtime-state --config config/core.toml
```

该模式只应用 checksum 受控的幂等 migration，创建/核验 `runtime_states`、`runtime_events`、singleton writer claim 当前态/只追加事件/事务连接绑定表、两条 claim-event 不可变 trigger，以及三条 runtime-state claim guard trigger；不登记或激活 generation，不修改 authority epoch，不导入本地运行数据，也不启动/停止 Elysium。命令重复执行必须返回相同 schema/当前内容证据；若 migration checksum 或 trigger 漂移则失败。普通业务启动固定使用 `initialize_schema=false`，不能代替迁移器建表。

已升级部署的启动顺序必须是：打开唯一 service-owned runtime → 在读取 `runtime_context/global` 前取得 singleton writer claim → 从权威 MySQL 读取当前 revision → 构造 `StatePersistence` 并携带该 claim 写入。这样，重启期间由其他实例推进的最新 revision 会先被读取；本地陈旧内存不能先写后补 claim。

### 6.3.1 单例 writer 事故诊断与恢复

出现 revision 持续被未知实例推进或 `Lock wait timeout exceeded` 时：

1. 先记录受影响的 namespace/key、expected/actual revision、数据库更新时间、当前本机 PID 与连接数量；不得仅凭 `Sleep` 状态猜测 blocker；
2. 只有在数据库能精确给出 blocking transaction/connection、并确认它属于已废弃实例时，才允许回滚或终止该连接。权限不足、行锁已经自然释放或连接身份不明时不执行 `KILL`；
3. 新连接设置有界 `wait_timeout`，连接池归还时强制 rollback；任务取消也必须先 rollback 再把连接归还，降低 FRP 断链或取消路径留下长事务的风险；
4. 检查 singleton claim 当前 owner、lease epoch、到期状态和最近 claim event。claim 冲突是正确的失败关闭，不应通过删除 claim 行、覆盖 token 或自动 rebase 绕过；
5. 需要切换 owner 时，等待数据库时间确认旧 lease 过期后执行 takeover，或在已确认的旧 owner 正常关闭路径释放；新 epoch 生效后旧 token 永久失效；
6. Elysium 仍由用户手工停止/启动。代码和 schema 就绪后，用户只启动预期实例；随后验证 claim owner 与本机实例一致、`runtime_context` 从最新 revision 加载、revision 不再由未知 owner 漂移，再做真实消息闭环。

### 6.3.2 Learning singleton guard 升级

Learning selected persistence 使用固定 claim scope `life_engine.learning/selected_persistence`。业务启动只核验 schema，不创建 trigger；新部署或 v1 升级必须先在 Elysium 停止/不重启窗口运行：

```bash
uv run python scripts/adopt_life_mysql_baseline.py \
  upgrade-learning --config config/core.toml
```

该命令只幂等核验/安装 generic claim schema、`life_learning_schema_migrations` v1/v2、既有两条 Learning event 不可变 trigger 与四条 singleton guard（event INSERT，projection INSERT/UPDATE/DELETE），随后输出 Learning 表的 content-free 行数/root hash。它不取得 Learning claim、不写 `learning_events`/`learning_projections`、不修改 runtime state、generation/epoch 或 claim 业务行，也不启动/停止进程。配置中的 MySQL 密码仍须使用环境变量引用；命令不会接受或打印明文密码。

升级后必须只读验证 migration checksum、四条 trigger body 与 transaction binding 为空。缺失或漂移时业务启动 fail closed；不得通过关闭 trigger 校验、删除 claim 或回退旧 adapter 绕过。

### 6.4 单 runtime 启动、性能与关闭顺序

每个进程内部的固定顺序仍是：service 打开并拥有一个 runtime → 注入 Memory → 从同一 runtime 构造 Life Event/Presence/World/Subject/Learning → 启动上层消费者。Learning 只消费注入的 `LearningStorePort` 与同一 `SubjectDocumentStorePort`，不得另开 store。这里的“单 runtime”表示**每个进程只有一个 runtime owner**，不是整个 MySQL generation 只能启动一个进程。

启动热路径遵循以下约束：

- authority migration/schema 初始化在进程内幂等；authority 审计哈希链只对新观察到的 audit head 做完整验证，同一 head 不在每次 health/join 中重复全表重放；
- shared generation 的 generation 与 registry 状态使用一次关联查询取得，不能串行重复读取同一控制面；
- 启动只执行形成安全可用 runtime 所必需的 generation、schema、bounded outbox flush、World catch-up 和领域恢复；昂贵的全组件 health sweep 保留为显式按需诊断，不阻塞正常 readiness；
- 不得为了启动速度跳过 migration checksum、不可变 trigger、generation 验证、历史 frontier 或真实恢复错误。

关闭顺序相反：先停止上层任务 → flush/关闭 Learning、Memory 等消费者 → flush Presence outbox 并追平 World → 最后且仅一次关闭该进程拥有的 runtime。shared writer 关闭不撤销全局 generation。任一消费者关闭失败都不得阻止后续消费者和 runtime 释放；最终以聚合异常报告，不静默吞掉。启动中途失败同样执行这一逆序清理。

### 6.5 回复热路径性能合同

MySQL 的网络/TLS、连接池 checkout、协议编解码、服务端调度、InnoDB 锁和 COMMIT 确认决定其单次往返通常慢于 local SQLite/文件。优化目标是消除无意义的重复往返，而不是绕过权威校验：

- Router 投影由完整主体快照生成并验证；稳定状态下只读取 `SOUL.md`、`USER.md`、`MEMORY.md` 三个 head 的 `current_version_id/revision` 形成 content-free change marker；marker 不变时复用进程内不可变投影；
- marker 变化后必须读取一个一致的三文档完整快照并验证 exact-byte revision。读取失败或投影不匹配时不得继续把旧投影冒充当前版本；
- 有效 Router 投影已经证明其来源快照可用，Router 前不再重复拉取三份主体正文；降级或投影不可用时仍显式验证 `SOUL.md`，不能用空人格 fallback；
- 全局聊天历史使用固定批量查询读取 messages、streams、persons，查询次数不得随消息数量线性增长；滚动 payload chain 已合并历史后，后续 turn 不再重新读取最终不会注入的全局历史；
- 缓存只覆盖 content-free marker、不可变版本和可重建投影。每个耐久写事务仍在提交前执行 generation/epoch fence，主体权威、原始历史和权限判断不得使用过期缓存替代。

性能验收应至少记录 20 次冷启动和 100 个真实文本 turn，分别报告冷启动、首轮模型调用前、同一滚动链后续模型调用前的 p50/p95，并同步采集 MySQL `performance_schema` 或 slow log 的语句次数和等待。必须保持相同 Provider、上下文规模、TLS、网络位置和适配器配置；按“模型 / 数据库 / 平台发送”拆分耗时，不能把模型或平台延迟误归因给 MySQL。

本阶段只验证适配器合同，不会修改全局 `[storage].backend`、不会迁移正式数据、不会注册或激活正式 generation，也不会启动或停止 Elysium、NapCat 或其他运行进程。

### 6.6 候选复制控制面

候选复制不得借用正式 authority。`life_storage_copy_runs` 只协调复制/导出批次，使用数据库时间租约、单调 epoch 与 fencing token；它不能注册或激活 backend generation。

- 每个批次绑定不可变的 source manifest hash、source snapshot/root hash、目标后端与 `writer_frozen` 事实；
- 进度使用绝对单调计数，崩溃重试不会重复累计；
- 冲突证据只追加，发生冲突的批次不得标记 `verified`；
- 已过期租约可由协调器按数据库时间收束为 `failed`，不能强制抢占仍有效的租约；
- 即使逐条校验通过，只要 `writer_frozen=false`，状态最多为 `copied`；
- 候选复制不得修改全局 `[storage].backend`、active generation 或正在运行的 Elysium。

Life Event 旧账本迁移必须使用 exact snapshot import。禁止先反序列化为当前 `LifeEvent` 再序列化，因为新增可选字段也会改变原始 JSON 字节与证据哈希。MySQL 的原始事件 payload 使用带 `JSON_VALID` 检查的 binary-collated `LONGTEXT` 保存原文，同时保留 SHA-256。

正式 activation 默认要求数据库级不可变 trigger。若 MySQL 开启 `log_bin`，DBA 必须在维护窗口通过持久化服务端配置启用 `log_bin_trust_function_creators`，或采用等价且经过审计的管理员安装流程；业务账号仅保留目标库 `TRIGGER` 权限，不授予 `SUPER` 或 `SYSTEM_VARIABLES_ADMIN`。前置条件不满足时初始化必须 fail closed。仅非冻结、不可激活的影子复制可显式降级为应用层不可变，并必须在报告中记录。

MySQL 8.x 管理员可执行：

```sql
SET PERSIST log_bin_trust_function_creators = ON;
SELECT @@GLOBAL.log_bin, @@GLOBAL.log_bin_trust_function_creators;
```

第二条查询必须返回 `log_bin = 1`、`log_bin_trust_function_creators = 1`。若托管平台禁止 `SET PERSIST`，应在平台参数组或 `[mysqld]` 配置中持久化同名变量，并按平台要求重启或应用配置。不要临时授予 Elysium 业务账号 `SUPER` 或 `SYSTEM_VARIABLES_ADMIN` 来绕过该前置条件。

### 6.7 反向导出

反向导出只能写入一个此前不存在的新目录：

- 创建时先写 `EXPORT_INCOMPLETE`；
- 逐条导出 identity、位置、原始 payload 字节、payload hash 与 consumer cursor；
- SQLite `integrity_check`、逐条 hash 与聚合根全部通过后才写 manifest 并移除 incomplete 标记；
- 失败目录保留现场且不可复用；不得覆盖迁移前 SQLite 或任何既有备份；
- MySQL `DATETIME(6)` 导出为 UTC 规范时间，事件 payload 原文字节保持不变。

当前已验证的 Life Event 恢复副本位于 `C:\Temp\Data\ElysiumBackups\life-event-reverse-20260804T0735Z`。它是恢复演练资产，不是 active backend，也不授权自动切换。

### 6.8 Subject Document 精确复制与工作区边界

Subject Document 的候选复制使用：

```bash
uv run python scripts/migrate_life_subject_documents.py \
  --snapshot /absolute/life-domain-candidate \
  --run-id subject-shadow-<manifest-prefix> \
  --reverse-export /new/subject-reverse-export
```

连接信息只能通过 `ELYSIUM_LIFE_STORAGE_MYSQL_HOST/PORT/DATABASE/USER/PASSWORD`
环境变量提供。命令只选择明确声明的 SOUL、USER、MEMORY 与两个 diary
命名空间，逐文件保存 `LONGBLOB` 原始字节、hash、字节长度、换行/编码诊断与
“语义来源未知”状态。反向导出同样只允许写入此前不存在的新目录。

只读独立复核使用：

```bash
uv run python scripts/audit_life_subject_shadow.py \
  --snapshot /absolute/life-domain-candidate \
  --reverse-export /absolute/subject-reverse-export
```

当前验证通过的恢复副本位于
`C:\Temp\Data\ElysiumBackups\subject-reverse-20260804T0806Z`，共 1,404
份文档、10,316,470 字节，正向/远端/反向根均为
`d4c83a81d8df0895898ced696ba0ef63167281224faecff25ca9ce99f7cca966`。
它仍来自 `writer_frozen=false` 快照，不是 active backend。

工作区投影必须遵守 parent-hash 门：目标文件等于新版本时幂等确认；等于已知
parent 时才允许原子替换；任何其他字节都视为外部变化并保留原文件，随后由
observer 追加为新观察。禁止为了“让数据库和文件一致”而覆盖未知外部改动。

selected storage 启用后，`nucleus_write_file`、`nucleus_edit_file` 与 Memory
Witness 的声明路径必须先调用 service 的 Subject 写入口，再执行工作区投影。
通用 `nucleus_bash` 无法证明任意 shell 命令的写前入账，因此在该模式下 fail
closed；读取或修改应使用专用 file/领域工具。disabled/local 模式保持原行为。

### 6.9 Life Memory 无损候选复制

Life Memory 使用显式的 32 表选择合同，不复制 SQLite FTS 内部影子表，也不把
Chroma 当作权威。候选复制命令为：

```bash
uv run python scripts/migrate_life_memory.py \
  --snapshot /absolute/life-domain-candidate \
  --run-id life-memory-shadow-<manifest-prefix> \
  --reverse-export /new/life-memory-reverse-export
```

连接信息只允许通过 `ELYSIUM_LIFE_STORAGE_MYSQL_HOST/PORT/DATABASE/USER/PASSWORD`
环境变量提供。Memory schema v7 保留节点删除历史、事件日期、旧 FTS/embedding
可逆投影字段；v8 把开放元数据保存为规范 JSON 的 `LONGTEXT` 原文，避免 MySQL
原生 JSON 改写高精度小数。见证投影路径使用 SHA-256 作为可索引派生列，但查询
命中后必须核对完整路径，hash 永远不替代路径身份。

Memory 权威历史 trigger 位于独立的 `life_memory_immutability_schema_migrations` namespace。Experience、Witness 来源、artifact/interpretation/recall/corecall 与 epistemic/retrieval 账本禁止原地更新和删除；`memory_witnesses` 采用列级保护，只放行投影路径、投影状态与错误信息。`memory_artifact_heads`、Witness cursor、索引任务、关联投影和 legacy graph 不属于 append-only 表，必须保留其 CAS、重建与衰减更新能力。active 或冻结 generation 无法创建这些 trigger 时初始化失败；只有非冻结 candidate-copy shadow 可显式记录降级并跳过。

生产 MySQL 的 Memory 启动恢复还必须遵守以下工程合同：

- Memory 只能挂载 `LifeEngineService` 已打开并持有的 selected-storage runtime，不得从插件旧配置重新推导或另开权威后端；
- 工作区文档读取必须使用平台兼容的安全路径检查。Windows 不支持 POSIX 目录文件描述符打开方式，仍须逐级拒绝符号链接、边界逃逸和非普通文件，并核对读取前后文件身份；
- 可重建的 ghost 文件节点清理必须采用有界批次和集合式 SQL，每批独立提交并记录累计进度；禁止把大规模投影修复放入单个长事务，避免长期持有 `memory_nodes` 行锁；
- `memory_artifact_heads` 的 revision CAS 仍须严格执行。启动观察发生 head 冲突时，只允许刷新精确最新 head：若另一合法 writer 已提交等价正文或 tombstone，则幂等吸收；否则以最新版本为 parent 有界重试一次。禁止覆盖竞争版本、猜测 head 或无限重试；
- shared writer 启动时加入当前 verified generation，并在恢复及每笔提交中复核 generation/epoch。另一合法 writer 正在运行不是拒绝启动的条件；generation 被切换或封存后，旧进程的下一次写入必须被 fence；
- shared writer 的异常退出不要求等待独占租约过期，正常退出也不得撤销全局 generation。只有显式 generation 切换/封存才改变 epoch；该操作前仍须确认旧 generation 的写入已被事务 fence 隔离。

验收至少包含：实际配置打开 selected runtime 并通过 writer 校验、真实 Memory 初始化成功、ghost 缺口收敛、目标行锁可立即获取并回滚、artifact CAS 并发回归通过，以及初始化进程退出后无残留连接或后台 Python 进程。

只读独立复核使用：

```bash
uv run python scripts/audit_life_memory_shadow.py \
  --snapshot /absolute/life-domain-candidate \
  --reverse-export /absolute/life-memory-reverse-export
```

当前已验证恢复副本位于
`C:\Temp\Data\ElysiumBackups\life-memory-reverse-20260804T0920Z-v2`。
源 SQLite、MySQL 与反向 SQLite 均包含 32 张显式表、210,104 条记录，总根均为
`4703c2dc18470d16b9e4363f8b8c6a8b3d0f8cfda433baf1deddb9787951cf9c`；
76 个删除节点及其 1,936 条关联边完整保留。该快照仍是
`writer_frozen=false`，远端账号也不能创建数据库级不可变 trigger，因此只可作为
不可激活的 shadow，不得据此切换生产。

### 6.10 Presence / World 候选复制与反向恢复

Presence 与 World 从一致性快照复制，命令为：

```bash
uv run python scripts/migrate_life_presence_world.py \
  --snapshot /absolute/life-domain-candidate \
  --run-id life-presence-world-shadow-<manifest-prefix> \
  --reverse-export /new/presence-world-reverse-export
```

独立只读复核为：

```bash
uv run python scripts/audit_life_presence_world_shadow.py \
  --snapshot /absolute/life-domain-candidate \
  --reverse-export /absolute/presence-world-reverse-export
```

迁移器只选择 Presence snapshot/stream owner/lifecycle outbox 和 World
projector meta/assertion/change/perception cursor。World 的 projection policy、schema
version 与 rebuild state 由运行合同生成，不能冒充源记录；其余源字段逐行规范化
并计算稳定根。只有源 World 完全空白且唯一差异是初始化的 synthetic frontier=0
时，迁移器才允许把 frontier 修复为源值，并把该动作计入 copied record；任何已有
assertion/change/cursor 的目标都必须冲突失败，禁止覆盖。

当前验证通过的反向副本位于
`C:\Temp\Data\ElysiumBackups\life-presence-world-reverse-20260804T1030Z`。
源、MySQL 与反向副本共 2,031 条源记录，聚合根均为
`abd4f799f8208bc9edafdc3fb67a486486ba7746d599afd4fd5b0a341e5a2214`；
批次 `life-presence-world-shadow-v3-77435387f4acc59e` 为 `copied`。
它来自 `writer_frozen=false` 快照，只是恢复证据，不是 active generation。

### 6.11 Learning 候选复制与反向恢复

Learning 迁移只能从完整快照中的 `.life_learning` 读取，不能直接扫描仍在运行的正式工作区：

```bash
uv run python scripts/migrate_life_learning.py \
  --snapshot /absolute/life-domain-candidate \
  --run-id life-learning-shadow-<manifest-prefix> \
  --reverse-export /new/life-learning-reverse-export
```

连接信息只允许通过 `ELYSIUM_LIFE_STORAGE_MYSQL_HOST/PORT/DATABASE/USER/PASSWORD`
环境变量提供。迁移器会先逐文件核对 snapshot manifest，再把每个旧文件的精确原始
字节拆为不超过 1 MiB 的 immutable chunk events，追加完整 manifest/completion
证据并生成可重建投影。校验必须证明源文件集合、大小、SHA-256、逐 chunk hash、
重组字节与完成事件完全一致。反向导出只允许写入此前不存在的新目录，失败目录保留
incomplete 标记，不得覆盖源目录。

该脚本固定使用 copy authority 与 `CANDIDATE_COPY` runtime，不能激活 generation，
也不会修改全局 `[storage].backend`。业务启动固定 `initialize_schema=false`；缺少 Learning
schema 时必须拒绝启动，不能由 Elysium 运行进程临时建表。真实 MySQL 合同测试必须
使用专用隔离库并显式设置：

```text
ELYSIUM_TEST_MYSQL_LEARNING_ISOLATED=1
```

未设置时用例必须显示为 skipped；不得把 skipped 当作远程 MySQL 验收通过。旧在线候选
已经以批次 `life-learning-shadow-v2-77435387f4acc59e` 完成真实远程验证：452 条事件、
2 个 ready 投影、10 个精确源文件，导入与反向语义投影均通过。该批次仍是
`writer_frozen=false` 的 `copied` shadow，没有 active Learning generation。

### 6.12 Life Event 候选复制与反向恢复

```bash
uv run python scripts/migrate_life_events.py \
  --snapshot /absolute/life-domain-candidate \
  --run-id life-event-shadow-<manifest-prefix> \
  --reverse-export /new/life-event-reverse-export
```

迁移器只接受 manifest 唯一声明且物理 SHA-256 一致的 Life Event SQLite，逐条保留原始
payload 文本、occurrence、position 和 consumer cursor。在线 shadow 可显式跳过生产
trigger；冻结批次必须安装并核验 trigger。当前旧快照批次
`life-event-cli-v1-77435387f4acc59e` 已复制 86,094 条事件，源、MySQL 与反向 SQLite
root 一致，反向 `quick_check=ok`；它仍不可激活。

### 6.13 五域汇总切换审计

五个迁移批次完成后必须运行：

```bash
uv run python scripts/audit_life_storage_cutover.py \
  --snapshot /absolute/frozen-snapshot \
  --life-event-run <run-id> \
  --memory-run <run-id> \
  --subject-run <run-id> \
  --presence-world-run <run-id> \
  --learning-run <run-id> \
  --generation-id <verified-generation-id> \
  --output /new/generation-evidence-directory
```

`--generation-id` 与 `--output` 必须同时提供。审计器会先独立复核本地快照，再读取五个
copy run。只要快照未冻结、run 不同源、状态不是 `verified`、存在冲突、任一 verification
失败或 append-only 域不是 `trigger-enforced`，命令就拒绝签署且不创建输出目录。

## 7. 正式切换验收门

必须同时满足后才允许人工切换：

1. Life Event、Memory、Subject Document、Presence、World、Learning、cursor/outbox 的 local/MySQL 适配器全部通过同一合同测试；
2. 目标是新的空 generation 数据库；禁止清空或覆盖旧 shadow 表来规避可变投影冲突；
3. 目标数据库允许安装触发器，且启动时的 `information_schema` 漂移核验通过；
4. 已冻结快照通过逐记录、逐文件、谱系、frontier、visibility 与引用完整性校验；
5. MySQL 复制副本在隔离环境通过读写、并发、死锁重试、断连恢复和恢复演练；
6. 五域汇总审计返回 eligible 并写出 verified generation；
7. 旧 backend/generation 已人工停止或已由新 epoch 的事务 fence 隔离；同一新 generation 内允许多个已验证 writer 同时存在；
8. 新 runtime 的 backend、generation、schema、owner identity、epoch 与 registry 完全匹配，健康输出为 `writer_mode = "shared"`；
9. Chroma/FTS/World 等派生投影从新权威重建并报告明确 frontier；
10. 用户明确批准切换并手动启动 Elysium；
11. 真实聊天、记忆写入、检索、见证、Presence、World、Learning 与重启恢复全链路通过。

任一项缺失都不得启用全局 `backend = "mysql"`；若配置已选择 MySQL，启动必须失败关闭，不能回退成混合后端。

## 8. 失败与回滚

- 快照失败：保留源数据不动；失败目录不可复用，另选新目录重试；
- 复制失败：回滚目标事务，源快照保持只读；
- 激活前失败：不改变 authority；
- 激活后启动失败：撤销新 token、封存失败 generation，不能让旧 token 复活；
- 新后端已经产生业务写入后，禁止直接把旧 SQLite 重新设为权威。必须冻结新写入、生成新快照、验证差异，并执行经过合同测试的反向导出；
- Chroma/FTS/World 投影失败只允许进入 degraded 并重建，不能把“空结果”伪装成真实无数据。

## 9. 日常备份原则

- 本地快照与远程 MySQL 备份必须同时存在；远程库不是唯一副本；
- 每份备份必须有 manifest、完整性校验和隔离恢复演练，只有压缩包不算可恢复；
- 2026-08-05 在线候选约 2.1 GiB，独立复核 4,340 个项目且 0 失败；备份目标至少预留 10 GiB，正式迁移前按五倍增长重新测量；
- 任何自动清理/保留策略都不能覆盖原始生命数据和尚未封存的 generation。

## 10. AttentionThread / 旧 ThoughtStream 迁移

旧 `thoughts/streams.json` 不得直接转换成 AttentionThread 事件。正式候选批次必须执行：

```text
python scripts/migrate_life_attention_threads.py \
  --snapshot <冻结快照目录> \
  --archive <新的只增归档目录> \
  --reverse-export <新的反向导出目录> \
  --run-id <唯一批次 ID>
```

运行前提与验收门：

1. 快照 manifest 只能声明一份 `life_engine_workspace/thoughts/streams.json`，备份文件长度和 SHA-256 必须一致；
2. `--archive` 与 `--reverse-export` 使用新目录，禁止覆盖旧归档；已存在的 archive 只有在逐字节等价时才允许幂等复用；
3. 迁移目标必须是隔离 MySQL generation 数据库，writer 角色必须是 `CANDIDATE_COPY`，每个事务都重新校验 fencing；
4. 旧行只进入 `attention_legacy_snapshots/candidates`，不得写 `attention_thread_events/heads/focus`；
5. copy run 必须记录 `snapshot_only`、`no_fabricated_events`、archive manifest hash、数据库不可变状态和 canonical 空域 root；
6. frozen run 必须完成精确导入校验与反向字节导出；在线 shadow 只可作为演练证据，不可签署 generation；
7. 汇总审计增加 `--attention-run`。旧快照必须保持 `generation_eligible=false`，canonical authority 必须为零 event frontier、零 head、零 focus；
8. 该命令不会修改 `storage.enabled`、不会激活 generation，也不会启动、停止或重启 Elysium/NapCat。

任何一步失败时，保留源快照和带 incomplete marker 的失败归档，不清表、不覆盖、不把旧状态解释成主体决定；更换新的隔离目标或新目录后重试。
