"""Versioned schema for selected technical runtime state and events."""

from __future__ import annotations

from sqlalchemy import text

from src.kernel.storage.migration_runner import (
    MySQLMigrationRunner,
    MySQLTriggerContract,
    SchemaMigration,
    verify_mysql_trigger_contract,
)

from .contracts import StorageBackendRuntime
from .models import BackendKind

RUNTIME_STATE_SCHEMA_VERSION = 1

LOCAL_RUNTIME_STATE_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS runtime_states (
        namespace TEXT NOT NULL,
        state_key TEXT NOT NULL,
        revision INTEGER NOT NULL,
        schema_version INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (namespace, state_key)
    )""",
    """CREATE TABLE IF NOT EXISTS runtime_events (
        position INTEGER PRIMARY KEY AUTOINCREMENT,
        namespace TEXT NOT NULL,
        occurrence_id TEXT NOT NULL UNIQUE,
        event_kind TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_runtime_events_namespace_position
        ON runtime_events(namespace, position)""",
    """CREATE TRIGGER IF NOT EXISTS runtime_events_immutable_update_v1
        BEFORE UPDATE ON runtime_events BEGIN
            SELECT RAISE(ABORT, 'RuntimeEventImmutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS runtime_events_immutable_delete_v1
        BEFORE DELETE ON runtime_events BEGIN
            SELECT RAISE(ABORT, 'RuntimeEventImmutable');
        END""",
)

MYSQL_RUNTIME_STATE_MIGRATION = SchemaMigration(
    version=1,
    name="life_runtime_state_v1",
    statements=(
        """CREATE TABLE IF NOT EXISTS runtime_states (
            namespace VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            state_key VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            revision BIGINT UNSIGNED NOT NULL,
            schema_version INT UNSIGNED NOT NULL,
            payload_json LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            PRIMARY KEY (namespace, state_key),
            CONSTRAINT chk_runtime_state_payload_json CHECK (JSON_VALID(payload_json))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS runtime_events (
            position BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            namespace VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            event_kind VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            payload_json LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            occurred_at DATETIME(6) NOT NULL,
            recorded_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            UNIQUE KEY uq_runtime_events_occurrence (occurrence_id),
            KEY idx_runtime_events_namespace_position (namespace, position),
            CONSTRAINT chk_runtime_event_payload_json CHECK (JSON_VALID(payload_json))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TRIGGER IF NOT EXISTS runtime_events_immutable_update_v1
        BEFORE UPDATE ON runtime_events FOR EACH ROW
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'RuntimeEventImmutable'""",
        """CREATE TRIGGER IF NOT EXISTS runtime_events_immutable_delete_v1
        BEFORE DELETE ON runtime_events FOR EACH ROW
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'RuntimeEventImmutable'""",
    ),
)

MYSQL_RUNTIME_EVENT_TRIGGERS = (
    MySQLTriggerContract(
        "runtime_events_immutable_update_v1",
        "runtime_events",
        "UPDATE",
        "BEFORE",
        "RuntimeEventImmutable",
    ),
    MySQLTriggerContract(
        "runtime_events_immutable_delete_v1",
        "runtime_events",
        "DELETE",
        "BEFORE",
        "RuntimeEventImmutable",
    ),
)


async def ensure_runtime_state_schema(runtime: StorageBackendRuntime) -> None:
    """Create selected runtime-state tables under the active writer authority."""

    if not runtime.enabled or runtime.engine is None:
        raise RuntimeError("runtime state schema requires an enabled storage runtime")
    await runtime.validate_writer()
    if runtime.backend == BackendKind.MYSQL:
        runner = MySQLMigrationRunner(
            runtime.engine,
            table_name="life_runtime_state_schema_migrations",
            lock_name="elysium:life-runtime-state-schema",
        )
        await runner.apply((MYSQL_RUNTIME_STATE_MIGRATION,))
        await verify_mysql_trigger_contract(
            runtime.engine,
            MYSQL_RUNTIME_EVENT_TRIGGERS,
        )
    else:
        async with runtime.unit_of_work() as uow:
            for statement in LOCAL_RUNTIME_STATE_SCHEMA_STATEMENTS:
                await uow.session.execute(text(statement))
    await runtime.validate_writer()


__all__ = [
    "LOCAL_RUNTIME_STATE_SCHEMA_STATEMENTS",
    "MYSQL_RUNTIME_EVENT_TRIGGERS",
    "MYSQL_RUNTIME_STATE_MIGRATION",
    "RUNTIME_STATE_SCHEMA_VERSION",
    "ensure_runtime_state_schema",
]
