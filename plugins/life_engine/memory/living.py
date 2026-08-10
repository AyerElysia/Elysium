"""Traceable, living-memory records and rebuildable recall projections.

This module stores what happened during revision and recall.  It never turns
retrieval frequency into truth and never overwrites an earlier interpretation.
Open text predicates and actions preserve subject-authored meaning without a
closed cognitive taxonomy.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Iterable, Sequence
from uuid import uuid4

from .indexing import transaction


class ArtifactHeadConflict(RuntimeError):
    """Raised when an artifact-head compare-and-swap loses ownership."""


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat()


def _json_object(value: str | None) -> dict[str, Any]:
    try:
        loaded = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _json_array(value: str | None) -> tuple[str, ...]:
    try:
        loaded = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(loaded, list):
        return ()
    return tuple(str(item) for item in loaded if str(item))


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MemoryArtifactVersion:
    """One immutable version of a memory-bearing artifact."""

    artifact_id: str
    logical_key: str
    artifact_kind: str
    content: str
    content_hash: str
    recorded_at: str
    valid_from: str = ""
    valid_to: str = ""
    authored_by: str = ""
    consciousness_instance_id: str = ""
    stream_scope: str = ""
    visibility: str = "private"
    parent_artifact_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryArtifactDescriptor:
    """Content-free immutable artifact metadata for bounded lineage reads."""

    artifact_id: str
    logical_key: str
    artifact_kind: str
    content_hash: str
    content_byte_length: int
    recorded_at: str
    authored_by: str = ""
    consciousness_instance_id: str = ""
    stream_scope: str = ""
    visibility: str = "private"
    parent_artifact_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ArtifactHead:
    """Rebuildable current pointer plus its monotonic CAS revision."""

    logical_key: str
    artifact_id: str
    projected_at: str
    revision: int


@dataclass(frozen=True, slots=True)
class MemoryDerivation:
    """Open-vocabulary provenance between immutable artifact versions."""

    derivation_id: str
    generated_artifact_id: str
    used_artifact_id: str
    predicate: str
    reason: str
    actor: str
    recorded_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryInterpretation:
    """A subject-authored interpretation, distinct from source evidence."""

    interpretation_id: str
    subject_id: str
    content: str
    authored_by: str
    consciousness_instance_id: str
    recorded_at: str
    valid_from: str = ""
    valid_to: str = ""
    stream_scope: str = ""
    visibility: str = "private"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InterpretationSource:
    """A provenance link from an interpretation to any memory entity."""

    interpretation_id: str
    entity_ref: str
    predicate: str = "draws_from"
    ordinal: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InterpretationSearchResult:
    """One interpretation candidate with provenance and retrieval rank."""

    interpretation: MemoryInterpretation
    sources: tuple[InterpretationSource, ...]
    rank_score: float
    retrieval_source: str


@dataclass(frozen=True, slots=True)
class SemanticRelation:
    """An explicit, open-vocabulary relation authored by the subject."""

    relation_id: str
    source_ref: str
    target_ref: str
    predicate: str
    reason: str
    actor: str
    recorded_at: str
    consciousness_instance_id: str = ""
    stream_scope: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RecallEpisode:
    """The complete reproducibility envelope for one act of retrieval."""

    episode_id: str
    query: str
    retrieval_intent: str
    consciousness_instance_id: str
    stream_scope: str
    context_key: str
    policy_version: str
    random_seed: int
    recorded_at: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RecallEvent:
    """One objective or subject-authored trace inside a recall episode."""

    event_id: str
    episode_id: str
    action: str
    recorded_at: str
    entity_ref: str = ""
    ordinal: int = 0
    source: str = ""
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CoRecallEvent:
    """An immutable contextual hyperedge formed by memories recalled together."""

    corecall_id: str
    episode_id: str
    context_key: str
    signal: str
    entity_refs: tuple[str, ...]
    actor: str
    reason: str
    recorded_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AssociationEvidence:
    """One inspectable dimension of a derived pairwise association."""

    source_ref: str
    target_ref: str
    context_key: str
    signal: str
    event_count: int
    last_event_at: str


@dataclass(frozen=True, slots=True)
class AssociationSelection:
    """A replayable accessibility choice with its evidence dimensions."""

    entity_ref: str
    signals: tuple[str, ...]
    event_count: int
    last_event_at: str


def create_living_memory_schema(db: sqlite3.Connection) -> None:
    """Create append-only living-memory ledgers and rebuildable projections."""

    db.row_factory = sqlite3.Row
    with transaction(db):
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_artifact_versions (
                artifact_id TEXT PRIMARY KEY,
                logical_key TEXT NOT NULL,
                artifact_kind TEXT NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                valid_from TEXT NOT NULL DEFAULT '',
                valid_to TEXT NOT NULL DEFAULT '',
                authored_by TEXT NOT NULL DEFAULT '',
                consciousness_instance_id TEXT NOT NULL DEFAULT '',
                stream_scope TEXT NOT NULL DEFAULT '',
                visibility TEXT NOT NULL DEFAULT 'private',
                parent_artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_artifact_versions_logical
                ON memory_artifact_versions(logical_key, recorded_at, artifact_id);
            CREATE INDEX IF NOT EXISTS idx_artifact_versions_hash
                ON memory_artifact_versions(content_hash, logical_key);

            CREATE TABLE IF NOT EXISTS memory_artifact_derivations (
                derivation_id TEXT PRIMARY KEY,
                generated_artifact_id TEXT NOT NULL,
                used_artifact_id TEXT NOT NULL,
                predicate TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                actor TEXT NOT NULL DEFAULT '',
                recorded_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (generated_artifact_id)
                    REFERENCES memory_artifact_versions(artifact_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (used_artifact_id)
                    REFERENCES memory_artifact_versions(artifact_id)
                    ON DELETE RESTRICT,
                CHECK (generated_artifact_id <> used_artifact_id)
            );
            CREATE INDEX IF NOT EXISTS idx_artifact_derivation_generated
                ON memory_artifact_derivations(generated_artifact_id, recorded_at);
            CREATE INDEX IF NOT EXISTS idx_artifact_derivation_used
                ON memory_artifact_derivations(used_artifact_id, recorded_at);

            CREATE TABLE IF NOT EXISTS memory_artifact_heads (
                logical_key TEXT PRIMARY KEY,
                artifact_id TEXT NOT NULL,
                projected_at TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (artifact_id)
                    REFERENCES memory_artifact_versions(artifact_id)
                    ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS memory_interpretations (
                interpretation_id TEXT PRIMARY KEY,
                subject_id TEXT NOT NULL,
                content TEXT NOT NULL,
                authored_by TEXT NOT NULL DEFAULT '',
                consciousness_instance_id TEXT NOT NULL DEFAULT '',
                recorded_at TEXT NOT NULL,
                valid_from TEXT NOT NULL DEFAULT '',
                valid_to TEXT NOT NULL DEFAULT '',
                stream_scope TEXT NOT NULL DEFAULT '',
                visibility TEXT NOT NULL DEFAULT 'private',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_interpretations_subject
                ON memory_interpretations(subject_id, recorded_at, interpretation_id);
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_interpretation_fts USING fts5(
                interpretation_id UNINDEXED,
                subject_id,
                content,
                tokenize='unicode61'
            );
            CREATE TRIGGER IF NOT EXISTS memory_interpretation_fts_insert
            AFTER INSERT ON memory_interpretations BEGIN
                INSERT INTO memory_interpretation_fts(
                    interpretation_id, subject_id, content
                ) VALUES (
                    new.interpretation_id, new.subject_id, new.content
                );
            END;
            CREATE TABLE IF NOT EXISTS memory_interpretation_sources (
                interpretation_id TEXT NOT NULL,
                entity_ref TEXT NOT NULL,
                predicate TEXT NOT NULL DEFAULT 'draws_from',
                ordinal INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (interpretation_id, entity_ref, predicate),
                FOREIGN KEY (interpretation_id)
                    REFERENCES memory_interpretations(interpretation_id)
                    ON DELETE RESTRICT
            );
            CREATE INDEX IF NOT EXISTS idx_interpretation_sources_entity
                ON memory_interpretation_sources(entity_ref, interpretation_id);

            CREATE TABLE IF NOT EXISTS memory_semantic_relations (
                relation_id TEXT PRIMARY KEY,
                source_ref TEXT NOT NULL,
                target_ref TEXT NOT NULL,
                predicate TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                actor TEXT NOT NULL DEFAULT '',
                recorded_at TEXT NOT NULL,
                consciousness_instance_id TEXT NOT NULL DEFAULT '',
                stream_scope TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                CHECK (source_ref <> target_ref)
            );
            CREATE INDEX IF NOT EXISTS idx_semantic_relations_source
                ON memory_semantic_relations(source_ref, recorded_at);
            CREATE INDEX IF NOT EXISTS idx_semantic_relations_target
                ON memory_semantic_relations(target_ref, recorded_at);

            CREATE TABLE IF NOT EXISTS memory_recall_sessions (
                episode_id TEXT PRIMARY KEY,
                query TEXT NOT NULL,
                retrieval_intent TEXT NOT NULL DEFAULT '',
                consciousness_instance_id TEXT NOT NULL DEFAULT '',
                stream_scope TEXT NOT NULL DEFAULT '',
                context_key TEXT NOT NULL DEFAULT '',
                policy_version TEXT NOT NULL,
                random_seed INTEGER NOT NULL,
                recorded_at TEXT NOT NULL,
                context_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_recall_sessions_time
                ON memory_recall_sessions(recorded_at, episode_id);
            CREATE TABLE IF NOT EXISTS memory_recall_events (
                event_id TEXT PRIMARY KEY,
                episode_id TEXT NOT NULL,
                action TEXT NOT NULL,
                entity_ref TEXT NOT NULL DEFAULT '',
                ordinal INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                recorded_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (episode_id) REFERENCES memory_recall_sessions(episode_id)
                    ON DELETE RESTRICT
            );
            CREATE INDEX IF NOT EXISTS idx_recall_events_episode
                ON memory_recall_events(episode_id, ordinal, recorded_at, event_id);
            CREATE INDEX IF NOT EXISTS idx_recall_events_entity
                ON memory_recall_events(entity_ref, action, recorded_at);

            CREATE TABLE IF NOT EXISTS memory_corecall_events (
                corecall_id TEXT PRIMARY KEY,
                episode_id TEXT NOT NULL,
                context_key TEXT NOT NULL DEFAULT '',
                signal TEXT NOT NULL,
                entity_refs_json TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                recorded_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (episode_id) REFERENCES memory_recall_sessions(episode_id)
                    ON DELETE RESTRICT
            );
            CREATE INDEX IF NOT EXISTS idx_corecall_events_episode
                ON memory_corecall_events(episode_id, recorded_at, corecall_id);
            CREATE INDEX IF NOT EXISTS idx_corecall_events_context
                ON memory_corecall_events(context_key, recorded_at, corecall_id);

            CREATE TABLE IF NOT EXISTS memory_association_projection (
                source_ref TEXT NOT NULL,
                target_ref TEXT NOT NULL,
                context_key TEXT NOT NULL DEFAULT '',
                signal TEXT NOT NULL,
                event_count INTEGER NOT NULL,
                last_event_at TEXT NOT NULL,
                PRIMARY KEY (source_ref, target_ref, context_key, signal),
                CHECK (source_ref < target_ref)
            );
            CREATE INDEX IF NOT EXISTS idx_association_projection_source
                ON memory_association_projection(source_ref, context_key, signal);
            CREATE INDEX IF NOT EXISTS idx_association_projection_target
                ON memory_association_projection(target_ref, context_key, signal);
            """
        )
        head_columns = {
            str(row[1])
            for row in db.execute("PRAGMA table_info(memory_artifact_heads)")
        }
        if "revision" not in head_columns:
            db.execute(
                "ALTER TABLE memory_artifact_heads "
                "ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
            )
        for table in (
            "memory_artifact_versions",
            "memory_artifact_derivations",
            "memory_interpretations",
            "memory_interpretation_sources",
            "memory_semantic_relations",
            "memory_recall_sessions",
            "memory_recall_events",
            "memory_corecall_events",
        ):
            db.executescript(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table}_immutable_update
                BEFORE UPDATE ON {table} BEGIN
                    SELECT RAISE(ABORT, 'LivingMemoryRecordImmutable');
                END;
                CREATE TRIGGER IF NOT EXISTS {table}_immutable_delete
                BEFORE DELETE ON {table} BEGIN
                    SELECT RAISE(ABORT, 'LivingMemoryRecordImmutable');
                END;
                """
            )
        db.execute(
            """INSERT INTO memory_interpretation_fts(
                interpretation_id, subject_id, content
            )
            SELECT i.interpretation_id, i.subject_id, i.content
            FROM memory_interpretations i
            WHERE NOT EXISTS (
                SELECT 1 FROM memory_interpretation_fts f
                WHERE f.interpretation_id = i.interpretation_id
            )"""
        )


def _artifact_from_row(row: sqlite3.Row) -> MemoryArtifactVersion:
    return MemoryArtifactVersion(
        artifact_id=str(row["artifact_id"]),
        logical_key=str(row["logical_key"]),
        artifact_kind=str(row["artifact_kind"]),
        content=str(row["content"]),
        content_hash=str(row["content_hash"]),
        recorded_at=str(row["recorded_at"]),
        valid_from=str(row["valid_from"]),
        valid_to=str(row["valid_to"]),
        authored_by=str(row["authored_by"]),
        consciousness_instance_id=str(row["consciousness_instance_id"]),
        stream_scope=str(row["stream_scope"]),
        visibility=str(row["visibility"]),
        parent_artifact_ids=_json_array(row["parent_artifact_ids_json"]),
        metadata=_json_object(row["metadata_json"]),
    )


def _artifact_descriptor_from_row(row: sqlite3.Row) -> MemoryArtifactDescriptor:
    return MemoryArtifactDescriptor(
        artifact_id=str(row["artifact_id"]),
        logical_key=str(row["logical_key"]),
        artifact_kind=str(row["artifact_kind"]),
        content_hash=str(row["content_hash"]),
        content_byte_length=int(row["content_byte_length"]),
        recorded_at=str(row["recorded_at"]),
        authored_by=str(row["authored_by"]),
        consciousness_instance_id=str(row["consciousness_instance_id"]),
        stream_scope=str(row["stream_scope"]),
        visibility=str(row["visibility"]),
        parent_artifact_ids=_json_array(row["parent_artifact_ids_json"]),
        metadata=_json_object(row["metadata_json"]),
    )


def append_artifact_version(
    db: sqlite3.Connection,
    version: MemoryArtifactVersion,
    *,
    derivations: Sequence[MemoryDerivation] = (),
    expected_head_revision: int | None = None,
) -> MemoryArtifactVersion:
    """Append one version and CAS its rebuildable head projection.

    ``expected_head_revision`` is required by backend-neutral callers.  The
    optional form preserves compatibility for legacy in-process callers while
    still taking an exact revision snapshot inside the same SQLite write
    transaction.  A stale explicit expectation never falls back to last-write
    wins.
    """

    normalized = replace(
        version,
        content_hash=version.content_hash or _content_hash(version.content),
        recorded_at=version.recorded_at or _now_iso(),
        parent_artifact_ids=tuple(dict.fromkeys(version.parent_artifact_ids)),
    )
    if normalized.content_hash != _content_hash(normalized.content):
        raise ValueError(f"ArtifactContentHashMismatch:{normalized.artifact_id}")
    with transaction(db):
        head_row = db.execute(
            """SELECT logical_key, artifact_id, projected_at, revision
            FROM memory_artifact_heads WHERE logical_key = ?""",
            (normalized.logical_key,),
        ).fetchone()
        current_revision = int(head_row["revision"]) if head_row is not None else 0
        expected_revision = (
            current_revision
            if expected_head_revision is None
            else max(0, int(expected_head_revision))
        )
        if expected_revision != current_revision:
            raise ArtifactHeadConflict(
                "artifact head revision conflict: "
                f"logical_key={normalized.logical_key!r}, "
                f"expected={expected_revision}, actual={current_revision}"
            )
        existing = db.execute(
            "SELECT * FROM memory_artifact_versions WHERE artifact_id = ?",
            (normalized.artifact_id,),
        ).fetchone()
        if existing is not None:
            persisted = _artifact_from_row(existing)
            if persisted != normalized:
                raise ValueError(f"ArtifactIdentityConflict:{normalized.artifact_id}")
        else:
            for parent_id in normalized.parent_artifact_ids:
                if (
                    db.execute(
                        "SELECT 1 FROM memory_artifact_versions WHERE artifact_id = ?",
                        (parent_id,),
                    ).fetchone()
                    is None
                ):
                    raise ValueError(f"ArtifactParentMissing:{parent_id}")
            db.execute(
                """INSERT INTO memory_artifact_versions (
                    artifact_id, logical_key, artifact_kind, content, content_hash,
                    recorded_at, valid_from, valid_to, authored_by,
                    consciousness_instance_id, stream_scope, visibility,
                    parent_artifact_ids_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    normalized.artifact_id,
                    normalized.logical_key,
                    normalized.artifact_kind,
                    normalized.content,
                    normalized.content_hash,
                    normalized.recorded_at,
                    normalized.valid_from,
                    normalized.valid_to,
                    normalized.authored_by,
                    normalized.consciousness_instance_id,
                    normalized.stream_scope,
                    normalized.visibility,
                    json.dumps(normalized.parent_artifact_ids, ensure_ascii=False),
                    json.dumps(normalized.metadata, ensure_ascii=False),
                ),
            )
            for derivation in derivations:
                if derivation.generated_artifact_id != normalized.artifact_id:
                    raise ValueError(
                        f"ArtifactDerivationTargetMismatch:{derivation.derivation_id}"
                    )
                db.execute(
                    """INSERT INTO memory_artifact_derivations (
                        derivation_id, generated_artifact_id, used_artifact_id,
                        predicate, reason, actor, recorded_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        derivation.derivation_id,
                        derivation.generated_artifact_id,
                        derivation.used_artifact_id,
                        derivation.predicate,
                        derivation.reason,
                        derivation.actor,
                        derivation.recorded_at or normalized.recorded_at,
                        json.dumps(derivation.metadata, ensure_ascii=False),
                    ),
                )
        if (
            head_row is not None
            and str(head_row["artifact_id"]) == normalized.artifact_id
        ):
            return normalized
        if head_row is None:
            try:
                db.execute(
                    """INSERT INTO memory_artifact_heads
                    (logical_key, artifact_id, projected_at, revision)
                    VALUES (?, ?, ?, 1)""",
                    (
                        normalized.logical_key,
                        normalized.artifact_id,
                        normalized.recorded_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ArtifactHeadConflict(
                    f"artifact head was concurrently created: {normalized.logical_key!r}"
                ) from exc
        else:
            cursor = db.execute(
                """UPDATE memory_artifact_heads
                SET artifact_id = ?, projected_at = ?, revision = revision + 1
                WHERE logical_key = ? AND revision = ?""",
                (
                    normalized.artifact_id,
                    normalized.recorded_at,
                    normalized.logical_key,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise ArtifactHeadConflict(
                    f"artifact head CAS failed: {normalized.logical_key!r}"
                )
    return normalized


def new_artifact_version(
    *,
    logical_key: str,
    artifact_kind: str,
    content: str,
    parent_artifact_ids: Sequence[str] = (),
    **kwargs: Any,
) -> MemoryArtifactVersion:
    """Build a complete artifact version with a content-addressed identity."""

    digest = _content_hash(content)
    identity = hashlib.sha256(
        (logical_key + "\0" + digest + "\0" + "\0".join(parent_artifact_ids)).encode(
            "utf-8"
        )
    ).hexdigest()
    return MemoryArtifactVersion(
        artifact_id=f"artifact_{identity}",
        logical_key=logical_key,
        artifact_kind=artifact_kind,
        content=content,
        content_hash=digest,
        parent_artifact_ids=tuple(parent_artifact_ids),
        recorded_at=str(kwargs.pop("recorded_at", "") or _now_iso()),
        **kwargs,
    )


def list_artifact_history(
    db: sqlite3.Connection,
    logical_key: str,
) -> list[MemoryArtifactVersion]:
    """Return every version of an artifact in recorded order."""

    rows = db.execute(
        """SELECT * FROM memory_artifact_versions WHERE logical_key = ?
        ORDER BY recorded_at, artifact_id""",
        (logical_key,),
    ).fetchall()
    return [_artifact_from_row(row) for row in rows]


def get_artifact_version(
    db: sqlite3.Connection,
    artifact_id: str,
) -> MemoryArtifactVersion | None:
    """Read one immutable artifact by identity without scanning its lineage."""

    row = db.execute(
        "SELECT * FROM memory_artifact_versions WHERE artifact_id = ?",
        (str(artifact_id),),
    ).fetchone()
    return _artifact_from_row(row) if row is not None else None


def list_artifact_descriptors(
    db: sqlite3.Connection,
    logical_key: str,
) -> list[MemoryArtifactDescriptor]:
    """Return content-free lineage rows; immutable content is never loaded."""

    rows = db.execute(
        """SELECT artifact_id, logical_key, artifact_kind, content_hash,
        LENGTH(CAST(content AS BLOB)) AS content_byte_length,
        recorded_at, authored_by, consciousness_instance_id, stream_scope,
        visibility, parent_artifact_ids_json, metadata_json
        FROM memory_artifact_versions WHERE logical_key = ?
        ORDER BY recorded_at, artifact_id""",
        (logical_key,),
    ).fetchall()
    return [_artifact_descriptor_from_row(row) for row in rows]


def get_artifact_head(
    db: sqlite3.Connection,
    logical_key: str,
) -> MemoryArtifactVersion | None:
    """Read the current rebuildable head for one logical artifact."""

    row = db.execute(
        """SELECT v.* FROM memory_artifact_heads h
        JOIN memory_artifact_versions v ON v.artifact_id = h.artifact_id
        WHERE h.logical_key = ?""",
        (logical_key,),
    ).fetchone()
    return _artifact_from_row(row) if row is not None else None


def get_artifact_head_state(
    db: sqlite3.Connection,
    logical_key: str,
) -> ArtifactHead | None:
    """Read the current artifact pointer without loading immutable content."""

    row = db.execute(
        """SELECT logical_key, artifact_id, projected_at, revision
        FROM memory_artifact_heads WHERE logical_key = ?""",
        (logical_key,),
    ).fetchone()
    if row is None:
        return None
    return ArtifactHead(
        logical_key=str(row["logical_key"]),
        artifact_id=str(row["artifact_id"]),
        projected_at=str(row["projected_at"]),
        revision=int(row["revision"]),
    )


def list_artifact_heads(
    db: sqlite3.Connection,
) -> dict[str, MemoryArtifactVersion]:
    """Return the rebuildable current head for every logical artifact."""

    rows = db.execute(
        """SELECT v.* FROM memory_artifact_heads h
        JOIN memory_artifact_versions v ON v.artifact_id = h.artifact_id
        ORDER BY h.logical_key"""
    ).fetchall()
    return {str(row["logical_key"]): _artifact_from_row(row) for row in rows}


def append_interpretation(
    db: sqlite3.Connection,
    interpretation: MemoryInterpretation,
    *,
    sources: Sequence[InterpretationSource] = (),
) -> MemoryInterpretation:
    """Append an interpretation and its provenance without judging its truth."""

    normalized = replace(
        interpretation,
        recorded_at=interpretation.recorded_at or _now_iso(),
        content=interpretation.content.strip(),
    )
    with transaction(db):
        existing = db.execute(
            "SELECT * FROM memory_interpretations WHERE interpretation_id = ?",
            (normalized.interpretation_id,),
        ).fetchone()
        if existing is not None:
            persisted = _interpretation_from_row(existing)
            if persisted != normalized:
                raise ValueError(
                    f"InterpretationIdentityConflict:{normalized.interpretation_id}"
                )
            return persisted
        db.execute(
            """INSERT INTO memory_interpretations (
                interpretation_id, subject_id, content, authored_by,
                consciousness_instance_id, recorded_at, valid_from, valid_to,
                stream_scope, visibility, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                normalized.interpretation_id,
                normalized.subject_id,
                normalized.content,
                normalized.authored_by,
                normalized.consciousness_instance_id,
                normalized.recorded_at,
                normalized.valid_from,
                normalized.valid_to,
                normalized.stream_scope,
                normalized.visibility,
                json.dumps(normalized.metadata, ensure_ascii=False),
            ),
        )
        for source in sources:
            if source.interpretation_id != normalized.interpretation_id:
                raise ValueError(
                    f"InterpretationSourceTargetMismatch:{source.entity_ref}"
                )
            db.execute(
                """INSERT INTO memory_interpretation_sources (
                    interpretation_id, entity_ref, predicate, ordinal,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?)""",
                (
                    source.interpretation_id,
                    source.entity_ref,
                    source.predicate,
                    int(source.ordinal),
                    json.dumps(source.metadata, ensure_ascii=False),
                ),
            )
    return normalized


