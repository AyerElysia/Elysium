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

SUBJECT_SCHEMA_VERSION = 5

LOCAL_SUBJECT_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS subject_documents (
        document_id TEXT PRIMARY KEY,
        logical_path TEXT NOT NULL,
        declared_owner TEXT NULL,
        current_version_id TEXT NOT NULL DEFAULT '',
        revision INTEGER NOT NULL DEFAULT 0,
        binding_revision INTEGER NOT NULL DEFAULT 0,
        is_deleted INTEGER NOT NULL DEFAULT 0
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

_LOCAL_SUBJECT_LIFECYCLE = (
    """CREATE TABLE IF NOT EXISTS subject_document_path_bindings (
        logical_path TEXT PRIMARY KEY,
        document_id TEXT NULL UNIQUE,
        revision INTEGER NOT NULL,
        FOREIGN KEY (document_id) REFERENCES subject_documents(document_id)
            ON DELETE RESTRICT
    )""",
    """CREATE TABLE IF NOT EXISTS subject_document_path_events (
        event_id TEXT PRIMARY KEY,
        logical_path TEXT NOT NULL,
        previous_document_id TEXT NULL,
        document_id TEXT NULL,
        previous_revision INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        occurrence_id TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        UNIQUE(logical_path, revision),
        FOREIGN KEY (document_id) REFERENCES subject_documents(document_id)
            ON DELETE RESTRICT,
        FOREIGN KEY (previous_document_id) REFERENCES subject_documents(document_id)
            ON DELETE RESTRICT
    )""",
    """CREATE TABLE IF NOT EXISTS subject_document_operations (
        occurrence_id TEXT PRIMARY KEY,
        operation TEXT NOT NULL,
        document_id TEXT NOT NULL,
        command_digest TEXT NOT NULL,
        result_json TEXT NOT NULL,
        change_context_json TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        FOREIGN KEY (document_id) REFERENCES subject_documents(document_id)
            ON DELETE RESTRICT
    )""",
    """CREATE INDEX IF NOT EXISTS idx_subject_operation_history
        ON subject_document_operations(document_id, recorded_at, occurrence_id)""",
)

_PROJECTION_LIFECYCLE_COLUMNS = {
    "operation": "VARCHAR(16) NOT NULL DEFAULT 'write'",
    "previous_logical_path": "VARCHAR(512) NOT NULL DEFAULT ''",
    "binding_revision": "BIGINT NOT NULL DEFAULT 0",
    "previous_binding_revision": "BIGINT NOT NULL DEFAULT 0",
    "previous_version_id": "VARCHAR(128) NOT NULL DEFAULT ''",
    "previous_content_hash": "VARCHAR(64) NOT NULL DEFAULT ''",
}


def _legacy_projection_binding_repair_sql(backend: BackendKind) -> str:
    """Recover only v4 tasks' added binding field from immutable v5 bootstrap.

    Never use the current binding: a rename or path reuse may have advanced it.
    Original outbox state/leases/errors and all immutable history stay untouched.
    Modern operations and partially populated lifecycle rows are not legacy.
    """

    bootstrap_id = (
        "CONCAT('binding_bootstrap:', subject_projection_outbox.document_id)"
        if backend == BackendKind.MYSQL
        else "'binding_bootstrap:' || subject_projection_outbox.document_id"
    )
    operation = "BINARY operation" if backend == BackendKind.MYSQL else "operation"
    return f"""UPDATE subject_projection_outbox SET binding_revision = 1
        WHERE {operation} = 'write' AND binding_revision = 0
          AND previous_logical_path = '' AND previous_binding_revision = 0
          AND previous_version_id = '' AND previous_content_hash = ''
          AND EXISTS (
            SELECT 1 FROM subject_document_path_events AS bootstrap
            JOIN subject_document_head_events AS event
              ON event.document_id = bootstrap.document_id
            JOIN subject_document_versions AS version
              ON version.version_id = event.next_version_id
            WHERE bootstrap.event_id = {bootstrap_id}
              AND bootstrap.occurrence_id = bootstrap.event_id
              AND bootstrap.document_id = subject_projection_outbox.document_id
              AND bootstrap.logical_path = subject_projection_outbox.logical_path
              AND bootstrap.previous_document_id IS NULL
              AND bootstrap.previous_revision = 0 AND bootstrap.revision = 1
              AND event.head_event_id = subject_projection_outbox.head_event_id
              AND event.next_version_id = subject_projection_outbox.version_id
              AND event.previous_version_id = version.parent_version_id
              AND version.document_id = subject_projection_outbox.document_id
              AND version.logical_path = subject_projection_outbox.logical_path
              AND version.content_hash = subject_projection_outbox.content_hash
              AND NOT EXISTS (
                SELECT 1 FROM subject_document_operations AS operation_record
                WHERE operation_record.occurrence_id = event.occurrence_id
              )
          )"""


def _mysql_lifecycle_ddl(
    *, exists_query: str, statement: str, when_present: bool = False,
) -> tuple[str, ...]:
    """Keep each v5 DDL step replayable after MySQL's implicit DDL commit."""

    escaped = statement.replace("'", "''")
    choices = (
        f"'{escaped}', 'SELECT 1'" if when_present
        else f"'SELECT 1', '{escaped}'"
    )
    return (
        f"SET @subject_lifecycle_ddl = IF(({exists_query}), {choices})",
        "PREPARE subject_lifecycle_statement FROM @subject_lifecycle_ddl",
        "EXECUTE subject_lifecycle_statement",
        "DEALLOCATE PREPARE subject_lifecycle_statement",
    )


_MYSQL_SUBJECT_LIFECYCLE = SchemaMigration(
    version=5,
    name="subject_document_identity_lifecycle_v5",
    statements=(
        *_mysql_lifecycle_ddl(
            exists_query="""SELECT COUNT(*) FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'subject_documents'
                AND INDEX_NAME = 'uq_subject_document_path'""",
            statement="ALTER TABLE subject_documents DROP INDEX uq_subject_document_path",
            when_present=True,
        ),
        *(
            statement
            for name, definition in {
                "binding_revision": "BIGINT NOT NULL DEFAULT 0",
                "is_deleted": "BOOLEAN NOT NULL DEFAULT FALSE",
            }.items()
            for statement in _mysql_lifecycle_ddl(
                exists_query=f"""SELECT COUNT(*) FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'subject_documents'
                    AND COLUMN_NAME = '{name}'""",
                statement=f"ALTER TABLE subject_documents ADD COLUMN {name} {definition}",
            )
        ),
        """CREATE TABLE IF NOT EXISTS subject_document_path_bindings (
            logical_path VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY,
            document_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NULL UNIQUE,
            revision BIGINT NOT NULL,
            FOREIGN KEY (document_id) REFERENCES subject_documents(document_id)
                ON DELETE RESTRICT
        ) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS subject_document_path_events (
            event_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            logical_path VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            previous_document_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NULL,
            document_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NULL,
            previous_revision BIGINT NOT NULL,
            revision BIGINT NOT NULL,
            occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            recorded_at DATETIME(6) NOT NULL,
            UNIQUE KEY uq_subject_path_revision (logical_path, revision),
            FOREIGN KEY (document_id) REFERENCES subject_documents(document_id)
                ON DELETE RESTRICT,
            FOREIGN KEY (previous_document_id) REFERENCES subject_documents(document_id)
                ON DELETE RESTRICT
        ) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS subject_document_operations (
            occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY,
            operation VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            document_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            command_digest CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            result_json JSON NOT NULL,
            change_context_json JSON NOT NULL,
            recorded_at DATETIME(6) NOT NULL,
            KEY idx_subject_operation_history (document_id, recorded_at, occurrence_id),
            FOREIGN KEY (document_id) REFERENCES subject_documents(document_id)
                ON DELETE RESTRICT
        ) ENGINE=InnoDB""",
        *(
            statement
            for name, definition in _PROJECTION_LIFECYCLE_COLUMNS.items()
            for statement in _mysql_lifecycle_ddl(
                exists_query=f"""SELECT COUNT(*) FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                    AND TABLE_NAME = 'subject_projection_outbox'
                    AND COLUMN_NAME = '{name}'""",
                statement=f"ALTER TABLE subject_projection_outbox "
                    f"ADD COLUMN {name} {definition}",
            )
        ),
        """INSERT INTO subject_document_path_bindings
            (logical_path, document_id, revision)
            SELECT d.logical_path, d.document_id, 1 FROM subject_documents AS d
            WHERE d.binding_revision = 0 AND d.is_deleted = 0
            AND NOT EXISTS (SELECT 1 FROM subject_document_path_bindings AS b
                WHERE b.logical_path = d.logical_path)""",
        """INSERT INTO subject_document_path_events
            (event_id, logical_path, previous_document_id, document_id,
             previous_revision, revision, occurrence_id, recorded_at)
            SELECT CONCAT('binding_bootstrap:', document_id), logical_path,
                NULL, document_id, 0, 1, CONCAT('binding_bootstrap:', document_id),
                CURRENT_TIMESTAMP(6) FROM subject_documents AS d
            WHERE d.binding_revision = 0 AND d.is_deleted = 0
            AND NOT EXISTS (SELECT 1 FROM subject_document_path_events AS e
                WHERE e.event_id = CONCAT('binding_bootstrap:', d.document_id))""",
        """UPDATE subject_documents SET binding_revision = 1
            WHERE binding_revision = 0 AND is_deleted = 0""",
    ),
)

_LIFECYCLE_IMMUTABLE_TABLES = (
    ("subject_document_operations", "SubjectDocumentOperationImmutable"),
    ("subject_document_path_events", "SubjectDocumentPathEventImmutable"),
)

_MYSQL_SUBJECT_LIFECYCLE_IMMUTABILITY = SchemaMigration(
    version=2,
    name="subject_document_lifecycle_immutability_v2",
    statements=tuple(
        f"""CREATE TRIGGER IF NOT EXISTS {table}_immutable_{action.lower()}
        BEFORE {action} ON {table} FOR EACH ROW
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"""
        for table, message in _LIFECYCLE_IMMUTABLE_TABLES
        for action in ("UPDATE", "DELETE")
    ),
)


async def _ensure_local_lifecycle_schema(runtime: StorageBackendRuntime) -> None:
    """Upgrade only rebuildable head shape; all immutable rows remain intact."""

    await runtime.validate_writer()
    assert runtime.engine is not None
    async with runtime.engine.connect() as connection:
        # SQLite cannot drop a UNIQUE autoindex. Rebuild this one projection
        # table without renaming the parent, which would retarget child FKs.
        await connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        await connection.commit()
        try:
            async with connection.begin():
                # Start a real SQLite transaction before DDL; SQLAlchemy's
                # logical begin alone does not make SQLite DDL transactional.
                await connection.exec_driver_sql("BEGIN IMMEDIATE")
                columns = {
                    str(row[1])
                    for row in await connection.exec_driver_sql(
                        "PRAGMA table_info(subject_documents)"
                    )
                }
                if "is_deleted" not in columns:
                    await connection.exec_driver_sql(
                        """CREATE TABLE subject_documents_v5 (
                            document_id TEXT PRIMARY KEY,
                            logical_path TEXT NOT NULL,
                            declared_owner TEXT NULL,
                            current_version_id TEXT NOT NULL DEFAULT '',
                            revision INTEGER NOT NULL DEFAULT 0,
                            binding_revision INTEGER NOT NULL DEFAULT 0,
                            is_deleted INTEGER NOT NULL DEFAULT 0
                        )"""
                    )
                    await connection.exec_driver_sql(
                        """INSERT INTO subject_documents_v5
                        (document_id, logical_path, declared_owner,
                         current_version_id, revision)
                        SELECT document_id, logical_path, declared_owner,
                            current_version_id, revision FROM subject_documents"""
                    )
                    await connection.exec_driver_sql("DROP TABLE subject_documents")
                    await connection.exec_driver_sql(
                        "ALTER TABLE subject_documents_v5 RENAME TO subject_documents"
                    )
                projection_columns = {
                    str(row[1])
                    for row in await connection.exec_driver_sql(
                        "PRAGMA table_info(subject_projection_outbox)"
                    )
                }
                for name, definition in _PROJECTION_LIFECYCLE_COLUMNS.items():
                    if name not in projection_columns:
                        await connection.exec_driver_sql(
                            f"ALTER TABLE subject_projection_outbox "
                            f"ADD COLUMN {name} {definition}"
                        )
                for statement in _LOCAL_SUBJECT_LIFECYCLE:
                    await connection.exec_driver_sql(statement)
                await connection.exec_driver_sql(
                    """INSERT INTO subject_document_path_bindings
                    (logical_path, document_id, revision)
                    SELECT logical_path, document_id, 1 FROM subject_documents
                    WHERE binding_revision = 0 AND is_deleted = 0"""
                )
                await connection.exec_driver_sql(
                    """INSERT INTO subject_document_path_events
                    (event_id, logical_path, previous_document_id, document_id,
                     previous_revision, revision, occurrence_id, recorded_at)
                    SELECT 'binding_bootstrap:' || document_id, logical_path,
                        NULL, document_id, 0, 1,
                        'binding_bootstrap:' || document_id,
                        STRFTIME('%Y-%m-%dT%H:%M:%f+00:00', 'now')
                    FROM subject_documents
                    WHERE binding_revision = 0 AND is_deleted = 0"""
                )
                await connection.exec_driver_sql(
                    """UPDATE subject_documents SET binding_revision = 1
                    WHERE binding_revision = 0 AND is_deleted = 0"""
                )
                await connection.exec_driver_sql(
                    _legacy_projection_binding_repair_sql(BackendKind.LOCAL)
                )
                for table, message in _LIFECYCLE_IMMUTABLE_TABLES:
                    for action in ("UPDATE", "DELETE"):
                        await connection.exec_driver_sql(
                            f"""CREATE TRIGGER IF NOT EXISTS
                            {table}_immutable_{action.lower()}
                            BEFORE {action} ON {table} BEGIN
                                SELECT RAISE(ABORT, '{message}');
                            END"""
                        )
                invalid = (
                    await connection.exec_driver_sql("PRAGMA foreign_key_check")
                ).first()
                if invalid is not None:
                    raise RuntimeError("subject lifecycle migration FK check failed")
                await runtime.validate_writer()
        finally:
            await connection.exec_driver_sql("PRAGMA foreign_keys = ON")
            await connection.commit()


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
                _MYSQL_SUBJECT_LIFECYCLE,
            )
        )
        if require_database_immutability:
            immutable = MySQLMigrationRunner(
                runtime.engine,
                table_name="subject_document_immutability_migrations",
                lock_name="elysium:subject-document-immutability",
            )
            await immutable.apply((
                _MYSQL_SUBJECT_IMMUTABILITY,
                _MYSQL_SUBJECT_LIFECYCLE_IMMUTABILITY,
            ))
            await verify_mysql_trigger_contract(
                runtime.engine,
                _MYSQL_SUBJECT_IMMUTABILITY_TRIGGERS + tuple(
                    MySQLTriggerContract(
                        f"{table}_immutable_{action.lower()}", table,
                        action, "BEFORE", message,
                    )
                    for table, message in _LIFECYCLE_IMMUTABLE_TABLES
                    for action in ("UPDATE", "DELETE")
                ),
            )
        # v5 may already be recorded. Keep its checksum unchanged and repair
        # only derived legacy metadata in a separately fenced, idempotent UoW.
        async with runtime.unit_of_work() as uow:
            await uow.session.execute(text(
                _legacy_projection_binding_repair_sql(BackendKind.MYSQL)
            ))
        await runtime.validate_writer()
        return
    async with runtime.unit_of_work() as uow:
        for statement in LOCAL_SUBJECT_SCHEMA_STATEMENTS:
            await uow.session.execute(text(statement))
    await _ensure_local_lifecycle_schema(runtime)


__all__ = [
    "LOCAL_SUBJECT_SCHEMA_STATEMENTS",
    "SUBJECT_SCHEMA_VERSION",
    "ensure_subject_document_schema",
]
