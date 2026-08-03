# 离线同步运行手册

## 1. 当前上线状态

- 代码和远端阶段二表已验证；
- 配置默认 `enabled = false`；
- 当前运行中的 Elysium 未重启，也没有被自动拉起；
- 合并代码后，只有用户手动重启才会加载新同步 worker。

远端不可用不会阻止爱莉在本地聊天、形成事件或检索本地记忆。

## 2. 首次启用

在 `config/plugins/life_engine/config.toml` 增加：

```toml
[shared_sync]
enabled = true
remote_host = "数据库主机"
remote_port = 3306
remote_database = "elysium"
remote_user = "同步用户"
remote_password_env = "ELYSIUM_SYNC_MYSQL_PASSWORD"
mysql_ssl_mode = "disabled"
connect_timeout_seconds = 5
poll_interval_seconds = 1.0
batch_size = 100
lease_seconds = 60.0
base_backoff_seconds = 1.0
max_backoff_seconds = 300.0
push_enabled = true
pull_enabled = false
allowed_visibilities = ["shared"]
consumer_id = "life_engine.shared_sync"
```

在同一个将要手动启动 Elysium 的终端设置密码环境变量：

```bash
export ELYSIUM_SYNC_MYSQL_PASSWORD='由数据库管理员提供的密码'
```

初次上线建议先保持 `pull_enabled = false`，确认单节点推送、远端行数和健康状态后，再在第二个节点启用拉取。不要把密码写入 TOML、文档、日志或提交记录。

## 3. 手动启动后的检查

Life Engine 健康信息中的 `shared_sync` 应区分：

- `healthy`：远端最近一次操作成功；
- `degraded`：连接、投递、应用或冲突异常；
- `starting`：worker 已创建但尚未完成首次远端操作；
- `disabled`：配置关闭。

重点字段：

- `outbox_backlog`；
- `outbox.pending/retry/inflight/conflict/confirmed`；
- `last_attempt_at` 与 `last_success_at`；
- `remote_available`；
- `open_conflict_count`；
- `degraded_reason`。

健康输出故意不返回 payload、数据库密码或连接 URL。

## 4. 数据库只读核对

本地：

```sql
SELECT state, COUNT(*)
FROM sync_outbox
GROUP BY state;

SELECT last_attempt_at, last_success_at, remote_available, last_error
FROM sync_runtime_state
WHERE singleton_id = 1;

SELECT direction, conflict_key, event_id, detail, created_at
FROM sync_conflicts
WHERE state = 'open'
ORDER BY conflict_id;
```

远端：

```sql
SELECT schema_version
FROM elysium_sync_schema_meta
WHERE schema_key = 'offline_sync';

SELECT COUNT(*) AS event_count,
       COALESCE(MAX(remote_position), 0) AS latest_position
FROM elysium_shared_events;

SELECT COUNT(*) AS open_conflicts
FROM elysium_sync_conflicts
WHERE state = 'open';
```

不要用 UPDATE/DELETE “清理”同步账本。冲突必须先确认两份内容的来源、身份和哈希，再通过专门修复流程追加纠正事件；不能跳游标或覆盖历史。

## 5. 断网与恢复演练

1. 在隔离测试环境启动同步，写入一条显式 `shared + sync_export` 事件；
2. 暂时阻断测试数据库连接；
3. 确认本地权威事件已存在，Outbox 为 `retry` 或 `pending`，游标未前移；
4. 恢复连接；
5. 确认远端只出现一条事件、本地 Outbox 变为 `confirmed`；
6. 再投递同一信封，确认返回 duplicate 且远端总数不增加。

禁止拿正在运行的正式 Elysium 做 kill/restart 型故障注入。

## 6. 降级与回退

如同步持续异常：

1. 保留全部本地 SQLite、WAL、Outbox、Inbox 和远端账本；
2. 将 `shared_sync.enabled` 改为 `false`；
3. 等待用户选择维护窗口并手动重启；
4. 爱莉继续使用本地权威 Life Event 与记忆系统；
5. 修复后重新启用，Outbox 会从未确认序号继续。

关闭功能不会删除任何同步记录。不要删除 `life_events.sqlite3`，也不要清空远端阶段二表。

## 7. 备份

本地 `life_events.sqlite3` 应使用 SQLite 一致性快照或在线备份 API，不能只复制主文件而忽略 WAL。远端阶段二表纳入既有 MySQL 逻辑备份；恢复演练必须同时核对：

- `event_id` 唯一性；
- `(origin_node_id, origin_sequence)` 唯一性；
- `payload_hash`；
- `remote_position` 与服务端 Outbox 一一对应；
- 开放冲突数量。

完整架构见 [离线同步内核](../architecture/offline_sync_kernel.md)。
