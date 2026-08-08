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

RUNTIME_STATE_SCHEMA_VERSION = 3
MULTI_WRITER_PROTOCOL_VERSION = 1

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
    """CREATE TABLE IF NOT EXISTS operations (
        operation_id TEXT PRIMARY KEY,
        operation_type TEXT NOT NULL,
        scope_key TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        status TEXT NOT NULL,
        claim_owner TEXT,
        claim_epoch INTEGER NOT NULL DEFAULT 0,
        lease_until TEXT,
        input_frontier_json TEXT NOT NULL,
        result_ref TEXT,
        result_sha256 TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(operation_type, scope_key, sequence)
    )""",
        """CREATE INDEX IF NOT EXISTS idx_operations_claim
        ON operations(operation_type, status, lease_until)""",
        """CREATE TABLE IF NOT EXISTS heartbeat_operations (
            heartbeat_operation_id TEXT PRIMARY KEY,
            consciousness_instance_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            input_frontier_json TEXT NOT NULL,
            prepared_context_digest TEXT,
            status TEXT NOT NULL,
            claim_owner TEXT,
            claim_epoch INTEGER NOT NULL DEFAULT 0,
            lease_until TEXT,
            model_request_id TEXT,
            result_ref TEXT,
            result_digest TEXT,
            committed_frontier INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(consciousness_instance_id, sequence)
        )""",
        """CREATE INDEX IF NOT EXISTS idx_heartbeat_operation_claim
            ON heartbeat_operations(consciousness_instance_id, status, lease_until)""",
        """CREATE TABLE IF NOT EXISTS inbound_messages (
            message_id TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            platform_event_id TEXT NOT NULL,
            occurrence_id TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            stream_id TEXT NOT NULL,
            reply_target TEXT NOT NULL,
            source TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            received_at TEXT NOT NULL,
            raw_payload_ref TEXT NOT NULL,
            UNIQUE(platform, platform_event_id),
            UNIQUE(source, occurrence_id)
        )""",
        """CREATE TABLE IF NOT EXISTS stream_turns (
            turn_id TEXT PRIMARY KEY,
            stream_id TEXT NOT NULL,
            stream_sequence INTEGER NOT NULL,
            source_message_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            claim_owner TEXT,
            claim_epoch INTEGER NOT NULL DEFAULT 0,
            lease_until TEXT,
            input_frontier_json TEXT NOT NULL,
            result_ref TEXT,
            result_digest TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(stream_id, stream_sequence)
        )""",
        """CREATE INDEX IF NOT EXISTS idx_stream_turn_claim
            ON stream_turns(stream_id, status, lease_until)""",
        """CREATE TABLE IF NOT EXISTS operation_receipts (
        operation_id TEXT PRIMARY KEY,
        commit_revision INTEGER NOT NULL,
        result_sha256 TEXT NOT NULL,
        committed_by TEXT NOT NULL,
        committed_at TEXT NOT NULL
    )""",
        """CREATE TABLE IF NOT EXISTS outbox_actions (
            action_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            source_event_id TEXT NOT NULL,
            stream_id TEXT NOT NULL,
            target TEXT NOT NULL,
            payload_ref TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            status TEXT NOT NULL,
            claim_owner TEXT,
            claim_epoch INTEGER NOT NULL DEFAULT 0,
            lease_until TEXT,
            provider_request_id TEXT,
            provider_receipt_id TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error_type TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS idx_outbox_claim
            ON outbox_actions(status, lease_until)""",
        """CREATE INDEX IF NOT EXISTS idx_outbox_stream
            ON outbox_actions(stream_id, created_at)""",
        """CREATE TABLE IF NOT EXISTS projection_progress (
            projection_name TEXT NOT NULL,
            projection_node_id TEXT NOT NULL,
            source_frontier INTEGER NOT NULL,
            source_digest TEXT NOT NULL,
            config_digest TEXT NOT NULL,
            status TEXT NOT NULL,
            last_success_at TEXT,
            backlog INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(projection_name, projection_node_id)
        )""",
        """CREATE TABLE IF NOT EXISTS runtime_deltas (
            delta_id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL,
        namespace TEXT NOT NULL,
        state_key TEXT NOT NULL,
        delta_type TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        actor TEXT NOT NULL,
        source TEXT NOT NULL,
        causation_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(operation_id, delta_type)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_runtime_deltas_state
        ON runtime_deltas(namespace, state_key, created_at)""",
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

