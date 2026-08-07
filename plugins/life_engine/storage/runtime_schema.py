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
from .writer_claims import ensure_singleton_writer_claim_schema

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

_MYSQL_RUNTIME_STATE_CLAIM_GUARD_INSERT = """CREATE TRIGGER IF NOT EXISTS
    runtime_states_singleton_claim_insert_v2
    BEFORE INSERT ON runtime_states FOR EACH ROW
    BEGIN
        IF EXISTS (
            SELECT 1 FROM runtime_singleton_writer_claims c
            WHERE c.namespace = NEW.namespace AND c.state_key = NEW.state_key
        ) AND NOT EXISTS (
            SELECT 1
            FROM runtime_singleton_writer_claims c
            INNER JOIN runtime_singleton_writer_bindings b
                ON b.generation_id = c.generation_id
                AND b.namespace = c.namespace
                AND b.state_key = c.state_key
                AND b.owner_instance_id = c.owner_instance_id
                AND b.lease_epoch = c.lease_epoch
                AND b.fencing_token_sha256 = c.fencing_token_sha256
            WHERE b.connection_id = CONNECTION_ID()
                AND c.namespace = NEW.namespace
                AND c.state_key = NEW.state_key
                AND c.released_at IS NULL
                AND c.lease_until > CURRENT_TIMESTAMP(6)
        ) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'RuntimeStateSingletonWriterClaimRequired';
        END IF;
    END"""

_MYSQL_RUNTIME_STATE_CLAIM_GUARD_UPDATE = """CREATE TRIGGER IF NOT EXISTS
    runtime_states_singleton_claim_update_v2
    BEFORE UPDATE ON runtime_states FOR EACH ROW
    BEGIN
        IF EXISTS (
            SELECT 1 FROM runtime_singleton_writer_claims c
            WHERE c.namespace = OLD.namespace AND c.state_key = OLD.state_key
        ) AND NOT EXISTS (
            SELECT 1
            FROM runtime_singleton_writer_claims c
            INNER JOIN runtime_singleton_writer_bindings b
                ON b.generation_id = c.generation_id
                AND b.namespace = c.namespace
                AND b.state_key = c.state_key
                AND b.owner_instance_id = c.owner_instance_id
                AND b.lease_epoch = c.lease_epoch
                AND b.fencing_token_sha256 = c.fencing_token_sha256
            WHERE b.connection_id = CONNECTION_ID()
                AND c.namespace = OLD.namespace
                AND c.state_key = OLD.state_key
                AND c.released_at IS NULL
                AND c.lease_until > CURRENT_TIMESTAMP(6)
        ) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'RuntimeStateSingletonWriterClaimRequired';
        END IF;
    END"""

_MYSQL_RUNTIME_STATE_CLAIM_GUARD_DELETE = """CREATE TRIGGER IF NOT EXISTS
    runtime_states_singleton_claim_delete_v2
    BEFORE DELETE ON runtime_states FOR EACH ROW
    BEGIN
        IF EXISTS (
            SELECT 1 FROM runtime_singleton_writer_claims c
            WHERE c.namespace = OLD.namespace AND c.state_key = OLD.state_key
        ) AND NOT EXISTS (
            SELECT 1
            FROM runtime_singleton_writer_claims c
            INNER JOIN runtime_singleton_writer_bindings b
                ON b.generation_id = c.generation_id
                AND b.namespace = c.namespace
                AND b.state_key = c.state_key
                AND b.owner_instance_id = c.owner_instance_id
                AND b.lease_epoch = c.lease_epoch
                AND b.fencing_token_sha256 = c.fencing_token_sha256
            WHERE b.connection_id = CONNECTION_ID()
                AND c.namespace = OLD.namespace
                AND c.state_key = OLD.state_key
                AND c.released_at IS NULL
                AND c.lease_until > CURRENT_TIMESTAMP(6)
        ) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'RuntimeStateSingletonWriterClaimRequired';
        END IF;
    END"""

MYSQL_RUNTIME_STATE_CLAIM_GUARD_MIGRATION = SchemaMigration(
    version=2,
    name="life_runtime_state_singleton_claim_guard_v2",
    statements=(
        _MYSQL_RUNTIME_STATE_CLAIM_GUARD_INSERT,
        _MYSQL_RUNTIME_STATE_CLAIM_GUARD_UPDATE,
        _MYSQL_RUNTIME_STATE_CLAIM_GUARD_DELETE,
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

MYSQL_RUNTIME_STATE_CLAIM_GUARD_TRIGGERS = (
    MySQLTriggerContract(
        "runtime_states_singleton_claim_insert_v2",
        "runtime_states",
        "INSERT",
        "BEFORE",
        "RuntimeStateSingletonWriterClaimRequired",
    ),
    MySQLTriggerContract(
        "runtime_states_singleton_claim_update_v2",
        "runtime_states",
        "UPDATE",
        "BEFORE",
        "RuntimeStateSingletonWriterClaimRequired",
    ),
    MySQLTriggerContract(
        "runtime_states_singleton_claim_delete_v2",
        "runtime_states",
        "DELETE",
        "BEFORE",
        "RuntimeStateSingletonWriterClaimRequired",
    ),
)


async def ensure_runtime_state_schema(runtime: StorageBackendRuntime) -> None:
    """Create selected runtime-state tables under the active writer authority."""

    if not runtime.enabled or runtime.engine is None:
        raise RuntimeError("runtime state schema requires an enabled storage runtime")
    await ensure_singleton_writer_claim_schema(runtime)
    await runtime.validate_writer()
    if runtime.backend == BackendKind.MYSQL:
        runner = MySQLMigrationRunner(
            runtime.engine,
            table_name="life_runtime_state_schema_migrations",
            lock_name="elysium:life-runtime-state-schema",
        )
        await runner.apply(
            (
                MYSQL_RUNTIME_STATE_MIGRATION,
                MYSQL_RUNTIME_STATE_CLAIM_GUARD_MIGRATION,
            )
        )
        await verify_mysql_trigger_contract(
            runtime.engine,
            MYSQL_RUNTIME_EVENT_TRIGGERS
            + MYSQL_RUNTIME_STATE_CLAIM_GUARD_TRIGGERS,
        )
    else:
        async with runtime.unit_of_work() as uow:
            for statement in LOCAL_RUNTIME_STATE_SCHEMA_STATEMENTS:
                await uow.session.execute(text(statement))
    await runtime.validate_writer()


__all__ = [
    "LOCAL_RUNTIME_STATE_SCHEMA_STATEMENTS",
    "MYSQL_RUNTIME_EVENT_TRIGGERS",
    "MYSQL_RUNTIME_STATE_CLAIM_GUARD_MIGRATION",
    "MYSQL_RUNTIME_STATE_CLAIM_GUARD_TRIGGERS",
    "MYSQL_RUNTIME_STATE_MIGRATION",
    "RUNTIME_STATE_SCHEMA_VERSION",
    "ensure_runtime_state_schema",
]
