"""Versioned schema for subject-authoritative attention threads."""

from __future__ import annotations

from sqlalchemy import text

from src.kernel.storage.migration_runner import (
    MySQLMigrationRunner,
    MySQLTriggerContract,
    SchemaMigration,
    verify_mysql_trigger_contract,
)

from .contracts import StorageBackendRuntime, StorageWriterRole
from .models import BackendKind

ATTENTION_THREAD_SCHEMA_VERSION = 1

LOCAL_ATTENTION_THREAD_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS attention_thread_events (
        position INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        occurrence_id TEXT NOT NULL UNIQUE,
        thread_id TEXT NOT NULL,
        action TEXT NOT NULL
            CHECK (action IN ('open', 'note', 'pause', 'resume', 'close')),
        actor_consciousness_instance_id TEXT NOT NULL,
        source_instance_id TEXT NOT NULL,
        source_occurrence_ids_json TEXT NOT NULL,
        causation_occurrence_id TEXT NOT NULL,
        expected_revision INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        public_statement TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        event_sha256 TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_attention_events_thread_position
        ON attention_thread_events(thread_id, position)""",
    """CREATE TABLE IF NOT EXISTS attention_thread_heads (
        thread_id TEXT PRIMARY KEY,
        status TEXT NOT NULL CHECK (status IN ('open', 'paused', 'closed')),
        revision INTEGER NOT NULL,
        opened_at TEXT NOT NULL,
        last_changed_at TEXT NOT NULL,
        current_statement TEXT NOT NULL,
        statement_event_id TEXT NOT NULL,
        statement_sha256 TEXT NOT NULL,
        statement_bytes INTEGER NOT NULL,
        last_event_id TEXT NOT NULL,
        last_occurrence_id TEXT NOT NULL,
        last_event_position INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_attention_heads_status_position
        ON attention_thread_heads(status, last_event_position DESC, thread_id)""",
    """CREATE TABLE IF NOT EXISTS attention_instance_focus (
        instance_id TEXT PRIMARY KEY,
        focus_occurrence_id TEXT NOT NULL,
        source_occurrence_id TEXT NOT NULL,
        entered_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        revision INTEGER NOT NULL,
        thread_id TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL
    )""",
    """CREATE TRIGGER IF NOT EXISTS attention_events_immutable_update_v1
        BEFORE UPDATE ON attention_thread_events BEGIN
            SELECT RAISE(ABORT, 'AttentionThreadEventImmutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS attention_events_immutable_delete_v1
        BEFORE DELETE ON attention_thread_events BEGIN
            SELECT RAISE(ABORT, 'AttentionThreadEventImmutable');
        END""",
)

_MYSQL_MIGRATION = SchemaMigration(
    version=ATTENTION_THREAD_SCHEMA_VERSION,
    name="life_attention_thread_storage_v1",
    statements=(
        """CREATE TABLE IF NOT EXISTS attention_thread_events (
            position BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            event_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            thread_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            action VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            actor_consciousness_instance_id VARCHAR(255)
                CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            source_instance_id VARCHAR(255)
                CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            source_occurrence_ids_json JSON NOT NULL,
            causation_occurrence_id VARCHAR(255)
                CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            expected_revision BIGINT UNSIGNED NOT NULL,
            revision BIGINT UNSIGNED NOT NULL,
            public_statement MEDIUMTEXT CHARACTER SET utf8mb4
                COLLATE utf8mb4_bin NOT NULL,
            occurred_at DATETIME(6) NOT NULL,
            recorded_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            event_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            UNIQUE KEY uq_attention_event_id (event_id),
            UNIQUE KEY uq_attention_occurrence_id (occurrence_id),
            KEY idx_attention_events_thread_position (thread_id, position),
            CONSTRAINT chk_attention_action
                CHECK (action IN ('open', 'note', 'pause', 'resume', 'close')),
            CONSTRAINT chk_attention_source_occurrences
                CHECK (JSON_VALID(source_occurrence_ids_json))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS attention_thread_heads (
            thread_id VARCHAR(255) CHARACTER SET utf8mb4
                COLLATE utf8mb4_bin NOT NULL PRIMARY KEY,
            status VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            revision BIGINT UNSIGNED NOT NULL,
            opened_at DATETIME(6) NOT NULL,
            last_changed_at DATETIME(6) NOT NULL,
            current_statement MEDIUMTEXT CHARACTER SET utf8mb4
                COLLATE utf8mb4_bin NOT NULL,
            statement_event_id VARCHAR(255) CHARACTER SET utf8mb4
                COLLATE utf8mb4_bin NOT NULL,
            statement_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            statement_bytes BIGINT UNSIGNED NOT NULL,
            last_event_id VARCHAR(255) CHARACTER SET utf8mb4
                COLLATE utf8mb4_bin NOT NULL,
            last_occurrence_id VARCHAR(255) CHARACTER SET utf8mb4
                COLLATE utf8mb4_bin NOT NULL,
            last_event_position BIGINT UNSIGNED NOT NULL,
            updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            KEY idx_attention_heads_status_position
                (status, last_event_position DESC, thread_id),
            CONSTRAINT chk_attention_status
                CHECK (status IN ('open', 'paused', 'closed'))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS attention_instance_focus (
            instance_id VARCHAR(255) CHARACTER SET utf8mb4
                COLLATE utf8mb4_bin NOT NULL PRIMARY KEY,
            focus_occurrence_id VARCHAR(255) CHARACTER SET utf8mb4
                COLLATE utf8mb4_bin NOT NULL,
            source_occurrence_id VARCHAR(255) CHARACTER SET utf8mb4
                COLLATE utf8mb4_bin NOT NULL,
            entered_at DATETIME(6) NOT NULL,
            expires_at DATETIME(6) NOT NULL,
            revision BIGINT UNSIGNED NOT NULL,
            thread_id VARCHAR(255) CHARACTER SET utf8mb4
                COLLATE utf8mb4_bin NOT NULL,
            updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TRIGGER IF NOT EXISTS attention_events_immutable_update_v1
        BEFORE UPDATE ON attention_thread_events FOR EACH ROW
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'AttentionThreadEventImmutable'""",
        """CREATE TRIGGER IF NOT EXISTS attention_events_immutable_delete_v1
        BEFORE DELETE ON attention_thread_events FOR EACH ROW
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'AttentionThreadEventImmutable'""",
    ),
)

_MYSQL_SHADOW_MIGRATION = SchemaMigration(
    version=ATTENTION_THREAD_SCHEMA_VERSION,
    name="life_attention_thread_storage_shadow_v1",
    statements=_MYSQL_MIGRATION.statements[:3],
)

_MYSQL_IMMUTABILITY_TRIGGERS = (
    MySQLTriggerContract(
        "attention_events_immutable_update_v1",
        "attention_thread_events",
        "UPDATE",
        "BEFORE",
        "AttentionThreadEventImmutable",
    ),
    MySQLTriggerContract(
        "attention_events_immutable_delete_v1",
        "attention_thread_events",
        "DELETE",
        "BEFORE",
        "AttentionThreadEventImmutable",
    ),
)


async def ensure_attention_thread_schema(
    runtime: StorageBackendRuntime,
    *,
    require_database_immutability: bool = True,
) -> None:
    """Create attention tables without weakening an activatable generation."""

    if not runtime.enabled or runtime.engine is None:
        raise RuntimeError("attention schema requires an enabled storage runtime")
    if (
        not require_database_immutability
        and runtime.writer_role != StorageWriterRole.CANDIDATE_COPY
    ):
        raise RuntimeError(
            "attention database immutability may be relaxed only for candidate copy"
        )
    await runtime.validate_writer()
    if runtime.backend == BackendKind.MYSQL:
        migration = (
            _MYSQL_MIGRATION
            if require_database_immutability
            else _MYSQL_SHADOW_MIGRATION
        )
        suffix = "" if require_database_immutability else "_shadow"
        runner = MySQLMigrationRunner(
            runtime.engine,
            table_name=f"life_attention{suffix}_schema_migrations",
            lock_name=f"elysium:life-attention{suffix.replace('_', '-')}-schema",
        )
        await runner.apply((migration,))
        if require_database_immutability:
            await verify_mysql_trigger_contract(
                runtime.engine,
                _MYSQL_IMMUTABILITY_TRIGGERS,
            )
    else:
        async with runtime.unit_of_work() as uow:
            for statement in LOCAL_ATTENTION_THREAD_SCHEMA_STATEMENTS:
                await uow.session.execute(text(statement))
    await runtime.validate_writer()


__all__ = [
    "ATTENTION_THREAD_SCHEMA_VERSION",
    "LOCAL_ATTENTION_THREAD_SCHEMA_STATEMENTS",
    "ensure_attention_thread_schema",
]
