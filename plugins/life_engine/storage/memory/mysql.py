"""Fenced MySQL adapters for Life Memory domain ports."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, replace
from datetime import UTC, datetime
from itertools import combinations
from typing import Any, TypeVar
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from src.kernel.storage import (
    CursorConflict,
    canonical_json,
    compare_and_advance_cursor,
)

from ...memory.edges import DIRECTIONAL_EDGE_TYPES, EdgeType, MemoryEdge
from ...memory.epistemic import (
    ClaimEvidence,
    EpistemicConflict,
    MemoryBelief,
    MemoryClaim,
    MemoryStateEvent,
    RetrievalEpisode,
    RetrievalExposure,
    RetrievalFeedback,
)
from ...memory.experience import (
    ExperienceAppendReport,
    ExperienceRecord,
    WitnessMemory,
)
from ...memory.indexing import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
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
    CoRecallEvent,
    InterpretationSource,
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
from ..contracts import StorageBackendRuntime
from ..models import BackendKind, StorageAvailability
from .contracts import MemoryStorageBundle

_T = TypeVar("_T")
_MAX_WRITE_ATTEMPTS = 3


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
            await session.execute(
                text(
                    f"SELECT payload_sha256 FROM {table} "
                    f"WHERE {identity_column} = :identity FOR UPDATE"
                ),
                {"identity": identity},
            )
        ).mappings().one_or_none()
        if existing is not None:
            if _row_hash(existing) != payload_sha256:
                raise ImmutableMemoryRecordConflict(
                    f"{table}:{identity} already exists with different payload"
                )
            return False
        columns = tuple(values)
        await session.execute(
            text(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
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
            await session.execute(
                text(
                    "SELECT * FROM memory_nodes WHERE node_id = :node_id FOR UPDATE"
                ),
                {"node_id": node_id},
            )
        ).mappings().one_or_none()
        path_owner = (
            await session.execute(
                text(
                    "SELECT node_id, file_path FROM memory_nodes "
                    "WHERE file_path_sha256 = :path_hash FOR UPDATE"
                ),
                {"path_hash": path_hash},
            )
        ).mappings().one_or_none()
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
                raise CursorConflict("document projection revision changed concurrently")

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
                text(
                    """INSERT INTO memory_index_jobs (
                        job_id, node_id, content_hash, status, created_at,
                        updated_at, attempts, error, index_revision
                    ) VALUES (
                        :job_id, :node_id, :content_hash, 'pending', :now,
                        :now, 0, '', :revision
                    ) ON DUPLICATE KEY UPDATE
                        node_id = VALUES(node_id),
                        content_hash = VALUES(content_hash),
                        status = 'pending', updated_at = VALUES(updated_at),
                        error = '', index_revision = VALUES(index_revision)"""
                ),
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
                await session.execute(
                    text(
                        "SELECT file_path FROM memory_nodes "
                        "WHERE node_id = :node_id FOR UPDATE"
                    ),
                    {"node_id": node_id},
                )
            ).mappings().one_or_none()
            if row is None:
                return False
            if str(row["file_path"] or "") != canonical_path:
                raise DocumentIdentityConflict("document node ID belongs to another path")
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
                await session.execute(
                    text(
                        "SELECT * FROM memory_nodes WHERE node_id = :node_id FOR UPDATE"
                    ),
                    {"node_id": old_id},
                )
            ).mappings().one_or_none()
            if old is None:
                return False
            if str(old["file_path"] or "") != old_canonical:
                raise DocumentIdentityConflict("source node path is inconsistent")
            target = (
                await session.execute(
                    text(
                        "SELECT node_id FROM memory_nodes "
                        "WHERE file_path_sha256 = :path_hash FOR UPDATE"
                    ),
                    {"path_hash": _sha256(new_canonical)},
                )
            ).mappings().one_or_none()
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
                await session.execute(
                    text(
                        "SELECT * FROM memory_edges WHERE source_id = :old_id "
                        "OR target_id = :old_id FOR UPDATE"
                    ),
                    {"old_id": old_id},
                )
            ).mappings().all()
            for edge in edge_rows:
                source_id = new_id if str(edge["source_id"]) == old_id else str(edge["source_id"])
                target_id = new_id if str(edge["target_id"]) == old_id else str(edge["target_id"])
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
                await session.execute(
                    text(
                        "SELECT * FROM memory_index_jobs WHERE status = 'pending' "
                        "ORDER BY updated_at, job_id LIMIT :limit "
                        "FOR UPDATE SKIP LOCKED"
                    ),
                    {"limit": _safe_limit(limit)},
                )
            ).mappings().all()
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
                await connection.execute(
                    text(
                        "SELECT * FROM memory_nodes WHERE "
                        "(node_type = 'concept' OR (node_type = 'file' AND file_path IS NOT NULL)) "
                        f"{focus_clause} ORDER BY activation_strength DESC, node_id LIMIT :limit"
                    ),
                    params,
                )
            ).mappings().all()
            node_ids = {str(row["node_id"]) for row in node_rows}
            if not node_ids:
                return {"nodes": [], "links": []}
            edge_rows = (
                await connection.execute(
                    text(
                        "SELECT * FROM memory_edges WHERE weight >= :min_weight "
                        "ORDER BY weight DESC, edge_id"
                    ),
                    {"min_weight": float(min_weight)},
                )
            ).mappings().all()
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
                    "path": row["file_path"] if str(row["node_type"]) == "file" else None,
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
                source_event_id = str(
                    raw_record.source_event_id or raw_record.event_id
                )
                record = replace(
                    raw_record,
                    source_event_id=source_event_id,
                    recorded_at=raw_record.recorded_at or _now_iso(),
                    valid_from=raw_record.valid_from or raw_record.occurred_at,
                )
                evidence_hash = _record_hash(_experience_evidence_body(record))
                row = (
                    await session.execute(
                        text(
                            "SELECT * FROM memory_experiences "
                            "WHERE event_id = :event_id FOR UPDATE"
                        ),
                        {"event_id": record.event_id},
                    )
                ).mappings().one_or_none()
                if row is not None:
                    if str(row["payload_sha256"]) != evidence_hash:
                        raise ImmutableMemoryRecordConflict(
                            f"ExperienceIdentityConflict:{record.event_id}"
                        )
                    existing_records.append(_experience_from_row(row))
                    continue

                alias = (
                    await session.execute(
                        text(
                            "SELECT event_id FROM memory_experience_occurrence_aliases "
                            "WHERE occurrence_id = :occurrence_id FOR UPDATE"
                        ),
                        {"occurrence_id": record.event_id},
                    )
                ).mappings().one_or_none()
                if alias is not None:
                    target = (
                        await session.execute(
                            text(
                                "SELECT * FROM memory_experiences "
                                "WHERE event_id = :event_id"
                            ),
                            {"event_id": str(alias["event_id"])},
                        )
                    ).mappings().one_or_none()
                    if target is None:
                        raise RuntimeError(
                            f"ExperienceAliasTargetMissing:{record.event_id}"
                        )
                    existing_records.append(_experience_from_row(target))
                    continue

                if record.event_id != source_event_id:
                    legacy = (
                        await session.execute(
                            text(
                                "SELECT * FROM memory_experiences "
                                "WHERE event_id = :event_id FOR UPDATE"
                            ),
                            {"event_id": source_event_id},
                        )
                    ).mappings().one_or_none()
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


class _MySQLWitnessAppendStore(_MySQLPort):
    async def append(self, **kwargs: Any) -> WitnessMemory:
        source_ids = tuple(
            dict.fromkeys(
                str(item)
                for item in (kwargs.get("source_event_ids") or ())
                if str(item)
            )
        )
        if str(kwargs.get("source_kind") or "") == "experience_window" and not source_ids:
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
            "source_sequence_end": max(
                0, int(kwargs.get("source_sequence_end") or 0)
            ),
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
            inserted = await self._immutable_insert(
                session,
                table="memory_witnesses",
                identity_column="witness_id",
                identity=witness_id,
                values={**values, "payload_sha256": payload_sha256},
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
                await session.execute(
                    text(
                        "SELECT * FROM memory_witnesses WHERE witness_id = :witness_id"
                    ),
                    {"witness_id": witness_id},
                )
            ).mappings().one()
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
                await session.execute(
                    text(
                        "SELECT * FROM memory_artifact_heads "
                        "WHERE logical_key_sha256 = :logical_hash FOR UPDATE"
                    ),
                    {"logical_hash": logical_hash},
                )
            ).mappings().one_or_none()
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
                await connection.execute(
                    text(
                        "SELECT * FROM memory_artifact_heads "
                        "WHERE logical_key_sha256 = :logical_hash"
                    ),
                    {"logical_hash": _sha256(logical_key)},
                )
            ).mappings().one_or_none()
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
                ).mappings().one_or_none()
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
        refs = tuple(dict.fromkeys(str(item) for item in event.entity_refs if str(item)))
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
                                signal, signal_sha256, event_count, last_event_at
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
            result = await session.execute(
                text(
                    "UPDATE memory_witnesses SET projection_path = :projection_path, "
                    "projection_status = :status, projection_error = :error "
                    "WHERE witness_id = :witness_id"
                ),
                {
                    "witness_id": witness_id,
                    "projection_path": projection_path or None,
                    "status": status,
                    "error": error,
                },
            )
            return result.rowcount == 1

        return await self._write(_operation)


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
                await connection.execute(
                    text(
                        "SELECT * FROM memory_nodes "
                        "WHERE file_path_sha256 = :path_hash"
                    ),
                    {"path_hash": _sha256(canonical_path)},
                )
            ).mappings().one_or_none()
        if row is None:
            return None
        if str(row["file_path"] or "") != canonical_path:
            raise DocumentIdentityConflict("document path hash collision")
        return _node_from_row(row)

    async def get_node_by_id(self, node_id: str) -> MemoryNode | None:
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT * FROM memory_nodes WHERE node_id = :node_id"),
                    {"node_id": node_id},
                )
            ).mappings().one_or_none()
        return _node_from_row(row) if row is not None else None

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
                await session.execute(
                    text(
                        "SELECT node_id FROM memory_nodes "
                        "WHERE node_id IN (:source_id, :target_id) FOR UPDATE"
                    ),
                    {"source_id": source_id, "target_id": target_id},
                )
            ).scalars().all()
            if {str(item) for item in endpoints} != {source_id, target_id}:
                raise ValueError("memory edge endpoint does not exist")
            now = time.time()

            async def _upsert(left: str, right: str) -> Any:
                existing = (
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
                ).mappings().one_or_none()
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
                        await session.execute(
                            text("SELECT * FROM memory_edges WHERE edge_id = :edge_id"),
                            {"edge_id": edge_id},
                        )
                    ).mappings().one()
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
                    await session.execute(
                        text("SELECT * FROM memory_edges WHERE edge_id = :edge_id"),
                        {"edge_id": str(existing["edge_id"])},
                    )
                ).mappings().one()

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
                        str(row["stream_id"])
                        if row["stream_id"] is not None
                        else None
                    ),
                )
                for row in rows
            ]


class MySQLWitnessLedgerStore(_MySQLWitnessProjectionStore):
    async def list_pending(self, *, limit: int = 100) -> list[WitnessMemory]:
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT * FROM memory_witnesses WHERE projection_status "
                        "IN ('pending', 'failed') AND projection_path IS NOT NULL "
                        "ORDER BY recorded_at, witness_id LIMIT :limit"
                    ),
                    {"limit": _safe_limit(limit)},
                )
            ).mappings().all()
            return [await _witness_from_row(connection, row) for row in rows]  # type: ignore[arg-type]

    async def get_by_projection_path(
        self,
        projection_path: str,
    ) -> WitnessMemory | None:
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT * FROM memory_witnesses "
                        "WHERE projection_path = :projection_path"
                    ),
                    {"projection_path": projection_path},
                )
            ).mappings().one_or_none()
            return (
                await _witness_from_row(connection, row)  # type: ignore[arg-type]
                if row is not None
                else None
            )

    async def get_state(self, consciousness_instance_id: str) -> dict[str, Any]:
        assert self.runtime.engine is not None
        async with self.runtime.engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT * FROM memory_witness_state "
                        "WHERE consciousness_instance_id = :instance_id"
                    ),
                    {"instance_id": consciousness_instance_id},
                )
            ).mappings().one_or_none()
        return dict(row) if row is not None else {
            "consciousness_instance_id": consciousness_instance_id,
            "last_sequence": 0,
            "revision": 0,
            "last_run_at": "",
            "last_success_at": "",
            "last_error": "",
            "updated_at": "",
        }

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
                await session.execute(
                    text(
                        "SELECT * FROM memory_witness_state "
                        "WHERE consciousness_instance_id = :instance_id FOR UPDATE"
                    ),
                    {"instance_id": consciousness_instance_id},
                )
            ).mappings().one_or_none()
            current = dict(row) if row is not None else {
                "last_sequence": 0,
                "revision": 0,
                "last_run_at": "",
                "last_success_at": "",
                "last_error": "",
            }
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
                    str(current["last_run_at"])
                    if last_run_at is None
                    else last_run_at
                ),
                "last_success_at": (
                    str(current["last_success_at"])
                    if last_success_at is None
                    else last_success_at
                ),
                "last_error": (
                    str(current["last_error"])
                    if last_error is None
                    else last_error
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


def create_mysql_memory_storage_bundle(
    runtime: StorageBackendRuntime,
) -> MemoryStorageBundle:
    """Bind every Memory port to one fenced MySQL runtime generation."""

    document_index = MySQLDocumentIndexProjection(runtime)
    return MemoryStorageBundle(
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
    "MySQLWitnessLedgerStore",
    "create_mysql_memory_storage_bundle",
]
