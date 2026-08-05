"""Versioned MySQL schema for selectable Life Memory storage.

The schema deliberately keeps each memory ontology in explicit tables.
Canonical JSON text is used only for open metadata and ordered reference
collections; it is never used as a universal record envelope that would erase
domain constraints.  LONGTEXT preserves the exact canonical decimal spelling
that MySQL's native JSON binary representation would otherwise round.
"""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.kernel.storage.migration_runner import MySQLMigrationRunner, SchemaMigration

from ..contracts import StorageBackendRuntime, StorageWriterRole
from ..models import BackendKind

MEMORY_SCHEMA_VERSION = 8
MEMORY_IMMUTABILITY_SCHEMA_VERSION = 1

# Database immutability follows the Memory Port contract, not a blanket
# "nothing may change" rule.  These tables contain authoritative occurrences
# whose identity and payload are append-only.  Rebuildable projections,
# monotonic cursors and CAS heads are classified separately below.
MEMORY_IMMUTABLE_TABLES = (
    "memory_experiences",
    "memory_experience_occurrence_aliases",
    "memory_witness_sources",
    "memory_witness_migrations",
    "memory_artifact_versions",
    "memory_artifact_derivations",
    "memory_interpretations",
    "memory_interpretation_sources",
    "memory_semantic_relations",
    "memory_recall_sessions",
    "memory_recall_events",
    "memory_corecall_events",
    "memory_claims",
    "memory_claim_evidence",
    "memory_beliefs",
    "memory_epistemic_conflicts",
    "memory_state_events",
    "memory_retrieval_episodes",
    "memory_retrieval_exposures",
    "memory_retrieval_feedback",
)

# A witness row mixes immutable first-person testimony with mutable delivery
# bookkeeping.  Its update trigger protects only the authority columns and
# deliberately permits the projection columns listed here.
MEMORY_WITNESS_IMMUTABLE_COLUMNS = (
    "witness_id",
    "content",
    "consciousness_instance_id",
    "perspective_subject_id",
    "epistemic_kind",
    "source_kind",
    "status",
    "stream_scope",
    "visibility",
    "valid_from",
    "valid_to",
    "recorded_at",
    "source_sequence_start",
    "source_sequence_end",
    "model_task_name",
    "metadata_json",
    "payload_sha256",
)
MEMORY_WITNESS_MUTABLE_PROJECTION_COLUMNS = (
    "projection_path",
    "projection_path_sha256",
    "projection_status",
    "projection_error",
)

MEMORY_MUTABLE_TABLES = (
    "memory_schema",
    "memory_nodes",
    "memory_chunks",
    "memory_index_jobs",
    "memory_index_state",
    "memory_vector_tombstones",
    "memory_witness_state",
    "memory_artifact_heads",
    "memory_association_projection",
    "memory_edges",
    "memory_corrections",
)


class MemoryImmutabilityPolicyError(RuntimeError):
    """Raised when a caller attempts to weaken an activation-safe schema."""


class MemoryDatabaseImmutabilityError(RuntimeError):
    """Raised when the installed MySQL authority guards are absent or drifted."""


