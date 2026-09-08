"""Versioned schema for the canonical opportunity control plane."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.kernel.storage.migration_runner import (
    MySQLMigrationRunner,
    MySQLTriggerContract,
    SchemaMigration,
    verify_mysql_trigger_contract,
)

from .contracts import StorageBackendRuntime, StorageWriterRole
from .models import BackendKind
from .opportunity_contracts import (
    OPPORTUNITY_MANAGED_MARKER_KEY,
    OPPORTUNITY_SCHEDULER_CLAIM_NAMESPACE,
    OPPORTUNITY_SCHEDULER_CLAIM_STATE_KEY,
    OpportunityConflict,
    OpportunityRuntimeMarker,
)
from .writer_claims import ensure_singleton_writer_claim_schema

OPPORTUNITY_SCHEMA_VERSION = 2


class OpportunitySchemaNotReady(RuntimeError):
    """The selected backend lacks the explicitly migrated opportunity schema."""


LOCAL_OPPORTUNITY_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS opportunity_provider_events (
        position INTEGER PRIMARY KEY AUTOINCREMENT,
        occurrence_id TEXT NOT NULL UNIQUE,
        provider_id TEXT NOT NULL,
        action TEXT NOT NULL CHECK (action IN ('install','bind_workflow','pause','resume','uninstall')),
        status TEXT NOT NULL CHECK (status IN ('enabled','paused','uninstalled')),
        actor_consciousness_instance_id TEXT NOT NULL,
        source_instance_id TEXT NOT NULL,
        source_occurrence_ids_json TEXT NOT NULL,
        causation_occurrence_id TEXT NOT NULL,
        expected_revision INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        descriptor_version TEXT NOT NULL,
        descriptor_sha256 TEXT NOT NULL,
        workflow_id TEXT NOT NULL,
        workflow_revision INTEGER NOT NULL,
        workflow_sha256 TEXT NOT NULL,
        reason TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        event_sha256 TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_opportunity_provider_events
        ON opportunity_provider_events(provider_id, position)""",
    """CREATE TABLE IF NOT EXISTS opportunity_provider_heads (
        provider_id TEXT PRIMARY KEY,
        status TEXT NOT NULL CHECK (status IN ('enabled','paused','uninstalled')),
        revision INTEGER NOT NULL,
        descriptor_version TEXT NOT NULL,
        descriptor_sha256 TEXT NOT NULL,
        workflow_id TEXT NOT NULL,
        workflow_revision INTEGER NOT NULL,
        workflow_sha256 TEXT NOT NULL,
        last_occurrence_id TEXT NOT NULL,
        last_event_position INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS opportunity_workflow_versions (
        position INTEGER PRIMARY KEY AUTOINCREMENT,
        occurrence_id TEXT NOT NULL UNIQUE,
        workflow_id TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        schema_version INTEGER NOT NULL,
        actor_consciousness_instance_id TEXT NOT NULL,
        source_instance_id TEXT NOT NULL,
        source_occurrence_ids_json TEXT NOT NULL,
        causation_occurrence_id TEXT NOT NULL,
        content_bytes BLOB NOT NULL,
        content_sha256 TEXT NOT NULL,
        reason TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        event_sha256 TEXT NOT NULL,
        UNIQUE (workflow_id, revision)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_opportunity_workflow_provider
        ON opportunity_workflow_versions(provider_id, workflow_id, revision)""",
    """CREATE TABLE IF NOT EXISTS opportunity_registration_events (
        position INTEGER PRIMARY KEY AUTOINCREMENT,
        occurrence_id TEXT NOT NULL UNIQUE,
        opportunity_id TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        origin TEXT NOT NULL CHECK (origin IN ('subject','provider')),
        action TEXT NOT NULL CHECK (action IN ('open','configure','pause','resume','close')),
        status TEXT NOT NULL CHECK (status IN ('open','paused','closed')),
        actor_consciousness_instance_id TEXT NOT NULL,
        source_instance_id TEXT NOT NULL,
        source_occurrence_ids_json TEXT NOT NULL,
        causation_occurrence_id TEXT NOT NULL,
        expected_revision INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        referent_kind TEXT NOT NULL,
        referent_id TEXT NOT NULL,
        referent_revision INTEGER NOT NULL,
        referent_sha256 TEXT NOT NULL,
        workflow_id TEXT NOT NULL,
        workflow_revision INTEGER NOT NULL,
        workflow_sha256 TEXT NOT NULL,
        schedule TEXT NOT NULL CHECK (schedule IN ('manual','at','interval')),
        first_due_at TEXT NOT NULL,
        interval_seconds INTEGER NOT NULL,
        reason TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        event_sha256 TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_opportunity_registration_events
        ON opportunity_registration_events(opportunity_id, position)""",
    """CREATE TABLE IF NOT EXISTS opportunity_registration_heads (
        opportunity_id TEXT PRIMARY KEY,
        provider_id TEXT NOT NULL,
        origin TEXT NOT NULL CHECK (origin IN ('subject','provider')),
        status TEXT NOT NULL CHECK (status IN ('open','paused','closed')),
        revision INTEGER NOT NULL,
        referent_kind TEXT NOT NULL,
        referent_id TEXT NOT NULL,
        referent_revision INTEGER NOT NULL,
        referent_sha256 TEXT NOT NULL,
        workflow_id TEXT NOT NULL,
        workflow_revision INTEGER NOT NULL,
        workflow_sha256 TEXT NOT NULL,
        schedule TEXT NOT NULL CHECK (schedule IN ('manual','at','interval')),
        first_due_at TEXT NOT NULL,
        interval_seconds INTEGER NOT NULL,
        last_occurrence_id TEXT NOT NULL,
        last_event_position INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_opportunity_registration_heads
        ON opportunity_registration_heads(status, provider_id, opportunity_id)""",
    """CREATE TABLE IF NOT EXISTS opportunity_occurrences (
        position INTEGER PRIMARY KEY AUTOINCREMENT,
        occurrence_id TEXT NOT NULL UNIQUE,
        opportunity_id TEXT NOT NULL,
        registration_revision INTEGER NOT NULL,
        provider_id TEXT NOT NULL,
        provider_revision INTEGER NOT NULL,
        workflow_id TEXT NOT NULL,
        workflow_revision INTEGER NOT NULL,
        workflow_sha256 TEXT NOT NULL,
        referent_kind TEXT NOT NULL,
        referent_id TEXT NOT NULL,
        referent_revision INTEGER NOT NULL,
        referent_sha256 TEXT NOT NULL,
        due_index INTEGER NOT NULL,
        scheduled_for TEXT NOT NULL,
        available_at TEXT NOT NULL,
        source_frontier INTEGER NOT NULL,
        occurrence_sha256 TEXT NOT NULL,
        UNIQUE (opportunity_id, due_index)
    )""",
    """CREATE TABLE IF NOT EXISTS opportunity_activation_states (
        opportunity_id TEXT PRIMARY KEY,
        registration_revision INTEGER NOT NULL,
        due_index INTEGER NOT NULL,
        next_due_at TEXT NOT NULL,
        pending_occurrence_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS opportunity_publication_outbox (
        outbox_id TEXT PRIMARY KEY,
        occurrence_id TEXT NOT NULL UNIQUE,
        opportunity_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending','published','cancelled')),
        revision INTEGER NOT NULL,
        life_event_occurrence_id TEXT NOT NULL UNIQUE,
        life_event_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_opportunity_publication_pending
        ON opportunity_publication_outbox(status, created_at, outbox_id)""",
    """CREATE TABLE IF NOT EXISTS opportunity_delivery_receipts (
        position INTEGER PRIMARY KEY AUTOINCREMENT,
        receipt_id TEXT NOT NULL UNIQUE,
        occurrence_id TEXT NOT NULL,
        life_event_occurrence_id TEXT NOT NULL,
        consumer_consciousness_instance_id TEXT NOT NULL,
        context_delivery_id TEXT NOT NULL,
        final_request_id TEXT NOT NULL,
        final_attempt_id TEXT NOT NULL,
        exact_present INTEGER NOT NULL CHECK (exact_present = 1),
        expected_bytes INTEGER NOT NULL,
        effective_bytes INTEGER NOT NULL,
        expected_sha256 TEXT NOT NULL,
        effective_sha256 TEXT NOT NULL,
        perceived_at TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        receipt_sha256 TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_opportunity_delivery_occurrence
        ON opportunity_delivery_receipts(occurrence_id, position)""",
    """CREATE INDEX IF NOT EXISTS idx_opportunity_delivery_consumer
        ON opportunity_delivery_receipts(consumer_consciousness_instance_id, position)""",
    """CREATE TABLE IF NOT EXISTS opportunity_runtime_meta (
        marker_key TEXT PRIMARY KEY
            CHECK (marker_key = 'canonical_managed_v1'),
        generation_id TEXT NOT NULL,
        migration_occurrence_id TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        activated_at TEXT NOT NULL,
        marker_sha256 TEXT NOT NULL
    )""",
)

