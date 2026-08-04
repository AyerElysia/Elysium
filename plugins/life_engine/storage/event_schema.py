"""Versioned schema for the selectable authoritative Life Event ledger."""

from __future__ import annotations

from sqlalchemy import text

from src.kernel.storage.migration_runner import MySQLMigrationRunner, SchemaMigration

from .contracts import StorageBackendRuntime
from .models import BackendKind

EVENT_SCHEMA_VERSION = 2

LOCAL_EVENT_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS raw_life_events (
        ingest_position INTEGER PRIMARY KEY AUTOINCREMENT,
        occurrence_id TEXT NOT NULL UNIQUE,
        source_event_id TEXT NOT NULL,
        source_sequence INTEGER NOT NULL DEFAULT 0,
        occurred_at TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        payload_hash TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_raw_life_events_source
        ON raw_life_events(source_event_id, occurred_at, ingest_position)""",
    """CREATE TABLE IF NOT EXISTS raw_event_consumer_offsets (
        consumer_id TEXT PRIMARY KEY,
        ingest_position INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    )""",
    """CREATE TABLE IF NOT EXISTS raw_event_ledger_meta (
        meta_key TEXT PRIMARY KEY,
        meta_value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS raw_event_export_outbox (
        outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
        occurrence_id TEXT NOT NULL UNIQUE,
        payload_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('held', 'pending', 'confirmed')),
        remote_position INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        confirmed_at TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE INDEX IF NOT EXISTS idx_raw_event_export_pending
        ON raw_event_export_outbox(state, outbox_id)""",
    """CREATE TRIGGER IF NOT EXISTS raw_life_events_immutable_update_v2
        BEFORE UPDATE ON raw_life_events BEGIN
            SELECT RAISE(ABORT, 'RawLifeEventImmutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS raw_life_events_immutable_delete_v2
        BEFORE DELETE ON raw_life_events BEGIN
            SELECT RAISE(ABORT, 'RawLifeEventImmutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS raw_event_export_payload_immutable
        BEFORE UPDATE OF occurrence_id, payload_hash, payload_json, created_at
        ON raw_event_export_outbox BEGIN
            SELECT RAISE(ABORT, 'RawEventExportPayloadImmutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS raw_event_export_no_delete
        BEFORE DELETE ON raw_event_export_outbox BEGIN
            SELECT RAISE(ABORT, 'RawEventExportDeleteForbidden');
        END""",
)

_MYSQL_MIGRATION = SchemaMigration(
    version=1,
    name="life_event_ledger_v1",
    statements=(
        """CREATE TABLE IF NOT EXISTS raw_life_events (
            ingest_position BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            source_event_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            source_sequence BIGINT NOT NULL DEFAULT 0,
            occurred_at DATETIME(6) NOT NULL,
            recorded_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            payload_json JSON NOT NULL,
            payload_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            UNIQUE KEY uq_raw_life_events_occurrence (occurrence_id),
            KEY idx_raw_life_events_source (
                source_event_id, occurred_at, ingest_position
            )
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS raw_event_consumer_offsets (
            consumer_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY,
            ingest_position BIGINT UNSIGNED NOT NULL,
            revision BIGINT UNSIGNED NOT NULL,
            updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            metadata_json JSON NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS raw_event_ledger_meta (
            meta_key VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            meta_value VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS raw_event_export_outbox (
            outbox_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            payload_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            payload_json JSON NOT NULL,
            state VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            remote_position BIGINT UNSIGNED NOT NULL DEFAULT 0,
            created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            confirmed_at DATETIME(6) NULL,
            UNIQUE KEY uq_raw_event_export_occurrence (occurrence_id),
            KEY idx_raw_event_export_pending (state, outbox_id),
            CONSTRAINT chk_raw_event_export_state
                CHECK (state IN ('held', 'pending', 'confirmed'))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    ),
)

_MYSQL_PAYLOAD_TEXT_MIGRATION = SchemaMigration(
    version=2,
    name="life_event_exact_payload_text_v2",
    statements=(
        """ALTER TABLE raw_life_events
        MODIFY COLUMN payload_json LONGTEXT CHARACTER SET utf8mb4
            COLLATE utf8mb4_bin NOT NULL""",
        """ALTER TABLE raw_life_events
        ADD CONSTRAINT chk_raw_life_events_payload_json
            CHECK (JSON_VALID(payload_json))""",
        """ALTER TABLE raw_event_export_outbox
        MODIFY COLUMN payload_json LONGTEXT CHARACTER SET utf8mb4
            COLLATE utf8mb4_bin NOT NULL""",
        """ALTER TABLE raw_event_export_outbox
        ADD CONSTRAINT chk_raw_event_export_payload_json
            CHECK (JSON_VALID(payload_json))""",
    ),
)

_MYSQL_IMMUTABILITY_MIGRATION = SchemaMigration(
    version=1,
    name="life_event_ledger_immutability_v1",
    statements=(
        """CREATE TRIGGER IF NOT EXISTS raw_life_events_immutable_update_v2
        BEFORE UPDATE ON raw_life_events FOR EACH ROW
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'RawLifeEventImmutable'""",
        """CREATE TRIGGER IF NOT EXISTS raw_life_events_immutable_delete_v2
        BEFORE DELETE ON raw_life_events FOR EACH ROW
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'RawLifeEventImmutable'""",
        """CREATE TRIGGER IF NOT EXISTS raw_event_export_no_delete
        BEFORE DELETE ON raw_event_export_outbox FOR EACH ROW
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'RawEventExportDeleteForbidden'""",
        """CREATE TRIGGER IF NOT EXISTS raw_event_export_payload_immutable
        BEFORE UPDATE ON raw_event_export_outbox FOR EACH ROW
        BEGIN
            IF NOT (
                OLD.occurrence_id <=> NEW.occurrence_id
                AND OLD.payload_hash <=> NEW.payload_hash
                AND OLD.payload_json <=> NEW.payload_json
                AND OLD.created_at <=> NEW.created_at
            ) THEN
                SIGNAL SQLSTATE '45000'
                    SET MESSAGE_TEXT = 'RawEventExportPayloadImmutable';
            END IF;
        END""",
    ),
)


async def ensure_life_event_schema(
    runtime: StorageBackendRuntime,
    *,
    require_database_immutability: bool = True,
) -> None:
    """Create the selected schema, requiring DB immutability for activation."""

    if not runtime.enabled or runtime.engine is None:
        raise RuntimeError("Life Event schema requires an enabled storage runtime")
    if runtime.backend == BackendKind.MYSQL:
        await runtime.validate_writer()
        runner = MySQLMigrationRunner(
            runtime.engine,
            table_name="life_event_schema_migrations",
            lock_name="elysium:life-event-schema",
        )
        await runner.apply((_MYSQL_MIGRATION, _MYSQL_PAYLOAD_TEXT_MIGRATION))
        if require_database_immutability:
            immutability_runner = MySQLMigrationRunner(
                runtime.engine,
                table_name="life_event_immutability_schema_migrations",
                lock_name="elysium:life-event-immutability-schema",
            )
            await immutability_runner.apply((_MYSQL_IMMUTABILITY_MIGRATION,))
        await runtime.validate_writer()
    else:
        async with runtime.unit_of_work() as uow:
            for statement in LOCAL_EVENT_SCHEMA_STATEMENTS:
                await uow.session.execute(text(statement))


__all__ = [
    "EVENT_SCHEMA_VERSION",
    "LOCAL_EVENT_SCHEMA_STATEMENTS",
    "ensure_life_event_schema",
]
