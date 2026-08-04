"""Versioned schema for append-only life-learning evidence and projections."""

from __future__ import annotations

from sqlalchemy import text

from src.kernel.storage.migration_runner import MySQLMigrationRunner, SchemaMigration

from .contracts import StorageBackendRuntime
from .models import BackendKind

LEARNING_SCHEMA_VERSION = 1

LOCAL_LEARNING_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS learning_events (
        position INTEGER PRIMARY KEY AUTOINCREMENT,
        occurrence_id TEXT NOT NULL UNIQUE,
        event_kind TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        source TEXT NOT NULL,
        actor_consciousness_instance_id TEXT NOT NULL DEFAULT '',
        subject_revision TEXT NOT NULL DEFAULT '',
        provenance_json TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        event_sha256 TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_learning_events_kind_position
        ON learning_events(event_kind, position)""",
    """CREATE TABLE IF NOT EXISTS learning_projections (
        projection_name TEXT PRIMARY KEY,
        revision INTEGER NOT NULL,
        source_frontier INTEGER NOT NULL,
        schema_version INTEGER NOT NULL,
        projector_version TEXT NOT NULL,
        rebuild_state TEXT NOT NULL
            CHECK (rebuild_state IN ('ready', 'rebuilding', 'failed')),
        payload_json TEXT NOT NULL,
        projection_sha256 TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TRIGGER IF NOT EXISTS learning_events_immutable_update_v1
        BEFORE UPDATE ON learning_events BEGIN
            SELECT RAISE(ABORT, 'LearningEventImmutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS learning_events_immutable_delete_v1
        BEFORE DELETE ON learning_events BEGIN
            SELECT RAISE(ABORT, 'LearningEventImmutable');
        END""",
)

_MYSQL_SCHEMA_MIGRATION = SchemaMigration(
    version=1,
    name="life_learning_storage_v1",
    statements=(
        """CREATE TABLE IF NOT EXISTS learning_events (
            position BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            event_kind VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            occurred_at DATETIME(6) NOT NULL,
            recorded_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            source VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            actor_consciousness_instance_id VARCHAR(255)
                CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT '',
            subject_revision VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin
                NOT NULL DEFAULT '',
            provenance_json LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            payload_json LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            event_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            UNIQUE KEY uq_learning_events_occurrence (occurrence_id),
            KEY idx_learning_events_kind_position (event_kind, position),
            CONSTRAINT chk_learning_events_provenance_json
                CHECK (JSON_VALID(provenance_json)),
            CONSTRAINT chk_learning_events_payload_json
                CHECK (JSON_VALID(payload_json))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS learning_projections (
            projection_name VARCHAR(128) CHARACTER SET utf8mb4
                COLLATE utf8mb4_bin NOT NULL PRIMARY KEY,
            revision BIGINT UNSIGNED NOT NULL,
            source_frontier BIGINT UNSIGNED NOT NULL,
            schema_version INT UNSIGNED NOT NULL,
            projector_version VARCHAR(128) CHARACTER SET utf8mb4
                COLLATE utf8mb4_bin NOT NULL,
            rebuild_state VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            payload_json LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            projection_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            CONSTRAINT chk_learning_projection_state
                CHECK (rebuild_state IN ('ready', 'rebuilding', 'failed')),
            CONSTRAINT chk_learning_projection_payload_json
                CHECK (JSON_VALID(payload_json))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TRIGGER IF NOT EXISTS learning_events_immutable_update_v1
        BEFORE UPDATE ON learning_events FOR EACH ROW
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'LearningEventImmutable'""",
        """CREATE TRIGGER IF NOT EXISTS learning_events_immutable_delete_v1
        BEFORE DELETE ON learning_events FOR EACH ROW
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'LearningEventImmutable'""",
    ),
)


async def ensure_learning_schema(runtime: StorageBackendRuntime) -> None:
    """Create learning tables only under a validated coherent writer."""

    if not runtime.enabled or runtime.engine is None:
        raise RuntimeError("learning schema requires an enabled storage runtime")
    await runtime.validate_writer()
    if runtime.backend == BackendKind.MYSQL:
        runner = MySQLMigrationRunner(
            runtime.engine,
            table_name="life_learning_schema_migrations",
            lock_name="elysium:life-learning-schema",
        )
        await runner.apply((_MYSQL_SCHEMA_MIGRATION,))
    else:
        async with runtime.unit_of_work() as uow:
            for statement in LOCAL_LEARNING_SCHEMA_STATEMENTS:
                await uow.session.execute(text(statement))
    await runtime.validate_writer()


__all__ = [
    "LEARNING_SCHEMA_VERSION",
    "LOCAL_LEARNING_SCHEMA_STATEMENTS",
    "ensure_learning_schema",
]
