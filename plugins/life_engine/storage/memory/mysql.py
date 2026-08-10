"""Fenced MySQL adapters for Life Memory domain ports."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, replace
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from src.kernel.storage import (
    CursorConflict,
    canonical_json,
    compare_and_advance_cursor,
)

from ...memory.decay import compute_memory_strength
from ...memory.edges import DIRECTIONAL_EDGE_TYPES, EdgeType, MemoryEdge
from ...memory.epistemic import (
    ClaimEvidence,
    ClaimSearchResult,
    ClaimState,
    ClaimStatus,
    CurrentFactProjection,
    EpistemicConflict,
    MemoryAuditEntry,
    MemoryBelief,
    MemoryClaim,
    MemoryDisposition,
    MemoryStateEvent,
    RetrievalEpisode,
    RetrievalExposure,
    RetrievalFeedback,
    RetrievalPlasticity,
    reduce_claim_state,
    reduce_memory_disposition,
)
from ...memory.experience import (
    EpistemicKind,
    ExperienceAppendReport,
    ExperienceRecord,
    WitnessMemory,
    WitnessSearchResult,
)
from ...memory.indexing import (
    ACTIVE_CHUNK_STATE_KEY,
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    ChunkIndexState,
    DocumentChunk,
    DocumentIdentityConflict,
    DocumentIndexResult,
    IndexJob,
    chunk_document,
)
from ...memory.lineage import MemoryCorrection
from ...memory.living import (
    ArtifactHead,
    ArtifactHeadConflict,
    AssociationEvidence,
    AssociationSelection,
    CoRecallEvent,
    InterpretationSearchResult,
    InterpretationSource,
    MemoryArtifactDescriptor,
    MemoryArtifactVersion,
    MemoryDerivation,
    MemoryInterpretation,
    RecallEpisode,
    RecallEvent,
    SemanticRelation,
)
from ...memory.nodes import (
    MemoryNode,
    NodeType,
    canonical_file_node_id,
    compute_content_hash,
)
from ...memory.search import (
    DetailedSearchResult,
    LineageNodeView,
    SearchDiagnostics,
    SearchResult,
    vector_search,
)
from ...memory.worker import (
    CHUNK_INDEX_VERSION,
    IndexWorkerReport,
    chunk_collection_metadata,
    chunk_collection_name,
)
from ..contracts import StorageBackendRuntime
from ..models import BackendKind, StorageAvailability
from .contracts import MemoryStorageBundle

_T = TypeVar("_T")
_MAX_WRITE_ATTEMPTS = 3

_UPSERT_INDEX_JOB = text(
    """INSERT INTO memory_index_jobs (
        job_id, node_id, content_hash, status, created_at,
        updated_at, attempts, error, index_revision
    ) VALUES (
        :job_id, :node_id, :content_hash, 'pending', :now,
        :now, 0, '', :revision
    ) AS incoming
    ON DUPLICATE KEY UPDATE
        node_id = incoming.node_id,
        content_hash = incoming.content_hash,
        status = 'pending', updated_at = incoming.updated_at,
        error = '', index_revision = incoming.index_revision"""
)

_MYSQL_MEMORY_READINESS_REQUIREMENTS: dict[
    str,
    dict[str, tuple[str, ...]],
] = {
    "document_index": {
        "memory_schema": ("schema_name", "version"),
        "memory_nodes": (
            "node_id",
            "document_content",
            "index_revision",
            "is_deleted",
            "embedding_content_hash",
            "legacy_fts_present",
        ),
        "memory_chunks": ("chunk_id", "node_id", "content_hash", "content"),
        "memory_index_jobs": ("job_id", "node_id", "status", "index_revision"),
        "memory_index_state": ("state_key", "collection_name", "version"),
        "memory_vector_tombstones": (
            "tombstone_id",
            "node_id",
            "chunk_id",
            "consumed_at",
        ),
    },
    "experiences": {
        "memory_experiences": (
            "event_id",
            "source_event_id",
            "sequence",
            "payload_sha256",
        ),
        "memory_experience_occurrence_aliases": (
            "occurrence_id",
            "event_id",
            "ingest_position",
        ),
    },
    "witnesses": {
        "memory_witnesses": (
            "witness_id",
            "consciousness_instance_id",
            "projection_status",
            "payload_sha256",
        ),
        "memory_witness_sources": ("witness_id", "event_id", "ordinal"),
        "memory_witness_state": (
            "consciousness_instance_id",
            "last_sequence",
            "revision",
        ),
        "memory_witness_migrations": (
            "migration_key",
            "source_hash",
            "witness_id",
        ),
    },
    "living": {
        "memory_artifact_versions": (
            "artifact_id",
            "logical_key_sha256",
            "content_hash",
            "payload_sha256",
        ),
        "memory_artifact_derivations": (
            "derivation_id",
            "generated_artifact_id",
            "used_artifact_id",
            "payload_sha256",
        ),
        "memory_artifact_heads": (
            "logical_key_sha256",
            "artifact_id",
            "revision",
        ),
        "memory_interpretations": (
            "interpretation_id",
            "subject_id",
            "payload_sha256",
        ),
        "memory_interpretation_sources": (
            "interpretation_id",
            "entity_ref_sha256",
            "payload_sha256",
        ),
        "memory_semantic_relations": (
            "relation_id",
            "source_ref_sha256",
            "target_ref_sha256",
            "payload_sha256",
        ),
        "memory_recall_sessions": (
            "episode_id",
            "consciousness_instance_id",
            "payload_sha256",
        ),
        "memory_recall_events": (
            "event_id",
            "episode_id",
            "ordinal",
            "payload_sha256",
        ),
        "memory_corecall_events": (
            "corecall_id",
            "episode_id",
            "payload_sha256",
        ),
        "memory_association_projection": (
            "source_ref_sha256",
            "target_ref_sha256",
            "context_key_sha256",
            "signal_sha256",
        ),
    },
    "epistemic": {
        "memory_claims": ("claim_id", "subject_key_sha256", "payload_sha256"),
        "memory_claim_evidence": (
            "evidence_link_id",
            "claim_id",
            "payload_sha256",
        ),
        "memory_beliefs": ("belief_id", "claim_id", "payload_sha256"),
        "memory_epistemic_conflicts": (
            "conflict_id",
            "left_claim_id",
            "right_claim_id",
            "payload_sha256",
        ),
        "memory_state_events": (
            "event_id",
            "entity_id_sha256",
            "payload_sha256",
        ),
        "memory_retrieval_episodes": (
            "episode_id",
            "consciousness_instance_id",
            "payload_sha256",
        ),
        "memory_retrieval_exposures": (
            "exposure_id",
            "episode_id",
            "entity_id_sha256",
            "payload_sha256",
        ),
        "memory_retrieval_feedback": (
            "feedback_id",
            "exposure_id",
            "payload_sha256",
        ),
    },
    "legacy_graph": {
        "memory_edges": ("edge_id", "source_id", "target_id", "edge_type"),
        "memory_corrections": (
            "correction_id",
            "topic_sha256",
            "message",
        ),
    },
}


class MySQLMemoryReadinessProbeError(RuntimeError):
    """Report a content-free failure of the shared read-only schema probe."""

    def __init__(self, error_type: str) -> None:
        safe_error_type = (
            error_type
            if error_type.isascii()
            and error_type.isidentifier()
            and len(error_type) <= 64
            else "Exception"
        )
        self.error_type = safe_error_type
        super().__init__(f"MySQLMemoryReadinessProbeFailed:{safe_error_type}")


async def inspect_mysql_memory_readiness(
    runtime: StorageBackendRuntime,
) -> dict[str, StorageAvailability]:
    """Check all Memory domain tables with one read-only metadata query.

    The selected runtime already owns schema activation and migrations. This probe only
    proves that the active database exposes the tables and key columns consumed by each
    Memory port; it never runs DDL or a second authority health check.
    """
    if (
        not runtime.enabled
        or runtime.backend != BackendKind.MYSQL
        or runtime.engine is None
    ):
        raise MySQLMemoryReadinessProbeError("RuntimeUnavailable")

    table_names = tuple(
        dict.fromkeys(
            table
            for requirements in _MYSQL_MEMORY_READINESS_REQUIREMENTS.values()
            for table in requirements
        )
    )
    statement = text(
        """SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME IN :table_names
        ORDER BY TABLE_NAME, ORDINAL_POSITION"""
    ).bindparams(bindparam("table_names", expanding=True))

    try:
        async with runtime.engine.connect() as connection:
            rows = (
                await connection.execute(statement, {"table_names": table_names})
            ).mappings()
            observed: dict[str, set[str]] = {}
            for row in rows:
                table = str(row["table_name"] or "").lower()
                column = str(row["column_name"] or "").lower()
                if table and column:
                    observed.setdefault(table, set()).add(column)
    except Exception as exc:  # noqa: BLE001 - hide all driver/server details
        raise MySQLMemoryReadinessProbeError(type(exc).__name__) from None

    return {
        domain: (
            StorageAvailability.HEALTHY
            if all(
                set(required_columns).issubset(observed.get(table, set()))
                for table, required_columns in requirements.items()
            )
            else StorageAvailability.FAILED
        )
        for domain, requirements in _MYSQL_MEMORY_READINESS_REQUIREMENTS.items()
    }


class ImmutableMemoryRecordConflict(RuntimeError):
    """Raised when one immutable identity is replayed with different evidence."""


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_value(value: Any, *, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _payload(value: Any) -> tuple[str, str]:
    body = asdict(value)
    encoded = canonical_json(body)
    return encoded, _sha256(encoded)


def _record_hash(body: dict[str, Any]) -> str:
    return _sha256(canonical_json(body))


def _row_hash(row: Any) -> str:
    return str(row["payload_sha256"] or "")


def _safe_limit(value: int, *, maximum: int = 1000) -> int:
    return max(1, min(int(value), maximum))


async def _call_external(func: Any, *args: Any, **kwargs: Any) -> Any:
    if inspect.iscoroutinefunction(func):
        return await func(*args, **kwargs)
    value = await asyncio.to_thread(func, *args, **kwargs)
    if inspect.isawaitable(value):
        return await value
    return value


def _normalize_embeddings(
    value: Any, expected_count: int
) -> tuple[list[list[float]], str]:
    model_name = str(getattr(value, "model_name", "") or "")
    raw = getattr(value, "embeddings", value)
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], str):
        raw, model_name = value
    vectors = [[float(item) for item in vector] for vector in list(raw or [])]
    if len(vectors) != expected_count or any(not vector for vector in vectors):
        raise ValueError("Embedding 响应数量或维度无效")
    dimension = len(vectors[0]) if vectors else 0
    if any(len(vector) != dimension for vector in vectors):
        raise ValueError("Embedding 向量维度不一致")
    return vectors, model_name or "unknown"


class _MySQLPort:
    def __init__(self, runtime: StorageBackendRuntime) -> None:
        if (
            not runtime.enabled
            or runtime.backend != BackendKind.MYSQL
            or runtime.engine is None
        ):
            raise RuntimeError("MySQL Memory adapter requires an enabled MySQL runtime")
        self.runtime = runtime

    @staticmethod
    def _retryable(exc: DBAPIError) -> bool:
        values = {str(item) for item in getattr(exc.orig, "args", ())}
        message = str(exc.orig).lower()
        return bool(
            {"1062", "1205", "1213"} & values
            or "duplicate entry" in message
            or "deadlock" in message
            or "lock wait timeout" in message
        )

    async def _write(
        self,
        operation: Callable[[AsyncSession], Awaitable[_T]],
    ) -> _T:
        for attempt in range(_MAX_WRITE_ATTEMPTS):
            try:
                async with self.runtime.unit_of_work() as uow:
                    return await operation(uow.session)
            except DBAPIError as exc:
                if attempt + 1 >= _MAX_WRITE_ATTEMPTS or not self._retryable(exc):
                    raise
        raise AssertionError("bounded MySQL Memory retry loop exhausted")

    async def availability(self) -> StorageAvailability:
        status = str((await self.runtime.health()).get("status") or "failed")
        try:
            return StorageAvailability(status)
        except ValueError:
            return StorageAvailability.FAILED

    async def _immutable_insert(
        self,
        session: AsyncSession,
        *,
        table: str,
        identity_column: str,
        identity: str,
        values: dict[str, Any],
        payload_sha256: str,
    ) -> bool:
        existing = (
            (
                await session.execute(
                    text(
                        f"SELECT payload_sha256 FROM {table} "
                        f"WHERE {identity_column} = :identity FOR UPDATE"
                    ),
                    {"identity": identity},
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if _row_hash(existing) != payload_sha256:
                raise ImmutableMemoryRecordConflict(
                    f"{table}:{identity} already exists with different payload"
                )
            return False
        columns = tuple(values)
        sql_columns = tuple(
            "`signal`" if column == "signal" else column for column in columns
        )
        await session.execute(
            text(
                f"INSERT INTO {table} ({', '.join(sql_columns)}) VALUES "
                f"({', '.join(':' + column for column in columns)})"
            ),
            values,
        )
        return True


class MySQLDocumentIndexProjection(_MySQLPort):
    @staticmethod
    def _chunk_from_row(row: Any) -> DocumentChunk:
        return DocumentChunk(
            chunk_id=str(row["chunk_id"]),
            node_id=str(row["node_id"]),
            chunk_index=int(row["chunk_index"]),
            content_hash=str(row["content_hash"]),
            content=str(row["content"]),
            title=str(row["title"] or ""),
        )

    @staticmethod
    def _job_from_row(row: Any) -> IndexJob:
        return IndexJob(
            job_id=str(row["job_id"]),
            node_id=str(row["node_id"]),
            content_hash=str(row["content_hash"]),
            status=str(row["status"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            attempts=int(row["attempts"]),
            error=str(row["error"] or ""),
            index_revision=int(row["index_revision"]),
        )

    async def _load_chunks(
        self,
        session: AsyncSession,
        node_id: str,
    ) -> tuple[DocumentChunk, ...]:
        rows = (
            await session.execute(
                text(
                    "SELECT * FROM memory_chunks WHERE node_id = :node_id "
                    "ORDER BY chunk_index"
                ),
                {"node_id": node_id},
            )
        ).mappings()
        return tuple(self._chunk_from_row(row) for row in rows)

    async def _upsert_in_session(
        self,
        session: AsyncSession,
        path: str,
        content: str,
        title: str,
        source_mtime: float | None,
        *,
        max_chars: int,
        overlap_chars: int,
    ) -> DocumentIndexResult:
        canonical_path, node_id = canonical_file_node_id(path)
        path_hash = _sha256(canonical_path)
        now = time.time()
        body = str(content or "")
        content_hash = compute_content_hash(body)
        chunks = tuple(
            chunk_document(
                node_id,
                body,
                str(title or ""),
                max_chars=max_chars,
                overlap_chars=overlap_chars,
            )
        )
        existing = (
            (
                await session.execute(
                    text(
                        "SELECT * FROM memory_nodes WHERE node_id = :node_id FOR UPDATE"
                    ),
                    {"node_id": node_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        path_owner = (
            (
                await session.execute(
                    text(
                        "SELECT node_id, file_path FROM memory_nodes "
                        "WHERE file_path_sha256 = :path_hash FOR UPDATE"
                    ),
                    {"path_hash": path_hash},
                )
            )
            .mappings()
            .one_or_none()
        )
        if path_owner is not None and str(path_owner["node_id"]) != node_id:
            raise DocumentIdentityConflict("document path belongs to another node")
        if existing is not None and str(existing["file_path"] or "") != canonical_path:
            raise DocumentIdentityConflict("document node ID belongs to another path")

        unchanged = bool(
            existing is not None
            and str(existing["content_hash"] or "") == content_hash
            and str(existing["title"] or "") == str(title or "")
            and (
                (existing["source_mtime"] is None and source_mtime is None)
                or (
                    existing["source_mtime"] is not None
                    and source_mtime is not None
                    and float(existing["source_mtime"]) == float(source_mtime)
                )
            )
        )
        if unchanged:
            existing_job_id = await session.scalar(
                text(
                    "SELECT job_id FROM memory_index_jobs "
                    "WHERE node_id = :node_id AND content_hash = :content_hash "
                    "ORDER BY created_at LIMIT 1"
                ),
                {"node_id": node_id, "content_hash": content_hash},
            )
            return DocumentIndexResult(
                node_id=node_id,
                file_path=canonical_path,
                content_hash=content_hash,
                chunks=await self._load_chunks(session, node_id),
                job_id=str(existing_job_id) if existing_job_id is not None else None,
                source_mtime=source_mtime,
            )

        previous_revision = int(existing["index_revision"]) if existing else 0
        revision = previous_revision + 1
        if existing is None:
            await session.execute(
                text(
                    """INSERT INTO memory_nodes (
                        node_id, node_type, file_path, file_path_sha256,
                        content_hash, document_content, title, created_at,
                        updated_at, source_mtime, index_revision
                    ) VALUES (
                        :node_id, 'file', :file_path, :path_hash,
                        :content_hash, :content, :title, :now,
                        :now, :source_mtime, :revision
                    )"""
                ),
                {
                    "node_id": node_id,
                    "file_path": canonical_path,
                    "path_hash": path_hash,
                    "content_hash": content_hash,
                    "content": body,
                    "title": str(title or ""),
                    "now": now,
                    "source_mtime": source_mtime,
                    "revision": revision,
                },
            )
        else:
            updated = await session.execute(
                text(
                    """UPDATE memory_nodes SET content_hash = :content_hash,
                        document_content = :content, title = :title,
                        updated_at = :now, source_mtime = :source_mtime,
                        embedding_synced = FALSE, index_revision = :revision
                    WHERE node_id = :node_id AND index_revision = :previous_revision"""
                ),
                {
                    "node_id": node_id,
                    "content_hash": content_hash,
                    "content": body,
                    "title": str(title or ""),
                    "now": now,
                    "source_mtime": source_mtime,
                    "revision": revision,
                    "previous_revision": previous_revision,
                },
            )
            if updated.rowcount != 1:
                raise CursorConflict(
                    "document projection revision changed concurrently"
                )

        previous_chunks = await self._load_chunks(session, node_id)
        for chunk in previous_chunks:
            await session.execute(
                text(
                    """INSERT INTO memory_vector_tombstones (
                        node_id, chunk_id, collection_name, created_at
                    ) VALUES (:node_id, :chunk_id, '', :created_at)"""
                ),
                {
                    "node_id": node_id,
                    "chunk_id": chunk.chunk_id,
                    "created_at": now,
                },
            )
        await session.execute(
            text("DELETE FROM memory_chunks WHERE node_id = :node_id"),
            {"node_id": node_id},
        )
        for chunk in chunks:
            await session.execute(
                text(
                    """INSERT INTO memory_chunks (
                        chunk_id, node_id, chunk_index, content_hash,
                        content, title, created_at, updated_at
                    ) VALUES (
                        :chunk_id, :node_id, :chunk_index, :content_hash,
                        :content, :title, :now, :now
                    )"""
                ),
                {**asdict(chunk), "now": now},
            )

        job_id = f"{node_id}:{content_hash}" if body else None
        if job_id:
            await session.execute(
                _UPSERT_INDEX_JOB,
                {
                    "job_id": job_id,
                    "node_id": node_id,
                    "content_hash": content_hash,
                    "now": now,
                    "revision": revision,
                },
            )
        return DocumentIndexResult(
            node_id=node_id,
            file_path=canonical_path,
            content_hash=content_hash,
            chunks=chunks,
            job_id=job_id,
            source_mtime=source_mtime,
        )

    async def upsert_document(
        self,
        path: str,
        content: str,
        title: str = "",
        source_mtime: float | None = None,
        *,
        max_chars: int | None = None,
        overlap_chars: int | None = None,
    ) -> DocumentIndexResult:
        async def _operation(session: AsyncSession) -> DocumentIndexResult:
            return await self._upsert_in_session(
                session,
                path,
                content,
                title,
                source_mtime,
                max_chars=(DEFAULT_CHUNK_SIZE if max_chars is None else int(max_chars)),
                overlap_chars=(
                    DEFAULT_CHUNK_OVERLAP
                    if overlap_chars is None
                    else int(overlap_chars)
                ),
            )

        return await self._write(_operation)

    async def delete_document(self, path: str) -> bool:
        canonical_path, node_id = canonical_file_node_id(path)

        async def _operation(session: AsyncSession) -> bool:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT file_path FROM memory_nodes "
                            "WHERE node_id = :node_id FOR UPDATE"
                        ),
                        {"node_id": node_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return False
            if str(row["file_path"] or "") != canonical_path:
                raise DocumentIdentityConflict(
                    "document node ID belongs to another path"
                )
            chunks = await self._load_chunks(session, node_id)
            now = time.time()
            for chunk in chunks:
                await session.execute(
                    text(
                        "INSERT INTO memory_vector_tombstones "
                        "(node_id, chunk_id, collection_name, created_at) "
                        "VALUES (:node_id, :chunk_id, '', :created_at)"
                    ),
                    {
                        "node_id": node_id,
                        "chunk_id": chunk.chunk_id,
                        "created_at": now,
                    },
                )
            await session.execute(
                text("DELETE FROM memory_nodes WHERE node_id = :node_id"),
                {"node_id": node_id},
            )
            return True

        return await self._write(_operation)

    async def move_document(self, old_path: str, new_path: str) -> bool:
        old_canonical, old_id = canonical_file_node_id(old_path)
        new_canonical, new_id = canonical_file_node_id(new_path)
        if old_id == new_id:
            return old_canonical == new_canonical

        async def _operation(session: AsyncSession) -> bool:
            old = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM memory_nodes WHERE node_id = :node_id FOR UPDATE"
                        ),
                        {"node_id": old_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if old is None:
                return False
            if str(old["file_path"] or "") != old_canonical:
                raise DocumentIdentityConflict("source node path is inconsistent")
            target = (
                (
                    await session.execute(
                        text(
                            "SELECT node_id FROM memory_nodes "
                            "WHERE file_path_sha256 = :path_hash FOR UPDATE"
                        ),
                        {"path_hash": _sha256(new_canonical)},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if target is not None:
                raise DocumentIdentityConflict("target document already exists")
            await self._upsert_in_session(
                session,
                new_canonical,
                str(old["document_content"] or ""),
                str(old["title"] or ""),
                float(old["source_mtime"]) if old["source_mtime"] is not None else None,
                max_chars=DEFAULT_CHUNK_SIZE,
                overlap_chars=DEFAULT_CHUNK_OVERLAP,
            )
            await session.execute(
                text(
                    "UPDATE memory_corrections SET related_node_id = :new_id "
                    "WHERE related_node_id = :old_id"
                ),
                {"new_id": new_id, "old_id": old_id},
            )
            edge_rows = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM memory_edges WHERE source_id = :old_id "
                            "OR target_id = :old_id FOR UPDATE"
                        ),
                        {"old_id": old_id},
                    )
                )
                .mappings()
                .all()
            )
            for edge in edge_rows:
                source_id = (
                    new_id
                    if str(edge["source_id"]) == old_id
                    else str(edge["source_id"])
                )
                target_id = (
                    new_id
                    if str(edge["target_id"]) == old_id
                    else str(edge["target_id"])
                )
                if source_id == target_id:
                    continue
                await session.execute(
                    text(
                        """INSERT INTO memory_edges (
                            edge_id, source_id, target_id, edge_type, weight,
                            base_strength, reinforcement, activation_count,
                            last_activated_at, reason, created_at, bidirectional
                        ) VALUES (
                            :edge_id, :source_id, :target_id, :edge_type, :weight,
                            :base_strength, :reinforcement, :activation_count,
                            :last_activated_at, :reason, :created_at, :bidirectional
                        ) ON DUPLICATE KEY UPDATE
                            weight = GREATEST(weight, VALUES(weight)),
                            base_strength = GREATEST(base_strength, VALUES(base_strength)),
                            reinforcement = GREATEST(reinforcement, VALUES(reinforcement)),
                            activation_count = GREATEST(activation_count, VALUES(activation_count)),
                            last_activated_at = GREATEST(last_activated_at, VALUES(last_activated_at))"""
                    ),
                    {**dict(edge), "source_id": source_id, "target_id": target_id},
                )
            await session.execute(
                text(
                    "DELETE FROM memory_edges WHERE source_id = :old_id "
                    "OR target_id = :old_id"
                ),
                {"old_id": old_id},
            )
            await session.execute(
                text("DELETE FROM memory_nodes WHERE node_id = :old_id"),
                {"old_id": old_id},
            )
            return True

        return await self._write(_operation)

    async def list_jobs(
        self,
        *,
        status: str = "pending",
        limit: int = 100,
    ) -> list[IndexJob]:
        clause = "WHERE status = :status"
        params: dict[str, Any] = {
            "limit": _safe_limit(limit),
            "status": status,
        }
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        f"SELECT * FROM memory_index_jobs {clause} "
                        "ORDER BY updated_at, job_id LIMIT :limit"
                    ),
                    params,
                )
            ).mappings()
            return [self._job_from_row(row) for row in rows]

    async def claim_jobs(self, *, limit: int = 10) -> list[IndexJob]:
        async def _operation(session: AsyncSession) -> list[IndexJob]:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM memory_index_jobs WHERE status = 'pending' "
                            "ORDER BY updated_at, job_id LIMIT :limit "
                            "FOR UPDATE SKIP LOCKED"
                        ),
                        {"limit": _safe_limit(limit)},
                    )
                )
                .mappings()
                .all()
            )
            now = time.time()
            for row in rows:
                await session.execute(
                    text(
                        "UPDATE memory_index_jobs SET status = 'processing', "
                        "attempts = attempts + 1, updated_at = :now, error = '' "
                        "WHERE job_id = :job_id"
                    ),
                    {"now": now, "job_id": str(row["job_id"])},
                )
            return [
                replace(
                    self._job_from_row(row),
                    status="processing",
                    attempts=int(row["attempts"]) + 1,
                    updated_at=now,
                    error="",
                )
                for row in rows
            ]

        return await self._write(_operation)

    async def set_job_status(
        self,
        job_id: str,
        status: str,
        *,
        error: str = "",
    ) -> bool:
        async def _operation(session: AsyncSession) -> bool:
            result = await session.execute(
                text(
                    "UPDATE memory_index_jobs SET status = :status, error = :error, "
                    "updated_at = :updated_at WHERE job_id = :job_id"
                ),
                {
                    "status": status,
                    "error": error,
                    "updated_at": time.time(),
                    "job_id": job_id,
                },
            )
            return result.rowcount == 1

        return await self._write(_operation)

    async def enqueue_job(self, node_id: str, content_hash: str) -> str:
        job_id = f"{node_id}:{content_hash}"

        async def _operation(session: AsyncSession) -> str:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT node_id, content_hash, index_revision FROM memory_nodes "
                            "WHERE node_id = :node_id FOR UPDATE"
                        ),
                        {"node_id": node_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None or str(row["content_hash"] or "") != content_hash:
                raise ValueError("index job does not match the current document")
            now = time.time()
            await session.execute(
                _UPSERT_INDEX_JOB,
                {
                    "job_id": job_id,
                    "node_id": node_id,
                    "content_hash": content_hash,
                    "now": now,
                    "revision": int(row["index_revision"]),
                },
            )
            return job_id

        return await self._write(_operation)

    async def list_indexed_documents(self) -> list[MemoryNode]:
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT * FROM memory_nodes WHERE node_type = 'file' "
                        "AND COALESCE(is_deleted, FALSE) = FALSE "
                        "ORDER BY file_path, node_id"
                    )
                )
            ).mappings()
            return [_node_from_row(row) for row in rows]

    async def mark_documents_deleted(self, node_ids: Sequence[str]) -> int:
        identifiers = tuple(dict.fromkeys(str(node_id).strip() for node_id in node_ids))
        identifiers = tuple(node_id for node_id in identifiers if node_id)
        if not identifiers:
            return 0

        async def _operation(session: AsyncSession) -> int:
            now = time.time()
            expanding_ids = bindparam("node_ids", expanding=True)
            await session.execute(
                text(
                    "INSERT INTO memory_vector_tombstones "
                    "(node_id, chunk_id, collection_name, created_at) "
                    "SELECT c.node_id, c.chunk_id, '', :created_at "
                    "FROM memory_chunks c JOIN memory_nodes n ON n.node_id = c.node_id "
                    "WHERE c.node_id IN :node_ids "
                    "AND COALESCE(n.is_deleted, FALSE) = FALSE"
                ).bindparams(expanding_ids),
                {"node_ids": identifiers, "created_at": now},
            )
            result = await session.execute(
                text(
                    "UPDATE memory_nodes SET is_deleted = TRUE, "
                    "embedding_synced = FALSE, updated_at = :now "
                    "WHERE node_id IN :node_ids "
                    "AND COALESCE(is_deleted, FALSE) = FALSE"
                ).bindparams(expanding_ids),
                {"node_ids": identifiers, "now": now},
            )
            # 已删除节点的索引任务不再有处理意义，原子标记 stale（保留审计），
            # 避免它们停留在 pending/processing/failed 被反复认领空转。
            await session.execute(
                text(
                    "UPDATE memory_index_jobs SET status = 'stale', "
                    "error = 'InvalidDocumentIdentity', updated_at = :now "
                    "WHERE node_id IN :node_ids AND status IN "
                    "('pending', 'processing', 'failed')"
                ).bindparams(expanding_ids),
                {"node_ids": identifiers, "now": now},
            )
            return max(0, int(result.rowcount))

        return await self._write(_operation)

    async def read_chunk_index_state(self) -> ChunkIndexState | None:
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM memory_index_state WHERE state_key = :state_key"
                        ),
                        {"state_key": ACTIVE_CHUNK_STATE_KEY},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return ChunkIndexState(
            collection_name=str(row["collection_name"]),
            model_name=str(row["model_name"]),
            dimension=int(row["dimension"]),
            version=int(row["version"]),
            updated_at=float(row["updated_at"]),
        )

    async def invalidate_vector_projection(self) -> int:
        async def _operation(session: AsyncSession) -> int:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT node_id, content_hash, index_revision FROM memory_nodes "
                            "WHERE node_type = 'file' FOR UPDATE"
                        )
                    )
                )
                .mappings()
                .all()
            )
            await session.execute(
                text("DELETE FROM memory_index_state WHERE state_key = :state_key"),
                {"state_key": ACTIVE_CHUNK_STATE_KEY},
            )
            await session.execute(
                text(
                    "UPDATE memory_nodes SET embedding_synced = FALSE "
                    "WHERE node_type = 'file'"
                )
            )
            now = time.time()
            count = 0
            for row in rows:
                content_hash = str(row["content_hash"] or "")
                if not content_hash:
                    continue
                node_id = str(row["node_id"])
                await session.execute(
                    _UPSERT_INDEX_JOB,
                    {
                        "job_id": f"{node_id}:{content_hash}",
                        "node_id": node_id,
                        "content_hash": content_hash,
                        "now": now,
                        "revision": int(row["index_revision"]),
                    },
                )
                count += 1
            return count

        return await self._write(_operation)

    async def consume_vector_tombstones(
        self,
        collection: Any,
        *,
        limit: int = 200,
    ) -> int:
        if collection is None:
            return 0
        delete_func = getattr(collection, "delete", None)
        if not callable(delete_func):
            return 0
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT tombstone_id, chunk_id "
                            "FROM memory_vector_tombstones "
                            "WHERE consumed_at IS NULL "
                            "ORDER BY tombstone_id LIMIT :limit"
                        ),
                        {"limit": _safe_limit(limit)},
                    )
                )
                .mappings()
                .all()
            )
        if not rows:
            return 0
        # 同一个 chunk_id 可能因多次文档替换/删除产生多条未消费墓碑；
        # 外部向量后端（Chroma）要求单批删除内 ID 唯一，重复会使整批失败。
        # 先去重再删，删除成功后仍逐条确认每个原始 tombstone_id，避免重复墓碑永久堆积。
        unique_ids = list(dict.fromkeys(str(row["chunk_id"]) for row in rows))
        await _call_external(
            delete_func,
            ids=unique_ids,
        )

        async def _operation(session: AsyncSession) -> int:
            changed = 0
            consumed_at = time.time()
            for row in rows:
                result = await session.execute(
                    text(
                        "UPDATE memory_vector_tombstones SET consumed_at = :consumed_at "
                        "WHERE tombstone_id = :tombstone_id AND consumed_at IS NULL"
                    ),
                    {
                        "consumed_at": consumed_at,
                        "tombstone_id": int(row["tombstone_id"]),
                    },
                )
                changed += max(0, int(result.rowcount))
            return changed

        return await self._write(_operation)

    async def run_index_worker(
        self,
        *,
        limit: int,
        collection: Any,
        embed_texts_func: Any,
        collection_resolver: Any,
        collection_upsert_func: Any,
        retry_failed: bool,
        reclaim_after: float | None,
    ) -> IndexWorkerReport:
        if retry_failed:
            cutoff = time.time() - max(0.0, float(reclaim_after or 0.0))

            async def _requeue(session: AsyncSession) -> None:
                await session.execute(
                    text(
                        "UPDATE memory_index_jobs SET status = 'pending', error = '' "
                        "WHERE status = 'failed' OR "
                        "(status = 'processing' AND updated_at <= :cutoff)"
                    ),
                    {"cutoff": cutoff},
                )

            await self._write(_requeue)
        jobs = await self.claim_jobs(limit=limit)
        if not jobs:
            return IndexWorkerReport()

        assert self.runtime.engine is not None
        payloads: list[tuple[IndexJob, DocumentChunk, str, str]] = []
        stale: list[str] = []
        errors: dict[str, str] = {}
        async with self.runtime.engine.connect() as connection:
            for job in jobs:
                node = (
                    (
                        await connection.execute(
                            text("SELECT * FROM memory_nodes WHERE node_id = :node_id"),
                            {"node_id": job.node_id},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                is_deleted = bool(node.get("is_deleted")) if node is not None else False
                if node is None or is_deleted:
                    # 节点不存在或已删除：立即归类 stale，不做 chunks 检查，
                    # 避免已删除文档的 job 反复空转（InvalidDocumentIdentity）。
                    stale.append(job.job_id)
                    errors[job.job_id] = "InvalidDocumentIdentity"
                    continue
                if (
                    str(node["node_type"]) != "file"
                    or str(node["content_hash"] or "") != job.content_hash
                    or int(node["index_revision"]) != int(job.index_revision)
                ):
                    stale.append(job.job_id)
                    errors[job.job_id] = "StaleRevision"
                    continue
                chunks = (
                    (
                        await connection.execute(
                            text(
                                "SELECT * FROM memory_chunks WHERE node_id = :node_id "
                                "ORDER BY chunk_index, chunk_id"
                            ),
                            {"node_id": job.node_id},
                        )
                    )
                    .mappings()
                    .all()
                )
                if not chunks:
                    stale.append(job.job_id)
                    errors[job.job_id] = "EmptyDocument"
                    continue
                for row in chunks:
                    chunk = self._chunk_from_row(row)
                    if compute_content_hash(chunk.content) != chunk.content_hash:
                        stale.append(job.job_id)
                        errors[job.job_id] = "InvalidChunkIdentity"
                        break
                    payloads.append(
                        (job, chunk, str(node["file_path"]), str(node["title"] or ""))
                    )

        for job_id in stale:
            await self.set_job_status(job_id, "stale", error=errors[job_id])
        live_ids = {item[0].job_id for item in payloads}
        live_jobs = [job for job in jobs if job.job_id in live_ids]
        if not live_jobs:
            return IndexWorkerReport(
                claimed=len(jobs),
                stale=tuple(dict.fromkeys(stale)),
                errors=errors,
            )

        embedder = embed_texts_func
        if embedder is None:
            from ...memory.search import embed_texts as embedder
        try:
            vectors, model_name = _normalize_embeddings(
                await _call_external(embedder, [item[1].content for item in payloads]),
                len(payloads),
            )
            dimension = len(vectors[0])
            state = await self.read_chunk_index_state()
            if state is not None and (
                state.version != CHUNK_INDEX_VERSION
                or state.model_name != model_name
                or state.dimension != dimension
            ):
                raise RuntimeError("ActiveCollectionIdentityMismatch")
            if collection is None:
                if collection_resolver is None:
                    raise RuntimeError("CollectionUnavailable")
                try:
                    collection = await _call_external(
                        collection_resolver,
                        model_name,
                        dimension,
                        chunk_collection_metadata(model_name, dimension),
                    )
                except TypeError:
                    collection = await _call_external(
                        collection_resolver,
                        model_name,
                        dimension,
                    )
            if collection is None:
                raise RuntimeError("CollectionUnavailable")
            # 共享写者模式下必须走 shared 校验（backend/generation/epoch），
            # 不能用全量 validate：join_generation 的 shared token 是常量
            # "shared-generation"，与激活时写入的随机 fencing token hash
            # 永远不匹配，会导致双实例下每个批次都 failed。
            await self.runtime.validate_writer()
            upsert_kwargs = {
                "ids": [item[1].chunk_id for item in payloads],
                "embeddings": vectors,
                "documents": [item[1].content for item in payloads],
                "metadatas": [
                    {
                        "collection_kind": "life_memory_chunk",
                        "chunk_index_version": CHUNK_INDEX_VERSION,
                        "node_id": job.node_id,
                        "file_path": file_path,
                        "title": title,
                        "chunk_hash": chunk.content_hash,
                        "document_hash": job.content_hash,
                        "index_revision": job.index_revision,
                        "embedding_model": model_name,
                        "embedding_dimension": dimension,
                        "chunk_index": chunk.chunk_index,
                    }
                    for job, chunk, file_path, title in payloads
                ],
            }
            upserter = collection_upsert_func or collection.upsert
            await _call_external(upserter, **upsert_kwargs)
        except Exception as exc:  # noqa: BLE001 - isolate external embedding/vector providers
            error_type = type(exc).__name__ if not str(exc) else str(exc)
            for job in live_jobs:
                await self.set_job_status(job.job_id, "failed", error=error_type)
                errors[job.job_id] = error_type
            return IndexWorkerReport(
                claimed=len(jobs),
                embedded_chunks=0,
                failed=tuple(job.job_id for job in live_jobs),
                stale=tuple(dict.fromkeys(stale)),
                errors=errors,
            )

        collection_name = str(getattr(collection, "name", "") or "")
        if not collection_name:
            collection_name = chunk_collection_name(model_name, dimension)

        async def _complete(session: AsyncSession) -> tuple[list[str], list[str]]:
            completed: list[str] = []
            post_stale: list[str] = []
            now = time.time()
            for job in live_jobs:
                result = await session.execute(
                    text(
                        """UPDATE memory_index_jobs j
                        JOIN memory_nodes n ON n.node_id = j.node_id
                        SET j.status = 'completed', j.updated_at = :now, j.error = '',
                            n.embedding_synced = TRUE
                        WHERE j.job_id = :job_id AND j.status = 'processing'
                          AND j.content_hash = :content_hash
                          AND j.index_revision = :revision
                          AND n.content_hash = :content_hash
                          AND n.index_revision = :revision"""
                    ),
                    {
                        "now": now,
                        "job_id": job.job_id,
                        "content_hash": job.content_hash,
                        "revision": job.index_revision,
                    },
                )
                if result.rowcount == 1:
                    completed.append(job.job_id)
                else:
                    post_stale.append(job.job_id)
                    await session.execute(
                        text(
                            "UPDATE memory_index_jobs SET status = 'stale', "
                            "updated_at = :now, error = 'StaleRevision' "
                            "WHERE job_id = :job_id AND status = 'processing'"
                        ),
                        {"now": now, "job_id": job.job_id},
                    )
            await session.execute(
                text(
                    """INSERT INTO memory_index_state (
                        state_key, collection_name, model_name, dimension,
                        version, updated_at
                    ) VALUES (
                        :state_key, :collection_name, :model_name, :dimension,
                        :version, :updated_at
                    ) ON DUPLICATE KEY UPDATE
                        collection_name = VALUES(collection_name),
                        model_name = VALUES(model_name),
                        dimension = VALUES(dimension), version = VALUES(version),
                        updated_at = VALUES(updated_at)"""
                ),
                {
                    "state_key": ACTIVE_CHUNK_STATE_KEY,
                    "collection_name": collection_name,
                    "model_name": model_name,
                    "dimension": dimension,
                    "version": CHUNK_INDEX_VERSION,
                    "updated_at": now,
                },
            )
            return completed, post_stale

        completed, post_stale = await self._write(_complete)
        stale.extend(post_stale)
        errors.update({job_id: "StaleRevision" for job_id in post_stale})
        return IndexWorkerReport(
            claimed=len(jobs),
            embedded_chunks=len(payloads),
            upserted_chunks=len(payloads),
            completed=tuple(completed),
            stale=tuple(dict.fromkeys(stale)),
            model_name=model_name,
            dimension=dimension,
            errors=errors,
        )

    async def fts_search(self, query: str, *, top_k: int) -> list[tuple[Any, ...]]:
        query_text = str(query or "").strip()
        if not query_text:
            return []
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            """SELECT node_id,
                            MAX(MATCH(title, content) AGAINST (:query IN NATURAL LANGUAGE MODE)) score
                        FROM memory_chunks
                        WHERE MATCH(title, content) AGAINST (:query IN NATURAL LANGUAGE MODE)
                        GROUP BY node_id ORDER BY score DESC, node_id LIMIT :limit"""
                        ),
                        {"query": query_text, "limit": _safe_limit(top_k)},
                    )
                )
                .mappings()
                .all()
            )
            if not rows:
                rows = (
                    (
                        await connection.execute(
                            text(
                                "SELECT node_id, 1.0 score FROM memory_nodes "
                                "WHERE node_type = 'file' AND document_content LIKE :query "
                                "ORDER BY updated_at DESC, node_id LIMIT :limit"
                            ),
                            {"query": f"%{query_text}%", "limit": _safe_limit(top_k)},
                        )
                    )
                    .mappings()
                    .all()
                )
        return [(str(row["node_id"]), float(row["score"] or 0.0)) for row in rows]

    async def filter_existing_scores(
        self,
        scores: Sequence[tuple[Any, ...]],
    ) -> tuple[Any, ...]:
        ordered = list(dict.fromkeys(str(item[0]) for item in scores if item))
        if not ordered:
            return [], []
        params: dict[str, Any] = {}
        marks: list[str] = []
        for index, node_id in enumerate(ordered):
            name = f"node_{index}"
            marks.append(f":{name}")
            params[name] = node_id
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            existing = {
                str(value)
                for value in (
                    await connection.execute(
                        text(
                            "SELECT node_id FROM memory_nodes WHERE node_type = 'file' "
                            f"AND node_id IN ({', '.join(marks)})"
                        ),
                        params,
                    )
                ).scalars()
            }
        return (
            [item for item in scores if str(item[0]) in existing],
            [node_id for node_id in ordered if node_id not in existing],
        )

    async def vector_search(
        self,
        query: str,
        *,
        collection: Any,
        chunk_collection: Any,
        top_k: int,
    ) -> list[tuple[Any, ...]]:
        return await vector_search(
            query=query,
            collection=collection,
            top_k=top_k,
            filter_existing_func=self.filter_existing_scores,
            db=None,
            chunk_collection=chunk_collection,
        )

    async def get_snippet(self, node_id: str) -> str:
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            content = await connection.scalar(
                text(
                    "SELECT document_content FROM memory_nodes WHERE node_id = :node_id"
                ),
                {"node_id": node_id},
            )
        value = str(content or "").strip()
        return value if len(value) <= 300 else value[:297] + "..."

    async def search_detailed(
        self,
        query: str,
        *,
        collection: Any,
        chunk_collection: Any,
        top_k: int,
        enable_association: bool,
        file_types: Sequence[str] | None,
        time_range_days: int,
        now: Any,
        workspace_path: str | Path | None,
        emit_visual_event: Any,
    ) -> DetailedSearchResult:
        del enable_association, now, workspace_path, emit_visual_event
        started = time.monotonic()
        lexical = await self.fts_search(query, top_k=max(top_k * 4, top_k))
        vector_rows: list[tuple[Any, ...]] = []
        vector_error = ""
        if collection is not None or chunk_collection is not None:
            try:
                vector_rows = await self.vector_search(
                    query,
                    collection=collection,
                    chunk_collection=chunk_collection,
                    top_k=max(top_k * 4, top_k),
                )
            except Exception as exc:  # noqa: BLE001 - optional vector projection may degrade
                vector_error = type(exc).__name__
        ranks: dict[str, float] = {}
        sources: dict[str, str] = {}
        for rank, item in enumerate(lexical, start=1):
            node_id = str(item[0])
            ranks[node_id] = ranks.get(node_id, 0.0) + 1.0 / (60.0 + rank)
            sources[node_id] = "fts"
        for rank, item in enumerate(vector_rows, start=1):
            node_id = str(item[0])
            ranks[node_id] = ranks.get(node_id, 0.0) + 1.0 / (60.0 + rank)
            sources[node_id] = "hybrid" if node_id in sources else "vector"
        ordered = sorted(ranks, key=lambda item: (-ranks[item], item))
        if file_types:
            suffixes = {
                item.lower() if str(item).startswith(".") else "." + str(item).lower()
                for item in file_types
            }
        else:
            suffixes = set()
        results: list[SearchResult] = []
        for node_id in ordered:
            node = await self._load_node(node_id)
            if node is None or not node.file_path:
                continue
            if suffixes and not any(
                node.file_path.lower().endswith(item) for item in suffixes
            ):
                continue
            results.append(
                SearchResult(
                    file_path=node.file_path,
                    title=node.title,
                    snippet=await self.get_snippet(node_id),
                    relevance=ranks[node_id],
                    source=sources[node_id],
                    score_kind="accessibility_rank_not_truth",
                )
            )
            if len(results) >= max(1, int(top_k)):
                break
        diagnostics = SearchDiagnostics(
            degraded=bool(vector_error),
            fts_success=True,
            vector_success=bool(vector_rows),
            fts_candidate_count=len(lexical),
            vector_candidate_count=len(vector_rows),
            phase_timings={"mysql_search": time.monotonic() - started},
            error_types=({"vector": vector_error} if vector_error else {}),
            errors=(
                {"vector": "vector projection unavailable"} if vector_error else {}
            ),
        )
        return DetailedSearchResult(results=results, diagnostics=diagnostics)

    async def _load_node(self, node_id: str) -> MemoryNode | None:
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text("SELECT * FROM memory_nodes WHERE node_id = :node_id"),
                        {"node_id": node_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _node_from_row(row) if row is not None else None

    async def graph_snapshot(
        self,
        *,
        limit_nodes: int,
        min_weight: float,
        focus_id: str | None,
    ) -> dict[str, Any]:
        assert self.runtime.engine is not None
        limit = _safe_limit(limit_nodes, maximum=200)
        params: dict[str, Any] = {"limit": limit, "min_weight": float(min_weight)}
        focus_clause = ""
        if focus_id:
            focus_clause = (
                "AND (node_id = :focus_id OR node_id IN ("
                "SELECT CASE WHEN source_id = :focus_id THEN target_id ELSE source_id END "
                "FROM memory_edges WHERE weight >= :min_weight "
                "AND (source_id = :focus_id OR target_id = :focus_id)))"
            )
            params["focus_id"] = focus_id
        async with self.runtime.engine.connect() as connection:
            node_rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM memory_nodes WHERE "
                            "(node_type = 'concept' OR (node_type = 'file' AND file_path IS NOT NULL)) "
                            f"{focus_clause} ORDER BY activation_strength DESC, node_id LIMIT :limit"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
            node_ids = {str(row["node_id"]) for row in node_rows}
            if not node_ids:
                return {"nodes": [], "links": []}
            edge_rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM memory_edges WHERE weight >= :min_weight "
                            "ORDER BY weight DESC, edge_id"
                        ),
                        {"min_weight": float(min_weight)},
                    )
                )
                .mappings()
                .all()
            )
        visible_edges = [
            row
            for row in edge_rows
            if str(row["source_id"]) in node_ids and str(row["target_id"]) in node_ids
        ]
        degrees = {node_id: 0 for node_id in node_ids}
        for row in visible_edges:
            degrees[str(row["source_id"])] += 1
            degrees[str(row["target_id"])] += 1
        return {
            "nodes": [
                {
                    "id": str(row["node_id"]),
                    "type": str(row["node_type"]).upper(),
                    "title": str(row["title"] or row["file_path"] or "Untitled"),
                    "path": row["file_path"]
                    if str(row["node_type"]) == "file"
                    else None,
                    "activation": float(row["activation_strength"]),
                    "importance": float(row["importance"]),
                    "valence": float(row["emotional_valence"]),
                    "arousal": float(row["emotional_arousal"]),
                    "access_count": int(row["access_count"]),
                    "updated_at": row["updated_at"],
                    "last_accessed_at": row["last_accessed_at"],
                    "degree": degrees[str(row["node_id"])],
                }
                for row in node_rows
            ],
            "links": [
                {
                    "id": str(row["edge_id"]),
                    "source": str(row["source_id"]),
                    "target": str(row["target_id"]),
                    "type": str(row["edge_type"]),
                    "weight": float(row["weight"]),
                    "base_strength": float(row["base_strength"]),
                    "reinforcement": float(row["reinforcement"]),
                    "activation_count": int(row["activation_count"]),
                    "last_activated_at": row["last_activated_at"],
                    "reason": str(row["reason"] or ""),
                }
                for row in visible_edges
            ],
        }


def _experience_evidence_body(record: ExperienceRecord) -> dict[str, Any]:
    return {
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


def _experience_from_row(row: Any) -> ExperienceRecord:
    return ExperienceRecord(
        event_id=str(row["event_id"]),
        source_event_id=str(row["source_event_id"] or row["event_id"]),
        sequence=int(row["sequence"]),
        occurred_at=str(row["occurred_at"]),
        recorded_at=str(row["recorded_at"]),
        source=str(row["source"]),
        channel=str(row["channel"]),
        event_type=str(row["event_type"]),
        content=str(row["content"]),
        stream_id=str(row["stream_id"] or ""),
        consciousness_instance_id=str(row["consciousness_instance_id"] or ""),
        actor=str(row["actor"] or ""),
        visibility=str(row["visibility"] or "private"),
        valid_from=str(row["valid_from"] or ""),
        valid_to=str(row["valid_to"] or ""),
        metadata=dict(_json_value(row["metadata_json"], default={})),
    )


class MySQLExperienceLedgerStore(_MySQLPort):
    async def append(
        self,
        records: Sequence[ExperienceRecord],
    ) -> ExperienceAppendReport:
        async def _operation(session: AsyncSession) -> ExperienceAppendReport:
            inserted: list[ExperienceRecord] = []
            existing_records: list[ExperienceRecord] = []
            for raw_record in records:
                source_event_id = str(raw_record.source_event_id or raw_record.event_id)
                record = replace(
                    raw_record,
                    source_event_id=source_event_id,
                    recorded_at=raw_record.recorded_at or _now_iso(),
                    valid_from=raw_record.valid_from or raw_record.occurred_at,
                )
                evidence_hash = _record_hash(_experience_evidence_body(record))
                row = (
                    (
                        await session.execute(
                            text(
                                "SELECT * FROM memory_experiences "
                                "WHERE event_id = :event_id FOR UPDATE"
                            ),
                            {"event_id": record.event_id},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is not None:
                    if str(row["payload_sha256"]) != evidence_hash:
                        raise ImmutableMemoryRecordConflict(
                            f"ExperienceIdentityConflict:{record.event_id}"
                        )
                    existing_records.append(_experience_from_row(row))
                    continue

                alias = (
                    (
                        await session.execute(
                            text(
                                "SELECT event_id FROM memory_experience_occurrence_aliases "
                                "WHERE occurrence_id = :occurrence_id FOR UPDATE"
                            ),
                            {"occurrence_id": record.event_id},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if alias is not None:
                    target = (
                        (
                            await session.execute(
                                text(
                                    "SELECT * FROM memory_experiences "
                                    "WHERE event_id = :event_id"
                                ),
                                {"event_id": str(alias["event_id"])},
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if target is None:
                        raise RuntimeError(
                            f"ExperienceAliasTargetMissing:{record.event_id}"
                        )
                    existing_records.append(_experience_from_row(target))
                    continue

                if record.event_id != source_event_id:
                    legacy = (
                        (
                            await session.execute(
                                text(
                                    "SELECT * FROM memory_experiences "
                                    "WHERE event_id = :event_id FOR UPDATE"
                                ),
                                {"event_id": source_event_id},
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if (
                        legacy is not None
                        and str(legacy["payload_sha256"]) == evidence_hash
                    ):
                        await session.execute(
                            text(
                                """INSERT INTO memory_experience_occurrence_aliases (
                                    occurrence_id, event_id, source_event_id,
                                    ingest_position, recorded_at
                                ) VALUES (
                                    :occurrence_id, :event_id, :source_event_id,
                                    :ingest_position, :recorded_at
                                )"""
                            ),
                            {
                                "occurrence_id": record.event_id,
                                "event_id": str(legacy["event_id"]),
                                "source_event_id": source_event_id,
                                "ingest_position": int(record.sequence),
                                "recorded_at": _now_iso(),
                            },
                        )
                        existing_records.append(_experience_from_row(legacy))
                        continue

                await session.execute(
                    text(
                        """INSERT INTO memory_experiences (
                            event_id, source_event_id, sequence, occurred_at,
                            recorded_at, source, channel, event_type, content,
                            stream_id, consciousness_instance_id, actor,
                            visibility, valid_from, valid_to, metadata_json,
                            payload_sha256
                        ) VALUES (
                            :event_id, :source_event_id, :sequence, :occurred_at,
                            :recorded_at, :source, :channel, :event_type, :content,
                            :stream_id, :consciousness_instance_id, :actor,
                            :visibility, :valid_from, :valid_to, :metadata_json,
                            :payload_sha256
                        )"""
                    ),
                    {
                        **asdict(record),
                        "metadata_json": canonical_json(record.metadata),
                        "payload_sha256": evidence_hash,
                    },
                )
                inserted.append(record)
            return ExperienceAppendReport(
                inserted=tuple(inserted),
                existing=tuple(existing_records),
            )

        return await self._write(_operation)

    async def list_after(
        self,
        sequence: int,
        *,
        limit: int = 100,
        stream_scope: str | None = None,
    ) -> list[ExperienceRecord]:
        clause = "" if stream_scope is None else "AND stream_id = :stream_scope"
        params: dict[str, Any] = {
            "sequence": max(0, int(sequence)),
            "limit": _safe_limit(limit),
        }
        if stream_scope is not None:
            params["stream_scope"] = stream_scope
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT * FROM memory_experiences WHERE sequence > :sequence "
                        f"{clause} ORDER BY sequence, event_id LIMIT :limit"
                    ),
                    params,
                )
            ).mappings()
            return [_experience_from_row(row) for row in rows]


async def _witness_from_row(
    session: AsyncSession,
    row: Any,
) -> WitnessMemory:
    source_rows = (
        await session.execute(
            text(
                "SELECT event_id FROM memory_witness_sources "
                "WHERE witness_id = :witness_id ORDER BY ordinal"
            ),
            {"witness_id": str(row["witness_id"])},
        )
    ).mappings()
    return WitnessMemory(
        witness_id=str(row["witness_id"]),
        content=str(row["content"]),
        consciousness_instance_id=str(row["consciousness_instance_id"]),
        perspective_subject_id=str(row["perspective_subject_id"]),
        epistemic_kind=str(row["epistemic_kind"]),
        source_kind=str(row["source_kind"]),
        status=str(row["status"]),
        stream_scope=str(row["stream_scope"]),
        visibility=str(row["visibility"]),
        valid_from=str(row["valid_from"]),
        valid_to=str(row["valid_to"]),
        recorded_at=str(row["recorded_at"]),
        source_sequence_start=int(row["source_sequence_start"]),
        source_sequence_end=int(row["source_sequence_end"]),
        source_event_ids=tuple(str(item["event_id"]) for item in source_rows),
        model_task_name=str(row["model_task_name"]),
        projection_path=str(row["projection_path"] or ""),
        projection_status=str(row["projection_status"]),
        projection_error=str(row["projection_error"] or ""),
        metadata=dict(_json_value(row["metadata_json"], default={})),
    )


async def _assert_projection_path_available(
    session: AsyncSession,
    *,
    witness_id: str,
    projection_path: str,
) -> str | None:
    """Reserve one full path by its indexable digest without trusting the digest."""

    if not projection_path:
        return None
    projection_path_sha256 = _sha256(projection_path)
    existing = (
        (
            await session.execute(
                text(
                    "SELECT witness_id, projection_path FROM memory_witnesses "
                    "WHERE projection_path_sha256 = :projection_path_sha256 "
                    "FOR UPDATE"
                ),
                {"projection_path_sha256": projection_path_sha256},
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is None:
        return projection_path_sha256
    if str(existing["projection_path"] or "") != projection_path:
        raise ImmutableMemoryRecordConflict(
            "WitnessProjectionPathHashCollision: digest matched a different full path"
        )
    if str(existing["witness_id"]) == witness_id:
        return projection_path_sha256
    raise ImmutableMemoryRecordConflict(
        "WitnessProjectionPathConflict: path already belongs to another witness"
    )


class _MySQLWitnessAppendStore(_MySQLPort):
    async def append(self, **kwargs: Any) -> WitnessMemory:
        source_ids = tuple(
            dict.fromkeys(
                str(item)
                for item in (kwargs.get("source_event_ids") or ())
                if str(item)
            )
        )
        if (
            str(kwargs.get("source_kind") or "") == "experience_window"
            and not source_ids
        ):
            raise ValueError("WitnessSourceRequired")
        witness_id = str(kwargs.get("witness_id") or f"wit_{uuid4().hex}")
        recorded_at = str(kwargs.get("recorded_at") or _now_iso())
        values = {
            "witness_id": witness_id,
            "content": str(kwargs.get("content") or "").strip(),
            "consciousness_instance_id": str(
                kwargs.get("consciousness_instance_id") or ""
            ),
            "perspective_subject_id": str(kwargs.get("perspective_subject_id") or ""),
            "epistemic_kind": str(kwargs.get("epistemic_kind") or ""),
            "source_kind": str(kwargs.get("source_kind") or ""),
            "status": str(kwargs.get("status") or "active"),
            "stream_scope": str(kwargs.get("stream_scope") or ""),
            "visibility": str(kwargs.get("visibility") or "private"),
            "valid_from": str(kwargs.get("valid_from") or ""),
            "valid_to": str(kwargs.get("valid_to") or ""),
            "recorded_at": recorded_at,
            "source_sequence_start": max(
                0, int(kwargs.get("source_sequence_start") or 0)
            ),
            "source_sequence_end": max(0, int(kwargs.get("source_sequence_end") or 0)),
            "model_task_name": str(kwargs.get("model_task_name") or ""),
            "projection_path": str(kwargs.get("projection_path") or "") or None,
            "projection_status": "pending",
            "projection_error": "",
            "metadata_json": canonical_json(dict(kwargs.get("metadata") or {})),
        }
        hash_body = {
            **values,
            "projection_path": str(values["projection_path"] or ""),
            "source_event_ids": source_ids,
        }
        payload_sha256 = _record_hash(hash_body)

        async def _operation(session: AsyncSession) -> WitnessMemory:
            projection_path_sha256 = await _assert_projection_path_available(
                session,
                witness_id=witness_id,
                projection_path=str(values["projection_path"] or ""),
            )
            inserted = await self._immutable_insert(
                session,
                table="memory_witnesses",
                identity_column="witness_id",
                identity=witness_id,
                values={
                    **values,
                    "projection_path_sha256": projection_path_sha256,
                    "payload_sha256": payload_sha256,
                },
                payload_sha256=payload_sha256,
            )
            if inserted:
                for ordinal, event_id in enumerate(source_ids):
                    await session.execute(
                        text(
                            "INSERT INTO memory_witness_sources "
                            "(witness_id, event_id, ordinal) "
                            "VALUES (:witness_id, :event_id, :ordinal)"
                        ),
                        {
                            "witness_id": witness_id,
                            "event_id": event_id,
                            "ordinal": ordinal,
                        },
                    )
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM memory_witnesses WHERE witness_id = :witness_id"
                        ),
                        {"witness_id": witness_id},
                    )
                )
                .mappings()
                .one()
            )
            witness = await _witness_from_row(session, row)
            if witness.source_event_ids != source_ids:
                raise ImmutableMemoryRecordConflict(
                    f"memory_witnesses:{witness_id} has different source chain"
                )
            return witness

        return await self._write(_operation)


def _artifact_from_row(row: Any) -> MemoryArtifactVersion:
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
        parent_artifact_ids=tuple(
            str(item)
            for item in _json_value(row["parent_artifact_ids_json"], default=[])
        ),
        metadata=dict(_json_value(row["metadata_json"], default={})),
    )


def _artifact_descriptor_from_row(row: Any) -> MemoryArtifactDescriptor:
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
        parent_artifact_ids=tuple(
            str(item)
            for item in _json_value(row["parent_artifact_ids_json"], default=[])
        ),
        metadata=dict(_json_value(row["metadata_json"], default={})),
    )


def _interpretation_from_row(row: Any) -> MemoryInterpretation:
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
        metadata=dict(_json_value(row["metadata_json"], default={})),
    )


def _interpretation_source_from_row(row: Any) -> InterpretationSource:
    return InterpretationSource(
        interpretation_id=str(row["interpretation_id"]),
        entity_ref=str(row["entity_ref"]),
        predicate=str(row["predicate"]),
        ordinal=int(row["ordinal"]),
        metadata=dict(_json_value(row["metadata_json"], default={})),
    )


def _semantic_relation_from_row(row: Any) -> SemanticRelation:
    return SemanticRelation(
        relation_id=str(row["relation_id"]),
        source_ref=str(row["source_ref"]),
        target_ref=str(row["target_ref"]),
        predicate=str(row["predicate"]),
        reason=str(row["reason"]),
        actor=str(row["actor"]),
        recorded_at=str(row["recorded_at"]),
        consciousness_instance_id=str(row["consciousness_instance_id"]),
        stream_scope=str(row["stream_scope"]),
        metadata=dict(_json_value(row["metadata_json"], default={})),
    )


def _association_from_row(row: Any) -> AssociationEvidence:
    return AssociationEvidence(
        source_ref=str(row["source_ref"]),
        target_ref=str(row["target_ref"]),
        context_key=str(row["context_key"]),
        signal=str(row["signal"]),
        event_count=int(row["event_count"]),
        last_event_at=str(row["last_event_at"]),
    )


class MySQLLivingMemoryStore(_MySQLPort):
    async def append_artifact(
        self,
        version: MemoryArtifactVersion,
        *,
        derivations: Sequence[MemoryDerivation] = (),
        expected_head_revision: int,
    ) -> MemoryArtifactVersion:
        content_hash = _sha256(version.content)
        normalized = replace(
            version,
            content_hash=version.content_hash or content_hash,
            recorded_at=version.recorded_at or _now_iso(),
            parent_artifact_ids=tuple(dict.fromkeys(version.parent_artifact_ids)),
        )
        if normalized.content_hash != content_hash:
            raise ValueError(f"ArtifactContentHashMismatch:{normalized.artifact_id}")
        logical_hash = _sha256(normalized.logical_key)
        _, version_hash = _payload(normalized)

        async def _operation(session: AsyncSession) -> MemoryArtifactVersion:
            head = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM memory_artifact_heads "
                            "WHERE logical_key_sha256 = :logical_hash FOR UPDATE"
                        ),
                        {"logical_hash": logical_hash},
                    )
                )
                .mappings()
                .one_or_none()
            )
            current_revision = int(head["revision"]) if head is not None else 0
            if head is not None and str(head["logical_key"]) != normalized.logical_key:
                raise ArtifactHeadConflict("artifact logical-key hash collision")
            if current_revision != int(expected_head_revision):
                raise ArtifactHeadConflict(
                    f"artifact head revision conflict for {normalized.logical_key!r}: "
                    f"expected {expected_head_revision}, actual {current_revision}"
                )
            inserted = await self._immutable_insert(
                session,
                table="memory_artifact_versions",
                identity_column="artifact_id",
                identity=normalized.artifact_id,
                values={
                    "artifact_id": normalized.artifact_id,
                    "logical_key": normalized.logical_key,
                    "logical_key_sha256": logical_hash,
                    "artifact_kind": normalized.artifact_kind,
                    "content": normalized.content,
                    "content_hash": normalized.content_hash,
                    "recorded_at": normalized.recorded_at,
                    "valid_from": normalized.valid_from,
                    "valid_to": normalized.valid_to,
                    "authored_by": normalized.authored_by,
                    "consciousness_instance_id": normalized.consciousness_instance_id,
                    "stream_scope": normalized.stream_scope,
                    "visibility": normalized.visibility,
                    "parent_artifact_ids_json": canonical_json(
                        list(normalized.parent_artifact_ids)
                    ),
                    "metadata_json": canonical_json(normalized.metadata),
                    "payload_sha256": version_hash,
                },
                payload_sha256=version_hash,
            )
            if inserted:
                for parent_id in normalized.parent_artifact_ids:
                    parent_exists = await session.scalar(
                        text(
                            "SELECT 1 FROM memory_artifact_versions "
                            "WHERE artifact_id = :artifact_id"
                        ),
                        {"artifact_id": parent_id},
                    )
                    if parent_exists is None:
                        raise ValueError(f"ArtifactParentMissing:{parent_id}")
            for derivation in derivations:
                if derivation.generated_artifact_id != normalized.artifact_id:
                    raise ValueError(
                        f"ArtifactDerivationTargetMismatch:{derivation.derivation_id}"
                    )
                normalized_derivation = replace(
                    derivation,
                    recorded_at=derivation.recorded_at or normalized.recorded_at,
                )
                _, derivation_hash = _payload(normalized_derivation)
                await self._immutable_insert(
                    session,
                    table="memory_artifact_derivations",
                    identity_column="derivation_id",
                    identity=derivation.derivation_id,
                    values={
                        "derivation_id": normalized_derivation.derivation_id,
                        "generated_artifact_id": normalized_derivation.generated_artifact_id,
                        "used_artifact_id": normalized_derivation.used_artifact_id,
                        "predicate": normalized_derivation.predicate,
                        "reason": normalized_derivation.reason,
                        "actor": normalized_derivation.actor,
                        "recorded_at": normalized_derivation.recorded_at,
                        "metadata_json": canonical_json(normalized_derivation.metadata),
                        "payload_sha256": derivation_hash,
                    },
                    payload_sha256=derivation_hash,
                )
            if (
                not inserted
                and head is not None
                and str(head["artifact_id"]) == normalized.artifact_id
            ):
                return normalized
            projected_at = normalized.recorded_at
            if head is None:
                await session.execute(
                    text(
                        """INSERT INTO memory_artifact_heads (
                            logical_key_sha256, logical_key, artifact_id,
                            projected_at, revision
                        ) VALUES (
                            :logical_hash, :logical_key, :artifact_id,
                            :projected_at, 1
                        )"""
                    ),
                    {
                        "logical_hash": logical_hash,
                        "logical_key": normalized.logical_key,
                        "artifact_id": normalized.artifact_id,
                        "projected_at": projected_at,
                    },
                )
            else:
                updated = await session.execute(
                    text(
                        """UPDATE memory_artifact_heads SET
                            artifact_id = :artifact_id,
                            projected_at = :projected_at,
                            revision = revision + 1
                        WHERE logical_key_sha256 = :logical_hash
                          AND revision = :expected_revision"""
                    ),
                    {
                        "artifact_id": normalized.artifact_id,
                        "projected_at": projected_at,
                        "logical_hash": logical_hash,
                        "expected_revision": current_revision,
                    },
                )
                if updated.rowcount != 1:
                    raise ArtifactHeadConflict("artifact head changed during CAS")
            return normalized

        return await self._write(_operation)

    async def get_artifact_head(self, logical_key: str) -> ArtifactHead | None:
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM memory_artifact_heads "
                            "WHERE logical_key_sha256 = :logical_hash"
                        ),
                        {"logical_hash": _sha256(logical_key)},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        if str(row["logical_key"]) != logical_key:
            raise ArtifactHeadConflict("artifact logical-key hash collision")
        return ArtifactHead(
            logical_key=logical_key,
            artifact_id=str(row["artifact_id"]),
            projected_at=str(row["projected_at"]),
            revision=int(row["revision"]),
        )

    async def get_artifact_version(
        self,
        artifact_id: str,
    ) -> MemoryArtifactVersion | None:
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM memory_artifact_versions "
                            "WHERE artifact_id = :artifact_id"
                        ),
                        {"artifact_id": str(artifact_id)},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _artifact_from_row(row) if row is not None else None

    async def list_artifact_descriptors(
        self,
        logical_key: str,
    ) -> list[MemoryArtifactDescriptor]:
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT artifact_id, logical_key, artifact_kind, "
                        "content_hash, OCTET_LENGTH(content) AS content_byte_length, "
                        "recorded_at, authored_by, consciousness_instance_id, "
                        "stream_scope, visibility, parent_artifact_ids_json, "
                        "metadata_json FROM memory_artifact_versions "
                        "WHERE logical_key_sha256 = :logical_hash "
                        "ORDER BY recorded_at, artifact_id"
                    ),
                    {"logical_hash": _sha256(logical_key)},
                )
            ).mappings()
            result = [_artifact_descriptor_from_row(row) for row in rows]
        if any(item.logical_key != logical_key for item in result):
            raise ArtifactHeadConflict("artifact logical-key hash collision")
        return result

    async def list_artifact_history(
        self,
        logical_key: str,
    ) -> list[MemoryArtifactVersion]:
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT * FROM memory_artifact_versions "
                        "WHERE logical_key_sha256 = :logical_hash "
                        "ORDER BY recorded_at, artifact_id"
                    ),
                    {"logical_hash": _sha256(logical_key)},
                )
            ).mappings()
            result = [_artifact_from_row(row) for row in rows]
        if any(item.logical_key != logical_key for item in result):
            raise ArtifactHeadConflict("artifact logical-key hash collision")
        return result

    async def list_artifact_heads(
        self,
    ) -> list[tuple[MemoryArtifactVersion, ArtifactHead]]:
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT v.*, h.projected_at AS head_projected_at, "
                            "h.revision AS head_revision FROM memory_artifact_heads h "
                            "JOIN memory_artifact_versions v "
                            "ON v.artifact_id = h.artifact_id "
                            "ORDER BY h.logical_key, h.artifact_id"
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [
            (
                _artifact_from_row(row),
                ArtifactHead(
                    logical_key=str(row["logical_key"]),
                    artifact_id=str(row["artifact_id"]),
                    projected_at=str(row["head_projected_at"]),
                    revision=int(row["head_revision"]),
                ),
            )
            for row in rows
        ]

    async def append_interpretation(
        self,
        interpretation: MemoryInterpretation,
        *,
        sources: Sequence[InterpretationSource] = (),
    ) -> MemoryInterpretation:
        _, payload_hash = _payload(interpretation)

        async def _operation(session: AsyncSession) -> MemoryInterpretation:
            await self._immutable_insert(
                session,
                table="memory_interpretations",
                identity_column="interpretation_id",
                identity=interpretation.interpretation_id,
                values={
                    "interpretation_id": interpretation.interpretation_id,
                    "subject_id": interpretation.subject_id,
                    "content": interpretation.content,
                    "authored_by": interpretation.authored_by,
                    "consciousness_instance_id": interpretation.consciousness_instance_id,
                    "recorded_at": interpretation.recorded_at,
                    "valid_from": interpretation.valid_from,
                    "valid_to": interpretation.valid_to,
                    "stream_scope": interpretation.stream_scope,
                    "visibility": interpretation.visibility,
                    "metadata_json": canonical_json(interpretation.metadata),
                    "payload_sha256": payload_hash,
                },
                payload_sha256=payload_hash,
            )
            for source in sources:
                _, source_hash = _payload(source)
                identity = (
                    f"{source.interpretation_id}:{_sha256(source.entity_ref)}:"
                    f"{source.predicate}"
                )
                existing = (
                    (
                        await session.execute(
                            text(
                                "SELECT payload_sha256 FROM memory_interpretation_sources "
                                "WHERE interpretation_id = :interpretation_id "
                                "AND entity_ref_sha256 = :entity_hash "
                                "AND predicate = :predicate FOR UPDATE"
                            ),
                            {
                                "interpretation_id": source.interpretation_id,
                                "entity_hash": _sha256(source.entity_ref),
                                "predicate": source.predicate,
                            },
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is not None:
                    if _row_hash(existing) != source_hash:
                        raise ImmutableMemoryRecordConflict(
                            f"memory_interpretation_sources:{identity} conflict"
                        )
                    continue
                await session.execute(
                    text(
                        """INSERT INTO memory_interpretation_sources (
                            interpretation_id, entity_ref, entity_ref_sha256,
                            predicate, ordinal, metadata_json, payload_sha256
                        ) VALUES (
                            :interpretation_id, :entity_ref, :entity_ref_sha256,
                            :predicate, :ordinal, :metadata_json, :payload_sha256
                        )"""
                    ),
                    {
                        "interpretation_id": source.interpretation_id,
                        "entity_ref": source.entity_ref,
                        "entity_ref_sha256": _sha256(source.entity_ref),
                        "predicate": source.predicate,
                        "ordinal": int(source.ordinal),
                        "metadata_json": canonical_json(source.metadata),
                        "payload_sha256": source_hash,
                    },
                )
            return interpretation

        return await self._write(_operation)

    async def append_relation(self, relation: SemanticRelation) -> SemanticRelation:
        if relation.source_ref == relation.target_ref:
            raise ValueError("semantic relation endpoints must differ")
        _, payload_hash = _payload(relation)

        async def _operation(session: AsyncSession) -> SemanticRelation:
            await self._immutable_insert(
                session,
                table="memory_semantic_relations",
                identity_column="relation_id",
                identity=relation.relation_id,
                values={
                    "relation_id": relation.relation_id,
                    "source_ref": relation.source_ref,
                    "source_ref_sha256": _sha256(relation.source_ref),
                    "target_ref": relation.target_ref,
                    "target_ref_sha256": _sha256(relation.target_ref),
                    "predicate": relation.predicate,
                    "reason": relation.reason,
                    "actor": relation.actor,
                    "recorded_at": relation.recorded_at,
                    "consciousness_instance_id": relation.consciousness_instance_id,
                    "stream_scope": relation.stream_scope,
                    "metadata_json": canonical_json(relation.metadata),
                    "payload_sha256": payload_hash,
                },
                payload_sha256=payload_hash,
            )
            return relation

        return await self._write(_operation)

    async def list_relations(self, entity_ref: str) -> list[SemanticRelation]:
        ref_hash = _sha256(entity_ref)
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM memory_semantic_relations "
                            "WHERE source_ref_sha256 = :ref_hash "
                            "OR target_ref_sha256 = :ref_hash "
                            "ORDER BY recorded_at, relation_id"
                        ),
                        {"ref_hash": ref_hash},
                    )
                )
                .mappings()
                .all()
            )
        return [
            _semantic_relation_from_row(row)
            for row in rows
            if entity_ref in {str(row["source_ref"]), str(row["target_ref"])}
        ]

    async def list_interpretations(
        self,
        subject_id: str,
        *,
        recorded_as_of: str = "",
    ) -> list[MemoryInterpretation]:
        clauses = ["subject_id = :subject_id"]
        params: dict[str, Any] = {"subject_id": subject_id}
        if recorded_as_of:
            clauses.append("recorded_at <= :recorded_as_of")
            params["recorded_as_of"] = recorded_as_of
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM memory_interpretations WHERE "
                            + " AND ".join(clauses)
                            + " ORDER BY recorded_at, interpretation_id"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
        return [_interpretation_from_row(row) for row in rows]

    async def get_interpretation(
        self,
        interpretation_id: str,
    ) -> tuple[MemoryInterpretation, tuple[InterpretationSource, ...]] | None:
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM memory_interpretations "
                            "WHERE interpretation_id = :interpretation_id"
                        ),
                        {"interpretation_id": interpretation_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            source_rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM memory_interpretation_sources "
                            "WHERE interpretation_id = :interpretation_id "
                            "ORDER BY ordinal, entity_ref, predicate"
                        ),
                        {"interpretation_id": interpretation_id},
                    )
                )
                .mappings()
                .all()
            )
        return (
            _interpretation_from_row(row),
            tuple(_interpretation_source_from_row(item) for item in source_rows),
        )

    async def search_interpretations(
        self,
        query: str,
        *,
        top_k: int,
        stream_scope: str | None,
        visibility: Sequence[str],
        recorded_as_of: str = "",
    ) -> list[InterpretationSearchResult]:
        query_text = str(query or "").strip()
        visible = tuple(dict.fromkeys(str(item) for item in visibility if str(item)))
        if not query_text or not visible or top_k <= 0:
            return []
        params: dict[str, Any] = {
            "query": query_text,
            "pattern": f"%{query_text}%",
            "limit": _safe_limit(top_k),
        }
        visible_marks: list[str] = []
        for index, item in enumerate(visible):
            key = f"visible_{index}"
            visible_marks.append(f":{key}")
            params[key] = item
        clauses = [f"visibility IN ({', '.join(visible_marks)})"]
        if stream_scope is None:
            clauses.append("stream_scope = ''")
        else:
            clauses.append("stream_scope IN ('', :stream_scope)")
            params["stream_scope"] = stream_scope
        if recorded_as_of:
            clauses.append("recorded_at <= :recorded_as_of")
            params["recorded_as_of"] = recorded_as_of
        where = " AND ".join(clauses)
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT *, MATCH(subject_id, content) AGAINST "
                            "(:query IN NATURAL LANGUAGE MODE) AS lexical_rank "
                            "FROM memory_interpretations WHERE "
                            f"MATCH(subject_id, content) AGAINST "
                            f"(:query IN NATURAL LANGUAGE MODE) AND {where} "
                            "ORDER BY lexical_rank DESC, recorded_at DESC, "
                            "interpretation_id LIMIT :limit"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
            retrieval_source = "interpretation_fulltext"
            if not rows:
                rows = (
                    (
                        await connection.execute(
                            text(
                                "SELECT * FROM memory_interpretations WHERE "
                                "(content LIKE :pattern OR subject_id LIKE :pattern) AND "
                                f"{where} ORDER BY recorded_at DESC, "
                                "interpretation_id LIMIT :limit"
                            ),
                            params,
                        )
                    )
                    .mappings()
                    .all()
                )
                retrieval_source = "interpretation_substring"
        results: list[InterpretationSearchResult] = []
        for rank, row in enumerate(rows, start=1):
            item = await self.get_interpretation(str(row["interpretation_id"]))
            if item is None:
                continue
            interpretation, sources = item
            results.append(
                InterpretationSearchResult(
                    interpretation=interpretation,
                    sources=sources,
                    rank_score=1.0 / float(rank),
                    retrieval_source=retrieval_source,
                )
            )
        return results

    async def list_association_evidence(
        self,
        entity_ref: str,
        *,
        context_key: str | None = None,
    ) -> list[AssociationEvidence]:
        ref_hash = _sha256(entity_ref)
        params: dict[str, Any] = {"ref_hash": ref_hash}
        context_clause = ""
        if context_key is not None:
            context_clause = " AND context_key_sha256 IN (:context_hash, :empty_hash)"
            params.update(
                context_hash=_sha256(context_key),
                empty_hash=_sha256(""),
            )
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM memory_association_projection "
                            "WHERE (source_ref_sha256 = :ref_hash "
                            "OR target_ref_sha256 = :ref_hash)"
                            + context_clause
                            + " ORDER BY last_event_at DESC, event_count DESC, `signal`"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
        return [
            _association_from_row(row)
            for row in rows
            if entity_ref in {str(row["source_ref"]), str(row["target_ref"])}
        ]

    async def choose_association_neighbours(
        self,
        seed_refs: Sequence[str],
        *,
        context_key: str,
        random_seed: int,
        limit: int,
    ) -> list[AssociationSelection]:
        seeds = tuple(dict.fromkeys(str(item) for item in seed_refs if str(item)))
        if not seeds or limit <= 0:
            return []
        seed_set = set(seeds)
        evidence_by_target: dict[str, list[AssociationEvidence]] = {}
        for seed in seeds:
            evidence = await self.list_association_evidence(
                seed,
                context_key=context_key,
            )
            for item in evidence:
                target = item.target_ref if item.source_ref == seed else item.source_ref
                if target not in seed_set:
                    evidence_by_target.setdefault(target, []).append(item)
        rng = random.Random(int(random_seed))
        ranked: list[tuple[float, str, AssociationSelection]] = []
        for target, evidence in evidence_by_target.items():
            event_count = sum(max(0, item.event_count) for item in evidence)
            if event_count <= 0:
                continue
            priority = max(rng.random(), 1e-12) ** (1.0 / float(event_count))
            ranked.append(
                (
                    priority,
                    target,
                    AssociationSelection(
                        entity_ref=target,
                        signals=tuple(sorted({item.signal for item in evidence})),
                        event_count=event_count,
                        last_event_at=max(item.last_event_at for item in evidence),
                    ),
                )
            )
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in ranked[: int(limit)]]

    async def rebuild_association_projection(self) -> int:
        async def _operation(session: AsyncSession) -> int:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM memory_corecall_events "
                            "ORDER BY recorded_at, corecall_id"
                        )
                    )
                )
                .mappings()
                .all()
            )
            await session.execute(text("DELETE FROM memory_association_projection"))
            for row in rows:
                refs = tuple(
                    dict.fromkeys(
                        str(item)
                        for item in _json_value(row["entity_refs_json"], default=[])
                        if str(item)
                    )
                )
                for left, right in combinations(sorted(refs), 2):
                    await session.execute(
                        text(
                            """INSERT INTO memory_association_projection (
                                source_ref_sha256, source_ref,
                                target_ref_sha256, target_ref,
                                context_key_sha256, context_key,
                                `signal`, signal_sha256, event_count, last_event_at
                            ) VALUES (
                                :source_hash, :source_ref,
                                :target_hash, :target_ref,
                                :context_hash, :context_key,
                                :signal, :signal_hash, 1, :recorded_at
                            ) ON DUPLICATE KEY UPDATE
                                event_count = event_count + 1,
                                last_event_at = GREATEST(
                                    last_event_at, VALUES(last_event_at)
                                )"""
                        ),
                        {
                            "source_hash": _sha256(left),
                            "source_ref": left,
                            "target_hash": _sha256(right),
                            "target_ref": right,
                            "context_hash": _sha256(str(row["context_key"])),
                            "context_key": str(row["context_key"]),
                            "signal": str(row["signal"]),
                            "signal_hash": _sha256(str(row["signal"])),
                            "recorded_at": str(row["recorded_at"]),
                        },
                    )
            return len(rows)

        return await self._write(_operation)

    async def begin_recall(self, **kwargs: Any) -> RecallEpisode:
        episode = RecallEpisode(
            episode_id=str(kwargs.get("episode_id") or f"recall_{uuid4().hex}"),
            query=str(kwargs.get("query") or ""),
            retrieval_intent=str(kwargs.get("retrieval_intent") or ""),
            consciousness_instance_id=str(
                kwargs.get("consciousness_instance_id") or ""
            ),
            stream_scope=str(kwargs.get("stream_scope") or ""),
            context_key=str(kwargs.get("context_key") or ""),
            policy_version=str(kwargs.get("policy_version") or ""),
            random_seed=int(kwargs.get("random_seed") or 0),
            recorded_at=str(kwargs.get("recorded_at") or _now_iso()),
            context=dict(kwargs.get("context") or {}),
        )
        _, payload_hash = _payload(episode)

        async def _operation(session: AsyncSession) -> RecallEpisode:
            await self._immutable_insert(
                session,
                table="memory_recall_sessions",
                identity_column="episode_id",
                identity=episode.episode_id,
                values={
                    "episode_id": episode.episode_id,
                    "query": episode.query,
                    "retrieval_intent": episode.retrieval_intent,
                    "consciousness_instance_id": episode.consciousness_instance_id,
                    "stream_scope": episode.stream_scope,
                    "context_key": episode.context_key,
                    "policy_version": episode.policy_version,
                    "random_seed": episode.random_seed,
                    "recorded_at": episode.recorded_at,
                    "context_json": canonical_json(episode.context),
                    "payload_sha256": payload_hash,
                },
                payload_sha256=payload_hash,
            )
            return episode

        return await self._write(_operation)

    async def append_recall_events(
        self,
        events: Sequence[RecallEvent],
    ) -> tuple[RecallEvent, ...]:
        async def _operation(session: AsyncSession) -> tuple[RecallEvent, ...]:
            for event in events:
                _, payload_hash = _payload(event)
                await self._immutable_insert(
                    session,
                    table="memory_recall_events",
                    identity_column="event_id",
                    identity=event.event_id,
                    values={
                        "event_id": event.event_id,
                        "episode_id": event.episode_id,
                        "action": event.action,
                        "entity_ref": event.entity_ref,
                        "ordinal": int(event.ordinal),
                        "source": event.source,
                        "reason": event.reason,
                        "recorded_at": event.recorded_at,
                        "metadata_json": canonical_json(event.metadata),
                        "payload_sha256": payload_hash,
                    },
                    payload_sha256=payload_hash,
                )
            return tuple(events)

        return await self._write(_operation)

    async def append_corecall(self, event: CoRecallEvent) -> CoRecallEvent:
        refs = tuple(
            dict.fromkeys(str(item) for item in event.entity_refs if str(item))
        )
        normalized = replace(event, entity_refs=refs)
        _, payload_hash = _payload(normalized)

        async def _operation(session: AsyncSession) -> CoRecallEvent:
            inserted = await self._immutable_insert(
                session,
                table="memory_corecall_events",
                identity_column="corecall_id",
                identity=normalized.corecall_id,
                values={
                    "corecall_id": normalized.corecall_id,
                    "episode_id": normalized.episode_id,
                    "context_key": normalized.context_key,
                    "signal": normalized.signal,
                    "entity_refs_json": canonical_json(list(refs)),
                    "actor": normalized.actor,
                    "reason": normalized.reason,
                    "recorded_at": normalized.recorded_at,
                    "metadata_json": canonical_json(normalized.metadata),
                    "payload_sha256": payload_hash,
                },
                payload_sha256=payload_hash,
            )
            if inserted:
                for left, right in combinations(sorted(refs), 2):
                    await session.execute(
                        text(
                            """INSERT INTO memory_association_projection (
                                source_ref_sha256, source_ref,
                                target_ref_sha256, target_ref,
                                context_key_sha256, context_key,
                                `signal`, signal_sha256, event_count, last_event_at
                            ) VALUES (
                                :source_hash, :source_ref,
                                :target_hash, :target_ref,
                                :context_hash, :context_key,
                                :signal, :signal_hash, 1, :recorded_at
                            ) ON DUPLICATE KEY UPDATE
                                event_count = event_count + 1,
                                last_event_at = GREATEST(last_event_at, VALUES(last_event_at))"""
                        ),
                        {
                            "source_hash": _sha256(left),
                            "source_ref": left,
                            "target_hash": _sha256(right),
                            "target_ref": right,
                            "context_hash": _sha256(normalized.context_key),
                            "context_key": normalized.context_key,
                            "signal": normalized.signal,
                            "signal_hash": _sha256(normalized.signal),
                            "recorded_at": normalized.recorded_at,
                        },
                    )
            return normalized

        return await self._write(_operation)


class _MySQLWitnessProjectionStore(_MySQLWitnessAppendStore):
    async def mark_projection(
        self,
        witness_id: str,
        *,
        projection_path: str,
        status: str,
        error: str = "",
    ) -> bool:
        async def _operation(session: AsyncSession) -> bool:
            projection_path_sha256 = await _assert_projection_path_available(
                session,
                witness_id=witness_id,
                projection_path=projection_path,
            )
            result = await session.execute(
                text(
                    "UPDATE memory_witnesses SET projection_path = :projection_path, "
                    "projection_path_sha256 = :projection_path_sha256, "
                    "projection_status = :status, projection_error = :error "
                    "WHERE witness_id = :witness_id"
                ),
                {
                    "witness_id": witness_id,
                    "projection_path": projection_path or None,
                    "projection_path_sha256": projection_path_sha256,
                    "status": status,
                    "error": error,
                },
            )
            return result.rowcount == 1

        return await self._write(_operation)


def _claim_from_row(row: Any) -> MemoryClaim:
    return MemoryClaim(
        claim_id=str(row["claim_id"]),
        subject_key=str(row["subject_key"]),
        content=str(row["content"]),
        claim_kind=str(row["claim_kind"]),
        source=str(row["source"]),
        authority=str(row["authority"]),
        valid_from=str(row["valid_from"]),
        valid_to=str(row["valid_to"]),
        recorded_at=str(row["recorded_at"]),
        stream_scope=str(row["stream_scope"]),
        visibility=str(row["visibility"]),
        consciousness_instance_id=str(row["consciousness_instance_id"]),
        metadata=dict(_json_value(row["metadata_json"], default={})),
    )


def _claim_evidence_from_row(row: Any) -> ClaimEvidence:
    return ClaimEvidence(
        evidence_link_id=str(row["evidence_link_id"]),
        claim_id=str(row["claim_id"]),
        evidence_kind=str(row["evidence_kind"]),
        evidence_ref=str(row["evidence_ref"]),
        stance=str(row["stance"]),
        source_excerpt=str(row["source_excerpt"]),
        recorded_at=str(row["recorded_at"]),
        metadata=dict(_json_value(row["metadata_json"], default={})),
    )


def _epistemic_conflict_from_row(row: Any) -> EpistemicConflict:
    return EpistemicConflict(
        conflict_id=str(row["conflict_id"]),
        left_claim_id=str(row["left_claim_id"]),
        right_claim_id=str(row["right_claim_id"]),
        relation=str(row["relation"]),
        reason=str(row["reason"]),
        recorded_at=str(row["recorded_at"]),
        detected_by=str(row["detected_by"]),
        metadata=dict(_json_value(row["metadata_json"], default={})),
    )


def _state_event_from_row(row: Any) -> MemoryStateEvent:
    return MemoryStateEvent(
        event_id=str(row["event_id"]),
        entity_type=str(row["entity_type"]),
        entity_id=str(row["entity_id"]),
        event_type=str(row["event_type"]),
        actor=str(row["actor"]),
        authority=str(row["authority"]),
        reason=str(row["reason"]),
        recorded_at=str(row["recorded_at"]),
        valid_at=str(row["valid_at"]),
        caused_by_event_id=str(row["caused_by_event_id"]),
        reverses_event_id=str(row["reverses_event_id"]),
        payload=dict(_json_value(row["payload_json"], default={})),
    )


def _active_state_events(
    events: Sequence[MemoryStateEvent],
) -> list[MemoryStateEvent]:
    ordered = sorted(events, key=lambda item: (item.recorded_at, item.event_id))
    reversed_ids: set[str] = set()
    active_reversed: list[MemoryStateEvent] = []
    for event in reversed(ordered):
        if event.event_id in reversed_ids:
            continue
        active_reversed.append(event)
        if event.reverses_event_id:
            reversed_ids.add(event.reverses_event_id)
    return list(reversed(active_reversed))


class MySQLEpistemicMemoryStore(_MySQLPort):
    async def _append_record(
        self,
        *,
        table: str,
        identity_column: str,
        identity: str,
        record: Any,
        values: dict[str, Any],
    ) -> Any:
        _, payload_hash = _payload(record)

        async def _operation(session: AsyncSession) -> Any:
            await self._immutable_insert(
                session,
                table=table,
                identity_column=identity_column,
                identity=identity,
                values={**values, "payload_sha256": payload_hash},
                payload_sha256=payload_hash,
            )
            return record

        return await self._write(_operation)

    async def append_claim(self, claim: MemoryClaim) -> MemoryClaim:
        return await self._append_record(
            table="memory_claims",
            identity_column="claim_id",
            identity=claim.claim_id,
            record=claim,
            values={
                "claim_id": claim.claim_id,
                "subject_key": claim.subject_key,
                "subject_key_sha256": _sha256(claim.subject_key),
                "content": claim.content,
                "claim_kind": claim.claim_kind,
                "source": claim.source,
                "authority": claim.authority,
                "valid_from": claim.valid_from,
                "valid_to": claim.valid_to,
                "recorded_at": claim.recorded_at,
                "stream_scope": claim.stream_scope,
                "visibility": claim.visibility,
                "consciousness_instance_id": claim.consciousness_instance_id,
                "metadata_json": canonical_json(claim.metadata),
            },
        )

    async def append_evidence(self, evidence: ClaimEvidence) -> ClaimEvidence:
        return await self._append_record(
            table="memory_claim_evidence",
            identity_column="evidence_link_id",
            identity=evidence.evidence_link_id,
            record=evidence,
            values={
                "evidence_link_id": evidence.evidence_link_id,
                "claim_id": evidence.claim_id,
                "evidence_kind": evidence.evidence_kind,
                "evidence_ref": evidence.evidence_ref,
                "evidence_ref_sha256": _sha256(evidence.evidence_ref),
                "stance": evidence.stance,
                "source_excerpt": evidence.source_excerpt,
                "recorded_at": evidence.recorded_at,
                "metadata_json": canonical_json(evidence.metadata),
            },
        )

    async def append_belief(self, belief: MemoryBelief) -> MemoryBelief:
        return await self._append_record(
            table="memory_beliefs",
            identity_column="belief_id",
            identity=belief.belief_id,
            record=belief,
            values={
                "belief_id": belief.belief_id,
                "claim_id": belief.claim_id,
                "perspective_subject_id": belief.perspective_subject_id,
                "consciousness_instance_id": belief.consciousness_instance_id,
                "recorded_at": belief.recorded_at,
                "metadata_json": canonical_json(belief.metadata),
            },
        )

    async def append_conflict(
        self,
        conflict: EpistemicConflict,
    ) -> EpistemicConflict:
        if conflict.left_claim_id == conflict.right_claim_id:
            raise ValueError("epistemic conflict endpoints must differ")
        return await self._append_record(
            table="memory_epistemic_conflicts",
            identity_column="conflict_id",
            identity=conflict.conflict_id,
            record=conflict,
            values={
                "conflict_id": conflict.conflict_id,
                "left_claim_id": conflict.left_claim_id,
                "right_claim_id": conflict.right_claim_id,
                "relation": conflict.relation,
                "reason": conflict.reason,
                "recorded_at": conflict.recorded_at,
                "detected_by": conflict.detected_by,
                "metadata_json": canonical_json(conflict.metadata),
            },
        )

    async def append_state_event(
        self,
        event: MemoryStateEvent,
    ) -> MemoryStateEvent:
        return await self._append_record(
            table="memory_state_events",
            identity_column="event_id",
            identity=event.event_id,
            record=event,
            values={
                "event_id": event.event_id,
                "entity_type": event.entity_type,
                "entity_id": event.entity_id,
                "entity_id_sha256": _sha256(event.entity_id),
                "event_type": event.event_type,
                "actor": event.actor,
                "authority": event.authority,
                "reason": event.reason,
                "recorded_at": event.recorded_at,
                "valid_at": event.valid_at,
                "caused_by_event_id": event.caused_by_event_id,
                "reverses_event_id": event.reverses_event_id,
                "payload_json": canonical_json(event.payload),
            },
        )

    async def append_retrieval_episode(
        self,
        episode: RetrievalEpisode,
    ) -> RetrievalEpisode:
        return await self._append_record(
            table="memory_retrieval_episodes",
            identity_column="episode_id",
            identity=episode.episode_id,
            record=episode,
            values={
                "episode_id": episode.episode_id,
                "query": episode.query,
                "mode": episode.mode,
                "consciousness_instance_id": episode.consciousness_instance_id,
                "stream_scope": episode.stream_scope,
                "recorded_at": episode.recorded_at,
                "metadata_json": canonical_json(episode.metadata),
            },
        )

    async def append_retrieval_exposure(
        self,
        exposure: RetrievalExposure,
    ) -> RetrievalExposure:
        return await self._append_record(
            table="memory_retrieval_exposures",
            identity_column="exposure_id",
            identity=exposure.exposure_id,
            record=exposure,
            values={
                "exposure_id": exposure.exposure_id,
                "episode_id": exposure.episode_id,
                "entity_type": exposure.entity_type,
                "entity_id": exposure.entity_id,
                "entity_id_sha256": _sha256(exposure.entity_id),
                "rank_position": int(exposure.rank_position),
                "retrieval_source": exposure.retrieval_source,
                "recorded_at": exposure.recorded_at,
                "feedback": exposure.feedback,
                "feedback_reason": exposure.feedback_reason,
                "feedback_at": exposure.feedback_at,
                "metadata_json": canonical_json(exposure.metadata),
            },
        )

    async def append_retrieval_feedback(
        self,
        feedback: RetrievalFeedback,
    ) -> RetrievalFeedback:
        return await self._append_record(
            table="memory_retrieval_feedback",
            identity_column="feedback_id",
            identity=feedback.feedback_id,
            record=feedback,
            values={
                "feedback_id": feedback.feedback_id,
                "exposure_id": feedback.exposure_id,
                "feedback": feedback.feedback,
                "actor": feedback.actor,
                "reason": feedback.reason,
                "recorded_at": feedback.recorded_at,
                "metadata_json": canonical_json(feedback.metadata),
            },
        )

    async def list_state_events(
        self,
        entity_type: str,
        entity_id: str,
        *,
        recorded_as_of: str = "",
    ) -> list[MemoryStateEvent]:
        clauses = [
            "entity_type = :entity_type",
            "entity_id_sha256 = :entity_hash",
        ]
        params: dict[str, Any] = {
            "entity_type": entity_type,
            "entity_hash": _sha256(entity_id),
        }
        if recorded_as_of:
            clauses.append("recorded_at <= :recorded_as_of")
            params["recorded_as_of"] = recorded_as_of
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM memory_state_events WHERE "
                            + " AND ".join(clauses)
                            + " ORDER BY recorded_at, event_id"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
        return [
            _state_event_from_row(row)
            for row in rows
            if str(row["entity_id"]) == entity_id
        ]

    async def get_claim_state(
        self,
        claim_id: str,
        *,
        recorded_as_of: str = "",
    ) -> ClaimState | None:
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text("SELECT * FROM memory_claims WHERE claim_id = :claim_id"),
                        {"claim_id": claim_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return reduce_claim_state(
            _claim_from_row(row),
            await self.list_state_events(
                "claim",
                claim_id,
                recorded_as_of=recorded_as_of,
            ),
        )

    async def list_claim_states(
        self,
        subject_key: str,
        *,
        recorded_as_of: str = "",
        valid_at: str = "",
        stream_scope: str | None = None,
        visibility: Sequence[str] = ("private",),
    ) -> list[ClaimState]:
        visible = tuple(dict.fromkeys(str(item) for item in visibility if str(item)))
        if not visible:
            return []
        params: dict[str, Any] = {"subject_hash": _sha256(subject_key)}
        clauses = ["subject_key_sha256 = :subject_hash"]
        marks: list[str] = []
        for index, item in enumerate(visible):
            key = f"visible_{index}"
            marks.append(f":{key}")
            params[key] = item
        clauses.append(f"visibility IN ({', '.join(marks)})")
        if recorded_as_of:
            clauses.append("recorded_at <= :recorded_as_of")
            params["recorded_as_of"] = recorded_as_of
        if valid_at:
            clauses.extend(
                [
                    "(valid_from = '' OR valid_from <= :valid_at)",
                    "(valid_to = '' OR valid_to > :valid_at)",
                ]
            )
            params["valid_at"] = valid_at
        if stream_scope is None:
            clauses.append("stream_scope = ''")
        else:
            clauses.append("stream_scope IN ('', :stream_scope)")
            params["stream_scope"] = stream_scope
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM memory_claims WHERE "
                            + " AND ".join(clauses)
                            + " ORDER BY recorded_at, claim_id"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
        states: list[ClaimState] = []
        for row in rows:
            if str(row["subject_key"]) != subject_key:
                continue
            claim = _claim_from_row(row)
            states.append(
                reduce_claim_state(
                    claim,
                    await self.list_state_events(
                        "claim",
                        claim.claim_id,
                        recorded_as_of=recorded_as_of,
                    ),
                )
            )
        return states

    async def list_claim_evidence(self, claim_id: str) -> list[ClaimEvidence]:
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM memory_claim_evidence "
                            "WHERE claim_id = :claim_id "
                            "ORDER BY recorded_at, evidence_link_id"
                        ),
                        {"claim_id": claim_id},
                    )
                )
                .mappings()
                .all()
            )
        return [_claim_evidence_from_row(row) for row in rows]

    async def get_retrieval_plasticity(
        self,
        entity_type: str,
        entity_id: str,
    ) -> RetrievalPlasticity:
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT e.entity_id, f.feedback "
                            "FROM memory_retrieval_exposures e "
                            "JOIN memory_retrieval_feedback f "
                            "ON f.exposure_id = e.exposure_id "
                            "WHERE e.entity_type = :entity_type "
                            "AND e.entity_id_sha256 = :entity_hash "
                            "ORDER BY f.recorded_at, f.feedback_id"
                        ),
                        {
                            "entity_type": entity_type,
                            "entity_hash": _sha256(entity_id),
                        },
                    )
                )
                .mappings()
                .all()
            )
        counts = {"accepted": 0, "rejected": 0, "corrected": 0}
        for row in rows:
            if str(row["entity_id"]) != entity_id:
                continue
            value = str(row["feedback"])
            if value in counts:
                counts[value] += 1
        denominator = max(1, sum(counts.values()))
        affinity = (counts["accepted"] - counts["rejected"]) / denominator
        return RetrievalPlasticity(
            entity_type=entity_type,
            entity_id=entity_id,
            accepted_count=counts["accepted"],
            rejected_count=counts["rejected"],
            corrected_count=counts["corrected"],
            retrieval_affinity=max(-1.0, min(1.0, affinity)),
        )

    async def get_disposition(
        self,
        entity_type: str,
        entity_id: str,
        *,
        recorded_as_of: str = "",
    ) -> MemoryDisposition:
        table_map = {
            "claim": ("memory_claims", "claim_id"),
            "belief": ("memory_beliefs", "belief_id"),
            "conflict": ("memory_epistemic_conflicts", "conflict_id"),
            "experience": ("memory_experiences", "event_id"),
            "witness": ("memory_witnesses", "witness_id"),
        }
        location = table_map.get(entity_type)
        if location is None:
            raise ValueError(f"UnsupportedEpistemicEntity:{entity_type}")
        table, column = location
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            exists = await connection.scalar(
                text(f"SELECT 1 FROM {table} WHERE {column} = :entity_id"),
                {"entity_id": entity_id},
            )
        if exists is None:
            raise ValueError(f"EpistemicEntityMissing:{entity_type}:{entity_id}")
        return reduce_memory_disposition(
            entity_type,
            entity_id,
            await self.list_state_events(
                entity_type,
                entity_id,
                recorded_as_of=recorded_as_of,
            ),
        )

    async def get_audit_trail(
        self,
        entity_type: str,
        entity_id: str,
        *,
        recorded_as_of: str = "",
    ) -> list[MemoryAuditEntry]:
        events = await self.list_state_events(
            entity_type,
            entity_id,
            recorded_as_of=recorded_as_of,
        )
        reversed_by: dict[str, list[str]] = {}
        by_id = {event.event_id: event for event in events}
        for event in events:
            if event.reverses_event_id:
                reversed_by.setdefault(event.reverses_event_id, []).append(
                    event.event_id
                )
        active_ids = {event.event_id for event in _active_state_events(events)}
        return [
            MemoryAuditEntry(
                event=event,
                active=event.event_id in active_ids,
                reversed_by=tuple(reversed_by.get(event.event_id, ())),
                cause=by_id.get(event.caused_by_event_id),
            )
            for event in events
        ]

    async def project_current_facts(
        self,
        subject_key: str,
        *,
        valid_at: str,
        recorded_as_of: str = "",
        stream_scope: str | None = None,
        visibility: Sequence[str] = ("private",),
    ) -> CurrentFactProjection:
        states = await self.list_claim_states(
            subject_key,
            recorded_as_of=recorded_as_of,
            valid_at=valid_at,
            stream_scope=stream_scope,
            visibility=visibility,
        )
        active = tuple(
            state
            for state in states
            if state.status
            not in {ClaimStatus.SUPERSEDED.value, ClaimStatus.RETRACTED.value}
        )
        active_ids = {state.claim.claim_id for state in active}
        if not active_ids:
            return CurrentFactProjection(
                subject_key=subject_key,
                valid_at=valid_at,
                recorded_as_of=recorded_as_of,
                active_claims=(),
                conflicts=(),
                uncertainty=("没有满足当前双时间条件的主张。",),
            )
        params: dict[str, Any] = {}
        marks: list[str] = []
        for index, claim_id in enumerate(sorted(active_ids)):
            key = f"claim_{index}"
            marks.append(f":{key}")
            params[key] = claim_id
        clauses = [
            f"left_claim_id IN ({', '.join(marks)})",
            f"right_claim_id IN ({', '.join(marks)})",
        ]
        if recorded_as_of:
            clauses.append("recorded_at <= :recorded_as_of")
            params["recorded_as_of"] = recorded_as_of
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM memory_epistemic_conflicts WHERE "
                            + " AND ".join(clauses)
                            + " ORDER BY recorded_at, conflict_id"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
        conflicts = tuple(_epistemic_conflict_from_row(row) for row in rows)
        uncertainty: list[str] = []
        if conflicts:
            uncertainty.append("当前有效主张之间存在未裁决冲突。")
        if any(state.status == ClaimStatus.DISPUTED.value for state in active):
            uncertainty.append("至少一条当前有效主张处于争议状态。")
        if len(active) > 1 and not conflicts:
            uncertainty.append("存在多个当前有效主张；它们不被系统自动合并。")
        return CurrentFactProjection(
            subject_key=subject_key,
            valid_at=valid_at,
            recorded_as_of=recorded_as_of,
            active_claims=active,
            conflicts=conflicts,
            uncertainty=tuple(uncertainty),
        )

    async def search_claims(
        self,
        query: str,
        *,
        mode: str,
        top_k: int,
        stream_scope: str | None,
        visibility: Sequence[str],
        valid_at: str = "",
        recorded_as_of: str = "",
    ) -> list[ClaimSearchResult]:
        query_text = str(query or "").strip()
        visible = tuple(dict.fromkeys(str(item) for item in visibility if str(item)))
        if not query_text or not visible or top_k <= 0:
            return []
        params: dict[str, Any] = {
            "query": query_text,
            "pattern": f"%{query_text}%",
            "limit": _safe_limit(max(1, top_k) * 4),
        }
        marks: list[str] = []
        for index, item in enumerate(visible):
            key = f"visible_{index}"
            marks.append(f":{key}")
            params[key] = item
        clauses = [f"visibility IN ({', '.join(marks)})"]
        if recorded_as_of:
            clauses.append("recorded_at <= :recorded_as_of")
            params["recorded_as_of"] = recorded_as_of
        if valid_at:
            clauses.extend(
                [
                    "(valid_from = '' OR valid_from <= :valid_at)",
                    "(valid_to = '' OR valid_to > :valid_at)",
                ]
            )
            params["valid_at"] = valid_at
        if stream_scope is None:
            clauses.append("stream_scope = ''")
        else:
            clauses.append("stream_scope IN ('', :stream_scope)")
            params["stream_scope"] = stream_scope
        where = " AND ".join(clauses)
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT *, MATCH(subject_key, content) AGAINST "
                            "(:query IN NATURAL LANGUAGE MODE) AS lexical_rank "
                            "FROM memory_claims WHERE MATCH(subject_key, content) "
                            f"AGAINST (:query IN NATURAL LANGUAGE MODE) AND {where} "
                            "ORDER BY lexical_rank DESC, recorded_at DESC, claim_id "
                            "LIMIT :limit"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
            if not rows:
                rows = (
                    (
                        await connection.execute(
                            text(
                                "SELECT * FROM memory_claims WHERE "
                                f"content LIKE :pattern AND {where} "
                                "ORDER BY recorded_at DESC, claim_id LIMIT :limit"
                            ),
                            params,
                        )
                    )
                    .mappings()
                    .all()
                )
        results: list[ClaimSearchResult] = []
        for index, row in enumerate(rows):
            claim = _claim_from_row(row)
            state = reduce_claim_state(
                claim,
                await self.list_state_events(
                    "claim",
                    claim.claim_id,
                    recorded_as_of=recorded_as_of,
                ),
            )
            if mode == "current_fact" and state.status in {
                ClaimStatus.SUPERSEDED.value,
                ClaimStatus.RETRACTED.value,
            }:
                continue
            async with self.runtime.engine.connect() as connection:
                conflict_rows = (
                    (
                        await connection.execute(
                            text(
                                "SELECT * FROM memory_epistemic_conflicts "
                                "WHERE left_claim_id = :claim_id "
                                "OR right_claim_id = :claim_id "
                                "ORDER BY recorded_at, conflict_id"
                            ),
                            {"claim_id": claim.claim_id},
                        )
                    )
                    .mappings()
                    .all()
                )
            results.append(
                ClaimSearchResult(
                    state=state,
                    rank_score=1.0 / (1.0 + index),
                    evidence=tuple(await self.list_claim_evidence(claim.claim_id)),
                    conflicts=tuple(
                        _epistemic_conflict_from_row(item) for item in conflict_rows
                    ),
                    plasticity=await self.get_retrieval_plasticity(
                        "claim",
                        claim.claim_id,
                    ),
                )
            )
        results.sort(key=lambda item: (-item.rank_score, item.state.claim.recorded_at))
        return results[: max(1, int(top_k))]


def _node_from_row(row: Any) -> MemoryNode:
    return MemoryNode(
        node_id=str(row["node_id"]),
        node_type=NodeType(str(row["node_type"])),
        file_path=str(row["file_path"]) if row["file_path"] is not None else None,
        content_hash=(
            str(row["content_hash"]) if row["content_hash"] is not None else None
        ),
        title=str(row["title"] or ""),
        activation_strength=float(row["activation_strength"]),
        access_count=int(row["access_count"]),
        last_accessed_at=(
            float(row["last_accessed_at"])
            if row["last_accessed_at"] is not None
            else None
        ),
        emotional_valence=float(row["emotional_valence"]),
        emotional_arousal=float(row["emotional_arousal"]),
        importance=float(row["importance"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        embedding_synced=bool(row["embedding_synced"]),
    )


def _edge_from_row(row: Any) -> MemoryEdge:
    return MemoryEdge(
        edge_id=str(row["edge_id"]),
        source_id=str(row["source_id"]),
        target_id=str(row["target_id"]),
        edge_type=EdgeType(str(row["edge_type"])),
        weight=float(row["weight"]),
        base_strength=float(row["base_strength"]),
        reinforcement=float(row["reinforcement"]),
        activation_count=int(row["activation_count"]),
        last_activated_at=(
            float(row["last_activated_at"])
            if row["last_activated_at"] is not None
            else None
        ),
        reason=str(row["reason"] or ""),
        created_at=float(row["created_at"]),
        bidirectional=bool(row["bidirectional"]),
    )


class MySQLLegacyGraphStore(_MySQLPort):
    def __init__(
        self,
        runtime: StorageBackendRuntime,
        document_index: MySQLDocumentIndexProjection,
    ) -> None:
        super().__init__(runtime)
        self._document_index = document_index

    async def get_or_create_file_node(
        self,
        file_path: str,
        title: str,
        content: str,
    ) -> MemoryNode:
        existing = await self.get_node_by_file_path(file_path)
        if existing is not None and not str(content or ""):
            return existing
        await self._document_index.upsert_document(
            file_path,
            str(content or ""),
            title,
        )
        node = await self.get_node_by_file_path(file_path)
        if node is None:
            raise RuntimeError("document node missing after MySQL upsert")
        return node

    async def get_node_by_file_path(self, file_path: str) -> MemoryNode | None:
        canonical_path, _ = canonical_file_node_id(file_path)
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM memory_nodes "
                            "WHERE file_path_sha256 = :path_hash"
                        ),
                        {"path_hash": _sha256(canonical_path)},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        if str(row["file_path"] or "") != canonical_path:
            raise DocumentIdentityConflict("document path hash collision")
        return _node_from_row(row)

    async def get_node_by_id(self, node_id: str) -> MemoryNode | None:
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text("SELECT * FROM memory_nodes WHERE node_id = :node_id"),
                        {"node_id": node_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _node_from_row(row) if row is not None else None

    async def get_lineage_node_views(
        self,
        node_ids: Sequence[str],
    ) -> dict[str, LineageNodeView]:
        identifiers = tuple(dict.fromkeys(str(node_id).strip() for node_id in node_ids))
        identifiers = tuple(node_id for node_id in identifiers if node_id)
        if not identifiers:
            return {}
        params: dict[str, Any] = {}
        marks: list[str] = []
        for index, node_id in enumerate(identifiers):
            key = f"node_{index}"
            marks.append(f":{key}")
            params[key] = node_id
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT node_id, file_path, title, "
                            "LEFT(COALESCE(document_content, ''), 500) AS snippet "
                            "FROM memory_nodes "
                            f"WHERE node_id IN ({', '.join(marks)}) "
                            "AND COALESCE(is_deleted, FALSE) = FALSE"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
        by_id = {
            str(row["node_id"]): LineageNodeView(
                node_id=str(row["node_id"]),
                file_path=str(row["file_path"] or ""),
                title=str(row["title"] or ""),
                snippet=str(row["snippet"] or ""),
            )
            for row in rows
            if row["file_path"]
        }
        return {node_id: by_id[node_id] for node_id in identifiers if node_id in by_id}

    async def create_or_update_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: str,
        **kwargs: Any,
    ) -> MemoryEdge:
        if not source_id or not target_id:
            raise ValueError("memory edge endpoints must not be empty")
        if source_id == target_id:
            raise ValueError("memory edge does not permit a self-loop")
        kind = EdgeType(edge_type)
        strength = float(kwargs.get("strength", 0.5))
        if not math.isfinite(strength) or not 0.0 <= strength <= 1.0:
            raise ValueError("memory edge strength must be within [0, 1]")
        bidirectional = bool(
            kwargs.get("bidirectional", True) and kind not in DIRECTIONAL_EDGE_TYPES
        )
        reason = str(kwargs.get("reason") or "")

        async def _operation(session: AsyncSession) -> MemoryEdge:
            endpoints = (
                (
                    await session.execute(
                        text(
                            "SELECT node_id FROM memory_nodes "
                            "WHERE node_id IN (:source_id, :target_id) FOR UPDATE"
                        ),
                        {"source_id": source_id, "target_id": target_id},
                    )
                )
                .scalars()
                .all()
            )
            if {str(item) for item in endpoints} != {source_id, target_id}:
                raise ValueError("memory edge endpoint does not exist")
            now = time.time()

            async def _upsert(left: str, right: str) -> Any:
                existing = (
                    (
                        await session.execute(
                            text(
                                "SELECT * FROM memory_edges WHERE source_id = :source_id "
                                "AND target_id = :target_id AND edge_type = :edge_type "
                                "FOR UPDATE"
                            ),
                            {
                                "source_id": left,
                                "target_id": right,
                                "edge_type": kind.value,
                            },
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is None:
                    edge_id = uuid4().hex[:8]
                    await session.execute(
                        text(
                            """INSERT INTO memory_edges (
                                edge_id, source_id, target_id, edge_type,
                                weight, base_strength, reinforcement,
                                activation_count, last_activated_at, reason,
                                created_at, bidirectional
                            ) VALUES (
                                :edge_id, :source_id, :target_id, :edge_type,
                                :strength, :strength, 0.0, 0, NULL, :reason,
                                :created_at, :bidirectional
                            )"""
                        ),
                        {
                            "edge_id": edge_id,
                            "source_id": left,
                            "target_id": right,
                            "edge_type": kind.value,
                            "strength": strength,
                            "reason": reason,
                            "created_at": now,
                            "bidirectional": bidirectional,
                        },
                    )
                    return (
                        (
                            await session.execute(
                                text(
                                    "SELECT * FROM memory_edges WHERE edge_id = :edge_id"
                                ),
                                {"edge_id": edge_id},
                            )
                        )
                        .mappings()
                        .one()
                    )
                await session.execute(
                    text(
                        """UPDATE memory_edges SET weight = :strength,
                            base_strength = :strength,
                            last_activated_at = :activated_at,
                            reason = CASE WHEN :reason = '' THEN reason ELSE :reason END,
                            bidirectional = :bidirectional
                        WHERE edge_id = :edge_id"""
                    ),
                    {
                        "strength": strength,
                        "activated_at": now,
                        "reason": reason,
                        "bidirectional": bidirectional,
                        "edge_id": str(existing["edge_id"]),
                    },
                )
                return (
                    (
                        await session.execute(
                            text("SELECT * FROM memory_edges WHERE edge_id = :edge_id"),
                            {"edge_id": str(existing["edge_id"])},
                        )
                    )
                    .mappings()
                    .one()
                )

            forward = await _upsert(source_id, target_id)
            if bidirectional:
                await _upsert(target_id, source_id)
            else:
                await session.execute(
                    text(
                        "DELETE FROM memory_edges WHERE source_id = :target_id "
                        "AND target_id = :source_id AND edge_type = :edge_type "
                        "AND bidirectional = TRUE"
                    ),
                    {
                        "source_id": source_id,
                        "target_id": target_id,
                        "edge_type": kind.value,
                    },
                )
            return _edge_from_row(forward)

        return await self._write(_operation)

    async def get_edges_from(
        self,
        node_id: str,
        min_weight: float = 0.0,
    ) -> list[MemoryEdge]:
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT * FROM memory_edges WHERE source_id = :node_id "
                        "AND weight >= :min_weight ORDER BY weight DESC, edge_id"
                    ),
                    {"node_id": node_id, "min_weight": float(min_weight)},
                )
            ).mappings()
            return [_edge_from_row(row) for row in rows]

    async def get_edges_to(
        self,
        node_id: str,
        min_weight: float = 0.0,
    ) -> list[MemoryEdge]:
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT * FROM memory_edges WHERE target_id = :node_id "
                        "AND weight >= :min_weight ORDER BY weight DESC, edge_id"
                    ),
                    {"node_id": node_id, "min_weight": float(min_weight)},
                )
            ).mappings()
            return [_edge_from_row(row) for row in rows]

    async def delete_edge(
        self,
        source_path: str,
        target_path: str,
        edge_type: Any = None,
    ) -> bool:
        source = await self.get_node_by_file_path(source_path)
        target = await self.get_node_by_file_path(target_path)
        if source is None or target is None:
            return False
        normalized = None if edge_type is None else EdgeType(edge_type)

        async def _operation(session: AsyncSession) -> bool:
            params: dict[str, Any] = {
                "source_id": source.node_id,
                "target_id": target.node_id,
            }
            type_clause = ""
            if normalized is not None:
                type_clause = " AND edge_type = :edge_type"
                params["edge_type"] = normalized.value
            if normalized in DIRECTIONAL_EDGE_TYPES:
                predicate = "source_id = :source_id AND target_id = :target_id"
            else:
                predicate = (
                    "((source_id = :source_id AND target_id = :target_id) OR "
                    "(source_id = :target_id AND target_id = :source_id))"
                )
            result = await session.execute(
                text(f"DELETE FROM memory_edges WHERE {predicate}{type_clause}"),
                params,
            )
            return result.rowcount > 0

        return await self._write(_operation)

    async def reinforce_coactivated(
        self,
        node_ids: Sequence[str],
        *,
        learning_rate: float,
    ) -> None:
        ids = tuple(dict.fromkeys(str(item) for item in node_ids if str(item)))
        if len(ids) < 2:
            return
        rate = max(0.0, min(1.0, float(learning_rate)))

        async def _operation(session: AsyncSession) -> None:
            marks: list[str] = []
            params: dict[str, Any] = {}
            for index, node_id in enumerate(ids):
                key = f"node_{index}"
                marks.append(f":{key}")
                params[key] = node_id
            existing_ids = {
                str(item)
                for item in (
                    await session.execute(
                        text(
                            "SELECT node_id FROM memory_nodes WHERE node_id IN "
                            f"({', '.join(marks)}) FOR UPDATE"
                        ),
                        params,
                    )
                )
                .scalars()
                .all()
            }
            live_ids = tuple(item for item in ids if item in existing_ids)
            now = time.time()
            for left, right in combinations(live_ids, 2):
                for source_id, target_id in ((left, right), (right, left)):
                    row = (
                        (
                            await session.execute(
                                text(
                                    "SELECT * FROM memory_edges "
                                    "WHERE source_id = :source_id "
                                    "AND target_id = :target_id "
                                    "AND edge_type = :edge_type FOR UPDATE"
                                ),
                                {
                                    "source_id": source_id,
                                    "target_id": target_id,
                                    "edge_type": EdgeType.ASSOCIATES.value,
                                },
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if row is None:
                        reinforcement = rate
                        await session.execute(
                            text(
                                """INSERT INTO memory_edges (
                                    edge_id, source_id, target_id, edge_type,
                                    weight, base_strength, reinforcement,
                                    activation_count, last_activated_at, reason,
                                    created_at, bidirectional
                                ) VALUES (
                                    :edge_id, :source_id, :target_id, :edge_type,
                                    :weight, 0.2, :reinforcement,
                                    1, :now, :reason, :now, TRUE
                                )"""
                            ),
                            {
                                "edge_id": uuid4().hex[:8],
                                "source_id": source_id,
                                "target_id": target_id,
                                "edge_type": EdgeType.ASSOCIATES.value,
                                "weight": min(1.0, 0.2 + reinforcement),
                                "reinforcement": reinforcement,
                                "now": now,
                                "reason": "共同检索激活",
                            },
                        )
                        continue
                    reinforcement = min(
                        0.8,
                        float(row["reinforcement"]) + rate,
                    )
                    await session.execute(
                        text(
                            """UPDATE memory_edges SET
                                reinforcement = :reinforcement,
                                weight = LEAST(1.0, base_strength + :reinforcement),
                                activation_count = activation_count + 1,
                                last_activated_at = :now,
                                bidirectional = TRUE
                            WHERE edge_id = :edge_id"""
                        ),
                        {
                            "reinforcement": reinforcement,
                            "now": now,
                            "edge_id": str(row["edge_id"]),
                        },
                    )

        await self._write(_operation)

    async def increment_access(self, node_id: str) -> None:
        async def _operation(session: AsyncSession) -> None:
            result = await session.execute(
                text(
                    """UPDATE memory_nodes SET
                        access_count = access_count + 1,
                        last_accessed_at = :now,
                        activation_strength = LEAST(1.0, activation_strength + 0.1),
                        updated_at = :now
                    WHERE node_id = :node_id AND is_deleted = FALSE"""
                ),
                {"node_id": node_id, "now": time.time()},
            )
            if result.rowcount != 1:
                raise ValueError(f"MemoryNodeMissing:{node_id}")

        await self._write(_operation)

    async def spread_activation(
        self,
        seed_ids: Sequence[str],
        *,
        max_depth: int,
        max_results: int,
        spread_decay: float,
        spread_threshold: float,
        allowed_edge_types: Sequence[Any],
    ) -> list[tuple[Any, ...]]:
        seeds = tuple(dict.fromkeys(str(item) for item in seed_ids if str(item)))
        allowed = {EdgeType(item) for item in allowed_edge_types}
        if not seeds or not allowed or max_depth <= 0 or max_results <= 0:
            return []
        activation = {seed: 1.0 for seed in seeds}
        paths = {seed: [seed] for seed in seeds}
        reasons: dict[str, str] = {}
        frontier = [(seed, 1.0, [seed]) for seed in seeds]
        for _ in range(max(0, int(max_depth))):
            candidates: dict[str, tuple[float, list[str], str]] = {}
            for node_id, current, path in frontier:
                for edge in await self.get_edges_from(node_id, spread_threshold):
                    if edge.edge_type not in allowed or edge.target_id in path:
                        continue
                    propagated = current * edge.weight * float(spread_decay)
                    if propagated < float(spread_threshold):
                        continue
                    previous = max(
                        activation.get(edge.target_id, float("-inf")),
                        candidates.get(edge.target_id, (float("-inf"), [], ""))[0],
                    )
                    if propagated <= previous:
                        continue
                    candidates[edge.target_id] = (
                        propagated,
                        [*path, edge.target_id],
                        f"{edge.edge_type.value}: {edge.reason}",
                    )
            if not candidates:
                break
            frontier = []
            for node_id, (score, path, reason) in candidates.items():
                if await self.get_node_by_id(node_id) is None:
                    continue
                activation[node_id] = score
                paths[node_id] = path
                reasons[node_id] = reason
                frontier.append((node_id, score, path))
        for seed in seeds:
            activation.pop(seed, None)
        ranked = sorted(activation.items(), key=lambda item: (-item[1], item[0]))
        return [
            (node_id, score, paths.get(node_id, []), reasons.get(node_id, ""))
            for node_id, score in ranked[: max(0, int(max_results))]
        ]

    async def insert_correction(
        self,
        *,
        topic: str,
        message: str,
        source: str,
        related_node_id: str | None,
        query: str,
        stream_id: str | None,
    ) -> MemoryCorrection:
        correction = MemoryCorrection(
            correction_id=uuid4().hex[:12],
            topic=topic,
            message=message,
            source=source,
            created_at=time.time(),
            related_node_id=related_node_id,
            query=query,
            stream_id=stream_id,
        )

        async def _operation(session: AsyncSession) -> MemoryCorrection:
            await session.execute(
                text(
                    """INSERT INTO memory_corrections (
                        correction_id, topic, topic_sha256, message, source,
                        created_at, related_node_id, query, stream_id
                    ) VALUES (
                        :correction_id, :topic, :topic_hash, :message, :source,
                        :created_at, :related_node_id, :query, :stream_id
                    )"""
                ),
                {
                    "correction_id": correction.correction_id,
                    "topic": correction.topic,
                    "topic_hash": _sha256(correction.topic),
                    "message": correction.message,
                    "source": correction.source,
                    "created_at": correction.created_at,
                    "related_node_id": correction.related_node_id,
                    "query": correction.query,
                    "stream_id": correction.stream_id,
                },
            )
            return correction

        return await self._write(_operation)

    async def apply_decay(self) -> int:
        async def _operation(session: AsyncSession) -> int:
            node_rows = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM memory_nodes WHERE is_deleted = FALSE FOR UPDATE"
                        )
                    )
                )
                .mappings()
                .all()
            )
            updated = 0
            for row in node_rows:
                node = _node_from_row(row)
                strength = compute_memory_strength(node)
                if abs(strength - node.activation_strength) <= 0.01:
                    continue
                await session.execute(
                    text(
                        "UPDATE memory_nodes SET activation_strength = :strength "
                        "WHERE node_id = :node_id"
                    ),
                    {"strength": strength, "node_id": node.node_id},
                )
                updated += 1
            edge_rows = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM memory_edges "
                            "WHERE edge_type = :edge_type FOR UPDATE"
                        ),
                        {"edge_type": EdgeType.ASSOCIATES.value},
                    )
                )
                .mappings()
                .all()
            )
            now = time.time()
            for row in edge_rows:
                last = row["last_activated_at"]
                if last is None:
                    continue
                days = (now - float(last)) / 86400.0
                weight = float(row["base_strength"]) + float(
                    row["reinforcement"]
                ) * math.exp(-0.05 * days)
                if weight < 0.1:
                    await session.execute(
                        text("DELETE FROM memory_edges WHERE edge_id = :edge_id"),
                        {"edge_id": str(row["edge_id"])},
                    )
                elif abs(weight - float(row["weight"])) > 0.01:
                    await session.execute(
                        text(
                            "UPDATE memory_edges SET weight = :weight "
                            "WHERE edge_id = :edge_id"
                        ),
                        {"weight": weight, "edge_id": str(row["edge_id"])},
                    )
            return updated

        return await self._write(_operation)

    async def stats(self) -> dict[str, Any]:
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            """SELECT
                            SUM(node_type = 'file' AND is_deleted = FALSE) AS file_nodes,
                            SUM(node_type = 'concept' AND is_deleted = FALSE) AS concept_nodes,
                            AVG(CASE WHEN is_deleted = FALSE
                                THEN activation_strength END) AS avg_activation
                        FROM memory_nodes"""
                        )
                    )
                )
                .mappings()
                .one()
            )
            edge_count = await connection.scalar(
                text("SELECT COUNT(*) FROM memory_edges")
            )
        return {
            "file_nodes": int(row["file_nodes"] or 0),
            "concept_nodes": int(row["concept_nodes"] or 0),
            "total_edges": int(edge_count or 0),
            "avg_activation": round(float(row["avg_activation"] or 0.0), 3),
        }

    @staticmethod
    def _node_summary(row: Any) -> dict[str, Any]:
        return {
            "node_id": str(row["node_id"]),
            "file_path": str(row["file_path"] or ""),
            "title": str(row["title"] or ""),
            "activation_strength": float(row["activation_strength"] or 0.0),
            "access_count": int(row["access_count"] or 0),
            "emotional_valence": float(row["emotional_valence"] or 0.0),
            "emotional_arousal": float(row["emotional_arousal"] or 0.0),
            "importance": float(row["importance"] or 0.0),
            "updated_at": float(row["updated_at"] or 0.0),
        }

    async def list_dream_candidate_nodes(self, limit: int) -> list[dict[str, Any]]:
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM memory_nodes WHERE node_type = 'file' "
                            "AND is_deleted = FALSE AND file_path IS NOT NULL "
                            "ORDER BY importance DESC, emotional_arousal DESC, "
                            "access_count DESC, activation_strength DESC, "
                            "updated_at DESC LIMIT :limit"
                        ),
                        {"limit": _safe_limit(limit)},
                    )
                )
                .mappings()
                .all()
            )
        return [self._node_summary(row) for row in rows]

    async def list_random_file_nodes(self, limit: int) -> list[dict[str, Any]]:
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM memory_nodes WHERE node_type = 'file' "
                            "AND is_deleted = FALSE AND file_path IS NOT NULL "
                            "ORDER BY RAND() LIMIT :limit"
                        ),
                        {"limit": _safe_limit(limit)},
                    )
                )
                .mappings()
                .all()
            )
        return [self._node_summary(row) for row in rows]

    async def dream_walk(self, **kwargs: Any) -> dict[str, Any]:
        requested = tuple(
            dict.fromkeys(str(item) for item in kwargs.get("seed_ids", ()) if str(item))
        )
        num_seeds = max(1, int(kwargs.get("num_seeds", 5)))
        candidates = await self.list_dream_candidate_nodes(max(num_seeds * 4, 20))
        candidate_ids = [str(item["node_id"]) for item in candidates]
        seeds = [item for item in requested if item in candidate_ids]
        for node_id in candidate_ids:
            if len(seeds) >= num_seeds:
                break
            if node_id not in seeds:
                seeds.append(node_id)
        if not seeds:
            return {"nodes_activated": 0, "new_edges_created": 0, "seed_ids": []}
        activated = await self.spread_activation(
            seeds,
            max_depth=max(0, int(kwargs.get("max_depth", 3))),
            max_results=100,
            spread_decay=float(kwargs.get("decay_factor", 0.6)),
            spread_threshold=0.1,
            allowed_edge_types=tuple(EdgeType),
        )
        active_ids = [*seeds, *(str(item[0]) for item in activated)]
        if bool(kwargs.get("persist_learning", False)):
            await self.reinforce_coactivated(
                active_ids[:15],
                learning_rate=float(kwargs.get("learning_rate", 0.05)),
            )
        emit = kwargs.get("emit_visual_event")
        if callable(emit):
            emit(
                "memory.dream.walk",
                {"seed_ids": seeds, "activated_ids": active_ids},
                source="dream",
            )
        return {
            "nodes_activated": len(set(active_ids)),
            "new_edges_created": 0,
            "seed_ids": seeds,
        }

    async def prune_weak_edges(self, threshold: float) -> int:
        async def _operation(session: AsyncSession) -> int:
            result = await session.execute(
                text(
                    "DELETE FROM memory_edges WHERE edge_type = :edge_type "
                    "AND weight < :threshold"
                ),
                {
                    "edge_type": EdgeType.ASSOCIATES.value,
                    "threshold": max(0.0, float(threshold)),
                },
            )
            return max(0, int(result.rowcount))

        return await self._write(_operation)

    async def prune_orphan_edges(self) -> int:
        async def _operation(session: AsyncSession) -> int:
            result = await session.execute(
                text(
                    "DELETE e FROM memory_edges e "
                    "LEFT JOIN memory_nodes s ON s.node_id = e.source_id "
                    "LEFT JOIN memory_nodes t ON t.node_id = e.target_id "
                    "WHERE s.node_id IS NULL OR t.node_id IS NULL"
                )
            )
            return max(0, int(result.rowcount))

        return await self._write(_operation)

    async def list_corrections(
        self,
        *,
        query: str = "",
        related_node_ids: Sequence[str] = (),
        limit: int = 20,
    ) -> list[MemoryCorrection]:
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": _safe_limit(limit)}
        if query:
            clauses.append("(topic LIKE :query OR message LIKE :query)")
            params["query"] = f"%{query}%"
        related = tuple(dict.fromkeys(str(item) for item in related_node_ids if item))
        if related:
            marks = []
            for index, node_id in enumerate(related):
                name = f"related_{index}"
                marks.append(f":{name}")
                params[name] = node_id
            clauses.append(f"related_node_id IN ({', '.join(marks)})")
        where = "" if not clauses else "WHERE " + " AND ".join(clauses)
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        f"SELECT * FROM memory_corrections {where} "
                        "ORDER BY created_at DESC, correction_id LIMIT :limit"
                    ),
                    params,
                )
            ).mappings()
            return [
                MemoryCorrection(
                    correction_id=str(row["correction_id"]),
                    topic=str(row["topic"]),
                    message=str(row["message"]),
                    source=str(row["source"] or "user"),
                    created_at=float(row["created_at"]),
                    related_node_id=(
                        str(row["related_node_id"])
                        if row["related_node_id"] is not None
                        else None
                    ),
                    query=str(row["query"] or ""),
                    stream_id=(
                        str(row["stream_id"]) if row["stream_id"] is not None else None
                    ),
                )
                for row in rows
            ]


class MySQLWitnessLedgerStore(_MySQLWitnessProjectionStore):
    async def list_pending(self, *, limit: int = 100) -> list[WitnessMemory]:
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM memory_witnesses WHERE projection_status "
                            "IN ('pending', 'failed') AND projection_path IS NOT NULL "
                            "ORDER BY recorded_at, witness_id LIMIT :limit"
                        ),
                        {"limit": _safe_limit(limit)},
                    )
                )
                .mappings()
                .all()
            )
            return [await _witness_from_row(connection, row) for row in rows]  # type: ignore[arg-type]

    async def get_by_projection_path(
        self,
        projection_path: str,
    ) -> WitnessMemory | None:
        projection_path_sha256 = _sha256(projection_path)
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM memory_witnesses "
                            "WHERE projection_path_sha256 = :projection_path_sha256"
                        ),
                        {"projection_path_sha256": projection_path_sha256},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is not None and str(row["projection_path"] or "") != projection_path:
                raise ImmutableMemoryRecordConflict(
                    "WitnessProjectionPathHashCollision: digest matched a different full path"
                )
            return (
                await _witness_from_row(connection, row)  # type: ignore[arg-type]
                if row is not None
                else None
            )

    async def get_state(self, consciousness_instance_id: str) -> dict[str, Any]:
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM memory_witness_state "
                            "WHERE consciousness_instance_id = :instance_id"
                        ),
                        {"instance_id": consciousness_instance_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return (
            dict(row)
            if row is not None
            else {
                "consciousness_instance_id": consciousness_instance_id,
                "last_sequence": 0,
                "revision": 0,
                "last_run_at": "",
                "last_success_at": "",
                "last_error": "",
                "updated_at": "",
            }
        )

    async def compare_and_advance_state(
        self,
        consciousness_instance_id: str,
        *,
        expected_sequence: int,
        expected_revision: int,
        next_sequence: int,
        last_run_at: str | None = None,
        last_success_at: str | None = None,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        async def _operation(session: AsyncSession) -> dict[str, Any]:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM memory_witness_state "
                            "WHERE consciousness_instance_id = :instance_id FOR UPDATE"
                        ),
                        {"instance_id": consciousness_instance_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            current = (
                dict(row)
                if row is not None
                else {
                    "last_sequence": 0,
                    "revision": 0,
                    "last_run_at": "",
                    "last_success_at": "",
                    "last_error": "",
                }
            )
            next_position, next_revision = compare_and_advance_cursor(
                current_position=int(current["last_sequence"]),
                current_revision=int(current["revision"]),
                expected_position=max(0, int(expected_sequence)),
                expected_revision=max(0, int(expected_revision)),
                next_position=max(0, int(next_sequence)),
            )
            persisted = {
                "consciousness_instance_id": consciousness_instance_id,
                "last_sequence": next_position,
                "revision": next_revision,
                "last_run_at": (
                    str(current["last_run_at"]) if last_run_at is None else last_run_at
                ),
                "last_success_at": (
                    str(current["last_success_at"])
                    if last_success_at is None
                    else last_success_at
                ),
                "last_error": (
                    str(current["last_error"]) if last_error is None else last_error
                ),
                "updated_at": _now_iso(),
            }
            if row is None:
                await session.execute(
                    text(
                        """INSERT INTO memory_witness_state (
                            consciousness_instance_id, last_sequence, revision,
                            last_run_at, last_success_at, last_error, updated_at
                        ) VALUES (
                            :consciousness_instance_id, :last_sequence, :revision,
                            :last_run_at, :last_success_at, :last_error, :updated_at
                        )"""
                    ),
                    persisted,
                )
            else:
                updated = await session.execute(
                    text(
                        """UPDATE memory_witness_state SET
                            last_sequence = :last_sequence, revision = :revision,
                            last_run_at = :last_run_at,
                            last_success_at = :last_success_at,
                            last_error = :last_error, updated_at = :updated_at
                        WHERE consciousness_instance_id = :consciousness_instance_id
                          AND last_sequence = :expected_sequence
                          AND revision = :expected_revision"""
                    ),
                    {
                        **persisted,
                        "expected_sequence": int(current["last_sequence"]),
                        "expected_revision": int(current["revision"]),
                    },
                )
                if updated.rowcount != 1:
                    raise CursorConflict("witness state changed during CAS")
            return persisted

        return await self._write(_operation)

    async def search(
        self,
        query: str,
        *,
        mode: Any,
        top_k: int,
        stream_scope: str | None,
        visibility: Sequence[str],
    ) -> list[WitnessSearchResult]:
        query_text = str(query or "").strip()
        visible = tuple(dict.fromkeys(str(item) for item in visibility if str(item)))
        if not query_text or not visible or top_k <= 0:
            return []
        params: dict[str, Any] = {
            "pattern": f"%{query_text}%",
            "limit": _safe_limit(top_k),
        }
        marks: list[str] = []
        for index, item in enumerate(visible):
            key = f"visible_{index}"
            marks.append(f":{key}")
            params[key] = item
        clauses = [
            "content LIKE :pattern",
            f"visibility IN ({', '.join(marks)})",
            "status NOT IN ('privacy_sealed', 'suppressed')",
        ]
        if stream_scope is None:
            clauses.append("stream_scope = ''")
        else:
            clauses.append("stream_scope IN ('', :stream_scope)")
            params["stream_scope"] = stream_scope
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM memory_witnesses WHERE "
                            + " AND ".join(clauses)
                            + " ORDER BY recorded_at DESC, witness_id LIMIT :limit"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
            witnesses = [
                await _witness_from_row(connection, row)  # type: ignore[arg-type]
                for row in rows
            ]
        mode_text = str(getattr(mode, "value", mode) or "")
        results: list[WitnessSearchResult] = []
        for index, witness in enumerate(witnesses):
            note = "subjective witness, not objective truth"
            if witness.epistemic_kind == EpistemicKind.LEGACY_WITNESS.value:
                note = "legacy subjective witness with incomplete provenance"
            if mode_text == "current_fact":
                note += "; corroboration required for current facts"
            results.append(
                WitnessSearchResult(
                    witness=witness,
                    rank_score=1.0 / (1.0 + index),
                    retrieval_source="witness_substring",
                    epistemic_note=note,
                )
            )
        return results

    async def migration_exists(self, migration_key: str) -> bool:
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            exists = await connection.scalar(
                text(
                    "SELECT 1 FROM memory_witness_migrations "
                    "WHERE migration_key = :migration_key"
                ),
                {"migration_key": migration_key},
            )
        return exists is not None

    async def record_migration(self, **kwargs: Any) -> None:
        migration_key = str(kwargs.get("migration_key") or "")
        values = {
            "migration_key": migration_key,
            "source_path": str(kwargs.get("source_path") or ""),
            "source_hash": str(kwargs.get("source_hash") or ""),
            "witness_id": str(kwargs.get("witness_id") or ""),
            "migrated_at": str(kwargs.get("migrated_at") or _now_iso()),
        }
        if not migration_key or not values["witness_id"]:
            raise ValueError("WitnessMigrationIdentityRequired")

        async def _operation(session: AsyncSession) -> None:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM memory_witness_migrations "
                            "WHERE migration_key = :migration_key FOR UPDATE"
                        ),
                        {"migration_key": migration_key},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is not None:
                expected = (
                    values["source_path"],
                    values["source_hash"],
                    values["witness_id"],
                )
                actual = (
                    str(row["source_path"]),
                    str(row["source_hash"]),
                    str(row["witness_id"]),
                )
                if actual != expected:
                    raise ImmutableMemoryRecordConflict(
                        f"memory_witness_migrations:{migration_key} conflict"
                    )
                return
            await session.execute(
                text(
                    """INSERT INTO memory_witness_migrations (
                        migration_key, source_path, source_hash,
                        witness_id, migrated_at
                    ) VALUES (
                        :migration_key, :source_path, :source_hash,
                        :witness_id, :migrated_at
                    )"""
                ),
                values,
            )

        await self._write(_operation)

    async def migrate_legacy(self, **kwargs: Any) -> WitnessMemory | None:
        migration_key = str(kwargs.get("migration_key") or "")
        source_path = str(kwargs.get("source_path") or "")
        source_hash = str(kwargs.get("source_hash") or "")
        witness_id = "legacy_" + _sha256(migration_key)[:32]
        recorded_at = str(kwargs.get("recorded_at") or _now_iso())
        metadata = {
            "legacy_source_path": source_path,
            "legacy_source_hash": source_hash,
            "provenance_quality": "incomplete",
        }
        values = {
            "witness_id": witness_id,
            "content": str(kwargs.get("content") or "").strip(),
            "consciousness_instance_id": "legacy_diary_plugin",
            "perspective_subject_id": "elysia",
            "epistemic_kind": EpistemicKind.LEGACY_WITNESS.value,
            "source_kind": "legacy_diary",
            "status": "active",
            "stream_scope": "",
            "visibility": "private",
            "valid_from": str(kwargs.get("valid_from") or ""),
            "valid_to": str(kwargs.get("valid_from") or ""),
            "recorded_at": recorded_at,
            "source_sequence_start": 0,
            "source_sequence_end": 0,
            "model_task_name": "",
            "projection_path": None,
            "projection_status": "pending",
            "projection_error": "",
            "metadata_json": canonical_json(metadata),
        }
        payload_sha256 = _record_hash(
            {**values, "projection_path": "", "source_event_ids": ()}
        )

        async def _operation(session: AsyncSession) -> WitnessMemory | None:
            existing_migration = await session.scalar(
                text(
                    "SELECT 1 FROM memory_witness_migrations "
                    "WHERE migration_key = :migration_key FOR UPDATE"
                ),
                {"migration_key": migration_key},
            )
            if existing_migration is not None:
                return None
            await self._immutable_insert(
                session,
                table="memory_witnesses",
                identity_column="witness_id",
                identity=witness_id,
                values={**values, "payload_sha256": payload_sha256},
                payload_sha256=payload_sha256,
            )
            await session.execute(
                text(
                    """INSERT INTO memory_witness_migrations (
                        migration_key, source_path, source_hash,
                        witness_id, migrated_at
                    ) VALUES (
                        :migration_key, :source_path, :source_hash,
                        :witness_id, :migrated_at
                    )"""
                ),
                {
                    "migration_key": migration_key,
                    "source_path": source_path,
                    "source_hash": source_hash,
                    "witness_id": witness_id,
                    "migrated_at": _now_iso(),
                },
            )
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM memory_witnesses "
                            "WHERE witness_id = :witness_id"
                        ),
                        {"witness_id": witness_id},
                    )
                )
                .mappings()
                .one()
            )
            return await _witness_from_row(session, row)

        if not migration_key:
            raise ValueError("WitnessMigrationIdentityRequired")
        return await self._write(_operation)


def create_mysql_memory_storage_bundle(
    runtime: StorageBackendRuntime,
) -> MemoryStorageBundle:
    """Bind every Memory port to one fenced MySQL runtime generation."""

    document_index = MySQLDocumentIndexProjection(runtime)
    return MemoryStorageBundle(
        backend=BackendKind.MYSQL,
        document_index=document_index,
        experiences=MySQLExperienceLedgerStore(runtime),
        witnesses=MySQLWitnessLedgerStore(runtime),
        living=MySQLLivingMemoryStore(runtime),
        epistemic=MySQLEpistemicMemoryStore(runtime),
        legacy_graph=MySQLLegacyGraphStore(runtime, document_index),
    )


__all__ = [
    "ImmutableMemoryRecordConflict",
    "MySQLDocumentIndexProjection",
    "MySQLEpistemicMemoryStore",
    "MySQLExperienceLedgerStore",
    "MySQLLegacyGraphStore",
    "MySQLLivingMemoryStore",
    "MySQLMemoryReadinessProbeError",
    "MySQLWitnessLedgerStore",
    "create_mysql_memory_storage_bundle",
    "inspect_mysql_memory_readiness",
]