_DOCUMENT_INDEX = SchemaMigration(
    version=1,
    name="life_memory_document_projection_v1",
    statements=(
        """CREATE TABLE IF NOT EXISTS memory_schema (
            schema_name VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            version BIGINT UNSIGNED NOT NULL,
            metadata_json JSON NOT NULL,
            updated_at DOUBLE NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS memory_nodes (
            node_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            node_type VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            file_path TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL,
            file_path_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
            content_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
            document_content LONGTEXT NOT NULL,
            title TEXT NOT NULL,
            activation_strength DOUBLE NOT NULL DEFAULT 1.0,
            access_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
            last_accessed_at DOUBLE NULL,
            emotional_valence DOUBLE NOT NULL DEFAULT 0.0,
            emotional_arousal DOUBLE NOT NULL DEFAULT 0.0,
            importance DOUBLE NOT NULL DEFAULT 0.5,
            created_at DOUBLE NOT NULL,
            updated_at DOUBLE NOT NULL,
            embedding_synced BOOLEAN NOT NULL DEFAULT FALSE,
            source_mtime DOUBLE NULL,
            index_revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
            UNIQUE KEY uq_memory_nodes_file_path_hash (file_path_sha256),
            KEY idx_memory_nodes_type (node_type),
            KEY idx_memory_nodes_activation (activation_strength)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS memory_chunks (
            chunk_id VARCHAR(512) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            node_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            chunk_index BIGINT UNSIGNED NOT NULL,
            content_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            content LONGTEXT NOT NULL,
            title TEXT NOT NULL,
            created_at DOUBLE NOT NULL,
            updated_at DOUBLE NOT NULL,
            UNIQUE KEY uq_memory_chunk_ordinal (node_id, chunk_index),
            KEY idx_memory_chunks_node (node_id),
            FULLTEXT KEY ft_memory_chunks_content (title, content),
            CONSTRAINT fk_memory_chunks_node FOREIGN KEY (node_id)
                REFERENCES memory_nodes(node_id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS memory_index_jobs (
            job_id VARCHAR(512) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            node_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            content_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            status VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            created_at DOUBLE NOT NULL,
            updated_at DOUBLE NOT NULL,
            attempts BIGINT UNSIGNED NOT NULL DEFAULT 0,
            error TEXT NOT NULL,
            index_revision BIGINT UNSIGNED NOT NULL,
            KEY idx_memory_jobs_claim (status, updated_at, job_id),
            KEY idx_memory_jobs_node (node_id, index_revision),
            CONSTRAINT fk_memory_jobs_node FOREIGN KEY (node_id)
                REFERENCES memory_nodes(node_id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS memory_index_state (
            state_key VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            collection_name VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            model_name VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            dimension BIGINT UNSIGNED NOT NULL,
            version BIGINT UNSIGNED NOT NULL,
            updated_at DOUBLE NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS memory_vector_tombstones (
            tombstone_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            node_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            chunk_id VARCHAR(512) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            collection_name VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            created_at DOUBLE NOT NULL,
            consumed_at DOUBLE NULL,
            KEY idx_memory_tombstones_pending (consumed_at, tombstone_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    ),
)

_EXPERIENCE = SchemaMigration(
    version=2,
    name="life_memory_experience_ledger_v1",
    statements=(
        """CREATE TABLE IF NOT EXISTS memory_experiences (
            event_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY,
            source_event_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            sequence BIGINT UNSIGNED NOT NULL,
            occurred_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            recorded_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            source VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            channel VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            event_type VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            content LONGTEXT NOT NULL,
            stream_id VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            consciousness_instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            actor VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            visibility VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            valid_from VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            valid_to VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            metadata_json JSON NOT NULL,
            payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            KEY idx_memory_experiences_sequence (sequence, event_id),
            KEY idx_memory_experiences_source (source_event_id, occurred_at, event_id),
            KEY idx_memory_experiences_stream (stream_id, sequence)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS memory_experience_occurrence_aliases (
            occurrence_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY,
            event_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            source_event_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            ingest_position BIGINT UNSIGNED NOT NULL,
            recorded_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            KEY idx_memory_experience_alias_event (event_id, ingest_position),
            CONSTRAINT fk_memory_experience_alias_event FOREIGN KEY (event_id)
                REFERENCES memory_experiences(event_id) ON DELETE RESTRICT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    ),
)

_WITNESS = SchemaMigration(
    version=3,
    name="life_memory_witness_ledger_v1",
    statements=(
        """CREATE TABLE IF NOT EXISTS memory_witnesses (
            witness_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            content LONGTEXT NOT NULL,
            consciousness_instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            perspective_subject_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            epistemic_kind VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            source_kind VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            status VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            stream_scope VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            visibility VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            valid_from VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            valid_to VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            recorded_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            source_sequence_start BIGINT UNSIGNED NOT NULL,
            source_sequence_end BIGINT UNSIGNED NOT NULL,
            model_task_name VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            projection_path VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL,
            projection_path_sha256 CHAR(64)
                CHARACTER SET ascii COLLATE ascii_bin NULL,
            projection_status VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            projection_error TEXT NOT NULL,
            metadata_json JSON NOT NULL,
            payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            UNIQUE KEY uq_memory_witness_projection_path_hash (
                projection_path_sha256
            ),
            KEY idx_memory_witness_scope_time (stream_scope, recorded_at),
            KEY idx_memory_witness_status (status, epistemic_kind)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS memory_witness_sources (
            witness_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            event_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            ordinal BIGINT UNSIGNED NOT NULL,
            PRIMARY KEY (witness_id, event_id),
            KEY idx_memory_witness_source_event (event_id, witness_id),
            CONSTRAINT fk_memory_witness_source_witness FOREIGN KEY (witness_id)
                REFERENCES memory_witnesses(witness_id) ON DELETE CASCADE,
            CONSTRAINT fk_memory_witness_source_event FOREIGN KEY (event_id)
                REFERENCES memory_experiences(event_id) ON DELETE RESTRICT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS memory_witness_state (
            consciousness_instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY,
            last_sequence BIGINT UNSIGNED NOT NULL,
            revision BIGINT UNSIGNED NOT NULL,
            last_run_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            last_success_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            last_error TEXT NOT NULL,
            updated_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS memory_witness_migrations (
            migration_key VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY,
            source_path TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            source_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            witness_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            migrated_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            CONSTRAINT fk_memory_witness_migration FOREIGN KEY (witness_id)
                REFERENCES memory_witnesses(witness_id) ON DELETE RESTRICT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    ),
)

_LIVING = SchemaMigration(
    version=4,
    name="life_memory_living_ledgers_v1",
    statements=(
        """CREATE TABLE IF NOT EXISTS memory_artifact_versions (
            artifact_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            logical_key VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            logical_key_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            artifact_kind VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            content LONGTEXT NOT NULL,
            content_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            recorded_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            valid_from VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            valid_to VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            authored_by VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            consciousness_instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            stream_scope VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            visibility VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            parent_artifact_ids_json JSON NOT NULL,
            metadata_json JSON NOT NULL,
            payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            KEY idx_memory_artifact_logical (logical_key_sha256, recorded_at, artifact_id),
            KEY idx_memory_artifact_hash (content_hash, logical_key_sha256)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS memory_artifact_derivations (
            derivation_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            generated_artifact_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            used_artifact_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            predicate VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            reason TEXT NOT NULL,
            actor VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            recorded_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            metadata_json JSON NOT NULL,
            payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            KEY idx_memory_derivation_generated (generated_artifact_id, recorded_at),
            KEY idx_memory_derivation_used (used_artifact_id, recorded_at),
            CONSTRAINT chk_memory_derivation_distinct CHECK (generated_artifact_id <> used_artifact_id),
            CONSTRAINT fk_memory_derivation_generated FOREIGN KEY (generated_artifact_id)
                REFERENCES memory_artifact_versions(artifact_id) ON DELETE RESTRICT,
            CONSTRAINT fk_memory_derivation_used FOREIGN KEY (used_artifact_id)
                REFERENCES memory_artifact_versions(artifact_id) ON DELETE RESTRICT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS memory_artifact_heads (
            logical_key_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            logical_key VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            artifact_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            projected_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            revision BIGINT UNSIGNED NOT NULL,
            CONSTRAINT fk_memory_artifact_head FOREIGN KEY (artifact_id)
                REFERENCES memory_artifact_versions(artifact_id) ON DELETE RESTRICT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS memory_interpretations (
            interpretation_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            subject_id VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            content LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            authored_by VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            consciousness_instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            recorded_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            valid_from VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            valid_to VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            stream_scope VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            visibility VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            metadata_json JSON NOT NULL,
            payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            KEY idx_memory_interpretation_subject (subject_id, recorded_at, interpretation_id),
            FULLTEXT KEY ft_memory_interpretation_content (subject_id, content)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS memory_interpretation_sources (
            interpretation_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            entity_ref VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            entity_ref_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            predicate VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            ordinal BIGINT UNSIGNED NOT NULL,
            metadata_json JSON NOT NULL,
            payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            PRIMARY KEY (interpretation_id, entity_ref_sha256, predicate),
            KEY idx_memory_interpretation_source_entity (entity_ref_sha256, interpretation_id),
            CONSTRAINT fk_memory_interpretation_source FOREIGN KEY (interpretation_id)
                REFERENCES memory_interpretations(interpretation_id) ON DELETE RESTRICT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS memory_semantic_relations (
            relation_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            source_ref VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            source_ref_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            target_ref VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            target_ref_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            predicate VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            reason TEXT NOT NULL,
            actor VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            recorded_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            consciousness_instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            stream_scope VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            metadata_json JSON NOT NULL,
            payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            KEY idx_memory_relation_source (source_ref_sha256, recorded_at),
            KEY idx_memory_relation_target (target_ref_sha256, recorded_at),
            CONSTRAINT chk_memory_relation_distinct CHECK (source_ref_sha256 <> target_ref_sha256)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS memory_recall_sessions (
            episode_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            query LONGTEXT NOT NULL,
            retrieval_intent VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            consciousness_instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            stream_scope VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            context_key VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            policy_version VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            random_seed BIGINT NOT NULL,
            recorded_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            context_json JSON NOT NULL,
            payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            KEY idx_memory_recall_time (recorded_at, episode_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS memory_recall_events (
            event_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            episode_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            action VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            entity_ref VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            ordinal BIGINT UNSIGNED NOT NULL,
            source VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            reason TEXT NOT NULL,
            recorded_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            metadata_json JSON NOT NULL,
            payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            KEY idx_memory_recall_event_episode (episode_id, ordinal, recorded_at, event_id),
            CONSTRAINT fk_memory_recall_event_episode FOREIGN KEY (episode_id)
                REFERENCES memory_recall_sessions(episode_id) ON DELETE RESTRICT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS memory_corecall_events (
            corecall_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            episode_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            context_key VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            `signal` VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            entity_refs_json JSON NOT NULL,
            actor VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            reason TEXT NOT NULL,
            recorded_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            metadata_json JSON NOT NULL,
            payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            KEY idx_memory_corecall_episode (episode_id, recorded_at, corecall_id),
            CONSTRAINT fk_memory_corecall_episode FOREIGN KEY (episode_id)
                REFERENCES memory_recall_sessions(episode_id) ON DELETE RESTRICT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS memory_association_projection (
            source_ref_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            source_ref VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            target_ref_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            target_ref VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            context_key_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            context_key VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            `signal` VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            signal_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            event_count BIGINT UNSIGNED NOT NULL,
            last_event_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            PRIMARY KEY (source_ref_sha256, target_ref_sha256, context_key_sha256, signal_sha256),
            KEY idx_memory_association_target (target_ref_sha256, context_key_sha256)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    ),
)

_EPISTEMIC = SchemaMigration(
    version=5,
    name="life_memory_epistemic_ledgers_v1",
    statements=(
        """CREATE TABLE IF NOT EXISTS memory_claims (
            claim_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            subject_key VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            subject_key_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            content LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            claim_kind VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            source VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            authority VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            valid_from VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            valid_to VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            recorded_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            stream_scope VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            visibility VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            consciousness_instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            metadata_json JSON NOT NULL,
            payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            KEY idx_memory_claim_subject (subject_key_sha256, recorded_at, claim_id),
            KEY idx_memory_claim_scope (stream_scope, visibility),
            FULLTEXT KEY ft_memory_claim_content (subject_key, content)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS memory_claim_evidence (
            evidence_link_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            claim_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            evidence_kind VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            evidence_ref VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            evidence_ref_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            stance VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            source_excerpt LONGTEXT NOT NULL,
            recorded_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            metadata_json JSON NOT NULL,
            payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            KEY idx_memory_claim_evidence_claim (claim_id, recorded_at),
            KEY idx_memory_claim_evidence_ref (evidence_kind, evidence_ref_sha256),
            CONSTRAINT fk_memory_claim_evidence_claim FOREIGN KEY (claim_id)
                REFERENCES memory_claims(claim_id) ON DELETE RESTRICT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS memory_beliefs (
            belief_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            claim_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            perspective_subject_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            consciousness_instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            recorded_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            metadata_json JSON NOT NULL,
            payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            UNIQUE KEY uq_memory_belief_perspective (claim_id, perspective_subject_id, consciousness_instance_id),
            CONSTRAINT fk_memory_belief_claim FOREIGN KEY (claim_id)
                REFERENCES memory_claims(claim_id) ON DELETE RESTRICT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS memory_epistemic_conflicts (
            conflict_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            left_claim_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            right_claim_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            relation VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            reason TEXT NOT NULL,
            recorded_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            detected_by VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            metadata_json JSON NOT NULL,
            payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            KEY idx_memory_conflict_left (left_claim_id, recorded_at),
            KEY idx_memory_conflict_right (right_claim_id, recorded_at),
            CONSTRAINT chk_memory_conflict_distinct CHECK (left_claim_id <> right_claim_id),
            CONSTRAINT fk_memory_conflict_left FOREIGN KEY (left_claim_id)
                REFERENCES memory_claims(claim_id) ON DELETE RESTRICT,
            CONSTRAINT fk_memory_conflict_right FOREIGN KEY (right_claim_id)
                REFERENCES memory_claims(claim_id) ON DELETE RESTRICT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS memory_state_events (
            event_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            entity_type VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            entity_id VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            entity_id_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            event_type VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            actor VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            authority VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            reason TEXT NOT NULL,
            recorded_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            valid_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            caused_by_event_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            reverses_event_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            payload_json JSON NOT NULL,
            payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            KEY idx_memory_state_entity (entity_type, entity_id_sha256, recorded_at, event_id),
            KEY idx_memory_state_reverse (reverses_event_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS memory_retrieval_episodes (
            episode_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            query LONGTEXT NOT NULL,
            mode VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            consciousness_instance_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            stream_scope VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            recorded_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            metadata_json JSON NOT NULL,
            payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            KEY idx_memory_retrieval_episode_time (recorded_at, episode_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS memory_retrieval_exposures (
            exposure_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            episode_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            entity_type VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            entity_id VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            entity_id_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            rank_position BIGINT UNSIGNED NOT NULL,
            retrieval_source VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            recorded_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            feedback VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            feedback_reason TEXT NOT NULL,
            feedback_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            metadata_json JSON NOT NULL,
            payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            UNIQUE KEY uq_memory_retrieval_entity (episode_id, entity_type, entity_id_sha256),
            KEY idx_memory_retrieval_exposure_entity (entity_type, entity_id_sha256, recorded_at),
            CONSTRAINT fk_memory_retrieval_exposure_episode FOREIGN KEY (episode_id)
                REFERENCES memory_retrieval_episodes(episode_id) ON DELETE RESTRICT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS memory_retrieval_feedback (
            feedback_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            exposure_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            feedback VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            actor VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            reason TEXT NOT NULL,
            recorded_at VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            metadata_json JSON NOT NULL,
            payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            KEY idx_memory_retrieval_feedback_exposure (exposure_id, recorded_at),
            CONSTRAINT fk_memory_retrieval_feedback_exposure FOREIGN KEY (exposure_id)
                REFERENCES memory_retrieval_exposures(exposure_id) ON DELETE RESTRICT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    ),
)

_LEGACY_GRAPH = SchemaMigration(
    version=6,
    name="life_memory_legacy_graph_v1",
    statements=(
        """CREATE TABLE IF NOT EXISTS memory_edges (
            edge_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            source_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            target_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            edge_type VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            weight DOUBLE NOT NULL DEFAULT 0.5,
            base_strength DOUBLE NOT NULL DEFAULT 0.5,
            reinforcement DOUBLE NOT NULL DEFAULT 0.0,
            activation_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
            last_activated_at DOUBLE NULL,
            reason TEXT NOT NULL,
            created_at DOUBLE NOT NULL,
            bidirectional BOOLEAN NOT NULL DEFAULT TRUE,
            UNIQUE KEY uq_memory_edge_identity (source_id, target_id, edge_type),
            KEY idx_memory_edge_source (source_id, weight),
            KEY idx_memory_edge_target (target_id),
            CONSTRAINT chk_memory_edge_no_self_loop CHECK (source_id <> target_id),
            CONSTRAINT fk_memory_edge_source FOREIGN KEY (source_id)
                REFERENCES memory_nodes(node_id) ON DELETE CASCADE,
            CONSTRAINT fk_memory_edge_target FOREIGN KEY (target_id)
                REFERENCES memory_nodes(node_id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS memory_corrections (
            correction_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            topic VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            topic_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            message LONGTEXT NOT NULL,
            source VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            created_at DOUBLE NOT NULL,
            related_node_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NULL,
            query TEXT NOT NULL,
            stream_id VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL,
            KEY idx_memory_correction_topic (topic_sha256),
            KEY idx_memory_correction_related (related_node_id),
            KEY idx_memory_correction_created (created_at),
            CONSTRAINT fk_memory_correction_node FOREIGN KEY (related_node_id)
                REFERENCES memory_nodes(node_id) ON DELETE SET NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    ),
)

_NODE_HISTORY = SchemaMigration(
    version=7,
    name="life_memory_node_history_v1",
    statements=(
        """ALTER TABLE memory_nodes
            ADD COLUMN event_date VARCHAR(64)
                CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL,
            ADD COLUMN is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN fts_content_hash CHAR(64)
                CHARACTER SET ascii COLLATE ascii_bin NULL,
            ADD COLUMN embedding_content_hash CHAR(64)
                CHARACTER SET ascii COLLATE ascii_bin NULL,
            ADD COLUMN embedding_model VARCHAR(255)
                CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT '',
            ADD COLUMN embedding_updated_at DOUBLE NULL,
            ADD COLUMN legacy_fts_present BOOLEAN NOT NULL DEFAULT FALSE""",
        """CREATE INDEX idx_memory_nodes_live_type
            ON memory_nodes (is_deleted, node_type)""",
    ),
)

_LOSSLESS_JSON_TEXT = SchemaMigration(
    version=8,
    name="life_memory_lossless_json_text_v1",
    statements=(
        "ALTER TABLE memory_schema MODIFY COLUMN metadata_json LONGTEXT NOT NULL",
        "ALTER TABLE memory_experiences MODIFY COLUMN metadata_json LONGTEXT NOT NULL",
        "ALTER TABLE memory_witnesses MODIFY COLUMN metadata_json LONGTEXT NOT NULL",
        """ALTER TABLE memory_artifact_versions
            MODIFY COLUMN parent_artifact_ids_json LONGTEXT NOT NULL,
            MODIFY COLUMN metadata_json LONGTEXT NOT NULL""",
        "ALTER TABLE memory_artifact_derivations MODIFY COLUMN metadata_json LONGTEXT NOT NULL",
        "ALTER TABLE memory_interpretations MODIFY COLUMN metadata_json LONGTEXT NOT NULL",
        "ALTER TABLE memory_interpretation_sources MODIFY COLUMN metadata_json LONGTEXT NOT NULL",
        "ALTER TABLE memory_semantic_relations MODIFY COLUMN metadata_json LONGTEXT NOT NULL",
        "ALTER TABLE memory_recall_sessions MODIFY COLUMN context_json LONGTEXT NOT NULL",
        "ALTER TABLE memory_recall_events MODIFY COLUMN metadata_json LONGTEXT NOT NULL",
        """ALTER TABLE memory_corecall_events
            MODIFY COLUMN entity_refs_json LONGTEXT NOT NULL,
            MODIFY COLUMN metadata_json LONGTEXT NOT NULL""",
        "ALTER TABLE memory_claims MODIFY COLUMN metadata_json LONGTEXT NOT NULL",
        "ALTER TABLE memory_claim_evidence MODIFY COLUMN metadata_json LONGTEXT NOT NULL",
        "ALTER TABLE memory_beliefs MODIFY COLUMN metadata_json LONGTEXT NOT NULL",
        "ALTER TABLE memory_epistemic_conflicts MODIFY COLUMN metadata_json LONGTEXT NOT NULL",
        "ALTER TABLE memory_state_events MODIFY COLUMN payload_json LONGTEXT NOT NULL",
        "ALTER TABLE memory_retrieval_episodes MODIFY COLUMN metadata_json LONGTEXT NOT NULL",
        "ALTER TABLE memory_retrieval_exposures MODIFY COLUMN metadata_json LONGTEXT NOT NULL",
        "ALTER TABLE memory_retrieval_feedback MODIFY COLUMN metadata_json LONGTEXT NOT NULL",
    ),
)

MEMORY_MIGRATIONS = (
    _DOCUMENT_INDEX,
    _EXPERIENCE,
    _WITNESS,
    _LIVING,
    _EPISTEMIC,
    _LEGACY_GRAPH,
    _NODE_HISTORY,
    _LOSSLESS_JSON_TEXT,
)


def _created_table_columns() -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for migration in MEMORY_MIGRATIONS:
        for statement in migration.statements:
            table_match = re.match(
                r"CREATE TABLE IF NOT EXISTS ([a-z0-9_]+) \(",
                statement,
            )
            if table_match is None:
                continue
            columns: list[str] = []
            for line in statement.splitlines()[1:]:
                column_match = re.match(
                    r"^ {12}(`?[a-z][a-z0-9_]*`?)\s+",
                    line,
                )
                if column_match is None:
                    continue
                column = column_match.group(1).strip("`")
                if column.upper() in {
                    "CHECK",
                    "CONSTRAINT",
                    "FOREIGN",
                    "FULLTEXT",
                    "KEY",
                    "PRIMARY",
                    "UNIQUE",
                }:
                    continue
                columns.append(column)
            result[table_match.group(1)] = tuple(columns)
    return result


_CREATED_TABLE_COLUMNS = _created_table_columns()
MEMORY_IMMUTABLE_TABLE_COLUMNS = {
    table: _CREATED_TABLE_COLUMNS[table] for table in MEMORY_IMMUTABLE_TABLES
}


def _memory_immutability_trigger_contract() -> tuple[tuple[str, str, str], ...]:
    triggers = [
        (f"{table}_immutable_update", "UPDATE", table)
        for table in MEMORY_IMMUTABLE_TABLES
    ]
    triggers.extend(
        (f"{table}_immutable_delete", "DELETE", table)
        for table in MEMORY_IMMUTABLE_TABLES
    )
    triggers.extend(
        (
            (
                "memory_witnesses_authority_immutable_update",
                "UPDATE",
                "memory_witnesses",
            ),
            (
                "memory_witnesses_immutable_delete",
                "DELETE",
                "memory_witnesses",
            ),
        )
    )
    return tuple(triggers)


MEMORY_IMMUTABILITY_TRIGGER_CONTRACT = _memory_immutability_trigger_contract()


def _memory_immutability_statements() -> tuple[str, ...]:
    statements: list[str] = []
    for table in MEMORY_IMMUTABLE_TABLES:
        immutable_predicate = "\n                    AND ".join(
            f"OLD.`{column}` <=> NEW.`{column}`"
            for column in MEMORY_IMMUTABLE_TABLE_COLUMNS[table]
        )
        statements.extend(
            (
                f"""CREATE TRIGGER IF NOT EXISTS {table}_immutable_update
                BEFORE UPDATE ON {table} FOR EACH ROW
                BEGIN
                    IF NOT (
                        {immutable_predicate}
                    ) THEN
                        SIGNAL SQLSTATE '45000'
                            SET MESSAGE_TEXT = 'MemoryAuthorityRecordImmutable';
                    END IF;
                END""",
                f"""CREATE TRIGGER IF NOT EXISTS {table}_immutable_delete
                BEFORE DELETE ON {table} FOR EACH ROW
                SIGNAL SQLSTATE '45000'
                    SET MESSAGE_TEXT = 'MemoryAuthorityRecordImmutable'""",
            )
        )

    witness_predicate = "\n                AND ".join(
        f"OLD.{column} <=> NEW.{column}" for column in MEMORY_WITNESS_IMMUTABLE_COLUMNS
    )
    statements.extend(
        (
            f"""CREATE TRIGGER IF NOT EXISTS memory_witnesses_authority_immutable_update
            BEFORE UPDATE ON memory_witnesses FOR EACH ROW
            BEGIN
                IF NOT (
                    {witness_predicate}
                ) THEN
                    SIGNAL SQLSTATE '45000'
                        SET MESSAGE_TEXT = 'MemoryWitnessAuthorityImmutable';
                END IF;
            END""",
            """CREATE TRIGGER IF NOT EXISTS memory_witnesses_immutable_delete
            BEFORE DELETE ON memory_witnesses FOR EACH ROW
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'MemoryWitnessAuthorityImmutable'""",
        )
    )
    return tuple(statements)


_MEMORY_IMMUTABILITY = SchemaMigration(
    version=MEMORY_IMMUTABILITY_SCHEMA_VERSION,
    name="life_memory_authority_immutability_v1",
    statements=_memory_immutability_statements(),
)

MEMORY_IMMUTABILITY_MIGRATIONS = (_MEMORY_IMMUTABILITY,)


def memory_database_immutability_required(
    runtime: StorageBackendRuntime,
    *,
    require_database_immutability: bool,
    writer_frozen: bool | None,
) -> bool:
    if require_database_immutability:
        return True
    if (
        runtime.writer_role != StorageWriterRole.CANDIDATE_COPY
        or writer_frozen is not False
    ):
        raise MemoryImmutabilityPolicyError(
            "database immutability may be skipped only for an explicitly "
            "unfrozen candidate-copy shadow"
        )
    return False


async def _verify_memory_database_immutability(
    runtime: StorageBackendRuntime,
) -> None:
    assert runtime.engine is not None
    try:
        async with runtime.engine.connect() as connection:
            migration = (
                (
                    await connection.execute(
                        text(
                            "SELECT name, checksum FROM "
                            "life_memory_immutability_schema_migrations "
                            "WHERE version = :version"
                        ),
                        {"version": MEMORY_IMMUTABILITY_SCHEMA_VERSION},
                    )
                )
                .mappings()
                .one_or_none()
            )
            trigger_rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT TRIGGER_NAME AS trigger_name, "
                            "EVENT_MANIPULATION AS event_manipulation, "
                            "ACTION_TIMING AS action_timing, "
                            "EVENT_OBJECT_TABLE AS event_object_table, "
                            "ACTION_STATEMENT AS action_statement "
                            "FROM information_schema.TRIGGERS "
                            "WHERE TRIGGER_SCHEMA = DATABASE()"
                        )
                    )
                )
                .mappings()
                .all()
            )
    except DBAPIError as exc:
        raise MemoryDatabaseImmutabilityError(
            "Memory database immutability metadata is unavailable"
        ) from exc

    expected_migration = MEMORY_IMMUTABILITY_MIGRATIONS[0]
    if migration is None or (str(migration["name"]), str(migration["checksum"])) != (
        expected_migration.name,
        expected_migration.checksum,
    ):
        raise MemoryDatabaseImmutabilityError(
            "Memory database immutability migration is missing or drifted"
        )

    installed = {
        str(row["trigger_name"]).lower(): (
            str(row["event_manipulation"]).upper(),
            str(row["action_timing"]).upper(),
            str(row["event_object_table"]).lower(),
            re.sub(r"\s+", " ", str(row["action_statement"])).replace("`", "").lower(),
        )
        for row in trigger_rows
    }
    missing_or_drifted: list[str] = []
    for name, event, table in MEMORY_IMMUTABILITY_TRIGGER_CONTRACT:
        actual = installed.get(name)
        if actual is None or actual[:3] != (event, "BEFORE", table):
            missing_or_drifted.append(name)
            continue
        action = actual[3]
        marker = (
            "memorywitnessauthorityimmutable"
            if table == "memory_witnesses"
            else "memoryauthorityrecordimmutable"
        )
        if marker not in action:
            missing_or_drifted.append(name)
            continue
        if event != "UPDATE":
            continue
        protected_columns = (
            MEMORY_WITNESS_IMMUTABLE_COLUMNS
            if table == "memory_witnesses"
            else MEMORY_IMMUTABLE_TABLE_COLUMNS[table]
        )
        if any(
            f"old.{column}" not in action or f"new.{column}" not in action
            for column in protected_columns
        ):
            missing_or_drifted.append(name)
    if missing_or_drifted:
        raise MemoryDatabaseImmutabilityError(
            "Memory database immutability triggers are missing or drifted: "
            + ", ".join(sorted(missing_or_drifted))
        )


async def verify_memory_storage_immutability(
    runtime: StorageBackendRuntime,
) -> None:
    """Fail closed unless the exact checksummed MySQL trigger set is present."""

    if not runtime.enabled or runtime.engine is None:
        raise RuntimeError("memory immutability requires an enabled storage runtime")
    if runtime.backend != BackendKind.MYSQL:
        raise RuntimeError("memory immutability verification is MySQL-only")
    await runtime.validate_writer()
    await _verify_memory_database_immutability(runtime)
    await runtime.validate_writer()


async def ensure_memory_storage_schema(
    runtime: StorageBackendRuntime,
    *,
    require_database_immutability: bool = True,
    writer_frozen: bool | None = None,
) -> None:
    """Create MySQL Memory schema and enforce activation-safe immutability.

    The only supported downgrade is an explicit ``writer_frozen=False``
    candidate-copy shadow.  Active writers and frozen candidates fail closed
    if the database cannot install the independently checksummed triggers.
    """

    if not runtime.enabled or runtime.engine is None:
        raise RuntimeError("memory schema requires an enabled storage runtime")
    if runtime.backend != BackendKind.MYSQL:
        raise RuntimeError("selectable SQLite Memory continues to use local schemas")
    install_immutability = memory_database_immutability_required(
        runtime,
        require_database_immutability=require_database_immutability,
        writer_frozen=writer_frozen,
    )
    await runtime.validate_writer()
    runner = MySQLMigrationRunner(
        runtime.engine,
        table_name="life_memory_schema_migrations",
        lock_name="elysium:life-memory-schema",
    )
    await runner.apply(MEMORY_MIGRATIONS)
    if install_immutability:
        immutable_runner = MySQLMigrationRunner(
            runtime.engine,
            table_name="life_memory_immutability_schema_migrations",
            lock_name="elysium:life-memory-immutability",
        )
        await immutable_runner.apply(MEMORY_IMMUTABILITY_MIGRATIONS)
        await _verify_memory_database_immutability(runtime)
    await runtime.validate_writer()


__all__ = [
    "MEMORY_IMMUTABILITY_MIGRATIONS",
    "MEMORY_IMMUTABILITY_SCHEMA_VERSION",
    "MEMORY_IMMUTABILITY_TRIGGER_CONTRACT",
    "MEMORY_IMMUTABLE_TABLES",
    "MEMORY_IMMUTABLE_TABLE_COLUMNS",
    "MEMORY_MIGRATIONS",
    "MEMORY_MUTABLE_TABLES",
    "MEMORY_SCHEMA_VERSION",
    "MEMORY_WITNESS_IMMUTABLE_COLUMNS",
    "MEMORY_WITNESS_MUTABLE_PROJECTION_COLUMNS",
    "MemoryDatabaseImmutabilityError",
    "MemoryImmutabilityPolicyError",
    "ensure_memory_storage_schema",
    "memory_database_immutability_required",
    "verify_memory_storage_immutability",
]