def append_semantic_relation(
    db: sqlite3.Connection,
    relation: SemanticRelation,
) -> SemanticRelation:
    """Append a subject-authored relation without constraining its predicate."""

    normalized = replace(
        relation,
        predicate=relation.predicate.strip(),
        reason=relation.reason.strip(),
        recorded_at=relation.recorded_at or _now_iso(),
    )
    if not normalized.source_ref or not normalized.target_ref:
        raise ValueError("SemanticRelationEndpointsRequired")
    if not normalized.predicate:
        raise ValueError("SemanticRelationPredicateRequired")
    with transaction(db):
        db.execute(
            """INSERT INTO memory_semantic_relations (
                relation_id, source_ref, target_ref, predicate, reason, actor,
                recorded_at, consciousness_instance_id, stream_scope,
                metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                normalized.relation_id,
                normalized.source_ref,
                normalized.target_ref,
                normalized.predicate,
                normalized.reason,
                normalized.actor,
                normalized.recorded_at,
                normalized.consciousness_instance_id,
                normalized.stream_scope,
                json.dumps(normalized.metadata, ensure_ascii=False),
            ),
        )
    return normalized


def list_semantic_relations(
    db: sqlite3.Connection,
    entity_ref: str,
) -> list[SemanticRelation]:
    """Return every explicit relation touching an entity."""

    rows = db.execute(
        """SELECT * FROM memory_semantic_relations
        WHERE source_ref = ? OR target_ref = ?
        ORDER BY recorded_at, relation_id""",
        (entity_ref, entity_ref),
    ).fetchall()
    return [
        SemanticRelation(
            relation_id=str(row["relation_id"]),
            source_ref=str(row["source_ref"]),
            target_ref=str(row["target_ref"]),
            predicate=str(row["predicate"]),
            reason=str(row["reason"]),
            actor=str(row["actor"]),
            recorded_at=str(row["recorded_at"]),
            consciousness_instance_id=str(row["consciousness_instance_id"]),
            stream_scope=str(row["stream_scope"]),
            metadata=_json_object(row["metadata_json"]),
        )
        for row in rows
    ]


def _interpretation_from_row(row: sqlite3.Row) -> MemoryInterpretation:
    return MemoryInterpretation(
        interpretation_id=str(row["interpretation_id"]),
        subject_id=str(row["subject_id"]),
        content=str(row["content"]),
        authored_by=str(row["authored_by"]),
        consciousness_instance_id=str(row["consciousness_instance_id"]),
        recorded_at=str(row["recorded_at"]),
        valid_from=str(row["valid_from"]),
        valid_to=str(row["valid_to"]),
        stream_scope=str(row["stream_scope"]),
        visibility=str(row["visibility"]),
        metadata=_json_object(row["metadata_json"]),
    )


def list_interpretations(
    db: sqlite3.Connection,
    subject_id: str,
    *,
    recorded_as_of: str = "",
) -> list[MemoryInterpretation]:
    """Return the trace of interpretations available at a recorded time."""

    sql = "SELECT * FROM memory_interpretations WHERE subject_id = ?"
    params: list[Any] = [subject_id]
    if recorded_as_of:
        sql += " AND recorded_at <= ?"
        params.append(recorded_as_of)
    sql += " ORDER BY recorded_at, interpretation_id"
    rows = db.execute(sql, params).fetchall()
    return [_interpretation_from_row(row) for row in rows]


def get_interpretation(
    db: sqlite3.Connection,
    interpretation_id: str,
) -> tuple[MemoryInterpretation, tuple[InterpretationSource, ...]] | None:
    """Return one interpretation and its immutable provenance links."""

    row = db.execute(
        "SELECT * FROM memory_interpretations WHERE interpretation_id = ?",
        (interpretation_id,),
    ).fetchone()
    if row is None:
        return None
    source_rows = db.execute(
        """SELECT * FROM memory_interpretation_sources
        WHERE interpretation_id = ? ORDER BY ordinal, entity_ref, predicate""",
        (interpretation_id,),
    ).fetchall()
    return (
        _interpretation_from_row(row),
        tuple(
            InterpretationSource(
                interpretation_id=str(item["interpretation_id"]),
                entity_ref=str(item["entity_ref"]),
                predicate=str(item["predicate"]),
                ordinal=int(item["ordinal"]),
                metadata=_json_object(item["metadata_json"]),
            )
            for item in source_rows
        ),
    )


def _interpretation_fts_query(query: str) -> str:
    tokens = [item for item in re.findall(r"\w+", query, flags=re.UNICODE) if item]
    return " AND ".join(f'"{item.replace(chr(34), chr(34) * 2)}"' for item in tokens)


def search_interpretations(
    db: sqlite3.Connection,
    query: str,
    *,
    top_k: int,
    stream_scope: str | None,
    visibility: Sequence[str],
    recorded_as_of: str = "",
) -> list[InterpretationSearchResult]:
    """Search living interpretations while preserving source links and scope."""

    query_text = str(query or "").strip()
    if (
        db.execute(
            """SELECT 1 FROM sqlite_master
        WHERE type IN ('table', 'virtual table')
          AND name = 'memory_interpretation_fts'"""
        ).fetchone()
        is None
    ):
        return []
    visible = tuple(dict.fromkeys(str(item) for item in visibility if str(item)))
    if not query_text or not visible or top_k <= 0:
        return []
    marks = ",".join("?" for _ in visible)
    scope_sql = (
        "i.stream_scope = ''" if stream_scope is None else "i.stream_scope IN ('', ?)"
    )
    scope_params: list[Any] = [] if stream_scope is None else [stream_scope]
    time_sql = " AND i.recorded_at <= ?" if recorded_as_of else ""
    time_params: list[Any] = [recorded_as_of] if recorded_as_of else []
    limit = max(1, int(top_k))
    rows: list[sqlite3.Row] = []
    retrieval_source = "interpretation_fts"
    fts_query = _interpretation_fts_query(query_text)
    if fts_query:
        rows = db.execute(
            f"""SELECT i.* FROM memory_interpretation_fts f
            JOIN memory_interpretations i
              ON i.interpretation_id = f.interpretation_id
            WHERE memory_interpretation_fts MATCH ?
              AND i.visibility IN ({marks}) AND {scope_sql}{time_sql}
            ORDER BY bm25(memory_interpretation_fts),
                     i.recorded_at DESC, i.interpretation_id
            LIMIT ?""",
            [fts_query, *visible, *scope_params, *time_params, limit],
        ).fetchall()
    if not rows:
        retrieval_source = "interpretation_substring"
        rows = db.execute(
            f"""SELECT i.* FROM memory_interpretations i
            WHERE (instr(i.content, ?) > 0 OR instr(i.subject_id, ?) > 0)
              AND i.visibility IN ({marks}) AND {scope_sql}{time_sql}
            ORDER BY i.recorded_at DESC, i.interpretation_id
            LIMIT ?""",
            [query_text, query_text, *visible, *scope_params, *time_params, limit],
        ).fetchall()
    results: list[InterpretationSearchResult] = []
    for rank, row in enumerate(rows, start=1):
        interpretation = _interpretation_from_row(row)
        source_rows = db.execute(
            """SELECT * FROM memory_interpretation_sources
            WHERE interpretation_id = ? ORDER BY ordinal, entity_ref, predicate""",
            (interpretation.interpretation_id,),
        ).fetchall()
        sources = tuple(
            InterpretationSource(
                interpretation_id=str(item["interpretation_id"]),
                entity_ref=str(item["entity_ref"]),
                predicate=str(item["predicate"]),
                ordinal=int(item["ordinal"]),
                metadata=_json_object(item["metadata_json"]),
            )
            for item in source_rows
        )
        results.append(
            InterpretationSearchResult(
                interpretation=interpretation,
                sources=sources,
                rank_score=1.0 / float(60 + rank),
                retrieval_source=retrieval_source,
            )
        )
    return results


def begin_recall_episode(
    db: sqlite3.Connection,
    *,
    query: str,
    retrieval_intent: str = "",
    consciousness_instance_id: str = "",
    stream_scope: str = "",
    context_key: str = "",
    policy_version: str = "living-recall-v1",
    random_seed: int | None = None,
    context: dict[str, Any] | None = None,
    episode_id: str | None = None,
    recorded_at: str | None = None,
) -> RecallEpisode:
    """Begin a replayable recall episode with open retrieval intent."""

    seed = int(
        random_seed
        if random_seed is not None
        else random.SystemRandom().getrandbits(63)
    )
    episode = RecallEpisode(
        episode_id=episode_id or f"recall_{uuid4().hex}",
        query=query,
        retrieval_intent=retrieval_intent,
        consciousness_instance_id=consciousness_instance_id,
        stream_scope=stream_scope,
        context_key=context_key,
        policy_version=policy_version,
        random_seed=seed,
        recorded_at=recorded_at or _now_iso(),
        context=context or {},
    )
    with transaction(db):
        existing = db.execute(
            "SELECT * FROM memory_recall_sessions WHERE episode_id = ?",
            (episode.episode_id,),
        ).fetchone()
        if existing is not None:
            persisted = RecallEpisode(
                episode_id=str(existing["episode_id"]),
                query=str(existing["query"]),
                retrieval_intent=str(existing["retrieval_intent"]),
                consciousness_instance_id=str(existing["consciousness_instance_id"]),
                stream_scope=str(existing["stream_scope"]),
                context_key=str(existing["context_key"]),
                policy_version=str(existing["policy_version"]),
                random_seed=int(existing["random_seed"]),
                recorded_at=str(existing["recorded_at"]),
                context=_json_object(existing["context_json"]),
            )
            if persisted != episode:
                raise ValueError(f"RecallEpisodeIdentityConflict:{episode.episode_id}")
            return persisted
        db.execute(
            """INSERT INTO memory_recall_sessions (
                episode_id, query, retrieval_intent,
                consciousness_instance_id, stream_scope, context_key,
                policy_version, random_seed, recorded_at, context_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                episode.episode_id,
                episode.query,
                episode.retrieval_intent,
                episode.consciousness_instance_id,
                episode.stream_scope,
                episode.context_key,
                episode.policy_version,
                episode.random_seed,
                episode.recorded_at,
                json.dumps(episode.context, ensure_ascii=False),
            ),
        )
    return episode