MYSQL_MULTI_WRITER_MIGRATION = SchemaMigration(
    version=3,
    name="life_multi_writer_operations_v1",
    statements=(
        """CREATE TABLE IF NOT EXISTS operations (
            operation_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            operation_type VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            scope_key VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            sequence BIGINT NOT NULL,
            status VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            claim_owner VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL,
            claim_epoch BIGINT UNSIGNED NOT NULL DEFAULT 0,
            lease_until DATETIME(6) NULL,
            input_frontier_json JSON NOT NULL,
            result_ref VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL,
            result_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
            attempts INT UNSIGNED NOT NULL DEFAULT 0,
            created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            PRIMARY KEY (operation_id),
            UNIQUE KEY uq_operations_scope_sequence (operation_type, scope_key, sequence),
            KEY idx_operations_claim (operation_type, status, lease_until)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS inbound_messages (
            message_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            platform VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            platform_event_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            stream_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            reply_target VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            source VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            occurred_at DATETIME(6) NOT NULL,
            received_at DATETIME(6) NOT NULL,
            raw_payload_ref VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            PRIMARY KEY(message_id),
            UNIQUE KEY uq_inbound_platform_event(platform, platform_event_id),
            UNIQUE KEY uq_inbound_source_occurrence(source, occurrence_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS stream_turns (
            turn_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            stream_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            stream_sequence BIGINT UNSIGNED NOT NULL,
            source_message_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            status VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            claim_owner VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL,
            claim_epoch BIGINT UNSIGNED NOT NULL DEFAULT 0,
            lease_until DATETIME(6) NULL,
            input_frontier_json JSON NOT NULL,
            result_ref VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL,
            result_digest CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
            attempts INT UNSIGNED NOT NULL DEFAULT 0,
            created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            PRIMARY KEY(turn_id),
            UNIQUE KEY uq_stream_turn_sequence(stream_id, stream_sequence),
            UNIQUE KEY uq_stream_turn_message(source_message_id),
            KEY idx_stream_turn_claim(stream_id, status, lease_until)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS heartbeat_operations (
            heartbeat_operation_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            consciousness_instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            sequence BIGINT UNSIGNED NOT NULL,
            input_frontier_json JSON NOT NULL,
            prepared_context_digest CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
            status VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            claim_owner VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL,
            claim_epoch BIGINT UNSIGNED NOT NULL DEFAULT 0,
            lease_until DATETIME(6) NULL,
            model_request_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL,
            result_ref VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL,
            result_digest CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
            committed_frontier BIGINT UNSIGNED NULL,
            attempts INT UNSIGNED NOT NULL DEFAULT 0,
            created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            PRIMARY KEY(heartbeat_operation_id),
            UNIQUE KEY uq_heartbeat_instance_sequence(consciousness_instance_id, sequence),
            KEY idx_heartbeat_claim(consciousness_instance_id, status, lease_until)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS operation_receipts (
            operation_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            commit_revision BIGINT UNSIGNED NOT NULL,
            result_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            committed_by VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            committed_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            PRIMARY KEY (operation_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS outbox_actions (
            action_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            idempotency_key VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            source_event_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            stream_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            target VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            payload_ref VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            status VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            claim_owner VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL,
            claim_epoch BIGINT UNSIGNED NOT NULL DEFAULT 0,
            lease_until DATETIME(6) NULL,
            provider_request_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL,
            provider_receipt_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL,
            attempts INT UNSIGNED NOT NULL DEFAULT 0,
            last_error_type VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NULL,
            created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            PRIMARY KEY(action_id),
            UNIQUE KEY uq_outbox_idempotency(idempotency_key),
            KEY idx_outbox_claim(status, lease_until),
            KEY idx_outbox_stream(stream_id, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS projection_progress (
            projection_name VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            projection_node_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            source_frontier BIGINT UNSIGNED NOT NULL,
            source_digest CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            config_digest CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            status VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            last_success_at DATETIME(6) NULL,
            backlog BIGINT UNSIGNED NOT NULL DEFAULT 0,
            PRIMARY KEY(projection_name, projection_node_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS runtime_deltas (
            delta_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            operation_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            namespace VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            state_key VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            delta_type VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            schema_version INT UNSIGNED NOT NULL,
            payload_json JSON NOT NULL,
            payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            actor VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            source VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            causation_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            UNIQUE KEY uq_runtime_deltas_operation_type (operation_id, delta_type),
            KEY idx_runtime_deltas_state (namespace, state_key, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    ),
)

MYSQL_RUNTIME_STATE_CLAIM_GUARD_MIGRATION = SchemaMigration(
    version=2,
    name="life_runtime_state_singleton_claim_guard_v2",
    statements=(
        _MYSQL_RUNTIME_STATE_CLAIM_GUARD_INSERT,
        _MYSQL_RUNTIME_STATE_CLAIM_GUARD_UPDATE,
        _MYSQL_RUNTIME_STATE_CLAIM_GUARD_DELETE,
    ),
)

# Multi-writer retirement (spec section 16.2 step 8): the legacy
# generation-scoped singleton claim guard for ``life_engine.runtime_context``
# must leave the database.  ``runtime_states`` is written by every concurrent
# node through typed deltas (``commit_runtime_delta``) or CAS
# ``put_state``; the claim triggers below would otherwise reject every write
# from a node that does not hold the retired global singleton claim.
#
# The generic claim infrastructure (claims/bindings/events tables and the
# writer-event immutability triggers) stays; only these three table guards are
# dropped, idempotently.
MYSQL_RUNTIME_STATE_CLAIM_GUARD_RETIREMENT = SchemaMigration(
    version=4,
    name="life_runtime_state_singleton_claim_guard_retirement_v4",
    statements=(
        """DROP TRIGGER IF EXISTS runtime_states_singleton_claim_insert_v2""",
        """DROP TRIGGER IF EXISTS runtime_states_singleton_claim_update_v2""",
        """DROP TRIGGER IF EXISTS runtime_states_singleton_claim_delete_v2""",
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
                MYSQL_MULTI_WRITER_MIGRATION,
                MYSQL_RUNTIME_STATE_CLAIM_GUARD_MIGRATION,
                MYSQL_RUNTIME_STATE_CLAIM_GUARD_RETIREMENT,
            )
        )
        # The legacy claim guards are intentionally retired (dropped by v4);
        # only the append-only immutability triggers remain part of the
        # verified contract.
        await verify_mysql_trigger_contract(
            runtime.engine,
            MYSQL_RUNTIME_EVENT_TRIGGERS,
        )
    else:
        async with runtime.unit_of_work() as uow:
            for statement in LOCAL_RUNTIME_STATE_SCHEMA_STATEMENTS:
                await uow.session.execute(text(statement))
    await runtime.validate_writer()


__all__ = [
    "LOCAL_RUNTIME_STATE_SCHEMA_STATEMENTS",
    "MYSQL_MULTI_WRITER_MIGRATION",
    "MYSQL_RUNTIME_EVENT_TRIGGERS",
    "MULTI_WRITER_PROTOCOL_VERSION",
    "MYSQL_RUNTIME_STATE_CLAIM_GUARD_MIGRATION",
    "MYSQL_RUNTIME_STATE_CLAIM_GUARD_RETIREMENT",
    "MYSQL_RUNTIME_STATE_CLAIM_GUARD_TRIGGERS",
    "MYSQL_RUNTIME_STATE_MIGRATION",
    "RUNTIME_STATE_SCHEMA_VERSION",
    "ensure_runtime_state_schema",
]
