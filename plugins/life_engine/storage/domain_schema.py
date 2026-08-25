"""Versioned schema for selectable Presence and World domain adapters."""

from __future__ import annotations

from sqlalchemy import text

from src.kernel.storage.migration_runner import MySQLMigrationRunner, SchemaMigration

from .contracts import StorageBackendRuntime
from .models import BackendKind

DOMAIN_SCHEMA_VERSION = 1

_LOCAL_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS consciousness_presence (
        instance_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        created_at TEXT NULL,
        last_active_at TEXT NULL,
        suspended_at TEXT NULL,
        stream_ids_json TEXT NOT NULL,
        perception_filter_json TEXT NOT NULL,
        metadata_json TEXT NOT NULL,
        session_id TEXT NOT NULL DEFAULT '',
        process_epoch TEXT NOT NULL DEFAULT '',
        lease_expires_at TEXT NULL,
        lease_duration_seconds INTEGER,
        revision INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_consciousness_presence_status
        ON consciousness_presence(status, kind)""",
    """CREATE INDEX IF NOT EXISTS idx_consciousness_presence_lease
        ON consciousness_presence(status, lease_expires_at)""",
    """CREATE TABLE IF NOT EXISTS consciousness_stream_owners (
        stream_id TEXT PRIMARY KEY,
        instance_id TEXT NOT NULL,
        claimed_at TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_consciousness_owner_instance
        ON consciousness_stream_owners(instance_id)""",
    """CREATE TABLE IF NOT EXISTS consciousness_presence_outbox (
        outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
        occurrence_id TEXT NOT NULL UNIQUE,
        event_type TEXT NOT NULL,
        instance_id TEXT NOT NULL,
        stream_id TEXT NOT NULL DEFAULT '',
        occurred_at TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        published_at TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS idx_presence_outbox_pending
        ON consciousness_presence_outbox(published_at, outbox_id)""",
    """CREATE TABLE IF NOT EXISTS world_projection_meta (
        meta_key TEXT PRIMARY KEY,
        meta_value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS world_assertions (
        assertion_id TEXT PRIMARY KEY,
        subject TEXT NOT NULL,
        predicate TEXT NOT NULL,
        value_json TEXT NOT NULL,
        domain TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT '',
        source_instance_id TEXT NOT NULL DEFAULT '',
        source_event_id TEXT NOT NULL,
        occurrence_id TEXT NOT NULL,
        observed_at TEXT NULL,
        valid_from TEXT NULL,
        valid_to TEXT NULL,
        recorded_at TEXT NULL,
        supersedes_assertion_id TEXT NOT NULL DEFAULT '',
        retracted_at TEXT NULL,
        retracted_by_assertion_id TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_world_assertions_subject
        ON world_assertions(subject, predicate, observed_at)""",
    """CREATE INDEX IF NOT EXISTS idx_world_assertions_source
        ON world_assertions(source_instance_id, source_event_id)""",
    """CREATE TABLE IF NOT EXISTS world_projection_changes (
        ingest_position INTEGER PRIMARY KEY,
        event_id TEXT NOT NULL,
        occurrence_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        change_kind TEXT NOT NULL,
        source_instance_id TEXT NOT NULL DEFAULT '',
        stream_id TEXT NOT NULL DEFAULT '',
        occurred_at TEXT NULL,
        recorded_at TEXT NULL,
        payload_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS world_perception_cursors (
        instance_id TEXT PRIMARY KEY,
        ingest_position INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    )""",
)

_MYSQL_MIGRATION = SchemaMigration(
    version=DOMAIN_SCHEMA_VERSION,
    name="life_presence_world_domain_v1",
    statements=(
        """CREATE TABLE IF NOT EXISTS consciousness_presence (
            instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY,
            kind VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            display_name TEXT NOT NULL,
            status VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            created_at DATETIME(6) NULL,
            last_active_at DATETIME(6) NULL,
            suspended_at DATETIME(6) NULL,
            stream_ids_json JSON NOT NULL,
            perception_filter_json JSON NOT NULL,
            metadata_json JSON NOT NULL,
            session_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            process_epoch VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            lease_expires_at DATETIME(6) NULL,
            lease_duration_seconds BIGINT NULL,
            revision BIGINT UNSIGNED NOT NULL,
            updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            KEY idx_consciousness_presence_status (status, kind),
            KEY idx_consciousness_presence_lease (status, lease_expires_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS consciousness_stream_owners (
            stream_id VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY,
            instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            claimed_at DATETIME(6) NOT NULL,
            KEY idx_consciousness_owner_instance (instance_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS consciousness_presence_outbox (
            outbox_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            occurrence_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            event_type VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            stream_id VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            occurred_at DATETIME(6) NOT NULL,
            payload_json JSON NOT NULL,
            published_at DATETIME(6) NULL,
            UNIQUE KEY uq_presence_outbox_occurrence (occurrence_id),
            KEY idx_presence_outbox_pending (published_at, outbox_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS world_projection_meta (
            meta_key VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            meta_value VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS world_assertions (
            assertion_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY,
            subject VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            predicate VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            value_json JSON NOT NULL,
            domain VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            status VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            source_instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            source_event_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            observed_at DATETIME(6) NULL,
            valid_from DATETIME(6) NULL,
            valid_to DATETIME(6) NULL,
            recorded_at DATETIME(6) NULL,
            supersedes_assertion_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            retracted_at DATETIME(6) NULL,
            retracted_by_assertion_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            payload_json JSON NOT NULL,
            KEY idx_world_assertions_subject (subject(191), predicate(128), observed_at),
            KEY idx_world_assertions_source (source_instance_id, source_event_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS world_projection_changes (
            ingest_position BIGINT UNSIGNED PRIMARY KEY,
            event_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            event_type VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            change_kind VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            source_instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            stream_id VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            occurred_at DATETIME(6) NULL,
            recorded_at DATETIME(6) NULL,
            payload_json JSON NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS world_perception_cursors (
            instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY,
            ingest_position BIGINT UNSIGNED NOT NULL,
            revision BIGINT UNSIGNED NOT NULL,
            updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    ),
)


async def ensure_presence_world_schema(runtime: StorageBackendRuntime) -> None:
    """Create only the selected backend's empty domain schema."""

    if not runtime.enabled or runtime.engine is None:
        raise RuntimeError("presence/world schema requires an enabled storage runtime")
    if runtime.backend == BackendKind.MYSQL:
        # Active production and candidate-copy writers share one fail-closed
        # validation surface.  This lets a fenced shadow migration create its
        # empty schema without inventing or activating a production generation.
        await runtime.validate_writer()
        runner = MySQLMigrationRunner(
            runtime.engine,
            table_name="life_presence_world_schema_migrations",
            lock_name="elysium:life-presence-world-schema",
        )
        await runner.apply((_MYSQL_MIGRATION,))
        await runtime.validate_writer()
    else:
        async with runtime.unit_of_work() as uow:
            for statement in _LOCAL_STATEMENTS:
                await uow.session.execute(text(statement))


__all__ = ["DOMAIN_SCHEMA_VERSION", "ensure_presence_world_schema"]
