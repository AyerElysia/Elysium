# 统一记忆同步与恢复手册

## 1. 不可跳过的安全前提

- Elysium 只能由用户手动启动；NapCat/QQNT 可以由独立生命周期 owner 自动启动和恢复，但本工具不负责重启或拉起任何进程。
- 首次迁移只从 `backup_life_data.py` 生成的在线一致性快照读取。
- 同步前逐个校验 manifest 中的文件大小和 SHA-256；任何漂移都拒绝迁移。
- 密码只来自环境变量，不写进 TOML、文档、日志或 Git。
- 恢复目标必须是全新隔离目录，禁止覆盖正在使用的 `data/`。
- 归档 `archive_role` 只是工程存储分类，不是 claim authority、主体执笔权或事实状态。MySQL v1 的物理列名仍为 `authority`，不得据此决定记忆真假。
- workspace 中未明确声明所有权的文件仍逐字节备份，但标为 `unclassified_workspace_exact_bytes`；不得因为路径、扩展名或内容相似就自动提升为主体文件。

## 2. 创建一致性备份

```bash
uv run python scripts/backup_life_data.py \
  --data-root /root/Elysia/Elysium/data \
  --core-sqlite-relative MoFox.db \
  --output /root/Elysia/Elysium-backups/unified-memory-YYYYMMDD-HHMMSS
```

脚本使用 SQLite online backup API，不会只复制可能依赖 WAL 的主文件；workspace 会递归备份主体文件。远端 MySQL 在迁移前还应使用现有 `backup_mysql.py` 或受控 `mysqldump` 创建逻辑备份并保存校验清单。
旧安装若在已验证的 `config/core.toml` 中仍显式使用 `data/Elysium.db`，必须把上述参数同步改为 `Elysium.db`；工具不会根据文件是否存在自动猜测权威库。

## 3. 首次全量同步

```bash
export ELYSIUM_MEMORY_ARCHIVE_MYSQL_URL='mysql+asyncmy://USER:PASSWORD@HOST:PORT/DATABASE?ssl_mode=disabled'

uv run python scripts/sync_unified_memory.py sync \
  --backup-root /absolute/path/to/verified-backup \
  --state /root/Elysia/Elysium/data/life_engine_workspace/.memory/archive_sync_state.sqlite3 \
  --full-snapshot \
  --publish-batch-size 4000 \
  --publish-concurrency 4 \
  --scan-batch-size 16000 \
  --max-batch-mib 16
```

以上参数已在 `@@max_allowed_packet=64 MiB` 的目标服务器完成 379,469 条正式全量验证；其他服务器必须重新读取该变量并保留安全余量，不得机械照搬。进度只含计数，不含 payload。最终必须满足 `status=complete`、`conflicts=0`、`verification.verified=true`，且远端复算 root hash 相同。

中断后直接重放；已提交记录会返回 `duplicate`。不得清表“重来”。失败清单保留为审计证据。

## 4. 只读核验

```bash
uv run python scripts/sync_unified_memory.py health \
  --state /root/Elysia/Elysium/data/life_engine_workspace/.memory/archive_sync_state.sqlite3

uv run python scripts/sync_unified_memory.py verify-run --manifest-id MANIFEST_ID
```

```sql
SELECT status, COUNT(*) FROM elysium_memory_archive_runs GROUP BY status;
SELECT source_domain, COUNT(*)
FROM elysium_memory_archive_records
WHERE source_node_id = '目标节点 ID'
GROUP BY source_domain;
SELECT authority AS legacy_archive_role, COUNT(*)
FROM elysium_memory_archive_records
WHERE source_node_id = '目标节点 ID'
GROUP BY authority;
SELECT COUNT(*) FROM elysium_memory_archive_conflicts WHERE state = 'open';
```

禁止直接 UPDATE/DELETE `elysium_memory_archive_records`。如果外部工具改过内容，运行清单校验必须视为事故，不得通过更新 hash “修绿”。

`unclassified_storage_record` 表示出现了尚未登记归档契约的新 SQLite 表。数据已经保守保存，但必须先审查该组件的真实 schema 所有权，再通过代码和测试显式登记；禁止现场 UPDATE 旧记录或按表名猜测认知意义。

## 5. 隔离恢复演练

```bash
uv run python scripts/sync_unified_memory.py restore \
  --state /root/Elysia/Elysium/data/life_engine_workspace/.memory/archive_sync_state.sqlite3 \
  --output /root/Elysia/Elysium-restore-drill-YYYYMMDD-HHMMSS
```

每个 SQLite 域必须返回 `integrity_check=ok`、`record_identity_check=ok`，workspace 必须返回 `byte_hash_check=ok`。不要把演练目录自动替换为正式数据目录。

## 6. 启用持续增量归档

```toml
[memory_archive_sync]
enabled = true
remote_host = "数据库主机"
remote_port = 3306
remote_database = "elysium"
remote_user = "归档用户"
remote_password_env = "ELYSIUM_MEMORY_ARCHIVE_MYSQL_PASSWORD"
mysql_ssl_mode = "disabled"
connect_timeout_seconds = 5
interval_seconds = 300.0
retry_max_seconds = 900.0
publish_batch_size = 250
publish_concurrency = 2
scan_batch_size = 500
max_batch_mib = 4
local_state_path = ".memory/archive_sync_state.sqlite3"
```

在用户手动启动 Elysium 的同一终端设置 `ELYSIUM_MEMORY_ARCHIVE_MYSQL_PASSWORD`。当前实例不会热加载；只有用户批准维护窗口并手动重启后才生效。远端中断时健康状态 degraded，但聊天、本地记忆写入与检索继续。

## 7. 数据库 trigger 加固

当前远端账号若因 binary logging 策略无权创建 trigger，健康信息显示 `application_hash_audit`。DBA 可在维护窗口创建：

```sql
CREATE TRIGGER elysium_memory_archive_records_no_update
BEFORE UPDATE ON elysium_memory_archive_records
FOR EACH ROW SIGNAL SQLSTATE '45000'
SET MESSAGE_TEXT = 'unified memory archive records are append-only';

CREATE TRIGGER elysium_memory_archive_records_no_delete
BEFORE DELETE ON elysium_memory_archive_records
FOR EACH ROW SIGNAL SQLSTATE '45000'
SET MESSAGE_TEXT = 'unified memory archive records are append-only';
```

不要临时授予应用账号全库管理权限。trigger 就绪后下一次初始化会自动识别 `database_trigger`。

## 8. 故障处置

| 现象 | 处置 |
|---|---|
| 远端连接失败 | 保留本地数据和确认状态；等待退避或稍后重放 |
| 1062 并发唯一键 | 整事务重读并分类 duplicate/conflict；不要清表 |
| 1205/1213 | 有界重试；持续出现时降低并发并检查服务器锁 |
| manifest/hash 不符 | 停止并从权威源重建在线一致性备份 |
| `conflicts>0` | 停止成功切换，保留冲突证据并人工确认 |
| restore 校验失败 | 保留隔离目录，禁止覆盖正式数据，调查首个差异 |
| 远端长期不可用 | 本地权威继续运行；关闭配置需等待手动维护窗口 |

完整原理见 [统一记忆归档架构](../architecture/Elysium统一记忆归档架构.md)。
