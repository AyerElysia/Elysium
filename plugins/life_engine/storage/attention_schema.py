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

ATTENTION_THREAD_SCHEMA_VERSION = 2

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
    """CREATE TABLE IF NOT EXISTS attention_legacy_snapshots (
        snapshot_sha256 TEXT PRIMARY KEY,
        legacy_schema_version INTEGER NOT NULL,
        legacy_global_revision INTEGER,
        byte_length INTEGER NOT NULL,
        raw_bytes BLOB NOT NULL,
        row_count INTEGER NOT NULL,
        status_counts_json TEXT NOT NULL,
        rows_root_sha256 TEXT NOT NULL,
        import_mode TEXT NOT NULL CHECK (import_mode = 'snapshot_only'),
        generation_eligible INTEGER NOT NULL
            CHECK (generation_eligible = 0),
        source_label TEXT NOT NULL,
        imported_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS attention_legacy_candidates (
        snapshot_sha256 TEXT NOT NULL,
        source_ordinal INTEGER NOT NULL,
        legacy_stream_id TEXT NOT NULL,
        legacy_status TEXT NOT NULL,
        row_sha256 TEXT NOT NULL,
        original_fields_json TEXT NOT NULL,
        candidate_state TEXT NOT NULL
            CHECK (candidate_state = 'snapshot_only'),
        PRIMARY KEY (snapshot_sha256, source_ordinal),
        UNIQUE (snapshot_sha256, legacy_stream_id),
        FOREIGN KEY (snapshot_sha256)
            REFERENCES attention_legacy_snapshots(snapshot_sha256)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    """CREATE TRIGGER IF NOT EXISTS attention_events_immutable_update_v1
        BEFORE UPDATE ON attention_thread_events BEGIN
            SELECT RAISE(ABORT, 'AttentionThreadEventImmutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS attention_events_immutable_delete_v1
        BEFORE DELETE ON attention_thread_events BEGIN
            SELECT RAISE(ABORT, 'AttentionThreadEventImmutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS attention_legacy_snapshots_immutable_update_v1
        BEFORE UPDATE ON attention_legacy_snapshots BEGIN
            SELECT RAISE(ABORT, 'AttentionLegacySnapshotImmutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS attention_legacy_snapshots_immutable_delete_v1
        BEFORE DELETE ON attention_legacy_snapshots BEGIN
            SELECT RAISE(ABORT, 'AttentionLegacySnapshotImmutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS attention_legacy_candidates_immutable_update_v1
        BEFORE UPDATE ON attention_legacy_candidates BEGIN
            SELECT RAISE(ABORT, 'AttentionLegacyCandidateImmutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS attention_legacy_candidates_immutable_delete_v1
        BEFORE DELETE ON attention_legacy_candidates BEGIN
            SELECT RAISE(ABORT, 'AttentionLegacyCandidateImmutable');
        END""",
)

_MYSQL_MIGRATION_V1 = SchemaMigration(
    version=1,
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

_MYSQL_SHADOW_MIGRATION_V1 = SchemaMigration(
    version=1,
    name="life_attention_thread_storage_shadow_v1",
    statements=_MYSQL_MIGRATION_V1.statements[:3],
)

_MYSQL_LEGACY_MIGRATION_V2 = SchemaMigration(
    version=2,
    name="life_attention_legacy_snapshot_storage_v2",
    statements=(
        """CREATE TABLE IF NOT EXISTS attention_legacy_snapshots (
            snapshot_sha256 CHAR(64) CHARACTER SET ascii
                COLLATE ascii_bin NOT NULL PRIMARY KEY,
            legacy_schema_version BIGINT UNSIGNED NOT NULL,
            legacy_global_revision BIGINT UNSIGNED NULL,
            byte_length BIGINT UNSIGNED NOT NULL,
            raw_bytes LONGBLOB NOT NULL,
            row_count BIGINT UNSIGNED NOT NULL,
            status_counts_json JSON NOT NULL,
            rows_root_sha256 CHAR(64) CHARACTER SET ascii
                COLLATE ascii_bin NOT NULL,
            import_mode VARCHAR(32) CHARACTER SET ascii
                COLLATE ascii_bin NOT NULL,
            generation_eligible BOOLEAN NOT NULL DEFAULT FALSE,
            source_label VARCHAR(512) CHARACTER SET utf8mb4
                COLLATE utf8mb4_bin NOT NULL,
            imported_at DATETIME(6) NOT NULL,
            CONSTRAINT chk_attention_legacy_import_mode
                CHECK (import_mode = 'snapshot_only'),
            CONSTRAINT chk_attention_legacy_generation
                CHECK (generation_eligible = FALSE),
            CONSTRAINT chk_attention_legacy_status_counts
                CHECK (JSON_VALID(status_counts_json))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS attention_legacy_candidates (
            snapshot_sha256 CHAR(64) CHARACTER SET ascii
                COLLATE ascii_bin NOT NULL,
            source_ordinal BIGINT UNSIGNED NOT NULL,
            legacy_stream_id VARCHAR(255) CHARACTER SET utf8mb4
                COLLATE utf8mb4_bin NOT NULL,
            legacy_status VARCHAR(16) CHARACTER SET ascii
                COLLATE ascii_bin NOT NULL,
            row_sha256 CHAR(64) CHARACTER SET ascii
                COLLATE ascii_bin NOT NULL,
            original_fields_json JSON NOT NULL,
            candidate_state VARCHAR(32) CHARACTER SET ascii
                COLLATE ascii_bin NOT NULL,
            PRIMARY KEY (snapshot_sha256, source_ordinal),
            UNIQUE KEY uq_attention_legacy_stream
                (snapshot_sha256, legacy_stream_id),
            CONSTRAINT fk_attention_legacy_candidate_snapshot
                FOREIGN KEY (snapshot_sha256)
                REFERENCES attention_legacy_snapshots(snapshot_sha256)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            CONSTRAINT chk_attention_legacy_candidate_state
                CHECK (candidate_state = 'snapshot_only'),
            CONSTRAINT chk_attention_legacy_original_fields
                CHECK (JSON_VALID(original_fields_json))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TRIGGER IF NOT EXISTS attention_legacy_snapshots_immutable_update_v1
        BEFORE UPDATE ON attention_legacy_snapshots FOR EACH ROW
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'AttentionLegacySnapshotImmutable'""",
        """CREATE TRIGGER IF NOT EXISTS attention_legacy_snapshots_immutable_delete_v1
        BEFORE DELETE ON attention_legacy_snapshots FOR EACH ROW
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'AttentionLegacySnapshotImmutable'""",
        """CREATE TRIGGER IF NOT EXISTS attention_legacy_candidates_immutable_update_v1
        BEFORE UPDATE ON attention_legacy_candidates FOR EACH ROW
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'AttentionLegacyCandidateImmutable'""",
        """CREATE TRIGGER IF NOT EXISTS attention_legacy_candidates_immutable_delete_v1
        BEFORE DELETE ON attention_legacy_candidates FOR EACH ROW
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'AttentionLegacyCandidateImmutable'""",
    ),
)

_MYSQL_LEGACY_SHADOW_MIGRATION_V2 = SchemaMigration(
    version=2,
    name="life_attention_legacy_snapshot_storage_shadow_v2",
    statements=_MYSQL_LEGACY_MIGRATION_V2.statements,
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
    MySQLTriggerContract(
        "attention_legacy_snapshots_immutable_update_v1",
        "attention_legacy_snapshots",
        "UPDATE",
        "BEFORE",
        "AttentionLegacySnapshotImmutable",
    ),
    MySQLTriggerContract(
        "attention_legacy_snapshots_immutable_delete_v1",
        "attention_legacy_snapshots",
        "DELETE",
        "BEFORE",
        "AttentionLegacySnapshotImmutable",
    ),
    MySQLTriggerContract(
        "attention_legacy_candidates_immutable_update_v1",
        "attention_legacy_candidates",
        "UPDATE",
        "BEFORE",
        "AttentionLegacyCandidateImmutable",
    ),
    MySQLTriggerContract(
        "attention_legacy_candidates_immutable_delete_v1",
        "attention_legacy_candidates",
        "DELETE",
        "BEFORE",
        "AttentionLegacyCandidateImmutable",
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
        migrations = (
            (_MYSQL_MIGRATION_V1, _MYSQL_LEGACY_MIGRATION_V2)
            if require_database_immutability
            else (
                _MYSQL_SHADOW_MIGRATION_V1,
                _MYSQL_LEGACY_SHADOW_MIGRATION_V2,
            )
        )
        suffix = "" if require_database_immutability else "_shadow"
        runner = MySQLMigrationRunner(
            runtime.engine,
            table_name=f"life_attention{suffix}_schema_migrations",
            lock_name=f"elysium:life-attention{suffix.replace('_', '-')}-schema",
        )
        await runner.apply(migrations)
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