_HISTORY_IMMUTABLE_TABLES = (
    "opportunity_provider_events",
    "opportunity_workflow_versions",
    "opportunity_registration_events",
    "opportunity_occurrences",
    "opportunity_delivery_receipts",
)

_LOCAL_IMMUTABLE_TABLES = (
    *_HISTORY_IMMUTABLE_TABLES,
    "opportunity_runtime_meta",
)


@dataclass(frozen=True, slots=True)
class _SQLiteTriggerContract:
    name: str
    table: str
    statement: str


def _local_immutable_trigger_contract(
    table: str,
    operation: str,
) -> _SQLiteTriggerContract:
    name = f"{table}_immutable_{operation.lower()}_v1"
    return _SQLiteTriggerContract(
        name=name,
        table=table,
        statement=f"""CREATE TRIGGER IF NOT EXISTS {name}
        BEFORE {operation} ON {table} BEGIN
            SELECT RAISE(ABORT, 'OpportunityImmutable');
        END""",
    )


_LOCAL_IMMUTABILITY_TRIGGER_CONTRACTS = tuple(
    _local_immutable_trigger_contract(table, operation)
    for table in _LOCAL_IMMUTABLE_TABLES
    for operation in ("UPDATE", "DELETE")
)

LOCAL_OPPORTUNITY_IMMUTABILITY_STATEMENTS = tuple(
    contract.statement for contract in _LOCAL_IMMUTABILITY_TRIGGER_CONTRACTS
)

