# 数据库切换运维指南

> 本文档说明如何在本地数据库和远程数据库之间切换 Elysium 的 MySQL 后端。

## 1. 配置文件修改

编辑 `config/core.toml`，修改以下字段：

### 本地数据库（开发测试）

```toml
mysql_host = "localhost"
mysql_port = 3306
mysql_database = "elysium"
mysql_user = "root"
mysql_password = "root"
```

### 远程数据库（生产/协作）

```toml
mysql_host = "frp-one.com"
mysql_port = 65429
mysql_database = "elysium"
mysql_user = "elysia"
mysql_password = "${ELYSIUM_MYSQL_PASSWORD}"  # 或实际密码
```

## 2. 切换后必须清理的状态

切换数据库后，新数据库可能残留其他实例的状态，导致 Elysium 启动失败或功能异常。按以下顺序清理：

### 2.1 释放过期的 writer claims

```sql
UPDATE runtime_singleton_writer_claims
SET released_at = NOW(6)
WHERE released_at IS NULL AND lease_until < NOW(6);
```

### 2.2 重置 authority lease

```sql
UPDATE storage_authority_registry
SET lease_until = '2020-01-01 00:00:00'
WHERE registry_id = 'life-domain';
```

### 2.3 清理 workspace projection

```sql
-- 删除 heads 和 events（触发器可能阻止删除，需要先删触发器）
DROP TRIGGER IF EXISTS memory_workspace_projection_events_immutable_update;
DROP TRIGGER IF EXISTS memory_workspace_projection_events_immutable_delete;

DELETE FROM memory_workspace_projection_events;
DELETE FROM memory_workspace_projection_heads;
```

然后用当前数据库用户重建触发器：

```sql
CREATE TRIGGER memory_workspace_projection_events_immutable_update
BEFORE UPDATE ON memory_workspace_projection_events FOR EACH ROW
BEGIN
    IF NOT (
        OLD.`event_sha256` <=> NEW.`event_sha256`
        AND OLD.`event_kind` <=> NEW.`event_kind`
        AND OLD.`storage_generation_id` <=> NEW.`storage_generation_id`
        AND OLD.`projection_generation_id` <=> NEW.`projection_generation_id`
        AND OLD.`previous_projection_generation_id` <=> NEW.`previous_projection_generation_id`
        AND OLD.`owner_id` <=> NEW.`owner_id`
        AND OLD.`previous_owner_id` <=> NEW.`previous_owner_id`
        AND OLD.`workspace_root_sha256` <=> NEW.`workspace_root_sha256`
        AND OLD.`previous_workspace_root_sha256` <=> NEW.`previous_workspace_root_sha256`
        AND OLD.`source_root_sha256` <=> NEW.`source_root_sha256`
        AND OLD.`eligible_inventory_sha256` <=> NEW.`eligible_inventory_sha256`
        AND OLD.`revision` <=> NEW.`revision`
        AND OLD.`expected_revision` <=> NEW.`expected_revision`
        AND OLD.`actor_id` <=> NEW.`actor_id`
        AND OLD.`audit_occurrence_id` <=> NEW.`audit_occurrence_id`
        AND OLD.`reason_code` <=> NEW.`reason_code`
        AND OLD.`occurred_at` <=> NEW.`occurred_at`
        AND OLD.`previous_event_sha256` <=> NEW.`previous_event_sha256`
        AND OLD.`payload_sha256` <=> NEW.`payload_sha256`
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'MemoryAuthorityRecordImmutable';
    END IF;
END;

CREATE TRIGGER memory_workspace_projection_events_immutable_delete
BEFORE DELETE ON memory_workspace_projection_events FOR EACH ROW
SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'MemoryAuthorityRecordImmutable';
```

### 2.4 清理 stale stream_turns claims

```sql
UPDATE stream_turns
SET status = 'failed',
    result_digest = NULL,
    updated_at = NOW(6)
WHERE status = 'claimed' AND lease_until < NOW(6);
```

### 2.5 检查 schema 版本

确保目标数据库的 schema 版本与当前代码匹配：

```sql
SELECT * FROM life_learning_schema_migrations ORDER BY version DESC;
SELECT * FROM life_memory_schema_migrations ORDER BY version DESC;
SELECT * FROM life_event_schema_migrations ORDER BY version DESC;
```

如果版本不匹配，Elysium 启动时会自动运行迁移。

## 3. 一键清理脚本（Python）

保存为 `_clean_db_for_switch.py`，切换后运行：