def append_recall_events(
    db: sqlite3.Connection,
    events: Sequence[RecallEvent],
) -> tuple[RecallEvent, ...]:
    """Append open-vocabulary recall traces without semantic inference."""

    if not events:
        return ()
    normalized_events = tuple(
        replace(event, recorded_at=event.recorded_at or _now_iso()) for event in events
    )
    with transaction(db):
        for event in normalized_events:
            if (
                db.execute(
                    "SELECT 1 FROM memory_recall_sessions WHERE episode_id = ?",
                    (event.episode_id,),
                ).fetchone()
                is None
            ):
                raise ValueError(f"RecallEpisodeMissing:{event.episode_id}")
            existing = db.execute(
                "SELECT * FROM memory_recall_events WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()
            if existing is not None:
                persisted = RecallEvent(
                    event_id=str(existing["event_id"]),
                    episode_id=str(existing["episode_id"]),
                    action=str(existing["action"]),
                    recorded_at=str(existing["recorded_at"]),
                    entity_ref=str(existing["entity_ref"]),
                    ordinal=int(existing["ordinal"]),
                    source=str(existing["source"]),
                    reason=str(existing["reason"]),
                    metadata=_json_object(existing["metadata_json"]),
                )
                if persisted != event:
                    raise ValueError(f"RecallEventIdentityConflict:{event.event_id}")
                continue
            db.execute(
                """INSERT INTO memory_recall_events (
                    event_id, episode_id, action, entity_ref, ordinal, source,
                    reason, recorded_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.event_id,
                    event.episode_id,
                    event.action,
                    event.entity_ref,
                    int(event.ordinal),
                    event.source,
                    event.reason,
                    event.recorded_at,
                    json.dumps(event.metadata, ensure_ascii=False),
                ),
            )
    return normalized_events


def append_corecall_event(
    db: sqlite3.Connection,
    event: CoRecallEvent,
) -> CoRecallEvent:
    """Append one contextual hyperedge and update its rebuildable projection."""

    entity_refs = tuple(sorted(dict.fromkeys(event.entity_refs)))
    if len(entity_refs) < 2:
        raise ValueError("CoRecallRequiresAtLeastTwoEntities")
    normalized = replace(
        event,
        entity_refs=entity_refs,
        recorded_at=event.recorded_at or _now_iso(),
    )
    with transaction(db):
        if (
            db.execute(
                "SELECT 1 FROM memory_recall_sessions WHERE episode_id = ?",
                (normalized.episode_id,),
            ).fetchone()
            is None
        ):
            raise ValueError(f"RecallEpisodeMissing:{normalized.episode_id}")
        existing = db.execute(
            "SELECT * FROM memory_corecall_events WHERE corecall_id = ?",
            (normalized.corecall_id,),
        ).fetchone()
        if existing is not None:
            persisted = CoRecallEvent(
                corecall_id=str(existing["corecall_id"]),
                episode_id=str(existing["episode_id"]),
                context_key=str(existing["context_key"]),
                signal=str(existing["signal"]),
                entity_refs=_json_array(existing["entity_refs_json"]),
                actor=str(existing["actor"]),
                reason=str(existing["reason"]),
                recorded_at=str(existing["recorded_at"]),
                metadata=_json_object(existing["metadata_json"]),
            )
            if persisted != normalized:
                raise ValueError(f"CoRecallIdentityConflict:{normalized.corecall_id}")
            return persisted
        db.execute(
            """INSERT INTO memory_corecall_events (
                corecall_id, episode_id, context_key, signal,
                entity_refs_json, actor, reason, recorded_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                normalized.corecall_id,
                normalized.episode_id,
                normalized.context_key,
                normalized.signal,
                json.dumps(normalized.entity_refs, ensure_ascii=False),
                normalized.actor,
                normalized.reason,
                normalized.recorded_at,
                json.dumps(normalized.metadata, ensure_ascii=False),
            ),
        )
        _project_corecall(db, normalized)
    return normalized


def _pairs(entity_refs: Sequence[str]) -> Iterable[tuple[str, str]]:
    for index, source in enumerate(entity_refs):
        for target in entity_refs[index + 1 :]:
            if source != target:
                yield (source, target) if source < target else (target, source)


def _project_corecall(db: sqlite3.Connection, event: CoRecallEvent) -> None:
    for source_ref, target_ref in _pairs(event.entity_refs):
        db.execute(
            """INSERT INTO memory_association_projection (
                source_ref, target_ref, context_key, signal,
                event_count, last_event_at
            ) VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(source_ref, target_ref, context_key, signal)
            DO UPDATE SET
                event_count = memory_association_projection.event_count + 1,
                last_event_at = CASE
                    WHEN excluded.last_event_at > last_event_at
                    THEN excluded.last_event_at ELSE last_event_at END""",
            (
                source_ref,
                target_ref,
                event.context_key,
                event.signal,
                event.recorded_at,
            ),
        )


def rebuild_association_projection(db: sqlite3.Connection) -> int:
    """Rebuild pairwise accessibility evidence from immutable hyperedges."""

    rows = db.execute(
        """SELECT * FROM memory_corecall_events
        ORDER BY recorded_at, corecall_id"""
    ).fetchall()
    with transaction(db):
        db.execute("DELETE FROM memory_association_projection")
        for row in rows:
            _project_corecall(
                db,
                CoRecallEvent(
                    corecall_id=str(row["corecall_id"]),
                    episode_id=str(row["episode_id"]),
                    context_key=str(row["context_key"]),
                    signal=str(row["signal"]),
                    entity_refs=_json_array(row["entity_refs_json"]),
                    actor=str(row["actor"]),
                    reason=str(row["reason"]),
                    recorded_at=str(row["recorded_at"]),
                    metadata=_json_object(row["metadata_json"]),
                ),
            )
    return len(rows)


def list_association_evidence(
    db: sqlite3.Connection,
    entity_ref: str,
    *,
    context_key: str | None = None,
) -> list[AssociationEvidence]:
    """Return separate association dimensions; no scalar truth score is made."""

    sql = """SELECT * FROM memory_association_projection
    WHERE (source_ref = ? OR target_ref = ?)"""
    params: list[Any] = [entity_ref, entity_ref]
    if context_key is not None:
        sql += " AND context_key IN (?, '')"
        params.append(context_key)
    sql += " ORDER BY last_event_at DESC, event_count DESC, signal"
    rows = db.execute(sql, params).fetchall()
    return [
        AssociationEvidence(
            source_ref=str(row["source_ref"]),
            target_ref=str(row["target_ref"]),
            context_key=str(row["context_key"]),
            signal=str(row["signal"]),
            event_count=int(row["event_count"]),
            last_event_at=str(row["last_event_at"]),
        )
        for row in rows
    ]


def choose_association_neighbours(
    db: sqlite3.Connection,
    seed_refs: Sequence[str],
    *,
    context_key: str,
    random_seed: int,
    limit: int,
) -> list[AssociationSelection]:
    """Select contextual neighbours through a recorded stochastic policy.

    Event count influences accessibility only.  Signals remain separate in the
    returned evidence and are never interpreted as confidence or truth.
    """

    seeds = tuple(dict.fromkeys(str(item) for item in seed_refs if str(item)))
    if not seeds or limit <= 0:
        return []
    evidence_by_target: dict[str, list[AssociationEvidence]] = {}
    seed_set = set(seeds)
    for seed in seeds:
        for item in list_association_evidence(db, seed, context_key=context_key):
            target = item.target_ref if item.source_ref == seed else item.source_ref
            if target in seed_set:
                continue
            evidence_by_target.setdefault(target, []).append(item)
    rng = random.Random(int(random_seed))
    ranked: list[tuple[float, str, AssociationSelection]] = []
    for target, evidence in evidence_by_target.items():
        event_count = sum(max(0, item.event_count) for item in evidence)
        if event_count <= 0:
            continue
        # Weighted random priority gives every retained neighbour a path back
        # into recall while allowing repeated co-recall to affect accessibility.
        random_value = max(rng.random(), 1e-12)
        random_priority = random_value ** (1.0 / float(event_count))
        selection = AssociationSelection(
            entity_ref=target,
            signals=tuple(sorted({item.signal for item in evidence})),
            event_count=event_count,
            last_event_at=max(item.last_event_at for item in evidence),
        )
        ranked.append((random_priority, target, selection))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in ranked[: int(limit)]]


__all__ = [
    "AssociationEvidence",
    "AssociationSelection",
    "ArtifactHead",
    "ArtifactHeadConflict",
    "CoRecallEvent",
    "InterpretationSource",
    "InterpretationSearchResult",
    "MemoryArtifactDescriptor",
    "MemoryArtifactVersion",
    "MemoryDerivation",
    "MemoryInterpretation",
    "RecallEpisode",
    "RecallEvent",
    "SemanticRelation",
    "append_artifact_version",
    "append_corecall_event",
    "append_interpretation",
    "append_recall_events",
    "append_semantic_relation",
    "begin_recall_episode",
    "choose_association_neighbours",
    "create_living_memory_schema",
    "get_artifact_head",
    "get_artifact_head_state",
    "get_artifact_version",
    "get_interpretation",
    "list_artifact_descriptors",
    "list_artifact_heads",
    "list_artifact_history",
    "list_association_evidence",
    "list_interpretations",
    "search_interpretations",
    "list_semantic_relations",
    "new_artifact_version",
    "rebuild_association_projection",
]
