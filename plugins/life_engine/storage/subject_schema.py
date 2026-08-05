"""Versioned local/MySQL schema for subject document history."""

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

SUBJECT_SCHEMA_VERSION = 4

LOCAL_SUBJECT_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS subject_documents (
        document_id TEXT PRIMARY KEY,
        logical_path TEXT NOT NULL UNIQUE,
        declared_owner TEXT NULL,
        current_version_id TEXT NOT NULL DEFAULT '',
        revision INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS subject_document_versions (
        version_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        logical_path TEXT NOT NULL,
        parent_version_id TEXT NOT NULL DEFAULT '',
        occurrence_id TEXT NOT NULL,
        semantic_actor_id TEXT NULL,
        semantic_source_id TEXT NULL,
        occurred_at TEXT NULL,
        recorded_by TEXT NOT NULL,
        recorded_source TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        provenance_status TEXT NOT NULL,
        content_bytes BLOB NOT NULL,
        content_hash TEXT NOT NULL,
        byte_length INTEGER NOT NULL,
        byte_fidelity TEXT NOT NULL,
        encoding TEXT NULL,
        newline_style TEXT NULL,
        change_context_json TEXT NOT NULL DEFAULT '{}',
        UNIQUE(document_id, occurrence_id),
        FOREIGN KEY (document_id) REFERENCES subject_documents(document_id)
            ON DELETE RESTRICT
    )""",
    """CREATE INDEX IF NOT EXISTS idx_subject_document_history
        ON subject_document_versions(document_id, recorded_at, version_id)""",
    """CREATE TABLE IF NOT EXISTS subject_document_head_events (
        head_event_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        previous_version_id TEXT NOT NULL DEFAULT '',
        next_version_id TEXT NOT NULL,
        occurrence_id TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        authority_epoch INTEGER NOT NULL,
        change_context_json TEXT NOT NULL DEFAULT '{}',
        UNIQUE(document_id, occurrence_id),
        FOREIGN KEY (document_id) REFERENCES subject_documents(document_id)
            ON DELETE RESTRICT,
        FOREIGN KEY (next_version_id) REFERENCES subject_document_versions(version_id)
            ON DELETE RESTRICT
    )""",
    """CREATE TABLE IF NOT EXISTS subject_projection_outbox (
        outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
        head_event_id TEXT NOT NULL UNIQUE,
        document_id TEXT NOT NULL,
        logical_path TEXT NOT NULL,
        version_id TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending', 'confirmed', 'failed')),
        attempt_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        confirmed_at TEXT NOT NULL DEFAULT '',
        last_error TEXT NOT NULL DEFAULT '',
        lease_owner TEXT NOT NULL DEFAULT '',
        lease_until TEXT NOT NULL DEFAULT '',
        revision INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (head_event_id)
            REFERENCES subject_document_head_events(head_event_id) ON DELETE RESTRICT,
        FOREIGN KEY (document_id) REFERENCES subject_documents(document_id)
            ON DELETE RESTRICT,
        FOREIGN KEY (version_id) REFERENCES subject_document_versions(version_id)
            ON DELETE RESTRICT
    )""",
    """CREATE INDEX IF NOT EXISTS idx_subject_projection_pending
        ON subject_projection_outbox(state, lease_until, outbox_id)""",
    """CREATE TABLE IF NOT EXISTS subject_authority_decisions (
        decision_occurrence_id TEXT PRIMARY KEY,
        authority_occurrence_id TEXT NOT NULL UNIQUE,
        candidate_id TEXT NOT NULL,
        candidate_revision INTEGER NOT NULL,
        candidate_sha256 TEXT NOT NULL,
        candidate_occurrence_id TEXT NOT NULL,
        actor_consciousness_instance_id TEXT NOT NULL,
        expected_subject_revision TEXT NOT NULL,
        target_path TEXT NOT NULL,
        accepted_content_sha256 TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        previous_subject_revision TEXT NOT NULL,
        new_subject_revision TEXT NOT NULL,
        document_version_id TEXT NOT NULL,
        document_revision INTEGER NOT NULL,
        command_sha256 TEXT NOT NULL,
        committed_at TEXT NOT NULL,
        FOREIGN KEY (document_version_id)
            REFERENCES subject_document_versions(version_id) ON DELETE RESTRICT
    )""",
    """CREATE TRIGGER IF NOT EXISTS subject_versions_immutable_update
        BEFORE UPDATE ON subject_document_versions BEGIN
            SELECT RAISE(ABORT, 'SubjectDocumentVersionImmutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS subject_versions_immutable_delete
        BEFORE DELETE ON subject_document_versions BEGIN
            SELECT RAISE(ABORT, 'SubjectDocumentVersionImmutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS subject_head_events_immutable_update
        BEFORE UPDATE ON subject_document_head_events BEGIN
            SELECT RAISE(ABORT, 'SubjectDocumentHeadEventImmutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS subject_head_events_immutable_delete
        BEFORE DELETE ON subject_document_head_events BEGIN
            SELECT RAISE(ABORT, 'SubjectDocumentHeadEventImmutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS subject_authority_decisions_immutable_update
        BEFORE UPDATE ON subject_authority_decisions BEGIN
            SELECT RAISE(ABORT, 'SubjectAuthorityDecisionImmutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS subject_authority_decisions_immutable_delete
        BEFORE DELETE ON subject_authority_decisions BEGIN
            SELECT RAISE(ABORT, 'SubjectAuthorityDecisionImmutable');
    END""",
)

_MYSQL_SUBJECT_SCHEMA = SchemaMigration(
    version=1,
    name="subject_document_history_v1",
    statements=(
        """CREATE TABLE IF NOT EXISTS subject_documents (
            document_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            logical_path VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            declared_owner VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL,
            current_version_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '',
            revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
            UNIQUE KEY uq_subject_document_path (logical_path)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS subject_document_versions (
            version_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            document_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            logical_path VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            parent_version_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '',
            occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            semantic_actor_id VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL,
            semantic_source_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL,
            occurred_at DATETIME(6) NULL,
            recorded_by VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            recorded_source VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            recorded_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            provenance_status VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            content_bytes LONGBLOB NOT NULL,
            content_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            byte_length BIGINT UNSIGNED NOT NULL,
            byte_fidelity VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            encoding VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
            newline_style VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL,
            change_context_json JSON NOT NULL,
            UNIQUE KEY uq_subject_document_occurrence (document_id, occurrence_id),
            KEY idx_subject_document_history (document_id, recorded_at, version_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS subject_document_head_events (
            head_event_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            document_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            previous_version_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '',
            next_version_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            actor_id VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            source_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            occurred_at DATETIME(6) NOT NULL,
            authority_epoch BIGINT UNSIGNED NOT NULL,
            change_context_json JSON NOT NULL,
            UNIQUE KEY uq_subject_head_occurrence (document_id, occurrence_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS subject_projection_outbox (
            outbox_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            head_event_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            document_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            logical_path VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            version_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            content_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            state VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            attempt_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
            created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            confirmed_at DATETIME(6) NULL,
            last_error TEXT NOT NULL,
            UNIQUE KEY uq_subject_projection_head_event (head_event_id),
            KEY idx_subject_projection_pending (state, outbox_id),
            CONSTRAINT chk_subject_projection_state
                CHECK (state IN ('pending', 'confirmed', 'failed'))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    ),
)

_MYSQL_SUBJECT_REFERENCES = SchemaMigration(
    version=2,
    name="subject_document_references_v2",
    statements=(
        """ALTER TABLE subject_document_versions
        ADD CONSTRAINT fk_subject_version_document
        FOREIGN KEY (document_id) REFERENCES subject_documents(document_id)
        ON DELETE RESTRICT""",
        """ALTER TABLE subject_document_head_events
        ADD CONSTRAINT fk_subject_head_document
        FOREIGN KEY (document_id) REFERENCES subject_documents(document_id)
        ON DELETE RESTRICT""",
        """ALTER TABLE subject_document_head_events
        ADD CONSTRAINT fk_subject_head_version
        FOREIGN KEY (next_version_id) REFERENCES subject_document_versions(version_id)
        ON DELETE RESTRICT""",
        """ALTER TABLE subject_projection_outbox
        ADD CONSTRAINT fk_subject_outbox_head_event
        FOREIGN KEY (head_event_id)
        REFERENCES subject_document_head_events(head_event_id)
        ON DELETE RESTRICT""",
        """ALTER TABLE subject_projection_outbox
        ADD CONSTRAINT fk_subject_outbox_document
        FOREIGN KEY (document_id) REFERENCES subject_documents(document_id)
        ON DELETE RESTRICT""",
        """ALTER TABLE subject_projection_outbox
        ADD CONSTRAINT fk_subject_outbox_version
        FOREIGN KEY (version_id) REFERENCES subject_document_versions(version_id)
        ON DELETE RESTRICT""",
    ),
)

_MYSQL_SUBJECT_PROJECTION_LEASES = SchemaMigration(
    version=3,
    name="subject_projection_leases_v3",
    statements=(
        """ALTER TABLE subject_projection_outbox
        ADD COLUMN lease_owner VARCHAR(255) CHARACTER SET utf8mb4
            COLLATE utf8mb4_bin NOT NULL DEFAULT '',
        ADD COLUMN lease_until DATETIME(6) NULL,
        ADD COLUMN revision BIGINT UNSIGNED NOT NULL DEFAULT 0""",
        """ALTER TABLE subject_projection_outbox
        DROP INDEX idx_subject_projection_pending,
        ADD KEY idx_subject_projection_pending (state, lease_until, outbox_id)""",
    ),
)

_MYSQL_SUBJECT_AUTHORITY = SchemaMigration(
    version=4,
    name="subject_authority_decisions_v4",
    statements=(
        """CREATE TABLE IF NOT EXISTS subject_authority_decisions (
            decision_occurrence_id VARCHAR(255) CHARACTER SET utf8mb4
                COLLATE utf8mb4_bin NOT NULL PRIMARY KEY,
            authority_occurrence_id VARCHAR(96) CHARACTER SET ascii
                COLLATE ascii_bin NOT NULL,
            candidate_id VARCHAR(255) CHARACTER SET utf8mb4
                COLLATE utf8mb4_bin NOT NULL,
            candidate_revision BIGINT UNSIGNED NOT NULL,
            candidate_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            candidate_occurrence_id VARCHAR(255) CHARACTER SET utf8mb4
                COLLATE utf8mb4_bin NOT NULL,
            actor_consciousness_instance_id VARCHAR(255) CHARACTER SET utf8mb4
                COLLATE utf8mb4_bin NOT NULL,
            expected_subject_revision CHAR(64) CHARACTER SET ascii
                COLLATE ascii_bin NOT NULL,
            target_path VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            accepted_content_sha256 CHAR(64) CHARACTER SET ascii
                COLLATE ascii_bin NOT NULL,
            occurred_at DATETIME(6) NOT NULL,
            previous_subject_revision CHAR(64) CHARACTER SET ascii
                COLLATE ascii_bin NOT NULL,
            new_subject_revision CHAR(64) CHARACTER SET ascii
                COLLATE ascii_bin NOT NULL,
            document_version_id VARCHAR(128) CHARACTER SET ascii
                COLLATE ascii_bin NOT NULL,
            document_revision BIGINT UNSIGNED NOT NULL,
            command_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            committed_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            UNIQUE KEY uq_subject_authority_occurrence (authority_occurrence_id),
            CONSTRAINT fk_subject_authority_version
                FOREIGN KEY (document_version_id)
                REFERENCES subject_document_versions(version_id)
                ON DELETE RESTRICT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    ),
)

_MYSQL_SUBJECT_IMMUTABILITY = SchemaMigration(
    version=1,
    name="subject_document_immutability_v1",
    statements=(
        """CREATE TRIGGER IF NOT EXISTS subject_versions_immutable_update
        BEFORE UPDATE ON subject_document_versions FOR EACH ROW
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SubjectDocumentVersionImmutable'""",
        """CREATE TRIGGER IF NOT EXISTS subject_versions_immutable_delete
        BEFORE DELETE ON subject_document_versions FOR EACH ROW
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SubjectDocumentVersionImmutable'""",
        """CREATE TRIGGER IF NOT EXISTS subject_head_events_immutable_update
        BEFORE UPDATE ON subject_document_head_events FOR EACH ROW
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SubjectDocumentHeadEventImmutable'""",
        """CREATE TRIGGER IF NOT EXISTS subject_head_events_immutable_delete
        BEFORE DELETE ON subject_document_head_events FOR EACH ROW
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SubjectDocumentHeadEventImmutable'""",
        """CREATE TRIGGER IF NOT EXISTS subject_authority_decisions_immutable_update
        BEFORE UPDATE ON subject_authority_decisions FOR EACH ROW
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'SubjectAuthorityDecisionImmutable'""",
        """CREATE TRIGGER IF NOT EXISTS subject_authority_decisions_immutable_delete
        BEFORE DELETE ON subject_authority_decisions FOR EACH ROW
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'SubjectAuthorityDecisionImmutable'""",
    ),
)

_MYSQL_SUBJECT_IMMUTABILITY_TRIGGERS = (
    MySQLTriggerContract(
        "subject_versions_immutable_update",
        "subject_document_versions",
        "UPDATE",
        "BEFORE",
        "SubjectDocumentVersionImmutable",
    ),
    MySQLTriggerContract(
        "subject_versions_immutable_delete",
        "subject_document_versions",
        "DELETE",
        "BEFORE",
        "SubjectDocumentVersionImmutable",
    ),
    MySQLTriggerContract(
        "subject_head_events_immutable_update",
        "subject_document_head_events",
        "UPDATE",
        "BEFORE",
        "SubjectDocumentHeadEventImmutable",
    ),
    MySQLTriggerContract(
        "subject_head_events_immutable_delete",
        "subject_document_head_events",
        "DELETE",
        "BEFORE",
        "SubjectDocumentHeadEventImmutable",
    ),
    MySQLTriggerContract(
        "subject_authority_decisions_immutable_update",
        "subject_authority_decisions",
        "UPDATE",
        "BEFORE",
        "SubjectAuthorityDecisionImmutable",
    ),
    MySQLTriggerContract(
        "subject_authority_decisions_immutable_delete",
        "subject_authority_decisions",
        "DELETE",
        "BEFORE",
        "SubjectAuthorityDecisionImmutable",
    ),
)


async def ensure_subject_document_schema(
    runtime: StorageBackendRuntime,
    *,
    require_database_immutability: bool = True,
) -> None:
    """Create the selected subject schema and fail closed for activation."""

    if not runtime.enabled or runtime.engine is None:
        raise RuntimeError("subject document schema requires enabled storage")
    if (
        not require_database_immutability
        and runtime.writer_role != StorageWriterRole.CANDIDATE_COPY
    ):
        raise RuntimeError(
            "Subject database immutability may be relaxed only for candidate copy"
        )
    if runtime.backend == BackendKind.MYSQL:
        await runtime.validate_writer()
        runner = MySQLMigrationRunner(
            runtime.engine,
            table_name="subject_document_schema_migrations",
            lock_name="elysium:subject-document-schema",
        )
        await runner.apply(
            (
                _MYSQL_SUBJECT_SCHEMA,
                _MYSQL_SUBJECT_REFERENCES,
                _MYSQL_SUBJECT_PROJECTION_LEASES,
                _MYSQL_SUBJECT_AUTHORITY,
            )
        )
        if require_database_immutability:
            immutable = MySQLMigrationRunner(
                runtime.engine,
                table_name="subject_document_immutability_migrations",
                lock_name="elysium:subject-document-immutability",
            )
            await immutable.apply((_MYSQL_SUBJECT_IMMUTABILITY,))
            await verify_mysql_trigger_contract(
                runtime.engine,
                _MYSQL_SUBJECT_IMMUTABILITY_TRIGGERS,
            )
        await runtime.validate_writer()
        return
    async with runtime.unit_of_work() as uow:
        for statement in LOCAL_SUBJECT_SCHEMA_STATEMENTS:
            await uow.session.execute(text(statement))


__all__ = [
    "LOCAL_SUBJECT_SCHEMA_STATEMENTS",
    "SUBJECT_SCHEMA_VERSION",
    "ensure_subject_document_schema",
]