```python
"""数据库切换后清理脚本。"""
import asyncio
import asyncmy


async def clean_db(host, port, user, password, db):
    print(f"Connecting to {host}:{port}/{db} as {user}...")
    conn = await asyncmy.connect(
        host=host, port=port, user=user, password=password, db=db,
    )
    cur = conn.cursor()

    # 1. 释放过期 writer claims
    await cur.execute("""
        UPDATE runtime_singleton_writer_claims
        SET released_at = NOW(6)
        WHERE released_at IS NULL AND lease_until < NOW(6)
    """)
    print(f"  Released {cur.rowcount} stale writer claims")

    # 2. 重置 authority lease
    await cur.execute("""
        UPDATE storage_authority_registry
        SET lease_until = '2020-01-01 00:00:00'
        WHERE registry_id = 'life-domain'
    """)
    print(f"  Reset authority lease: {cur.rowcount} rows")

    # 3. 清理 workspace projection
    await cur.execute("DROP TRIGGER IF EXISTS memory_workspace_projection_events_immutable_update")
    await cur.execute("DROP TRIGGER IF EXISTS memory_workspace_projection_events_immutable_delete")
    await cur.execute("DELETE FROM memory_workspace_projection_events")
    events_deleted = cur.rowcount
    await cur.execute("DELETE FROM memory_workspace_projection_heads")
    heads_deleted = cur.rowcount
    print(f"  Cleaned workspace projection: {events_deleted} events, {heads_deleted} heads")

    # 4. 重建触发器（用当前用户）
    await cur.execute("""
        CREATE TRIGGER memory_workspace_projection_events_immutable_update
        BEFORE UPDATE ON memory_workspace_projection_events FOR EACH ROW
        BEGIN
            IF NOT (
                OLD.`event_sha256` <=> NEW.`event_sha256`
                AND OLD.`event_kind` <=> NEW.`event_kind`
                AND OLD.`storage_generation_id` <=> NEW.`storage_generation_id`
                AND OLD.`projection_generation_id` <=> NEW.`projection_generation_id`
                AND OLD.`previous_projection_generation_id` <=> NEW.`previous_projection_generation_id`
                AND OLD.`owner_id` <=> NEW.`owner_id`
                AND OLD.`previous_owner_id` <=> NEW.`previous_owner_id`
                AND OLD.`workspace_root_sha256` <=> NEW.`workspace_root_sha256`
                AND OLD.`previous_workspace_root_sha256` <=> NEW.`previous_workspace_root_sha256`
                AND OLD.`source_root_sha256` <=> NEW.`source_root_sha256`
                AND OLD.`eligible_inventory_sha256` <=> NEW.`eligible_inventory_sha256`
                AND OLD.`revision` <=> NEW.`revision`
                AND OLD.`expected_revision` <=> NEW.`expected_revision`
                AND OLD.`actor_id` <=> NEW.`actor_id`
                AND OLD.`audit_occurrence_id` <=> NEW.`audit_occurrence_id`
                AND OLD.`reason_code` <=> NEW.`reason_code`
                AND OLD.`occurred_at` <=> NEW.`occurred_at`
                AND OLD.`previous_event_sha256` <=> NEW.`previous_event_sha256`
                AND OLD.`payload_sha256` <=> NEW.`payload_sha256`
            ) THEN
                SIGNAL SQLSTATE '45000'
                    SET MESSAGE_TEXT = 'MemoryAuthorityRecordImmutable';
            END IF;
        END
    """)
    await cur.execute("""
        CREATE TRIGGER memory_workspace_projection_events_immutable_delete
        BEFORE DELETE ON memory_workspace_projection_events FOR EACH ROW
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'MemoryAuthorityRecordImmutable'
    """)
    print("  Recreated workspace projection triggers")

    # 5. 清理 stale stream_turns claims
    await cur.execute("""
        UPDATE stream_turns
        SET status = 'failed',
            result_digest = NULL,
            updated_at = NOW(6)
        WHERE status = 'claimed' AND lease_until < NOW(6)
    """)
    print(f"  Cleaned {cur.rowcount} stale stream_turns claims")

    await conn.commit()
    cur.close()
    conn.close()
    print("Done!")


async def main():
    # 本地数据库
    await clean_db("localhost", 3306, "root", "root", "elysium")

    # 远程数据库（取消注释使用）
    # await clean_db("frp-one.com", 65429, "elysia", "YOUR_PASSWORD", "elysium")


asyncio.run(main())
```

运行方式：

```bash
.\.venv\Scripts\python.exe _clean_db_for_switch.py
```

## 4. 验证清单

切换并清理后，重启 Elysium，检查以下日志：

- [ ] `life_engine 生命记忆服务已初始化`
- [ ] `multi-writer hot-path bridge attached`
- [ ] `life_engine 已启动`
- [ ] 没有 `MemoryDatabaseImmutabilityError`
- [ ] 没有 `WorkspaceProjectionRebuildRequired`
- [ ] 没有 `inbound message fact record failed`
- [ ] 发送测试消息，爱莉能正常回复

## 5. 常见问题

### Q: 启动时报 `MemoryDatabaseImmutabilityError: triggers are missing or drifted`

A: 触发器被删除或 definer 不匹配。运行 2.3 节的触发器重建 SQL。

### Q: 启动时报 `WorkspaceProjectionRebuildRequired`

A: workspace projection 被其他实例占用。运行 2.3 节清理 workspace projection。

### Q: 收到消息但不回复，日志有 `inbound message fact record failed: OperationalError (1292)`

A: 这是代码 bug，已修复（2026-08-19）。确保代码包含 `message_stream_adapters.py` 的 datetime 格式修复。

### Q: 学习调度器报错 `OperationalError claim=(namespace=life_engine.learning ... lease_epoch=-)`

A: writer claim 没有成功获取。检查 `runtime_singleton_writer_claims` 表是否有过期未释放的 claim，运行 2.1 节清理。

## 6. 注意事项

- 切换数据库后，爱莉的记忆、学习进度、对话历史都是目标数据库的状态
- 本地和远程数据库的数据是独立的，不会自动同步
- 如果两个实例同时运行在同一个数据库，会触发多写者协调机制
- `multi_writer_enabled = true` 配置保持不变，多写者协议会自动处理协调
