"""Versioned schema for append-only life-learning evidence and projections."""

from __future__ import annotations

from sqlalchemy import text

from src.kernel.storage.migration_runner import (
    MySQLMigrationRunner,
    MySQLTriggerContract,
    SchemaMigration,
    verify_mysql_trigger_contract,
)

from .contracts import StorageBackendRuntime, StorageWriterRole
from .learning_contracts import (
    LEARNING_WRITER_CLAIM_NAMESPACE,
    LEARNING_WRITER_CLAIM_STATE_KEY,
)
from .models import BackendKind
from .writer_claims import ensure_singleton_writer_claim_schema

LEARNING_SCHEMA_VERSION = 2

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

_MYSQL_SHADOW_SCHEMA_MIGRATION = SchemaMigration(
    version=1,
    name="life_learning_storage_shadow_v1",
    statements=_MYSQL_SCHEMA_MIGRATION.statements[:2],
)

_MYSQL_IMMUTABILITY_TRIGGERS = (
    MySQLTriggerContract(
        "learning_events_immutable_update_v1",
        "learning_events",
        "UPDATE",
        "BEFORE",
        "LearningEventImmutable",
    ),
    MySQLTriggerContract(
        "learning_events_immutable_delete_v1",
        "learning_events",
        "DELETE",
        "BEFORE",
        "LearningEventImmutable",
    ),
)


def _mysql_learning_claim_guard(
    *,
    trigger_name: str,
    table_name: str,
    operation: str,
) -> str:
    return f"""CREATE TRIGGER IF NOT EXISTS {trigger_name}
        BEFORE {operation} ON {table_name} FOR EACH ROW
        BEGIN
            IF EXISTS (
                SELECT 1 FROM runtime_singleton_writer_claims c
                WHERE c.namespace = '{LEARNING_WRITER_CLAIM_NAMESPACE}'
                    AND c.state_key = '{LEARNING_WRITER_CLAIM_STATE_KEY}'
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
                    AND c.namespace = '{LEARNING_WRITER_CLAIM_NAMESPACE}'
                    AND c.state_key = '{LEARNING_WRITER_CLAIM_STATE_KEY}'
                    AND c.released_at IS NULL
                    AND c.lease_until > CURRENT_TIMESTAMP(6)
            ) THEN
                SIGNAL SQLSTATE '45000'
                    SET MESSAGE_TEXT = 'LearningSingletonWriterClaimRequired';
            END IF;
        END"""


MYSQL_LEARNING_CLAIM_GUARD_MIGRATION = SchemaMigration(
    version=2,
    name="life_learning_singleton_claim_guard_v2",
    statements=(
        _mysql_learning_claim_guard(
            trigger_name="learning_events_singleton_claim_insert_v2",
            table_name="learning_events",
            operation="INSERT",
        ),
        _mysql_learning_claim_guard(
            trigger_name="learning_projections_singleton_claim_insert_v2",
            table_name="learning_projections",
            operation="INSERT",
        ),
        _mysql_learning_claim_guard(
            trigger_name="learning_projections_singleton_claim_update_v2",
            table_name="learning_projections",
            operation="UPDATE",
        ),
        _mysql_learning_claim_guard(
            trigger_name="learning_projections_singleton_claim_delete_v2",
            table_name="learning_projections",
            operation="DELETE",
        ),
    ),
)

MYSQL_LEARNING_CLAIM_GUARD_TRIGGERS = tuple(
    MySQLTriggerContract(
        trigger_name,
        table_name,
        operation,
        "BEFORE",
        "LearningSingletonWriterClaimRequired",
    )
    for trigger_name, table_name, operation in (
        (
            "learning_events_singleton_claim_insert_v2",
            "learning_events",
            "INSERT",
        ),
        (
            "learning_projections_singleton_claim_insert_v2",
            "learning_projections",
            "INSERT",
        ),
        (
            "learning_projections_singleton_claim_update_v2",
            "learning_projections",
            "UPDATE",
        ),
        (
            "learning_projections_singleton_claim_delete_v2",
            "learning_projections",
            "DELETE",
        ),
    )
)


async def ensure_learning_schema(
    runtime: StorageBackendRuntime,
    *,
    require_database_immutability: bool = True,
) -> None:
    """Create Learning tables without weakening an activatable generation."""

    if not runtime.enabled or runtime.engine is None:
        raise RuntimeError("learning schema requires an enabled storage runtime")
    if (
        not require_database_immutability
        and runtime.writer_role != StorageWriterRole.CANDIDATE_COPY
    ):
        raise RuntimeError(
            "Learning database immutability may be relaxed only for candidate copy"
        )
    if require_database_immutability:
        await ensure_singleton_writer_claim_schema(runtime)
    await runtime.validate_writer()
    if runtime.backend == BackendKind.MYSQL:
        if require_database_immutability:
            runner = MySQLMigrationRunner(
                runtime.engine,
                table_name="life_learning_schema_migrations",
                lock_name="elysium:life-learning-schema",
            )
            await runner.apply(
                (
                    _MYSQL_SCHEMA_MIGRATION,
                    MYSQL_LEARNING_CLAIM_GUARD_MIGRATION,
                )
            )
            await verify_mysql_trigger_contract(
                runtime.engine,
                _MYSQL_IMMUTABILITY_TRIGGERS + MYSQL_LEARNING_CLAIM_GUARD_TRIGGERS,
            )
        else:
            runner = MySQLMigrationRunner(
                runtime.engine,
                table_name="life_learning_shadow_schema_migrations",
                lock_name="elysium:life-learning-shadow-schema",
            )
            await runner.apply((_MYSQL_SHADOW_SCHEMA_MIGRATION,))
    else:
        async with runtime.unit_of_work() as uow:
            for statement in LOCAL_LEARNING_SCHEMA_STATEMENTS:
                await uow.session.execute(text(statement))
    await runtime.validate_writer()


async def verify_learning_writer_claim_guard(
    runtime: StorageBackendRuntime,
) -> None:
    """Fail closed when a claimed MySQL Learning writer lacks DB triggers."""

    if not runtime.enabled or runtime.engine is None:
        raise RuntimeError("learning writer guard requires an enabled storage runtime")
    if runtime.backend != BackendKind.MYSQL:
        return
    await verify_mysql_trigger_contract(
        runtime.engine,
        MYSQL_LEARNING_CLAIM_GUARD_TRIGGERS,
    )


__all__ = [
    "LEARNING_SCHEMA_VERSION",
    "LOCAL_LEARNING_SCHEMA_STATEMENTS",
    "MYSQL_LEARNING_CLAIM_GUARD_MIGRATION",
    "MYSQL_LEARNING_CLAIM_GUARD_TRIGGERS",
    "ensure_learning_schema",
    "verify_learning_writer_claim_guard",
]
