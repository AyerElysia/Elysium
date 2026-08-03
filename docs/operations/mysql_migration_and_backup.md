# MySQL 迁移、备份与恢复手册

本手册不包含真实地址、用户名或密码。连接凭据只通过环境变量注入。

## 1. 工具入口

- `scripts/migrate_core_to_mysql.py`：SQLite 审计、不可覆盖快照、原子迁移、只读复核；
- `scripts/backup_mysql.py`：远端 MySQL 事务一致性逻辑快照与完整性校验；
- `scripts/backup_life_data.py`：Core SQLite、生命记忆、Life Event 和工作区文件备份。

设置连接：

```bash
export ELYSIUM_MYSQL_URL='mysql+asyncmy://<user>:<password>@<host>:<port>/<database>?charset=utf8mb4'
```

不要把真实 URL 写进仓库、命令示例或报告。

## 2. SQLite 迁移流程

### 2.1 只读审计

```bash
uv run python scripts/migrate_core_to_mysql.py audit \
  --source data/Elysium.db
```

审计检查：SQLite 完整性、缺表/多列、可空新列兼容、全部有界字符串真实长度和逐表内容指纹。

### 2.2 不可覆盖快照

```bash
uv run python scripts/migrate_core_to_mysql.py snapshot \
  --source data/Elysium.db \
  --output /absolute/backup/path/Elysium.db
```

输出旁会生成 `.manifest.json`。目标存在时命令拒绝覆盖。

### 2.3 原子迁移与复核

目标库首次迁移必须为空或只包含迁移器此前创建的空 Core schema：

```bash
uv run python scripts/migrate_core_to_mysql.py migrate \
  --source /absolute/backup/path/Elysium.db

uv run python scripts/migrate_core_to_mysql.py verify \
  --source /absolute/backup/path/Elysium.db
```

重复执行同一逻辑数据会返回 `already_applied: true`，不会重复插入；SQLite 因 WAL/checkpoint 导致物理文件 SHA 改变时也不会误判。目标含未知表、未审计数据或指纹不一致时会拒绝继续。

## 3. MySQL 本地快照

```bash
uv run python scripts/backup_mysql.py snapshot \
  --output-dir /absolute/backup/mysql

uv run python scripts/backup_mysql.py verify \
  --snapshot /absolute/backup/mysql/elysium-mysql-<UTC>.sql.gz
```

备份使用 `mysqldump --single-transaction --quick`，流式 gzip，完成后校验 CRC 和 SHA-256，再从 `.partial` 原子改名。每次生成唯一文件，不自动覆盖或删除历史备份。

### 恢复演练

恢复目标必须是新建的隔离库，名称建议以 `elysium_restore_` 开头：

```bash
gzip -dc /absolute/backup.sql.gz | mysql <isolated_restore_database>
```

恢复后使用 `migrate_core_to_mysql.py verify`，把原 SQLite 快照与恢复库逐表对比。仅验证压缩包能解压不算恢复成功。

## 4. 生命域本地备份

```bash
uv run python scripts/backup_life_data.py \
  --data-root /absolute/Elysium/data \
  --output /absolute/backup/life-domain-<timestamp>
```

脚本使用 SQLite Online Backup API 备份：

- `Elysium.db`；
- `.memory/memory.db`；
- `life_events.sqlite3`；
- `consciousness_presence.sqlite3`；
- `world_projection.sqlite3`。

同时复制日记、叙事、技能、思考等生命工作区文件并生成逐文件 SHA-256。Chroma 不作为权威副本，它应从结构化记忆重建。

## 5. 连续本地备份

当前远端账号可以做一致性逻辑快照，但不能执行 `SHOW BINARY LOG STATUS`，因此还不能启动秒级 binlog 归档。

在权限补齐前：

- 每 5 分钟运行一次 `backup_mysql.py snapshot`；
- 每次快照完成后运行 `verify`；
- 每天至少一次把最新快照恢复到隔离库并做逐表指纹复核；
- 定期运行 `backup_life_data.py`，生命域本地数据不能只依赖 MySQL 备份。

要启用真正连续的本地增量归档，数据库管理员需为专用备份账号提供 binlog 读取能力。获得权限后使用 `mysqlbinlog --read-from-remote-server --raw --stop-never` 归档，并保留定期全量快照作为恢复基线。

## 6. 回滚

迁移阶段失败时：

- MySQL 业务事务自动回滚；
- SQLite 源和快照不修改；
- 迁移审计保留 `failed` 状态；
- 不修改运行配置。

切换后若需要回滚，不能直接指向旧 SQLite 并忽略 MySQL 新写入。应先停止写入，备份 MySQL，核对差异，再选择恢复到本地 MySQL或执行经过审计的反向同步。
