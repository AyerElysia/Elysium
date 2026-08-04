"""Lossless domain copy from one immutable Life Memory SQLite snapshot.

The copy is deliberately table-shaped.  It preserves every explicit memory
record and every legacy graph row, including deleted nodes.  SQLite FTS shadow
tables are not copied as opaque implementation bytes; their visible rows are
carried by the explicit MySQL document/content columns and remain rebuildable.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text

from plugins.life_engine.memory.epistemic import (
    ClaimEvidence,
    EpistemicConflict,
    MemoryBelief,
    MemoryClaim,
    MemoryStateEvent,
    RetrievalEpisode,
    RetrievalExposure,
    RetrievalFeedback,
)
from plugins.life_engine.memory.experience import ExperienceRecord
from plugins.life_engine.memory.living import (
    CoRecallEvent,
    InterpretationSource,
    MemoryArtifactVersion,
    MemoryDerivation,
    MemoryInterpretation,
    RecallEpisode,
    RecallEvent,
    SemanticRelation,
)
from src.kernel.storage import canonical_json

from ..contracts import StorageBackendRuntime, StorageWriterRole
from ..memory.schema import ensure_memory_storage_schema
from .copy_authority import CopyAuthorityToken, MySQLCopyAuthorityRegistry
from .manifest import LifeSnapshotError, load_snapshot_manifest
from .snapshot import sha256_file

_SOURCE_RELATIVE = "life_engine_workspace/.memory/memory.db"


class MemoryCopyError(RuntimeError):
    """Raised when source evidence or target equivalence cannot be proven."""


@dataclass(frozen=True, slots=True)
class MemoryTableCopyReport:
    table_name: str
    row_count: int
    source_root_sha256: str
    target_root_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MemoryCopyReport:
    source_path: str
    source_database_sha256: str
    table_count: int
    copied_count: int
    deleted_node_count: int
    deleted_node_edge_count: int
    source_root_sha256: str
    target_root_sha256: str
    tables: tuple[MemoryTableCopyReport, ...]
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "source_database_sha256": self.source_database_sha256,
            "table_count": self.table_count,
            "copied_count": self.copied_count,
            "deleted_node_count": self.deleted_node_count,
            "deleted_node_edge_count": self.deleted_node_edge_count,
            "source_root_sha256": self.source_root_sha256,
            "target_root_sha256": self.target_root_sha256,
            "tables": [item.to_dict() for item in self.tables],
            "verified": self.verified,
        }


@dataclass(frozen=True, slots=True)
class _TableSpec:
    name: str
    columns: tuple[str, ...]
    key_columns: tuple[str, ...]


_SPECS = (
    _TableSpec(
        "memory_schema",
        ("schema_name", "version", "metadata_json", "updated_at"),
        ("schema_name",),
    ),
    _TableSpec(
        "memory_nodes",
        (
            "node_id",
            "node_type",
            "file_path",
            "file_path_sha256",
            "content_hash",
            "document_content",
            "title",
            "activation_strength",
            "access_count",
            "last_accessed_at",
            "emotional_valence",
            "emotional_arousal",
            "importance",
            "created_at",
            "updated_at",
            "embedding_synced",
            "source_mtime",
            "index_revision",
            "event_date",
            "is_deleted",
            "fts_content_hash",
            "embedding_content_hash",
            "embedding_model",
            "embedding_updated_at",
            "legacy_fts_present",
        ),
        ("node_id",),
    ),
    _TableSpec(
        "memory_chunks",
        (
            "chunk_id",
            "node_id",
            "chunk_index",
            "content_hash",
            "content",
            "title",
            "created_at",
            "updated_at",
        ),
        ("chunk_id",),
    ),
    _TableSpec(
        "memory_index_jobs",
        (
            "job_id",
            "node_id",
            "content_hash",
            "status",
            "created_at",
            "updated_at",
            "attempts",
            "error",
            "index_revision",
        ),
        ("job_id",),
    ),
    _TableSpec(
        "memory_index_state",
        (
            "state_key",
            "collection_name",
            "model_name",
            "dimension",
            "version",
            "updated_at",
        ),
        ("state_key",),
    ),
    _TableSpec(
        "memory_vector_tombstones",
        (
            "tombstone_id",
            "node_id",
            "chunk_id",
            "collection_name",
            "created_at",
            "consumed_at",
        ),
        ("tombstone_id",),
    ),
    _TableSpec(
        "memory_experiences",
        (
            "event_id",
            "source_event_id",
            "sequence",
            "occurred_at",
            "recorded_at",
            "source",
            "channel",
            "event_type",
            "content",
            "stream_id",
            "consciousness_instance_id",
            "actor",
            "visibility",
            "valid_from",
            "valid_to",
            "metadata_json",
            "payload_sha256",
        ),
        ("event_id",),
    ),
    _TableSpec(
        "memory_experience_occurrence_aliases",
        (
            "occurrence_id",
            "event_id",
            "source_event_id",
            "ingest_position",
            "recorded_at",
        ),
        ("occurrence_id",),
    ),
    _TableSpec(
        "memory_witnesses",
        (
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
            "projection_path",
            "projection_path_sha256",
            "projection_status",
            "projection_error",
            "metadata_json",
            "payload_sha256",
        ),
        ("witness_id",),
    ),
    _TableSpec(
        "memory_witness_sources",
        ("witness_id", "event_id", "ordinal"),
        ("witness_id", "event_id"),
    ),
    _TableSpec(
        "memory_witness_state",
        (
            "consciousness_instance_id",
            "last_sequence",
            "revision",
            "last_run_at",
            "last_success_at",
            "last_error",
            "updated_at",
        ),
        ("consciousness_instance_id",),
    ),
    _TableSpec(
        "memory_witness_migrations",
        (
            "migration_key",
            "source_path",
            "source_hash",
            "witness_id",
            "migrated_at",
        ),
        ("migration_key",),
    ),
    _TableSpec(
        "memory_artifact_versions",
        (
            "artifact_id",
            "logical_key",
            "logical_key_sha256",
            "artifact_kind",
            "content",
            "content_hash",
            "recorded_at",
            "valid_from",
            "valid_to",
            "authored_by",
            "consciousness_instance_id",
            "stream_scope",
            "visibility",
            "parent_artifact_ids_json",
            "metadata_json",
            "payload_sha256",
        ),
        ("artifact_id",),
    ),
    _TableSpec(
        "memory_artifact_derivations",
        (
            "derivation_id",
            "generated_artifact_id",
            "used_artifact_id",
            "predicate",
            "reason",
            "actor",
            "recorded_at",
            "metadata_json",
            "payload_sha256",
        ),
        ("derivation_id",),
    ),
    _TableSpec(
        "memory_artifact_heads",
        (
            "logical_key_sha256",
            "logical_key",
            "artifact_id",
            "projected_at",
            "revision",
        ),
        ("logical_key_sha256",),
    ),
    _TableSpec(
        "memory_interpretations",
        (
            "interpretation_id",
            "subject_id",
            "content",
            "authored_by",
            "consciousness_instance_id",
            "recorded_at",
            "valid_from",
            "valid_to",
            "stream_scope",
            "visibility",
            "metadata_json",
            "payload_sha256",
        ),
        ("interpretation_id",),
    ),
    _TableSpec(
        "memory_interpretation_sources",
        (
            "interpretation_id",
            "entity_ref",
            "entity_ref_sha256",
            "predicate",
            "ordinal",
            "metadata_json",
            "payload_sha256",
        ),
        ("interpretation_id", "entity_ref_sha256", "predicate"),
    ),
    _TableSpec(
        "memory_semantic_relations",
        (
            "relation_id",
            "source_ref",
            "source_ref_sha256",
            "target_ref",
            "target_ref_sha256",
            "predicate",
            "reason",
            "actor",
            "recorded_at",
            "consciousness_instance_id",
            "stream_scope",
            "metadata_json",
            "payload_sha256",
        ),
        ("relation_id",),
    ),
    _TableSpec(
        "memory_recall_sessions",
        (
            "episode_id",
            "query",
            "retrieval_intent",
            "consciousness_instance_id",
            "stream_scope",
            "context_key",
            "policy_version",
            "random_seed",
            "recorded_at",
            "context_json",
            "payload_sha256",
        ),
        ("episode_id",),
    ),
    _TableSpec(
        "memory_recall_events",
        (
            "event_id",
            "episode_id",
            "action",
            "entity_ref",
            "ordinal",
            "source",
            "reason",
            "recorded_at",
            "metadata_json",
            "payload_sha256",
        ),
        ("event_id",),
    ),
    _TableSpec(
        "memory_corecall_events",
        (
            "corecall_id",
            "episode_id",
            "context_key",
            "signal",
            "entity_refs_json",
            "actor",
            "reason",
            "recorded_at",
            "metadata_json",
            "payload_sha256",
        ),
        ("corecall_id",),
    ),
    _TableSpec(
        "memory_association_projection",
        (
            "source_ref_sha256",
            "source_ref",
            "target_ref_sha256",
            "target_ref",
            "context_key_sha256",
            "context_key",
            "signal",
            "signal_sha256",
            "event_count",
            "last_event_at",
        ),
        (
            "source_ref_sha256",
            "target_ref_sha256",
            "context_key_sha256",
            "signal_sha256",
        ),
    ),
    _TableSpec(
        "memory_claims",
        (
            "claim_id",
            "subject_key",
            "subject_key_sha256",
            "content",
            "claim_kind",
            "source",
            "authority",
            "valid_from",
            "valid_to",
            "recorded_at",
            "stream_scope",
            "visibility",
            "consciousness_instance_id",
            "metadata_json",
            "payload_sha256",
        ),
        ("claim_id",),
    ),
    _TableSpec(
        "memory_claim_evidence",
        (
            "evidence_link_id",
            "claim_id",
            "evidence_kind",
            "evidence_ref",
            "evidence_ref_sha256",
            "stance",
            "source_excerpt",
            "recorded_at",
            "metadata_json",
            "payload_sha256",
        ),
        ("evidence_link_id",),
    ),
    _TableSpec(
        "memory_beliefs",
        (
            "belief_id",
            "claim_id",
            "perspective_subject_id",
            "consciousness_instance_id",
            "recorded_at",
            "metadata_json",
            "payload_sha256",
        ),
        ("belief_id",),
    ),
    _TableSpec(
        "memory_epistemic_conflicts",
        (
            "conflict_id",
            "left_claim_id",
            "right_claim_id",
            "relation",
            "reason",
            "recorded_at",
            "detected_by",
            "metadata_json",
            "payload_sha256",
        ),
        ("conflict_id",),
    ),
    _TableSpec(
        "memory_state_events",
        (
            "event_id",
            "entity_type",
            "entity_id",
            "entity_id_sha256",
            "event_type",
            "actor",
            "authority",
            "reason",
            "recorded_at",
            "valid_at",
            "caused_by_event_id",
            "reverses_event_id",
            "payload_json",
            "payload_sha256",
        ),
        ("event_id",),
    ),
    _TableSpec(
        "memory_retrieval_episodes",
        (
            "episode_id",
            "query",
            "mode",
            "consciousness_instance_id",
            "stream_scope",
            "recorded_at",
            "metadata_json",
            "payload_sha256",
        ),
        ("episode_id",),
    ),
    _TableSpec(
        "memory_retrieval_exposures",
        (
            "exposure_id",
            "episode_id",
            "entity_type",
            "entity_id",
            "entity_id_sha256",
            "rank_position",
            "retrieval_source",
            "recorded_at",
            "feedback",
            "feedback_reason",
            "feedback_at",
            "metadata_json",
            "payload_sha256",
        ),
        ("exposure_id",),
    ),
    _TableSpec(
        "memory_retrieval_feedback",
        (
            "feedback_id",
            "exposure_id",
            "feedback",
            "actor",
            "reason",
            "recorded_at",
            "metadata_json",
            "payload_sha256",
        ),
        ("feedback_id",),
    ),
    _TableSpec(
        "memory_edges",
        (
            "edge_id",
            "source_id",
            "target_id",
            "edge_type",
            "weight",
            "base_strength",
            "reinforcement",
            "activation_count",
            "last_activated_at",
            "reason",
            "created_at",
            "bidirectional",
        ),
        ("edge_id",),
    ),
    _TableSpec(
        "memory_corrections",
        (
            "correction_id",
            "topic",
            "topic_sha256",
            "message",
            "source",
            "created_at",
            "related_node_id",
            "query",
            "stream_id",
        ),
        ("correction_id",),
    ),
)

TABLE_SPECS = {item.name: item for item in _SPECS}
SOURCE_TABLE_ORDER = tuple(item.name for item in _SPECS)

_JSON_COLUMNS = {
    "metadata_json",
    "parent_artifact_ids_json",
    "context_json",
    "entity_refs_json",
    "payload_json",
}
_BOOL_COLUMNS = {
    "embedding_synced",
    "is_deleted",
    "legacy_fts_present",
    "bidirectional",
}
_INT_COLUMNS = {
    "version",
    "access_count",
    "index_revision",
    "chunk_index",
    "attempts",
    "dimension",
    "tombstone_id",
    "sequence",
    "ingest_position",
    "source_sequence_start",
    "source_sequence_end",
    "last_sequence",
    "revision",
    "ordinal",
    "random_seed",
    "event_count",
    "rank_position",
    "activation_count",
}
_FLOAT_FIELDS = {
    ("memory_schema", "updated_at"),
    *(("memory_nodes", column) for column in (
        "last_accessed_at",
        "activation_strength",
        "emotional_valence",
        "emotional_arousal",
        "importance",
        "created_at",
        "updated_at",
        "source_mtime",
        "embedding_updated_at",
    )),
    *(("memory_chunks", column) for column in ("created_at", "updated_at")),
    *(("memory_index_jobs", column) for column in ("created_at", "updated_at")),
    ("memory_index_state", "updated_at"),
    *(("memory_vector_tombstones", column) for column in ("created_at", "consumed_at")),
    *(("memory_edges", column) for column in (
        "weight",
        "base_strength",
        "reinforcement",
        "last_activated_at",
        "created_at",
    )),
    ("memory_corrections", "created_at"),
}

_SOURCE_ORDER = {
    "memory_vector_tombstones": ("chunk_id",),
    "memory_artifact_heads": ("logical_key",),
    "memory_interpretation_sources": (
        "interpretation_id",
        "entity_ref",
        "predicate",
    ),
    "memory_association_projection": (
        "source_ref",
        "target_ref",
        "context_key",
        "signal",
    ),
}


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _payload_hash(value: Any) -> str:
    return _sha256(canonical_json(asdict(value)))


def _strict_json(value: Any, *, expected: type) -> Any:
    if isinstance(value, expected):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    try:
        decoded = json.loads(str(value or ("{}" if expected is dict else "[]")))
    except json.JSONDecodeError as exc:
        raise MemoryCopyError("Life Memory snapshot contains malformed JSON") from exc
    if not isinstance(decoded, expected):
        raise MemoryCopyError(
            f"Life Memory JSON must be {expected.__name__}, got {type(decoded).__name__}"
        )
    return decoded


def _canonical_object(value: Any) -> str:
    return canonical_json(_strict_json(value, expected=dict))


def _canonical_array(value: Any) -> str:
    return canonical_json(_strict_json(value, expected=list))


def _source_database(snapshot_root: Path) -> tuple[Path, dict[str, Any]]:
    try:
        manifest = load_snapshot_manifest(snapshot_root / "manifest.json")
    except LifeSnapshotError as exc:
        raise MemoryCopyError(str(exc)) from exc
    if (snapshot_root / "SNAPSHOT_INCOMPLETE").exists():
        raise MemoryCopyError("Life Memory snapshot is marked incomplete")
    entries = [
        item
        for item in list(manifest.get("sqlite") or [])
        if isinstance(item, dict)
        and str(item.get("source_relative") or "") == _SOURCE_RELATIVE
    ]
    if len(entries) != 1:
        raise MemoryCopyError("snapshot has no unique Life Memory database")
    entry = entries[0]
    source = (snapshot_root / str(entry.get("backup_relative") or "")).resolve()
    try:
        source.relative_to(snapshot_root)
    except ValueError as exc:
        raise MemoryCopyError("Life Memory snapshot path escapes snapshot root") from exc
    expected_hash = str(entry.get("backup_sha256") or entry.get("sha256") or "")
    if not source.is_file() or len(expected_hash) != 64:
        raise MemoryCopyError("Life Memory snapshot database is incomplete")
    if source.stat().st_size != int(entry.get("backup_bytes") or entry.get("bytes")):
        raise MemoryCopyError("Life Memory snapshot byte length changed")
    if sha256_file(source) != expected_hash:
        raise MemoryCopyError("Life Memory snapshot checksum changed")
    return source, {"manifest": manifest, "entry": entry}


def open_memory_source(snapshot_directory: str | Path) -> tuple[sqlite3.Connection, dict[str, Any]]:
    """Open and validate the one declared read-only Memory snapshot database."""

    snapshot = Path(snapshot_directory).resolve()
    source, evidence = _source_database(snapshot)
    connection = sqlite3.connect(
        f"file:{source.as_posix()}?mode=ro",
        uri=True,
        timeout=30.0,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or str(integrity[0]).lower() != "ok":
        connection.close()
        raise MemoryCopyError("Life Memory source failed SQLite integrity_check")
    evidence["source_path"] = source
    return connection, evidence


@dataclass(slots=True)
class _SourceContext:
    fts_content: dict[str, str]
    witness_sources: dict[str, tuple[str, ...]]
    artifact_revisions: dict[str, int]
    witness_state_has_revision: bool
    vector_collection_name: str


def _source_context(connection: sqlite3.Connection) -> _SourceContext:
    fts_content = {
        str(row["node_id"]): str(row["content"] or "")
        for row in connection.execute("SELECT node_id, content FROM memory_fts")
    }
    witness_source_map: dict[str, list[tuple[int, str]]] = {}
    for row in connection.execute(
        "SELECT witness_id, event_id, ordinal FROM memory_witness_sources "
        "ORDER BY witness_id, ordinal, event_id"
    ):
        witness_source_map.setdefault(str(row["witness_id"]), []).append(
            (int(row["ordinal"]), str(row["event_id"]))
        )
    witness_sources = {
        witness_id: tuple(event_id for _, event_id in rows)
        for witness_id, rows in witness_source_map.items()
    }
    artifact_revisions = {
        str(row["logical_key"]): int(row["revision"])
        for row in connection.execute(
            "SELECT logical_key, COUNT(*) AS revision "
            "FROM memory_artifact_versions GROUP BY logical_key"
        )
    }
    state_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(memory_witness_state)")
    }
    collection_row = connection.execute(
        "SELECT collection_name FROM memory_index_state ORDER BY state_key LIMIT 1"
    ).fetchone()
    return _SourceContext(
        fts_content=fts_content,
        witness_sources=witness_sources,
        artifact_revisions=artifact_revisions,
        witness_state_has_revision="revision" in state_columns,
        vector_collection_name=(
            str(collection_row["collection_name"]) if collection_row else ""
        ),
    )


def _experience_hash(record: ExperienceRecord) -> str:
    return _sha256(
        canonical_json(
            {
                "occurred_at": record.occurred_at,
                "source": record.source,
                "channel": record.channel,
                "event_type": record.event_type,
                "content": record.content,
                "stream_id": record.stream_id,
                "consciousness_instance_id": record.consciousness_instance_id,
                "actor": record.actor,
                "visibility": record.visibility,
                "valid_from": record.valid_from or record.occurred_at,
                "valid_to": record.valid_to,
                "metadata": record.metadata,
            }
        )
    )


def _transform_source_row(
    table: str,
    row: sqlite3.Row,
    context: _SourceContext,
    ordinal: int,
) -> dict[str, Any]:
    raw = dict(row)
    if table == "memory_schema":
        return {
            "schema_name": str(raw["schema_name"]),
            "version": int(raw["version"]),
            "metadata_json": canonical_json(
                {"legacy_tokenizer": str(raw.get("tokenizer") or "")}
            ),
            "updated_at": float(raw["updated_at"]),
        }
    if table == "memory_nodes":
        node_id = str(raw["node_id"])
        file_path = str(raw["file_path"]) if raw["file_path"] is not None else None
        return {
            "node_id": node_id,
            "node_type": str(raw["node_type"]),
            "file_path": file_path,
            "file_path_sha256": _sha256(file_path) if file_path is not None else None,
            "content_hash": (
                str(raw["content_hash"]) if raw["content_hash"] is not None else None
            ),
            "document_content": context.fts_content.get(node_id, ""),
            "title": str(raw.get("title") or ""),
            "activation_strength": float(raw.get("activation_strength") or 0.0),
            "access_count": int(raw.get("access_count") or 0),
            "last_accessed_at": (
                float(raw["last_accessed_at"])
                if raw.get("last_accessed_at") is not None
                else None
            ),
            "emotional_valence": float(raw.get("emotional_valence") or 0.0),
            "emotional_arousal": float(raw.get("emotional_arousal") or 0.0),
            "importance": float(raw.get("importance") or 0.0),
            "created_at": float(raw["created_at"]),
            "updated_at": float(raw["updated_at"]),
            "embedding_synced": bool(raw.get("embedding_synced")),
            "source_mtime": (
                float(raw["source_mtime"])
                if raw.get("source_mtime") is not None
                else None
            ),
            "index_revision": int(raw.get("index_revision") or 0),
            "event_date": (
                str(raw["event_date"]) if raw.get("event_date") is not None else None
            ),
            "is_deleted": bool(raw.get("is_deleted")),
            "fts_content_hash": (
                str(raw["fts_content_hash"])
                if raw.get("fts_content_hash") is not None
                else None
            ),
            "embedding_content_hash": (
                str(raw["embedding_content_hash"])
                if raw.get("embedding_content_hash") is not None
                else None
            ),
            "embedding_model": str(raw.get("embedding_model") or ""),
            "embedding_updated_at": (
                float(raw["embedding_updated_at"])
                if raw.get("embedding_updated_at") is not None
                else None
            ),
            "legacy_fts_present": node_id in context.fts_content,
        }
    if table in {"memory_chunks", "memory_index_jobs", "memory_index_state"}:
        return {column: raw[column] for column in TABLE_SPECS[table].columns}
    if table == "memory_vector_tombstones":
        chunk_id = str(raw["chunk_id"])
        parts = chunk_id.rsplit(":", 2)
        if len(parts) != 3 or not parts[0]:
            raise MemoryCopyError(f"cannot derive tombstone node: {chunk_id}")
        return {
            "tombstone_id": ordinal,
            "node_id": parts[0],
            "chunk_id": chunk_id,
            "collection_name": context.vector_collection_name,
            "created_at": float(raw["created_at"]),
            "consumed_at": None,
        }
    if table == "memory_experiences":
        metadata = _strict_json(raw.get("metadata_json"), expected=dict)
        record = ExperienceRecord(
            event_id=str(raw["event_id"]),
            source_event_id=str(raw.get("source_event_id") or ""),
            sequence=int(raw["sequence"]),
            occurred_at=str(raw["occurred_at"]),
            recorded_at=str(raw["recorded_at"]),
            source=str(raw["source"]),
            channel=str(raw["channel"]),
            event_type=str(raw["event_type"]),
            content=str(raw["content"]),
            stream_id=str(raw.get("stream_id") or ""),
            consciousness_instance_id=str(
                raw.get("consciousness_instance_id") or ""
            ),
            actor=str(raw.get("actor") or ""),
            visibility=str(raw.get("visibility") or "private"),
            valid_from=str(raw.get("valid_from") or ""),
            valid_to=str(raw.get("valid_to") or ""),
            metadata=metadata,
        )
        values = asdict(record)
        values["metadata_json"] = canonical_json(metadata)
        values.pop("metadata")
        values["payload_sha256"] = _experience_hash(record)
        return values
    if table == "memory_experience_occurrence_aliases":
        return {column: raw[column] for column in TABLE_SPECS[table].columns}
    if table == "memory_witnesses":
        witness_id = str(raw["witness_id"])
        metadata_json = _canonical_object(raw.get("metadata_json"))
        projection_path = str(raw.get("projection_path") or "") or None
        source_ids = context.witness_sources.get(witness_id, ())
        initial_values = {
            "witness_id": witness_id,
            "content": str(raw["content"]),
            "consciousness_instance_id": str(raw["consciousness_instance_id"]),
            "perspective_subject_id": str(raw["perspective_subject_id"]),
            "epistemic_kind": str(raw["epistemic_kind"]),
            "source_kind": str(raw["source_kind"]),
            "status": str(raw.get("status") or "active"),
            "stream_scope": str(raw.get("stream_scope") or ""),
            "visibility": str(raw.get("visibility") or "private"),
            "valid_from": str(raw.get("valid_from") or ""),
            "valid_to": str(raw.get("valid_to") or ""),
            "recorded_at": str(raw["recorded_at"]),
            "source_sequence_start": int(raw.get("source_sequence_start") or 0),
            "source_sequence_end": int(raw.get("source_sequence_end") or 0),
            "model_task_name": str(raw.get("model_task_name") or ""),
            "projection_path": projection_path,
            "projection_status": "pending",
            "projection_error": "",
            "metadata_json": metadata_json,
        }
        values = {
            **initial_values,
            "projection_path_sha256": (
                _sha256(str(projection_path)) if projection_path is not None else None
            ),
            "projection_status": str(raw.get("projection_status") or "pending"),
            "projection_error": str(raw.get("projection_error") or ""),
        }
        hash_body = {
            **initial_values,
            "projection_path": str(projection_path or ""),
            "source_event_ids": source_ids,
        }
        values["payload_sha256"] = _sha256(canonical_json(hash_body))
        return values
    if table == "memory_witness_sources":
        return {column: raw[column] for column in TABLE_SPECS[table].columns}
    if table == "memory_witness_state":
        revision = int(raw.get("revision") or 0) if context.witness_state_has_revision else 1
        return {
            "consciousness_instance_id": str(raw["consciousness_instance_id"]),
            "last_sequence": int(raw.get("last_sequence") or 0),
            "revision": max(1, revision),
            "last_run_at": str(raw.get("last_run_at") or ""),
            "last_success_at": str(raw.get("last_success_at") or ""),
            "last_error": str(raw.get("last_error") or ""),
            "updated_at": str(raw.get("updated_at") or ""),
        }
    if table == "memory_witness_migrations":
        return {column: raw[column] for column in TABLE_SPECS[table].columns}
    if table == "memory_artifact_versions":
        parents = tuple(
            dict.fromkeys(str(item) for item in _strict_json(
                raw.get("parent_artifact_ids_json"), expected=list
            ))
        )
        metadata = _strict_json(raw.get("metadata_json"), expected=dict)
        record = MemoryArtifactVersion(
            artifact_id=str(raw["artifact_id"]),
            logical_key=str(raw["logical_key"]),
            artifact_kind=str(raw["artifact_kind"]),
            content=str(raw["content"]),
            content_hash=str(raw["content_hash"]),
            recorded_at=str(raw["recorded_at"]),
            valid_from=str(raw.get("valid_from") or ""),
            valid_to=str(raw.get("valid_to") or ""),
            authored_by=str(raw.get("authored_by") or ""),
            consciousness_instance_id=str(
                raw.get("consciousness_instance_id") or ""
            ),
            stream_scope=str(raw.get("stream_scope") or ""),
            visibility=str(raw.get("visibility") or "private"),
            parent_artifact_ids=parents,
            metadata=metadata,
        )
        if _sha256(record.content) != record.content_hash:
            raise MemoryCopyError(
                f"artifact content hash mismatch: {record.artifact_id}"
            )
        return {
            "artifact_id": record.artifact_id,
            "logical_key": record.logical_key,
            "logical_key_sha256": _sha256(record.logical_key),
            "artifact_kind": record.artifact_kind,
            "content": record.content,
            "content_hash": record.content_hash,
            "recorded_at": record.recorded_at,
            "valid_from": record.valid_from,
            "valid_to": record.valid_to,
            "authored_by": record.authored_by,
            "consciousness_instance_id": record.consciousness_instance_id,
            "stream_scope": record.stream_scope,
            "visibility": record.visibility,
            "parent_artifact_ids_json": canonical_json(list(parents)),
            "metadata_json": canonical_json(metadata),
            "payload_sha256": _payload_hash(record),
        }
    if table == "memory_artifact_derivations":
        metadata = _strict_json(raw.get("metadata_json"), expected=dict)
        record = MemoryDerivation(
            derivation_id=str(raw["derivation_id"]),
            generated_artifact_id=str(raw["generated_artifact_id"]),
            used_artifact_id=str(raw["used_artifact_id"]),
            predicate=str(raw["predicate"]),
            reason=str(raw.get("reason") or ""),
            actor=str(raw.get("actor") or ""),
            recorded_at=str(raw["recorded_at"]),
            metadata=metadata,
        )
        return {
            **{key: value for key, value in asdict(record).items() if key != "metadata"},
            "metadata_json": canonical_json(metadata),
            "payload_sha256": _payload_hash(record),
        }
    if table == "memory_artifact_heads":
        logical_key = str(raw["logical_key"])
        revision = context.artifact_revisions.get(logical_key, 0)
        if revision <= 0:
            raise MemoryCopyError(f"artifact head has no history: {logical_key}")
        return {
            "logical_key_sha256": _sha256(logical_key),
            "logical_key": logical_key,
            "artifact_id": str(raw["artifact_id"]),
            "projected_at": str(raw["projected_at"]),
            "revision": revision,
        }
    if table == "memory_interpretations":
        metadata = _strict_json(raw.get("metadata_json"), expected=dict)
        record = MemoryInterpretation(
            interpretation_id=str(raw["interpretation_id"]),
            subject_id=str(raw["subject_id"]),
            content=str(raw["content"]),
            authored_by=str(raw.get("authored_by") or ""),
            consciousness_instance_id=str(
                raw.get("consciousness_instance_id") or ""
            ),
            recorded_at=str(raw["recorded_at"]),
            valid_from=str(raw.get("valid_from") or ""),
            valid_to=str(raw.get("valid_to") or ""),
            stream_scope=str(raw.get("stream_scope") or ""),
            visibility=str(raw.get("visibility") or "private"),
            metadata=metadata,
        )
        return {
            **{key: value for key, value in asdict(record).items() if key != "metadata"},
            "metadata_json": canonical_json(metadata),
            "payload_sha256": _payload_hash(record),
        }
    if table == "memory_interpretation_sources":
        metadata = _strict_json(raw.get("metadata_json"), expected=dict)
        record = InterpretationSource(
            interpretation_id=str(raw["interpretation_id"]),
            entity_ref=str(raw["entity_ref"]),
            predicate=str(raw.get("predicate") or "draws_from"),
            ordinal=int(raw.get("ordinal") or 0),
            metadata=metadata,
        )
        return {
            "interpretation_id": record.interpretation_id,
            "entity_ref": record.entity_ref,
            "entity_ref_sha256": _sha256(record.entity_ref),
            "predicate": record.predicate,
            "ordinal": record.ordinal,
            "metadata_json": canonical_json(metadata),
            "payload_sha256": _payload_hash(record),
        }
    if table == "memory_semantic_relations":
        metadata = _strict_json(raw.get("metadata_json"), expected=dict)
        record = SemanticRelation(
            relation_id=str(raw["relation_id"]),
            source_ref=str(raw["source_ref"]),
            target_ref=str(raw["target_ref"]),
            predicate=str(raw["predicate"]),
            reason=str(raw.get("reason") or ""),
            actor=str(raw.get("actor") or ""),
            recorded_at=str(raw["recorded_at"]),
            consciousness_instance_id=str(
                raw.get("consciousness_instance_id") or ""
            ),
            stream_scope=str(raw.get("stream_scope") or ""),
            metadata=metadata,
        )
        return {
            "relation_id": record.relation_id,
            "source_ref": record.source_ref,
            "source_ref_sha256": _sha256(record.source_ref),
            "target_ref": record.target_ref,
            "target_ref_sha256": _sha256(record.target_ref),
            "predicate": record.predicate,
            "reason": record.reason,
            "actor": record.actor,
            "recorded_at": record.recorded_at,
            "consciousness_instance_id": record.consciousness_instance_id,
            "stream_scope": record.stream_scope,
            "metadata_json": canonical_json(metadata),
            "payload_sha256": _payload_hash(record),
        }
    if table == "memory_recall_sessions":
        context_value = _strict_json(raw.get("context_json"), expected=dict)
        record = RecallEpisode(
            episode_id=str(raw["episode_id"]),
            query=str(raw["query"]),
            retrieval_intent=str(raw.get("retrieval_intent") or ""),
            consciousness_instance_id=str(
                raw.get("consciousness_instance_id") or ""
            ),
            stream_scope=str(raw.get("stream_scope") or ""),
            context_key=str(raw.get("context_key") or ""),
            policy_version=str(raw["policy_version"]),
            random_seed=int(raw["random_seed"]),
            recorded_at=str(raw["recorded_at"]),
            context=context_value,
        )
        return {
            **{key: value for key, value in asdict(record).items() if key != "context"},
            "context_json": canonical_json(context_value),
            "payload_sha256": _payload_hash(record),
        }
    if table == "memory_recall_events":
        metadata = _strict_json(raw.get("metadata_json"), expected=dict)
        record = RecallEvent(
            event_id=str(raw["event_id"]),
            episode_id=str(raw["episode_id"]),
            action=str(raw["action"]),
            recorded_at=str(raw["recorded_at"]),
            entity_ref=str(raw.get("entity_ref") or ""),
            ordinal=int(raw.get("ordinal") or 0),
            source=str(raw.get("source") or ""),
            reason=str(raw.get("reason") or ""),
            metadata=metadata,
        )
        return {
            **{key: value for key, value in asdict(record).items() if key != "metadata"},
            "metadata_json": canonical_json(metadata),
            "payload_sha256": _payload_hash(record),
        }
    if table == "memory_corecall_events":
        refs = tuple(
            dict.fromkeys(
                str(item)
                for item in _strict_json(raw.get("entity_refs_json"), expected=list)
                if str(item)
            )
        )
        metadata = _strict_json(raw.get("metadata_json"), expected=dict)
        record = CoRecallEvent(
            corecall_id=str(raw["corecall_id"]),
            episode_id=str(raw["episode_id"]),
            context_key=str(raw.get("context_key") or ""),
            signal=str(raw["signal"]),
            entity_refs=refs,
            actor=str(raw.get("actor") or ""),
            reason=str(raw.get("reason") or ""),
            recorded_at=str(raw["recorded_at"]),
            metadata=metadata,
        )
        return {
            "corecall_id": record.corecall_id,
            "episode_id": record.episode_id,
            "context_key": record.context_key,
            "signal": record.signal,
            "entity_refs_json": canonical_json(list(refs)),
            "actor": record.actor,
            "reason": record.reason,
            "recorded_at": record.recorded_at,
            "metadata_json": canonical_json(metadata),
            "payload_sha256": _payload_hash(record),
        }
    if table == "memory_association_projection":
        source_ref = str(raw["source_ref"])
        target_ref = str(raw["target_ref"])
        context_key = str(raw.get("context_key") or "")
        signal = str(raw["signal"])
        return {
            "source_ref_sha256": _sha256(source_ref),
            "source_ref": source_ref,
            "target_ref_sha256": _sha256(target_ref),
            "target_ref": target_ref,
            "context_key_sha256": _sha256(context_key),
            "context_key": context_key,
            "signal": signal,
            "signal_sha256": _sha256(signal),
            "event_count": int(raw["event_count"]),
            "last_event_at": str(raw["last_event_at"]),
        }
    if table == "memory_claims":
        metadata = _strict_json(raw.get("metadata_json"), expected=dict)
        record = MemoryClaim(
            claim_id=str(raw["claim_id"]),
            subject_key=str(raw["subject_key"]),
            content=str(raw["content"]),
            claim_kind=str(raw["claim_kind"]),
            source=str(raw["source"]),
            authority=str(raw["authority"]),
            valid_from=str(raw.get("valid_from") or ""),
            valid_to=str(raw.get("valid_to") or ""),
            recorded_at=str(raw["recorded_at"]),
            stream_scope=str(raw.get("stream_scope") or ""),
            visibility=str(raw.get("visibility") or "private"),
            consciousness_instance_id=str(
                raw.get("consciousness_instance_id") or ""
            ),
            metadata=metadata,
        )
        return {
            "claim_id": record.claim_id,
            "subject_key": record.subject_key,
            "subject_key_sha256": _sha256(record.subject_key),
            "content": record.content,
            "claim_kind": record.claim_kind,
            "source": record.source,
            "authority": record.authority,
            "valid_from": record.valid_from,
            "valid_to": record.valid_to,
            "recorded_at": record.recorded_at,
            "stream_scope": record.stream_scope,
            "visibility": record.visibility,
            "consciousness_instance_id": record.consciousness_instance_id,
            "metadata_json": canonical_json(metadata),
            "payload_sha256": _payload_hash(record),
        }
    if table == "memory_claim_evidence":
        metadata = _strict_json(raw.get("metadata_json"), expected=dict)
        record = ClaimEvidence(
            evidence_link_id=str(raw["evidence_link_id"]),
            claim_id=str(raw["claim_id"]),
            evidence_kind=str(raw["evidence_kind"]),
            evidence_ref=str(raw["evidence_ref"]),
            stance=str(raw["stance"]),
            source_excerpt=str(raw.get("source_excerpt") or ""),
            recorded_at=str(raw["recorded_at"]),
            metadata=metadata,
        )
        return {
            "evidence_link_id": record.evidence_link_id,
            "claim_id": record.claim_id,
            "evidence_kind": record.evidence_kind,
            "evidence_ref": record.evidence_ref,
            "evidence_ref_sha256": _sha256(record.evidence_ref),
            "stance": record.stance,
            "source_excerpt": record.source_excerpt,
            "recorded_at": record.recorded_at,
            "metadata_json": canonical_json(metadata),
            "payload_sha256": _payload_hash(record),
        }
    if table in {
        "memory_beliefs",
        "memory_epistemic_conflicts",
        "memory_state_events",
        "memory_retrieval_episodes",
        "memory_retrieval_exposures",
        "memory_retrieval_feedback",
    }:
        metadata = _strict_json(raw.get("metadata_json"), expected=dict)
        if table == "memory_beliefs":
            record: Any = MemoryBelief(
                belief_id=str(raw["belief_id"]),
                claim_id=str(raw["claim_id"]),
                perspective_subject_id=str(raw["perspective_subject_id"]),
                consciousness_instance_id=str(
                    raw.get("consciousness_instance_id") or ""
                ),
                recorded_at=str(raw["recorded_at"]),
                metadata=metadata,
            )
            values = {key: value for key, value in asdict(record).items() if key != "metadata"}
        elif table == "memory_epistemic_conflicts":
            record = EpistemicConflict(
                conflict_id=str(raw["conflict_id"]),
                left_claim_id=str(raw["left_claim_id"]),
                right_claim_id=str(raw["right_claim_id"]),
                relation=str(raw["relation"]),
                reason=str(raw.get("reason") or ""),
                recorded_at=str(raw["recorded_at"]),
                detected_by=str(raw.get("detected_by") or ""),
                metadata=metadata,
            )
            values = {key: value for key, value in asdict(record).items() if key != "metadata"}
        elif table == "memory_state_events":
            payload = _strict_json(raw.get("payload_json"), expected=dict)
            record = MemoryStateEvent(
                event_id=str(raw["event_id"]),
                entity_type=str(raw["entity_type"]),
                entity_id=str(raw["entity_id"]),
                event_type=str(raw["event_type"]),
                actor=str(raw.get("actor") or ""),
                authority=str(raw.get("authority") or "unknown"),
                reason=str(raw.get("reason") or ""),
                recorded_at=str(raw["recorded_at"]),
                valid_at=str(raw.get("valid_at") or ""),
                caused_by_event_id=str(raw.get("caused_by_event_id") or ""),
                reverses_event_id=str(raw.get("reverses_event_id") or ""),
                payload=payload,
            )
            values = {
                **{key: value for key, value in asdict(record).items() if key != "payload"},
                "entity_id_sha256": _sha256(record.entity_id),
                "payload_json": canonical_json(payload),
            }
        elif table == "memory_retrieval_episodes":
            record = RetrievalEpisode(
                episode_id=str(raw["episode_id"]),
                query=str(raw["query"]),
                mode=str(raw["mode"]),
                consciousness_instance_id=str(
                    raw.get("consciousness_instance_id") or ""
                ),
                stream_scope=str(raw.get("stream_scope") or ""),
                recorded_at=str(raw["recorded_at"]),
                metadata=metadata,
            )
            values = {key: value for key, value in asdict(record).items() if key != "metadata"}
        elif table == "memory_retrieval_exposures":
            record = RetrievalExposure(
                exposure_id=str(raw["exposure_id"]),
                episode_id=str(raw["episode_id"]),
                entity_type=str(raw["entity_type"]),
                entity_id=str(raw["entity_id"]),
                rank_position=int(raw["rank_position"]),
                retrieval_source=str(raw["retrieval_source"]),
                recorded_at=str(raw["recorded_at"]),
                feedback=str(raw.get("feedback") or "unreviewed"),
                feedback_reason=str(raw.get("feedback_reason") or ""),
                feedback_at=str(raw.get("feedback_at") or ""),
                metadata=metadata,
            )
            values = {
                **{key: value for key, value in asdict(record).items() if key != "metadata"},
                "entity_id_sha256": _sha256(record.entity_id),
            }
        else:
            record = RetrievalFeedback(
                feedback_id=str(raw["feedback_id"]),
                exposure_id=str(raw["exposure_id"]),
                feedback=str(raw["feedback"]),
                actor=str(raw.get("actor") or ""),
                reason=str(raw.get("reason") or ""),
                recorded_at=str(raw["recorded_at"]),
                metadata=metadata,
            )
            values = {key: value for key, value in asdict(record).items() if key != "metadata"}
        if table != "memory_state_events":
            values["metadata_json"] = canonical_json(metadata)
        values["payload_sha256"] = _payload_hash(record)
        return values
    if table == "memory_edges":
        return {
            "edge_id": str(raw["edge_id"]),
            "source_id": str(raw["source_id"]),
            "target_id": str(raw["target_id"]),
            "edge_type": str(raw["edge_type"]),
            "weight": float(raw.get("weight") or 0.0),
            "base_strength": float(raw.get("base_strength") or 0.0),
            "reinforcement": float(raw.get("reinforcement") or 0.0),
            "activation_count": int(raw.get("activation_count") or 0),
            "last_activated_at": (
                float(raw["last_activated_at"])
                if raw.get("last_activated_at") is not None
                else None
            ),
            "reason": str(raw.get("reason") or ""),
            "created_at": float(raw["created_at"]),
            "bidirectional": bool(raw.get("bidirectional")),
        }
    if table == "memory_corrections":
        topic = str(raw["topic"])
        return {
            "correction_id": str(raw["correction_id"]),
            "topic": topic,
            "topic_sha256": _sha256(topic),
            "message": str(raw["message"]),
            "source": str(raw.get("source") or "user"),
            "created_at": float(raw["created_at"]),
            "related_node_id": (
                str(raw["related_node_id"])
                if raw.get("related_node_id") is not None
                else None
            ),
            "query": str(raw.get("query") or ""),
            "stream_id": (
                str(raw["stream_id"]) if raw.get("stream_id") is not None else None
            ),
        }
    raise MemoryCopyError(f"unsupported Life Memory table: {table}")


def iter_transformed_source_rows(
    connection: sqlite3.Connection,
    spec: _TableSpec,
    context: _SourceContext,
    *,
    batch_size: int,
) -> Iterable[list[dict[str, Any]]]:
    order = ", ".join(_SOURCE_ORDER.get(spec.name, spec.key_columns))
    cursor = connection.execute(f"SELECT * FROM {spec.name} ORDER BY {order}")
    ordinal = 0
    while True:
        rows = cursor.fetchmany(int(batch_size))
        if not rows:
            return
        transformed: list[dict[str, Any]] = []
        for row in rows:
            ordinal += 1
            values = _transform_source_row(spec.name, row, context, ordinal)
            missing = set(spec.columns) - set(values)
            extra = set(values) - set(spec.columns)
            if missing or extra:
                raise MemoryCopyError(
                    f"{spec.name} transform columns differ: missing={sorted(missing)}, "
                    f"extra={sorted(extra)}"
                )
            transformed.append(values)
        yield transformed


def _normalize_json(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return canonical_json(value)
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    decoded = json.loads(str(value))
    return canonical_json(decoded)


def normalize_target_row(spec: _TableSpec, row: Any) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for column in spec.columns:
        value = row[column]
        if value is None:
            normalized[column] = None
        elif column in _JSON_COLUMNS:
            normalized[column] = _normalize_json(value)
        elif column in _BOOL_COLUMNS:
            normalized[column] = bool(value)
        elif column in _INT_COLUMNS:
            normalized[column] = int(value)
        elif (spec.name, column) in _FLOAT_FIELDS:
            normalized[column] = float(value)
        elif isinstance(value, (bytes, bytearray, memoryview)):
            normalized[column] = bytes(value).decode("utf-8")
        else:
            normalized[column] = str(value)
    return normalized


def _row_digest(table: str, row: dict[str, Any]) -> bytes:
    digest = hashlib.sha256()
    digest.update(table.encode("utf-8"))
    digest.update(b"\0")
    digest.update(canonical_json(row).encode("utf-8"))
    return digest.digest()


def _table_root(row_digests: list[bytes]) -> str:
    digest = hashlib.sha256()
    for item in sorted(row_digests):
        digest.update(item)
    return digest.hexdigest()


def _insert_statement(spec: _TableSpec) -> Any:
    columns = ", ".join(
        "`signal`" if column == "signal" else column for column in spec.columns
    )
    values = ", ".join(f":{column}" for column in spec.columns)
    lossless_json_columns = tuple(
        column for column in spec.columns if column in _JSON_COLUMNS
    )
    if "payload_sha256" in spec.columns and lossless_json_columns:
        update = ", ".join(
            f"{column} = IF({spec.name}.payload_sha256 = new.payload_sha256, "
            f"new.{column}, {spec.name}.{column})"
            for column in lossless_json_columns
        )
    else:
        update = f"{spec.key_columns[0]} = {spec.name}.{spec.key_columns[0]}"
    return text(
        f"INSERT INTO {spec.name} ({columns}) VALUES ({values}) AS new "
        f"ON DUPLICATE KEY UPDATE {update}"
    )


async def _target_table_root(
    runtime: StorageBackendRuntime,
    spec: _TableSpec,
) -> tuple[int, str]:
    if runtime.engine is None:
        raise MemoryCopyError("Memory target runtime has no engine")
    row_digests: list[bytes] = []
    count = 0
    order = ", ".join(spec.key_columns)
    columns = ", ".join(
        "`signal`" if column == "signal" else column for column in spec.columns
    )
    async with runtime.engine.connect() as connection:
        result = await connection.stream(
            text(f"SELECT {columns} FROM {spec.name} ORDER BY {order}")
        )
        async for row in result.mappings():
            row_digests.append(
                _row_digest(spec.name, normalize_target_row(spec, row))
            )
            count += 1
    return count, _table_root(row_digests)


async def copy_memory_from_snapshot(
    snapshot_directory: str | Path,
    runtime: StorageBackendRuntime,
    *,
    copy_registry: MySQLCopyAuthorityRegistry,
    token: CopyAuthorityToken,
    batch_size: int = 500,
    progress_interval: int = 5_000,
) -> MemoryCopyReport:
    """Copy all explicit Memory tables and prove normalized table parity."""

    if int(batch_size) <= 0 or int(progress_interval) <= 0:
        raise ValueError("Memory copy batch/progress sizes must be positive")
    if runtime.writer_role != StorageWriterRole.CANDIDATE_COPY:
        raise MemoryCopyError("Memory snapshot copy requires candidate-copy authority")
    await ensure_memory_storage_schema(runtime)
    source, evidence = open_memory_source(snapshot_directory)
    context = _source_context(source)
    copied_count = 0
    next_progress = int(progress_interval)
    source_global = hashlib.sha256()
    target_global = hashlib.sha256()
    reports: list[MemoryTableCopyReport] = []
    try:
        deleted_node_count = int(
            source.execute(
                "SELECT COUNT(*) FROM memory_nodes WHERE is_deleted = 1"
            ).fetchone()[0]
        )
        deleted_node_edge_count = int(
            source.execute(
                """SELECT COUNT(*) FROM memory_edges e
                JOIN memory_nodes s ON s.node_id = e.source_id
                JOIN memory_nodes t ON t.node_id = e.target_id
                WHERE s.is_deleted = 1 OR t.is_deleted = 1"""
            ).fetchone()[0]
        )
        for spec in _SPECS:
            source_row_digests: list[bytes] = []
            source_count = 0
            for batch in iter_transformed_source_rows(
                source,
                spec,
                context,
                batch_size=int(batch_size),
            ):
                for row in batch:
                    source_row_digests.append(_row_digest(spec.name, row))
                source_count += len(batch)
            source_root = _table_root(source_row_digests)
            target_count, target_root = await _target_table_root(runtime, spec)
            if target_count != source_count or target_root != source_root:
                for batch in iter_transformed_source_rows(
                    source,
                    spec,
                    context,
                    batch_size=int(batch_size),
                ):
                    async with runtime.unit_of_work() as uow:
                        await uow.session.execute(_insert_statement(spec), batch)
                target_count, target_root = await _target_table_root(runtime, spec)
            copied_count += source_count
            if copied_count >= next_progress:
                await copy_registry.set_progress(
                    token,
                    copied_records=copied_count,
                )
                while next_progress <= copied_count:
                    next_progress += int(progress_interval)
            if target_count != source_count or target_root != source_root:
                await copy_registry.record_conflict(
                    token,
                    domain_name="life_memory",
                    source_identity=spec.name,
                    expected_hash=source_root,
                    actual_hash=target_root,
                    detail=(
                        f"source_count={source_count}; target_count={target_count}; "
                        "normalized explicit-table roots differ"
                    ),
                )
                raise MemoryCopyError(f"Memory target mismatch: {spec.name}")
            table_report = MemoryTableCopyReport(
                table_name=spec.name,
                row_count=source_count,
                source_root_sha256=source_root,
                target_root_sha256=target_root,
            )
            reports.append(table_report)
            source_global.update(spec.name.encode())
            source_global.update(b"\0")
            source_global.update(str(source_count).encode())
            source_global.update(b"\0")
            source_global.update(source_root.encode())
            source_global.update(b"\n")
            target_global.update(spec.name.encode())
            target_global.update(b"\0")
            target_global.update(str(target_count).encode())
            target_global.update(b"\0")
            target_global.update(target_root.encode())
            target_global.update(b"\n")
        await copy_registry.set_progress(token, copied_records=copied_count)
        source_root_sha256 = source_global.hexdigest()
        target_root_sha256 = target_global.hexdigest()
        if source_root_sha256 != target_root_sha256:
            raise MemoryCopyError("Memory aggregate root mismatch")
        entry = evidence["entry"]
        return MemoryCopyReport(
            source_path=str(evidence["source_path"]),
            source_database_sha256=str(
                entry.get("backup_sha256") or entry.get("sha256")
            ),
            table_count=len(reports),
            copied_count=copied_count,
            deleted_node_count=deleted_node_count,
            deleted_node_edge_count=deleted_node_edge_count,
            source_root_sha256=source_root_sha256,
            target_root_sha256=target_root_sha256,
            tables=tuple(reports),
            verified=True,
        )
    finally:
        source.close()


__all__ = [
    "SOURCE_TABLE_ORDER",
    "TABLE_SPECS",
    "MemoryCopyError",
    "MemoryCopyReport",
    "MemoryTableCopyReport",
    "copy_memory_from_snapshot",
    "iter_transformed_source_rows",
    "normalize_target_row",
    "open_memory_source",
]
