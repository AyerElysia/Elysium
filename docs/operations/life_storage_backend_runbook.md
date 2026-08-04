# 生命域存储快照与权威切换运行手册

本手册服务于 Elysium 生命域可选本地/MySQL 存储。它不包含真实主机、用户名、密码或 fencing token。Memory 的六 Port、local/MySQL 适配与 MySQL schema 已实现，但完整生命域适配、正式数据复制校验、恢复演练和人工切换尚未完成，因此本手册中的正式切换步骤是**验收门**，不是当前可以执行的上线指令。

## 1. 当前安全状态

- `storage.enabled = false` 是默认值，也是当前生产要求；
- 正式权威仍为既有本地 SQLite、Markdown、JSON/JSONL 与媒体文件；
- 新的存储 runtime 不会自动创建、注册、激活或切换 generation；
- 后端打开失败时 fail closed，不会从 MySQL 静默回退到 local，也不会反向回退；
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

## 5. Authority 与 fencing

权威选择与数据后端选择彼此独立：

- 单主机部署默认使用 `authority_provider = "file"`；即使数据后端是 MySQL，也由本机哈希链 registry 协调一次人工切换，并在完整本地事务期间持有共享 fence；
- 多主机同时写 MySQL 才使用 `authority_provider = "mysql"`。每个写事务在提交前必须于同一数据库事务中锁定并复核 generation、epoch、owner、lease 和 fencing token；
- `local + mysql authority` 被明确拒绝，因为它不能给本地文件提供跨主机事务 fencing；
- token 只从环境变量注入，持久化状态和健康输出只保留 token hash，不暴露秘密；
- register、activate、renew、revoke 形成哈希链审计。审计损坏时 fail closed；
- authority 被撤销、过期或未激活时，整体 runtime 只能报告 degraded/failed，并拒绝新写。

## 6. 配置形状

以下只是无秘密示例；当前不要把 `enabled` 改为 `true`：

```toml
[storage]
enabled = false
authoritative_backend = "local"
backend_generation = ""
schema_version = 1
registry_id = "life-domain"
authority_provider = "file"
authority_epoch = 0
authority_owner_id = ""
fencing_token_env = "ELYSIUM_LIFE_STORAGE_FENCING_TOKEN"
require_verified_generation = true

[storage_local]
database_path = "data/life_storage/local.sqlite3"
authority_state_path = "data/life_storage/authority.json"
busy_timeout_seconds = 10

[storage_mysql]
host = "127.0.0.1"
port = 3306
database = "elysium"
user = "elysium"
password_env = "ELYSIUM_LIFE_STORAGE_MYSQL_PASSWORD"
ssl_mode = "disabled"
pool_size = 20
max_overflow = 20
```

连接密码与 fencing token 只能通过配置中指定的环境变量提供。异常和健康输出使用不含密码的 backend identity。

### 6.1 Memory 隔离合同验证

Memory 的本地合同测试不需要外部服务；真实 MySQL 合同测试必须显式指向专用测试库，并通过以下环境变量提供连接：

- `ELYSIUM_TEST_MYSQL_HOST`
- `ELYSIUM_TEST_MYSQL_PORT`
- `ELYSIUM_TEST_MYSQL_DATABASE`
- `ELYSIUM_TEST_MYSQL_USER`
- `ELYSIUM_TEST_MYSQL_PASSWORD`
- `ELYSIUM_TEST_MYSQL_SSL_MODE`

未提供必要变量时，真实 MySQL 用例必须明确显示为 skipped；不得借用正式数据库，也不得把 skipped 记作真实远程验收通过。测试会创建 Memory v1-v6 schema、注册隔离 generation、取得短租约 authority 并在结束时清理本次稳定身份；schema 初始化尚未完成时不得尝试清理尚不存在的领域表。

本阶段只验证适配器合同，不会修改 `storage.enabled`、不会迁移正式数据、不会注册或激活正式 generation，也不会启动或停止 Elysium、NapCat 或其他运行进程。

### 6.2 候选复制控制面

候选复制不得借用正式 authority。`life_storage_copy_runs` 只协调复制/导出批次，使用数据库时间租约、单调 epoch 与 fencing token；它不能注册或激活 backend generation。

- 每个批次绑定不可变的 source manifest hash、source snapshot/root hash、目标后端与 `writer_frozen` 事实；
- 进度使用绝对单调计数，崩溃重试不会重复累计；
- 冲突证据只追加，发生冲突的批次不得标记 `verified`；
- 已过期租约可由协调器按数据库时间收束为 `failed`，不能强制抢占仍有效的租约；
- 即使逐条校验通过，只要 `writer_frozen=false`，状态最多为 `copied`；
- 候选复制不得修改 `storage.enabled`、active generation 或正在运行的 Elysium。

Life Event 旧账本迁移必须使用 exact snapshot import。禁止先反序列化为当前 `LifeEvent` 再序列化，因为新增可选字段也会改变原始 JSON 字节与证据哈希。MySQL 的原始事件 payload 使用带 `JSON_VALID` 检查的 binary-collated `LONGTEXT` 保存原文，同时保留 SHA-256。

正式 activation 默认要求数据库级不可变 trigger。若 MySQL 账号因 binary logging / `SUPER` 权限限制无法创建 trigger，初始化必须 fail closed。仅非冻结、不可激活的影子复制可显式降级为应用层不可变，并必须在报告中记录。

### 6.3 反向导出

反向导出只能写入一个此前不存在的新目录：

- 创建时先写 `EXPORT_INCOMPLETE`；
- 逐条导出 identity、位置、原始 payload 字节、payload hash 与 consumer cursor；
- SQLite `integrity_check`、逐条 hash 与聚合根全部通过后才写 manifest 并移除 incomplete 标记；
- 失败目录保留现场且不可复用；不得覆盖迁移前 SQLite 或任何既有备份；
- MySQL `DATETIME(6)` 导出为 UTC 规范时间，事件 payload 原文字节保持不变。

当前已验证的 Life Event 恢复副本位于 `C:\Temp\Data\ElysiumBackups\life-event-reverse-20260804T0735Z`。它是恢复演练资产，不是 active backend，也不授权自动切换。

## 7. 正式切换验收门

必须同时满足后才允许人工切换：

1. Life Event、Memory、Subject Document、Presence、World、cursor/outbox 的 local/MySQL 适配器全部通过同一合同测试；
2. 已冻结快照通过逐记录、逐文件、谱系、frontier、visibility 与引用完整性校验；
3. MySQL 复制副本在隔离环境通过读写、并发、死锁重试、断连恢复和恢复演练；
4. 旧 writer 已人工停止，旧 authority token 已撤销，只有一个新 epoch 被激活；
5. 新 runtime 的 backend、generation、schema、owner、lease、epoch 与 registry 完全匹配；
6. Chroma/FTS/World 等派生投影从新权威重建并报告明确 frontier；
7. 用户明确批准切换并手动启动 Elysium；
8. 真实聊天、记忆写入、检索、见证、Presence、World 与重启恢复全链路通过。

任一项缺失都必须保持 `storage.enabled=false`。

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
- 当前精确生命数据约 2.6 GB（其中媒体约 1.49 GB，六个 SQLite 约 0.61 GB，工作区聚合存在重叠），备份目标至少预留 10 GB，正式迁移前按五倍增长重新测量；
- 任何自动清理/保留策略都不能覆盖原始生命数据和尚未封存的 generation。