_MYSQL_TABLE_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS opportunity_provider_events (
        position BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        provider_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        action VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        status VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        actor_consciousness_instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        source_instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        source_occurrence_ids_json JSON NOT NULL,
        causation_occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        expected_revision BIGINT UNSIGNED NOT NULL,
        revision BIGINT UNSIGNED NOT NULL,
        descriptor_version VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        descriptor_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        workflow_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        workflow_revision BIGINT UNSIGNED NOT NULL,
        workflow_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        reason MEDIUMTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        occurred_at DATETIME(6) NOT NULL,
        recorded_at DATETIME(6) NOT NULL,
        event_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        UNIQUE KEY uq_opportunity_provider_occurrence (occurrence_id),
        KEY idx_opportunity_provider_events (provider_id, position),
        CONSTRAINT chk_opportunity_provider_action CHECK (action IN ('install','bind_workflow','pause','resume','uninstall'))
        ,CONSTRAINT chk_opportunity_provider_event_status CHECK (status IN ('enabled','paused','uninstalled'))
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    """CREATE TABLE IF NOT EXISTS opportunity_provider_heads (
        provider_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL PRIMARY KEY,
        status VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        revision BIGINT UNSIGNED NOT NULL,
        descriptor_version VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        descriptor_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        workflow_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        workflow_revision BIGINT UNSIGNED NOT NULL,
        workflow_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        last_occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        last_event_position BIGINT UNSIGNED NOT NULL,
        updated_at DATETIME(6) NOT NULL,
        CONSTRAINT chk_opportunity_provider_status CHECK (status IN ('enabled','paused','uninstalled'))
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    """CREATE TABLE IF NOT EXISTS opportunity_workflow_versions (
        position BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        workflow_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        provider_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        revision BIGINT UNSIGNED NOT NULL,
        schema_version INT UNSIGNED NOT NULL,
        actor_consciousness_instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        source_instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        source_occurrence_ids_json JSON NOT NULL,
        causation_occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        content_bytes LONGBLOB NOT NULL,
        content_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        reason MEDIUMTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        occurred_at DATETIME(6) NOT NULL,
        recorded_at DATETIME(6) NOT NULL,
        event_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        UNIQUE KEY uq_opportunity_workflow_occurrence (occurrence_id),
        UNIQUE KEY uq_opportunity_workflow_revision (workflow_id, revision),
        KEY idx_opportunity_workflow_provider (provider_id, workflow_id, revision)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    """CREATE TABLE IF NOT EXISTS opportunity_registration_events (
        position BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        opportunity_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        provider_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        origin VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        action VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        status VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        actor_consciousness_instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        source_instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        source_occurrence_ids_json JSON NOT NULL,
        causation_occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        expected_revision BIGINT UNSIGNED NOT NULL,
        revision BIGINT UNSIGNED NOT NULL,
        referent_kind VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        referent_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        referent_revision BIGINT UNSIGNED NOT NULL,
        referent_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        workflow_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        workflow_revision BIGINT UNSIGNED NOT NULL,
        workflow_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        schedule VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        first_due_at DATETIME(6) NULL,
        interval_seconds BIGINT UNSIGNED NOT NULL,
        reason MEDIUMTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        occurred_at DATETIME(6) NOT NULL,
        recorded_at DATETIME(6) NOT NULL,
        event_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        UNIQUE KEY uq_opportunity_registration_occurrence (occurrence_id),
        KEY idx_opportunity_registration_events (opportunity_id, position),
        CONSTRAINT chk_opportunity_registration_origin CHECK (origin IN ('subject','provider')),
        CONSTRAINT chk_opportunity_registration_action CHECK (action IN ('open','configure','pause','resume','close')),
        CONSTRAINT chk_opportunity_registration_status CHECK (status IN ('open','paused','closed')),
        CONSTRAINT chk_opportunity_registration_schedule CHECK (schedule IN ('manual','at','interval'))
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    """CREATE TABLE IF NOT EXISTS opportunity_registration_heads (
        opportunity_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL PRIMARY KEY,
        provider_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        origin VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        status VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        revision BIGINT UNSIGNED NOT NULL,
        referent_kind VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        referent_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        referent_revision BIGINT UNSIGNED NOT NULL,
        referent_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        workflow_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        workflow_revision BIGINT UNSIGNED NOT NULL,
        workflow_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        schedule VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        first_due_at DATETIME(6) NULL,
        interval_seconds BIGINT UNSIGNED NOT NULL,
        last_occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        last_event_position BIGINT UNSIGNED NOT NULL,
        updated_at DATETIME(6) NOT NULL,
        KEY idx_opportunity_registration_heads (status, provider_id, opportunity_id),
        CONSTRAINT chk_opportunity_head_origin CHECK (origin IN ('subject','provider')),
        CONSTRAINT chk_opportunity_head_status CHECK (status IN ('open','paused','closed')),
        CONSTRAINT chk_opportunity_head_schedule CHECK (schedule IN ('manual','at','interval'))
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
)

_MYSQL_RUNTIME_TABLE_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS opportunity_occurrences (
        position BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        opportunity_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        registration_revision BIGINT UNSIGNED NOT NULL,
        provider_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        provider_revision BIGINT UNSIGNED NOT NULL,
        workflow_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        workflow_revision BIGINT UNSIGNED NOT NULL,
        workflow_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        referent_kind VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        referent_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        referent_revision BIGINT UNSIGNED NOT NULL,
        referent_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        due_index BIGINT UNSIGNED NOT NULL,
        scheduled_for DATETIME(6) NOT NULL,
        available_at DATETIME(6) NOT NULL,
        source_frontier BIGINT UNSIGNED NOT NULL,
        occurrence_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        UNIQUE KEY uq_opportunity_occurrence_id (occurrence_id),
        UNIQUE KEY uq_opportunity_occurrence_due (opportunity_id, due_index)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    """CREATE TABLE IF NOT EXISTS opportunity_activation_states (
        opportunity_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL PRIMARY KEY,
        registration_revision BIGINT UNSIGNED NOT NULL,
        due_index BIGINT UNSIGNED NOT NULL,
        next_due_at DATETIME(6) NULL,
        pending_occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        revision BIGINT UNSIGNED NOT NULL,
        updated_at DATETIME(6) NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    """CREATE TABLE IF NOT EXISTS opportunity_publication_outbox (
        outbox_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL PRIMARY KEY,
        occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        opportunity_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        status VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        revision BIGINT UNSIGNED NOT NULL,
        life_event_occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        life_event_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        created_at DATETIME(6) NOT NULL,
        updated_at DATETIME(6) NOT NULL,
        UNIQUE KEY uq_opportunity_outbox_occurrence (occurrence_id),
        UNIQUE KEY uq_opportunity_outbox_life_event (life_event_occurrence_id),
        KEY idx_opportunity_publication_pending (status, created_at, outbox_id),
        CONSTRAINT chk_opportunity_publication_status CHECK (status IN ('pending','published','cancelled'))
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    """CREATE TABLE IF NOT EXISTS opportunity_delivery_receipts (
        position BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        receipt_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        life_event_occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        consumer_consciousness_instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        context_delivery_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        final_request_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        final_attempt_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        exact_present BOOLEAN NOT NULL,
        expected_bytes BIGINT UNSIGNED NOT NULL,
        effective_bytes BIGINT UNSIGNED NOT NULL,
        expected_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        effective_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        perceived_at DATETIME(6) NOT NULL,
        recorded_at DATETIME(6) NOT NULL,
        receipt_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        UNIQUE KEY uq_opportunity_receipt_id (receipt_id),
        KEY idx_opportunity_delivery_occurrence (occurrence_id, position),
        KEY idx_opportunity_delivery_consumer (consumer_consciousness_instance_id, position),
        CONSTRAINT chk_opportunity_delivery_exact CHECK (exact_present = TRUE)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
)

_MYSQL_RUNTIME_META_STATEMENT = """CREATE TABLE IF NOT EXISTS opportunity_runtime_meta (
    marker_key VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
    generation_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    migration_occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    schema_version INT UNSIGNED NOT NULL,
    activated_at DATETIME(6) NOT NULL,
    marker_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    CONSTRAINT chk_opportunity_runtime_marker_key
        CHECK (marker_key = 'canonical_managed_v1')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"""


def _immutable_trigger(table: str, operation: str, *, version: int = 1) -> str:
    return f"""CREATE TRIGGER IF NOT EXISTS {table}_immutable_{operation.lower()}_v{version}
        BEFORE {operation} ON {table} FOR EACH ROW
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'OpportunityImmutable'"""


_MYSQL_IMMUTABILITY_STATEMENTS = tuple(
    _immutable_trigger(table, operation)
    for table in _HISTORY_IMMUTABLE_TABLES
    for operation in ("UPDATE", "DELETE")
)

_MYSQL_IMMUTABILITY_TRIGGERS = tuple(
    MySQLTriggerContract(
        f"{table}_immutable_{operation.lower()}_v1",
        table,
        operation,
        "BEFORE",
        "OpportunityImmutable",
    )
    for table in _HISTORY_IMMUTABLE_TABLES
    for operation in ("UPDATE", "DELETE")
)

_MYSQL_META_IMMUTABILITY_STATEMENTS = tuple(
    _immutable_trigger("opportunity_runtime_meta", operation, version=2)
    for operation in ("UPDATE", "DELETE")
)

_MYSQL_META_IMMUTABILITY_TRIGGERS = tuple(
    MySQLTriggerContract(
        f"opportunity_runtime_meta_immutable_{operation.lower()}_v2",
        "opportunity_runtime_meta",
        operation,
        "BEFORE",
        "OpportunityImmutable",
    )
    for operation in ("UPDATE", "DELETE")
)


def _claim_trigger(table: str, operation: str) -> str:
    name = f"{table}_claim_{operation.lower()}_v1"
    return f"""CREATE TRIGGER IF NOT EXISTS {name}
        BEFORE {operation} ON {table} FOR EACH ROW
        BEGIN
            IF NOT EXISTS (
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
                    AND c.namespace = '{OPPORTUNITY_SCHEDULER_CLAIM_NAMESPACE}'
                    AND c.state_key = '{OPPORTUNITY_SCHEDULER_CLAIM_STATE_KEY}'
                    AND c.released_at IS NULL
                    AND c.lease_until > CURRENT_TIMESTAMP(6)
            ) THEN
                SIGNAL SQLSTATE '45000'
                    SET MESSAGE_TEXT = 'OpportunitySchedulerClaimRequired';
            END IF;
        END"""


_SCHEDULER_TABLES = (
    "opportunity_activation_states",
    "opportunity_publication_outbox",
)

_MYSQL_CLAIM_STATEMENTS = tuple(
    _claim_trigger(table, operation)
    for table in _SCHEDULER_TABLES
    for operation in ("INSERT", "UPDATE", "DELETE")
)

MYSQL_OPPORTUNITY_CLAIM_TRIGGERS = tuple(
    MySQLTriggerContract(
        f"{table}_claim_{operation.lower()}_v1",
        table,
        operation,
        "BEFORE",
        "OpportunitySchedulerClaimRequired",
    )
    for table in _SCHEDULER_TABLES
    for operation in ("INSERT", "UPDATE", "DELETE")
)

_MYSQL_MIGRATION_V1 = SchemaMigration(
    version=1,
    name="life_opportunity_storage_v1",
    statements=(
        *_MYSQL_TABLE_STATEMENTS,
        *_MYSQL_RUNTIME_TABLE_STATEMENTS,
        *_MYSQL_IMMUTABILITY_STATEMENTS,
        *_MYSQL_CLAIM_STATEMENTS,
    ),
)

_MYSQL_SHADOW_MIGRATION_V1 = SchemaMigration(
    version=1,
    name="life_opportunity_storage_shadow_v1",
    statements=(*_MYSQL_TABLE_STATEMENTS, *_MYSQL_RUNTIME_TABLE_STATEMENTS),
)

_MYSQL_MIGRATION_V2 = SchemaMigration(
    version=2,
    name="life_opportunity_runtime_marker_v2",
    statements=(
        _MYSQL_RUNTIME_META_STATEMENT,
        *_MYSQL_META_IMMUTABILITY_STATEMENTS,
    ),
)

_MYSQL_SHADOW_MIGRATION_V2 = SchemaMigration(
    version=2,
    name="life_opportunity_runtime_marker_shadow_v2",
    statements=(_MYSQL_RUNTIME_META_STATEMENT,),
)

_REQUIRED_TABLES = (
    "runtime_events",
    "opportunity_provider_events",
    "opportunity_provider_heads",
    "opportunity_workflow_versions",
    "opportunity_registration_events",
    "opportunity_registration_heads",
    "opportunity_occurrences",
    "opportunity_activation_states",
    "opportunity_publication_outbox",
    "opportunity_delivery_receipts",
    "opportunity_runtime_meta",
)


def _decode_runtime_marker(row: object) -> OpportunityRuntimeMarker:
    try:
        mapping = row._mapping  # type: ignore[attr-defined]
        return OpportunityRuntimeMarker(
            marker_key=str(mapping["marker_key"]),
            generation_id=str(mapping["generation_id"]),
            migration_occurrence_id=str(mapping["migration_occurrence_id"]),
            schema_version=int(mapping["schema_version"]),
            activated_at=_iso_time(mapping["activated_at"]),
            marker_sha256=str(mapping["marker_sha256"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("OpportunityRuntimeMarkerCorrupt") from exc


def _iso_time(value: object) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value or ""))
        except ValueError as exc:
            raise RuntimeError("OpportunityRuntimeMarkerTimeCorrupt") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


async def _runtime_meta_table_exists(
    runtime: StorageBackendRuntime,
    session: AsyncSession,
) -> bool:
    if runtime.backend == BackendKind.MYSQL:
        present = await session.scalar(
            text(
                """SELECT COUNT(*) FROM information_schema.tables
                WHERE table_schema=DATABASE()
                  AND table_name='opportunity_runtime_meta'"""
            )
        )
    else:
        present = await session.scalar(
            text(
                """SELECT COUNT(*) FROM sqlite_master
                WHERE type='table' AND name='opportunity_runtime_meta'"""
            )
        )
    return int(present or 0) == 1


async def read_opportunity_runtime_marker(
    runtime: StorageBackendRuntime,
) -> OpportunityRuntimeMarker | None:
    """Read the irreversible managed marker without creating any schema."""

    if not runtime.enabled or runtime.engine is None:
        raise OpportunitySchemaNotReady("OpportunitySchemaNotReady:runtime_disabled")
    async with runtime.unit_of_work() as uow:
        if not await _runtime_meta_table_exists(runtime, uow.session):
            return None
        rows = (
            await uow.session.execute(
                text(
                    """SELECT marker_key, generation_id,
                        migration_occurrence_id, schema_version,
                        activated_at, marker_sha256
                        FROM opportunity_runtime_meta ORDER BY marker_key LIMIT 2"""
                )
            )
        ).all()
    if not rows:
        return None
    if (
        len(rows) != 1
        or str(rows[0]._mapping["marker_key"]) != OPPORTUNITY_MANAGED_MARKER_KEY
    ):
        raise RuntimeError("OpportunityRuntimeMarkerCorrupt")
    return _decode_runtime_marker(rows[0])


async def mark_opportunity_runtime_managed(
    runtime: StorageBackendRuntime,
    *,
    migration_occurrence_id: str,
) -> OpportunityRuntimeMarker:
    """Insert the infrastructure cutover fact once under active authority."""

    identity = str(migration_occurrence_id or "").strip()
    if not identity or len(identity) > 255:
        raise ValueError("migration_occurrence_id must be 1..255 characters")
    if runtime.writer_role != StorageWriterRole.ACTIVE:
        raise RuntimeError("OpportunityRuntimeMarkerRequiresActiveAuthority")
    if runtime.generation is None:
        raise RuntimeError("OpportunityRuntimeMarkerRequiresGeneration")
    await verify_opportunity_schema(runtime, require_database_immutability=True)
    await runtime.validate_writer()
    for_update = " FOR UPDATE" if runtime.backend == BackendKind.MYSQL else ""
    async with runtime.unit_of_work() as uow:
        rows = (
            await uow.session.execute(
                text(
                    """SELECT marker_key, generation_id,
                        migration_occurrence_id, schema_version,
                        activated_at, marker_sha256
                        FROM opportunity_runtime_meta ORDER BY marker_key LIMIT 2"""
                    + for_update
                )
            )
        ).all()
        if rows:
            if len(rows) != 1:
                raise RuntimeError("OpportunityRuntimeMarkerCorrupt")
            existing = _decode_runtime_marker(rows[0])
            if existing.migration_occurrence_id != identity:
                raise OpportunityConflict(
                    scope="runtime_marker",
                    identity=identity,
                )
            result = existing
        else:
            database_now = await uow.session.scalar(
                text(
                    "SELECT CURRENT_TIMESTAMP(6)"
                    if runtime.backend == BackendKind.MYSQL
                    else "SELECT STRFTIME('%Y-%m-%dT%H:%M:%f+00:00', 'now')"
                )
            )
            marker = OpportunityRuntimeMarker(
                generation_id=runtime.generation.generation_id,
                migration_occurrence_id=identity,
                schema_version=OPPORTUNITY_SCHEMA_VERSION,
                activated_at=_iso_time(database_now),
            )
            prefix = (
                "INSERT IGNORE"
                if runtime.backend == BackendKind.MYSQL
                else "INSERT OR IGNORE"
            )
            inserted = await uow.session.execute(
                text(
                    f"""{prefix} INTO opportunity_runtime_meta (
                        marker_key, generation_id, migration_occurrence_id,
                        schema_version, activated_at, marker_sha256
                    ) VALUES (
                        :marker_key, :generation_id, :migration_occurrence_id,
                        :schema_version, :activated_at, :marker_sha256
                    )"""
                ),
                {
                    "marker_key": marker.marker_key,
                    "generation_id": marker.generation_id,
                    "migration_occurrence_id": marker.migration_occurrence_id,
                    "schema_version": marker.schema_version,
                    "activated_at": (
                        datetime.fromisoformat(marker.activated_at).replace(tzinfo=None)
                        if runtime.backend == BackendKind.MYSQL
                        else marker.activated_at
                    ),
                    "marker_sha256": marker.marker_sha256,
                },
            )
            persisted = (
                await uow.session.execute(
                    text(
                        """SELECT marker_key, generation_id,
                        migration_occurrence_id, schema_version,
                        activated_at, marker_sha256
                        FROM opportunity_runtime_meta
                        WHERE marker_key=:marker_key"""
                    ),
                    {"marker_key": OPPORTUNITY_MANAGED_MARKER_KEY},
                )
            ).one()
            result = _decode_runtime_marker(persisted)
            if result.migration_occurrence_id != identity:
                raise OpportunityConflict(scope="runtime_marker", identity=identity)
            if inserted.rowcount == 1 and result.marker_sha256 != marker.marker_sha256:
                raise RuntimeError("OpportunityRuntimeMarkerInsertCorrupt")
    await runtime.validate_writer()
    return result


def _normalize_sqlite_trigger_definition(definition: object) -> str:
    normalized = " ".join(str(definition or "").strip().rstrip(";").split())
    optional_prefix = "CREATE TRIGGER IF NOT EXISTS "
    if normalized.startswith(optional_prefix):
        return "CREATE TRIGGER " + normalized[len(optional_prefix) :]
    return normalized


async def _verify_local_immutability_triggers(session: AsyncSession) -> None:
    rows = (
        await session.execute(
            text(
                """SELECT name, tbl_name, sql FROM sqlite_master
                WHERE type='trigger'"""
            )
        )
    ).mappings()
    by_name = {str(row["name"]): row for row in rows}
    for contract in _LOCAL_IMMUTABILITY_TRIGGER_CONTRACTS:
        row = by_name.get(contract.name)
        if row is None:
            raise OpportunitySchemaNotReady(
                f"OpportunitySchemaNotReady:missing_trigger:{contract.name}"
            )
        if str(row["tbl_name"] or "") != contract.table:
            raise OpportunitySchemaNotReady(
                f"OpportunitySchemaNotReady:trigger_table_mismatch:{contract.name}"
            )
        if _normalize_sqlite_trigger_definition(
            row["sql"]
        ) != _normalize_sqlite_trigger_definition(contract.statement):
            raise OpportunitySchemaNotReady(
                f"OpportunitySchemaNotReady:trigger_definition_mismatch:{contract.name}"
            )


async def verify_opportunity_schema(
    runtime: StorageBackendRuntime,
    *,
    require_database_immutability: bool = True,
    require_scheduler_claim_guard: bool = False,
) -> None:
    """Fail closed when the selected backend was not explicitly migrated."""

    if not runtime.enabled or runtime.engine is None:
        raise OpportunitySchemaNotReady("OpportunitySchemaNotReady:runtime_disabled")
    async with runtime.unit_of_work() as uow:
        for table in _REQUIRED_TABLES:
            if runtime.backend == BackendKind.MYSQL:
                present = await uow.session.scalar(
                    text(
                        """SELECT COUNT(*) FROM information_schema.tables
                        WHERE table_schema = DATABASE() AND table_name = :table"""
                    ),
                    {"table": table},
                )
            else:
                present = await uow.session.scalar(
                    text(
                        """SELECT COUNT(*) FROM sqlite_master
                        WHERE type = 'table' AND name = :table"""
                    ),
                    {"table": table},
                )
            if int(present or 0) != 1:
                raise OpportunitySchemaNotReady(
                    f"OpportunitySchemaNotReady:missing_table:{table}"
                )
        if runtime.backend == BackendKind.LOCAL and require_database_immutability:
            await _verify_local_immutability_triggers(uow.session)
    if runtime.backend == BackendKind.MYSQL and require_database_immutability:
        await verify_mysql_trigger_contract(
            runtime.engine,
            (
                *_MYSQL_IMMUTABILITY_TRIGGERS,
                *_MYSQL_META_IMMUTABILITY_TRIGGERS,
            ),
        )
    if runtime.backend == BackendKind.MYSQL and require_scheduler_claim_guard:
        await verify_mysql_trigger_contract(
            runtime.engine,
            MYSQL_OPPORTUNITY_CLAIM_TRIGGERS,
        )


async def ensure_opportunity_schema(
    runtime: StorageBackendRuntime,
    *,
    require_database_immutability: bool = True,
) -> None:
    """Migration-only schema installation; business startup passes False."""

    if not runtime.enabled or runtime.engine is None:
        raise RuntimeError("opportunity schema requires an enabled runtime")
    if (
        not require_database_immutability
        and runtime.writer_role != StorageWriterRole.CANDIDATE_COPY
    ):
        raise RuntimeError(
            "opportunity immutability may be relaxed only for candidate copy"
        )
    if require_database_immutability:
        await ensure_singleton_writer_claim_schema(runtime)
    await runtime.validate_writer()
    if runtime.backend == BackendKind.MYSQL:
        suffix = "" if require_database_immutability else "_shadow"
        runner = MySQLMigrationRunner(
            runtime.engine,
            table_name=f"life_opportunity{suffix}_schema_migrations",
            lock_name=f"elysium:life-opportunity{suffix.replace('_', '-')}-schema",
        )
        await runner.apply(
            (_MYSQL_MIGRATION_V1, _MYSQL_MIGRATION_V2)
            if require_database_immutability
            else (_MYSQL_SHADOW_MIGRATION_V1, _MYSQL_SHADOW_MIGRATION_V2)
        )
    else:
        async with runtime.unit_of_work() as uow:
            for statement in LOCAL_OPPORTUNITY_SCHEMA_STATEMENTS:
                await uow.session.execute(text(statement))
            if require_database_immutability:
                for statement in LOCAL_OPPORTUNITY_IMMUTABILITY_STATEMENTS:
                    await uow.session.execute(text(statement))
    await runtime.validate_writer()
    await verify_opportunity_schema(
        runtime,
        require_database_immutability=require_database_immutability,
        require_scheduler_claim_guard=require_database_immutability,
    )


__all__ = [
    "LOCAL_OPPORTUNITY_IMMUTABILITY_STATEMENTS",
    "LOCAL_OPPORTUNITY_SCHEMA_STATEMENTS",
    "MYSQL_OPPORTUNITY_CLAIM_TRIGGERS",
    "OPPORTUNITY_SCHEMA_VERSION",
    "OpportunitySchemaNotReady",
    "ensure_opportunity_schema",
    "mark_opportunity_runtime_managed",
    "read_opportunity_runtime_marker",
    "verify_opportunity_schema",
]
