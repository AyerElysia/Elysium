"""Asynchronous chunk-vector indexing worker.

SQLite document writes only enqueue jobs.  This module owns the optional
embedding and vector-store side effects and keeps them out of the file-write
path.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from .eligibility import assess_indexed_document_path
from .indexing import (
    ACTIVE_CHUNK_STATE_KEY,
    IndexJob,
    _enqueue_index_job_in_transaction,
    claim_index_jobs,
    index_job_report_identity,
    read_active_chunk_index_state,
    transaction,
    write_active_chunk_index_state,
)
from .nodes import NodeType, compute_content_hash, generate_file_node_id
from .search import EmbeddingResult, embed_texts
from .sqlite_runtime import run_db

CHUNK_INDEX_VERSION = 1
CHUNK_COLLECTION_PREFIX = "life_memory_chunks"
DEFAULT_RECLAIM_AFTER = 600.0
_IndexJobIdentity = tuple[str, int]


@dataclass(frozen=True)
class _ChunkPayload:
    job_id: str
    node_id: str
    file_path: str
    title: str
    document_hash: str
    index_revision: int
    chunk_id: str
    chunk_index: int
    chunk_hash: str
    content: str


def _job_identity(job: IndexJob) -> _IndexJobIdentity:
    return job.job_id, int(job.index_revision)


def _payload_identity(payload: _ChunkPayload) -> _IndexJobIdentity:
    return payload.job_id, int(payload.index_revision)


@dataclass(frozen=True)
class IndexWorkerReport:
    """Serializable summary of one worker pass."""

    claimed: int = 0
    embedded_chunks: int = 0
    upserted_chunks: int = 0
    completed: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    stale: tuple[str, ...] = ()
    model_name: str = ""
    dimension: int = 0
    errors: Mapping[str, str] = field(default_factory=dict)

    @property
    def processed(self) -> int:
        return len(self.completed) + len(self.failed) + len(self.stale)


def chunk_collection_name(model_name: str, dimension: int) -> str:
    """Return a stable, versioned collection name for one vector shape."""
    model = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(model_name or "unknown")).strip("_")
    model = model[:48] or "unknown"
    return f"{CHUNK_COLLECTION_PREFIX}_v{CHUNK_INDEX_VERSION}_{model}_{int(dimension)}"


def chunk_collection_metadata(model_name: str, dimension: int) -> dict[str, Any]:
    """Metadata written when a versioned chunk collection is created."""
    return {
        "collection_kind": "life_memory_chunk",
        "chunk_index_version": CHUNK_INDEX_VERSION,
        "embedding_model": str(model_name or "unknown"),
        "embedding_dimension": int(dimension),
    }


async def get_chunk_collection(
    db_path: str,
    model_name: str,
    dimension: int,
) -> Any:
    """Resolve/create the versioned collection without touching legacy data."""
    from src.kernel.vector_db import get_vector_db_service

    vector_service = get_vector_db_service(db_path)
    name = chunk_collection_name(model_name, dimension)
    metadata = chunk_collection_metadata(model_name, dimension)
    try:
        return await vector_service.get_or_create_collection(name, metadata=metadata)
    except TypeError:
        # Small test doubles and older vector services accepted only ``name``.
        return await vector_service.get_or_create_collection(name)


async def get_named_chunk_collection(db_path: str, collection_name: str) -> Any:
    """Get an exact existing collection by name without creating or scanning."""
    from src.kernel.vector_db import get_vector_db_service

    vector_service = get_vector_db_service(db_path)
    cached = getattr(vector_service, "_collections", None)
    if isinstance(cached, Mapping) and collection_name in cached:
        return cached[collection_name]

    if not bool(getattr(vector_service, "_initialized", False)):
        initialize = getattr(vector_service, "initialize", None)
        if callable(initialize):
            await initialize(db_path)

    client = getattr(vector_service, "_client", None)
    get_collection = getattr(client, "get_collection", None)
    if not callable(get_collection):
        get_collection = getattr(vector_service, "get_collection", None)
    if not callable(get_collection):
        raise RuntimeError("ExactCollectionLookupUnavailable")

    collection = await _call_external(get_collection, name=collection_name)
    if collection is None:
        raise LookupError(collection_name)
    if isinstance(cached, dict):
        cached[collection_name] = collection
    return collection


async def _call_external(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run sync test/backend hooks off-loop while retaining async hooks."""
    if inspect.iscoroutinefunction(func):
        return await func(*args, **kwargs)
    value = await asyncio.to_thread(func, *args, **kwargs)
    if inspect.isawaitable(value):
        return await value
    return value


async def _resolve_collection(
    resolver: Callable[..., Any],
    model_name: str,
    dimension: int,
    metadata: Mapping[str, Any],
) -> Any:
    """Support zero-, one-, two-, and three-argument collection hooks."""
    try:
        signature = inspect.signature(resolver)
    except (TypeError, ValueError):
        signature = None
    if signature is not None:
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        has_varargs = any(
            parameter.kind == inspect.Parameter.VAR_POSITIONAL
            for parameter in signature.parameters.values()
        )
        if not has_varargs:
            count = len(positional)
            if count <= 0:
                return await _call_external(resolver)
            if count == 1:
                return await _call_external(resolver, model_name)
            if count == 2:
                return await _call_external(resolver, model_name, dimension)
    return await _call_external(resolver, model_name, dimension, metadata)


async def _db_call(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Keep SQLite work off-loop while supporting thread-bound test handles."""
    try:
        return await run_db(func, *args, **kwargs)
    except sqlite3.ProgrammingError as exc:
        if "created in a thread" not in str(exc):
            raise
        return func(*args, **kwargs)


def _tombstone_table_exists(db: sqlite3.Connection) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_vector_tombstones'",
    ).fetchone()
    return row is not None


def _read_pending_tombstones(
    db: sqlite3.Connection,
    *,
    collection_name: str,
    limit: int,
) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """Partition pending tombstones into obsolete and currently live IDs.

    A document can be deleted and later revived with the same content.  Chunk
    IDs are content-addressed, so an old tombstone may then name the exact
    current chunk.  Deleting it after the replacement vector was upserted
    would make the SQL projection claim success while Chroma silently lost the
    vector.  Live IDs are therefore acknowledged without an external delete.
    """
    if not _tombstone_table_exists(db):
        return [], []
    target = str(collection_name or "").strip()
    if not target:
        return [], []
    rows = db.execute(
        """SELECT tombstone_id, chunk_id, force_delete
           FROM memory_vector_tombstones
           WHERE consumed_at IS NULL AND (
               collection_name = ? OR (
                   collection_name = '' AND EXISTS (
                       SELECT 1 FROM memory_index_state
                       WHERE state_key = ? AND collection_name = ?
                   )
               )
           )
           ORDER BY tombstone_id LIMIT ?""",
        (target, ACTIVE_CHUNK_STATE_KEY, target, max(0, int(limit))),
    ).fetchall()
    chunk_ids = list(dict.fromkeys(str(row[1]) for row in rows if row[1]))
    if not chunk_ids:
        return [], []
    placeholders = ",".join("?" for _ in chunk_ids)
    live_rows = db.execute(
        "SELECT c.chunk_id FROM memory_chunks c "
        "JOIN memory_nodes n ON n.node_id = c.node_id "
        f"WHERE c.chunk_id IN ({placeholders}) "
        "AND COALESCE(n.is_deleted, 0) = 0",
        chunk_ids,
    ).fetchall()
    live = {str(row[0]) for row in live_rows if row[0]}
    to_delete: list[tuple[int, str]] = []
    acknowledge: list[tuple[int, str]] = []
    for row in rows:
        tombstone = (int(row[0]), str(row[1]))
        if bool(row[2]) or tombstone[1] not in live:
            to_delete.append(tombstone)
        else:
            acknowledge.append(tombstone)
    return to_delete, acknowledge


def _remove_processed_tombstones(
    db: sqlite3.Connection,
    tombstone_ids: Sequence[int],
    *,
    now: float,
) -> None:
    """Acknowledge only rows consumed against the exact target collection."""
    if not tombstone_ids or not _tombstone_table_exists(db):
        return
    placeholders = ",".join("?" for _ in tombstone_ids)
    db.execute(
        "UPDATE memory_vector_tombstones SET consumed_at = ? "
        f"WHERE consumed_at IS NULL AND tombstone_id IN ({placeholders})",
        [now, *tombstone_ids],
    )


def _normalize_embedding_result(value: Any, expected_count: int) -> EmbeddingResult:
    """Normalize injected fakes and provider responses to the validated shape."""
    model_name = str(getattr(value, "model_name", "") or "")
    raw = getattr(value, "embeddings", value)
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], str):
        raw, model_name = value
    if raw is None:
        raise ValueError("Embedding 响应为空")
    try:
        raw_vectors = list(raw)
    except TypeError as exc:
        raise ValueError("Embedding 响应不是向量列表") from exc
    if len(raw_vectors) != expected_count:
        raise ValueError(
            f"Embedding 数量不匹配: expected={expected_count}, actual={len(raw_vectors)}"
        )

    vectors: list[list[float]] = []
    dimension: int | None = None
    for index, raw_vector in enumerate(raw_vectors):
        if raw_vector is None:
            raise ValueError(f"Embedding 向量为空: index={index}")
        vector = [float(item) for item in raw_vector]
        if not vector:
            raise ValueError(f"Embedding 向量为空: index={index}")
        if dimension is None:
            dimension = len(vector)
        elif len(vector) != dimension:
            raise ValueError("Embedding 向量维度不一致")
        vectors.append(vector)
    return EmbeddingResult(embeddings=vectors, model_name=model_name)


def _is_deleted(value: Any) -> bool:
    """Interpret legacy SQLite deletion values without treating ``'0'`` as true."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    try:
        return bool(int(value or 0))
    except (TypeError, ValueError):
        return bool(value)


def _job_identity_error(job: IndexJob) -> str | None:
    """Reject a persisted outbox row whose deterministic identity is damaged."""
    node_id = str(job.node_id or "").strip()
    content_hash = str(job.content_hash or "").strip()
    if not node_id or not content_hash:
        return "InvalidJobIdentity"
    if str(job.job_id or "") != f"{node_id}:{content_hash}":
        return "InvalidJobIdentity"
    return None


def _is_active_embeddable_file_node(node: sqlite3.Row | None) -> bool:
    """Return whether a stored node is safe to export as a file document."""
    if node is None or _is_deleted(node["is_deleted"]):
        return False
    if str(node["node_type"] or "").lower() != NodeType.FILE.value:
        return False
    decision = assess_indexed_document_path(node["file_path"])
    return bool(
        decision.eligible
        and str(node["node_id"] or "") == generate_file_node_id(decision.path)
    )


def _current_revision(node: sqlite3.Row, job: IndexJob) -> tuple[int | None, str | None]:
    """Return the compatible outbox revision or a non-exportable identity error."""
    try:
        revision = int(node["index_revision"] or 0)
        expected = int(job.index_revision)
    except (TypeError, ValueError):
        return None, "InvalidJobIdentity"
    if revision < 0 or expected < 0:
        return None, "InvalidJobIdentity"
    return expected, None


def _payloads_for_job(
    db: sqlite3.Connection,
    job: IndexJob,
    node: sqlite3.Row,
    *,
    index_revision: int,
) -> tuple[list[_ChunkPayload], str | None]:
    """Build a complete deterministic chunk set only after node identity passed."""
    rows = db.execute(
        "SELECT chunk_id, node_id, chunk_index, content_hash, content, title "
        "FROM memory_chunks WHERE node_id = ? ORDER BY chunk_index, chunk_id",
        (job.node_id,),
    ).fetchall()
    if not rows:
        return [], "EmptyDocument"

    payloads: list[_ChunkPayload] = []
    for expected_index, row in enumerate(rows):
        try:
            chunk_index = int(row["chunk_index"])
        except (TypeError, ValueError):
            return [], "InvalidChunkIdentity"
        content = row["content"]
        chunk_hash = str(row["content_hash"] or "")
        if (
            str(row["node_id"] or "") != str(job.node_id)
            or chunk_index != expected_index
            or not isinstance(content, str)
            or not content
            or chunk_hash != compute_content_hash(content)
            or str(row["chunk_id"] or "")
            != f"{job.node_id}:{chunk_index}:{chunk_hash}"
        ):
            return [], "InvalidChunkIdentity"
        payloads.append(
            _ChunkPayload(
                job_id=job.job_id,
                node_id=str(node["node_id"]),
                file_path=str(node["file_path"]),
                title=str(node["title"] or ""),
                document_hash=job.content_hash,
                index_revision=index_revision,
                chunk_id=str(row["chunk_id"]),
                chunk_index=chunk_index,
                chunk_hash=chunk_hash,
                content=content,
            )
        )
    return payloads, None


def _load_job_payloads(
    db: sqlite3.Connection,
    jobs: Sequence[IndexJob],
) -> tuple[list[_ChunkPayload], list[str], dict[str, str]]:
    """Read only complete, strictly identified document payloads for claimed jobs."""
    payloads: list[_ChunkPayload] = []
    stale: list[str] = []
    errors: dict[str, str] = {}
    for job in jobs:
        identity = index_job_report_identity(job)
        node = db.execute(
            "SELECT node_id, node_type, file_path, title, content_hash, index_revision, is_deleted "
            "FROM memory_nodes WHERE node_id = ?",
            (job.node_id,),
        ).fetchone()
        if not _is_active_embeddable_file_node(node):
            stale.append(identity)
            errors[identity] = "InvalidDocumentIdentity"
            continue
        if _job_identity_error(job) is not None:
            stale.append(identity)
            errors[identity] = "InvalidJobIdentity"
            continue
        assert node is not None
        job_revision, revision_error = _current_revision(node, job)
        if revision_error:
            stale.append(identity)
            errors[identity] = revision_error
            continue
        assert job_revision is not None
        if (
            str(node["content_hash"] or "") != job.content_hash
            or int(node["index_revision"] or 0) != job_revision
        ):
            stale.append(identity)
            errors[identity] = "StaleRevision"
            continue
        job_payloads, payload_error = _payloads_for_job(
            db,
            job,
            node,
            index_revision=job_revision,
        )
        if payload_error == "EmptyDocument":
            stale.append(identity)  # 永久空文档 → stale 而非 failed，避免 retry 循环
            errors[identity] = payload_error
            continue
        if payload_error:
            stale.append(identity)
            errors[identity] = payload_error
            continue
        payloads.extend(job_payloads)
    return payloads, stale, errors


def _mark_jobs(
    db: sqlite3.Connection,
    jobs: Iterable[IndexJob],
    status: str,
    errors: Mapping[str, str] | None = None,
    *,
    now: float | None = None,
) -> None:
    """Update only jobs still owned by this worker pass."""
    claimed = {index_job_report_identity(job): job for job in jobs}
    if not claimed:
        return
    timestamp = float(time.time() if now is None else now)
    with transaction(db):
        db.executemany(
            "UPDATE memory_index_jobs SET status = ?, updated_at = ?, error = ?, "
            "claim_token = '' WHERE job_id = ? AND node_id = ? "
            "AND content_hash = ? AND index_revision = ? "
            "AND status = 'processing' AND claim_token = ?",
            [
                (
                    status,
                    timestamp,
                    str((errors or {}).get(index_job_report_identity(job), "")),
                    job.job_id,
                    job.node_id,
                    job.content_hash,
                    int(job.index_revision),
                    job.claim_token,
                )
                for job in claimed.values()
            ],
        )


def _revalidate_payloads(
    db: sqlite3.Connection,
    jobs: Sequence[IndexJob],
    payloads: Sequence[_ChunkPayload],
    *,
    now: float,
) -> tuple[list[_ChunkPayload], list[int], list[IndexJob], list[str], dict[str, str]]:
    """Keep only payloads that still match strict current SQLite identity."""
    payloads_by_job: dict[_IndexJobIdentity, list[_ChunkPayload]] = {}
    for payload in payloads:
        payloads_by_job.setdefault(_payload_identity(payload), []).append(payload)

    valid_jobs_by_identity: dict[_IndexJobIdentity, IndexJob] = {}
    stale: list[str] = []
    errors: dict[str, str] = {}
    with transaction(db):
        for job in jobs:
            identity = _job_identity(job)
            report_identity = index_job_report_identity(job)
            node = db.execute(
                "SELECT node_id, node_type, file_path, title, content_hash, index_revision, is_deleted "
                "FROM memory_nodes WHERE node_id = ?",
                (job.node_id,),
            ).fetchone()
            job_row = db.execute(
                "SELECT job_id, node_id, status, content_hash, index_revision, claim_token "
                "FROM memory_index_jobs WHERE job_id = ? AND index_revision = ?",
                (job.job_id, int(job.index_revision)),
            ).fetchone()
            error_type: str | None = None
            if not _is_active_embeddable_file_node(node):
                error_type = "InvalidDocumentIdentity"
            elif _job_identity_error(job) is not None:
                error_type = "InvalidJobIdentity"
            job_revision: int | None = None
            if error_type is None:
                assert node is not None
                job_revision, error_type = _current_revision(node, job)
            if error_type is None:
                assert node is not None and job_revision is not None
                if (
                    str(node["content_hash"] or "") != job.content_hash
                    or int(node["index_revision"] or 0) != job_revision
                ):
                    error_type = "StaleRevision"
                elif (
                    job_row is None
                    or str(job_row["status"] or "") != "processing"
                    or str(job_row["claim_token"] or "") != job.claim_token
                ):
                    error_type = "JobStateChanged"
                elif (
                    str(job_row["job_id"] or "") != job.job_id
                    or str(job_row["node_id"] or "") != job.node_id
                    or str(job_row["content_hash"] or "") != job.content_hash
                    or int(job_row["index_revision"] or job_revision) != job_revision
                ):
                    error_type = "StaleRevision"
                else:
                    current_payloads, payload_error = _payloads_for_job(
                        db,
                        job,
                        node,
                        index_revision=job_revision,
                    )
                    if payload_error:
                        error_type = payload_error
                    elif current_payloads != payloads_by_job.get(identity, []):
                        error_type = "StalePayload"

            if error_type:
                if job_row is not None and str(job_row["status"] or "") == "processing":
                    db.execute(
                        "UPDATE memory_index_jobs SET status = 'stale', updated_at = ?, error = ? "
                        "WHERE job_id = ? AND index_revision = ? "
                        "AND status = 'processing' AND claim_token = ?",
                        (
                            now,
                            error_type,
                            job.job_id,
                            int(job.index_revision),
                            job.claim_token,
                        ),
                    )
                stale.append(report_identity)
                errors[report_identity] = error_type
                continue

            assert job_row is not None and job_revision is not None
            valid_jobs_by_identity[identity] = job

    valid_payloads: list[_ChunkPayload] = []
    embedding_indices: list[int] = []
    for index, payload in enumerate(payloads):
        if _payload_identity(payload) in valid_jobs_by_identity:
            valid_payloads.append(payload)
            embedding_indices.append(index)
    valid_jobs = [
        valid_jobs_by_identity[_job_identity(job)]
        for job in jobs
        if _job_identity(job) in valid_jobs_by_identity
    ]
    return valid_payloads, embedding_indices, valid_jobs, stale, errors


def _complete_jobs(
    db: sqlite3.Connection,
    jobs: Sequence[IndexJob],
    *,
    vector_chunk_ids: Mapping[_IndexJobIdentity, Sequence[str]],
    collection_name: str,
    model_name: str,
    dimension: int,
    now: float,
) -> tuple[list[str], list[str], dict[str, str]]:
    """Atomically guard node/job completion against concurrent document writes."""
    completed: list[str] = []
    stale: list[str] = []
    errors: dict[str, str] = {}

    def compensate(
        job: IndexJob,
        identity: _IndexJobIdentity,
        report_identity: str,
        reason: str,
    ) -> None:
        chunk_ids = tuple(dict.fromkeys(vector_chunk_ids.get(identity, ())))
        db.executemany(
            """INSERT INTO memory_vector_tombstones (
                node_id, chunk_id, collection_name, created_at, consumed_at, force_delete
            ) VALUES (?, ?, ?, ?, NULL, 1)""",
            [(job.node_id, chunk_id, collection_name, now) for chunk_id in chunk_ids],
        )
        current = db.execute(
            "SELECT content_hash, index_revision FROM memory_nodes "
            "WHERE node_id = ? AND COALESCE(is_deleted, 0) = 0",
            (job.node_id,),
        ).fetchone()
        if current is None or not str(current["content_hash"] or ""):
            return
        current_hash = str(current["content_hash"])
        current_revision = int(current["index_revision"] or 0)
        db.execute(
            "UPDATE memory_nodes SET embedding_synced = 0, "
            "embedding_content_hash = NULL, embedding_model = NULL, "
            "embedding_updated_at = NULL WHERE node_id = ? "
            "AND content_hash = ? AND index_revision = ?",
            (job.node_id, current_hash, current_revision),
        )
        _enqueue_index_job_in_transaction(
            db,
            job.node_id,
            current_hash,
            now=now,
            index_revision=current_revision,
            requeue_statuses=frozenset({"completed", "failed", "stale"}),
        )
        errors[report_identity] = reason

    with transaction(db):
        for job in jobs:
            identity = _job_identity(job)
            report_identity = index_job_report_identity(job)
            node_row = db.execute(
                "SELECT node_id, node_type, file_path, title, content_hash, index_revision, is_deleted "
                "FROM memory_nodes WHERE node_id = ?",
                (job.node_id,),
            ).fetchone()
            error_type: str | None = None
            if not _is_active_embeddable_file_node(node_row):
                error_type = "InvalidDocumentIdentity"
            elif _job_identity_error(job) is not None:
                error_type = "InvalidJobIdentity"
            expected_revision: int | None = None
            if error_type is None:
                assert node_row is not None
                expected_revision, error_type = _current_revision(node_row, job)
            if error_type is None:
                assert node_row is not None and expected_revision is not None
                if (
                    str(node_row["content_hash"] or "") != job.content_hash
                    or int(node_row["index_revision"] or 0) != expected_revision
                ):
                    error_type = "StaleRevision"
            if error_type:
                db.execute(
                    "UPDATE memory_index_jobs SET status = 'stale', updated_at = ?, error = ? "
                    "WHERE job_id = ? AND index_revision = ? "
                    "AND status = 'processing' AND claim_token = ?",
                    (
                        now,
                        error_type,
                        job.job_id,
                        int(job.index_revision),
                        job.claim_token,
                    ),
                )
                stale.append(report_identity)
                errors[report_identity] = error_type
                compensate(job, identity, report_identity, error_type)
                continue

            assert expected_revision is not None
            job_cursor = db.execute(
                "UPDATE memory_index_jobs SET status = 'completed', updated_at = ?, "
                "error = '', claim_token = '' "
                "WHERE job_id = ? AND node_id = ? AND status = 'processing' AND content_hash = ? "
                "AND index_revision = ? AND claim_token = ? AND EXISTS ("
                "SELECT 1 FROM memory_nodes WHERE node_id = ? AND node_type = ? "
                "AND is_deleted = 0 AND file_path = ? AND content_hash = ? "
                "AND index_revision = ?)",
                (
                    now,
                    job.job_id,
                    job.node_id,
                    job.content_hash,
                    expected_revision,
                    job.claim_token,
                    job.node_id,
                    NodeType.FILE.value,
                    str(node_row["file_path"]),
                    job.content_hash,
                    expected_revision,
                ),
            )
            if job_cursor.rowcount != 1:
                stale.append(report_identity)
                errors[report_identity] = "JobStateChanged"
                compensate(
                    job,
                    identity,
                    report_identity,
                    "ClaimLeaseLostRequeued",
                )
                continue
            node_cursor = db.execute(
                "UPDATE memory_nodes SET embedding_synced = 1, "
                "embedding_content_hash = ?, embedding_model = ?, embedding_updated_at = ? "
                "WHERE node_id = ? AND content_hash = ? AND index_revision = ?",
                (
                    job.content_hash,
                    str(model_name or "unknown"),
                    now,
                    job.node_id,
                    job.content_hash,
                    expected_revision,
                ),
            )
            if node_cursor.rowcount == 1:
                completed.append(report_identity)
            else:
                db.execute(
                    "UPDATE memory_index_jobs SET status = 'stale', error = 'StaleRevision' "
                    "WHERE job_id = ? AND index_revision = ? AND status = 'completed'",
                    (job.job_id, expected_revision),
                )
                stale.append(report_identity)
                errors[report_identity] = "StaleRevision"
                compensate(
                    job,
                    identity,
                    report_identity,
                    "StaleRevisionRequeued",
                )
        if completed:
            write_active_chunk_index_state(
                db,
                collection_name,
                model_name,
                dimension,
                CHUNK_INDEX_VERSION,
                now=now,
            )
    return completed, stale, errors


async def consume_vector_tombstones(
    db: sqlite3.Connection,
    collection: Any,
    *,
    limit: int = 200,
    now: float | None = None,
) -> int:
    """Delete superseded chunk vectors from Chroma and clear tombstone records."""
    if collection is None:
        return 0
    collection_name = str(getattr(collection, "name", "") or "").strip()
    if not collection_name:
        return 0
    delete_rows, acknowledge_rows = await _db_call(
        _read_pending_tombstones,
        db,
        collection_name=collection_name,
        limit=limit,
    )
    if not delete_rows and not acknowledge_rows:
        return 0

    obsolete_ids = list(dict.fromkeys(chunk_id for _, chunk_id in delete_rows))

    delete_func = getattr(collection, "delete", None)
    if not callable(delete_func):
        return 0

    if obsolete_ids:
        try:
            await _call_external(delete_func, ids=obsolete_ids)
        except Exception as exc:  # noqa: BLE001
            import logging

            logging.getLogger("life_engine.memory.worker").warning(
                "向量 tombstone 清理失败 "
                f"({len(obsolete_ids)} ids): {type(exc).__name__}: {exc}"
            )
            return 0

    def _clear(db_: sqlite3.Connection) -> None:
        with transaction(db_):
            if obsolete_ids:
                placeholders = ",".join("?" for _ in obsolete_ids)
                revived_rows = db_.execute(
                    "SELECT DISTINCT n.node_id, n.content_hash, n.index_revision "
                    "FROM memory_chunks c "
                    "JOIN memory_nodes n ON n.node_id = c.node_id "
                    f"WHERE c.chunk_id IN ({placeholders}) "
                    "AND COALESCE(n.is_deleted, 0) = 0",
                    obsolete_ids,
                ).fetchall()
                requeue_at = time.time()
                for row in revived_rows:
                    node_id = str(row[0] or "")
                    content_hash = str(row[1] or "")
                    revision = int(row[2] or 0)
                    if not node_id or not content_hash or revision <= 0:
                        continue
                    db_.execute(
                        "UPDATE memory_nodes SET embedding_synced = 0, "
                        "embedding_content_hash = NULL, embedding_model = NULL, "
                        "embedding_updated_at = NULL WHERE node_id = ? "
                        "AND content_hash = ? AND index_revision = ?",
                        (node_id, content_hash, revision),
                    )
                    _enqueue_index_job_in_transaction(
                        db_,
                        node_id,
                        content_hash,
                        now=requeue_at,
                        index_revision=revision,
                        requeue_statuses=frozenset({"completed", "failed", "stale"}),
                    )
            _remove_processed_tombstones(
                db_,
                [row_id for row_id, _ in [*delete_rows, *acknowledge_rows]],
                now=float(time.time() if now is None else now),
            )

    await _db_call(_clear, db)
    return len(delete_rows) + len(acknowledge_rows)


async def process_index_jobs(
    db: sqlite3.Connection,
    collection: Any = None,
    *,
    limit: int = 10,
    embed_texts_func: Callable[[Sequence[str]], Any] | None = None,
    collection_resolver: Callable[..., Any] | None = None,
    db_path: str | None = None,
    collection_upsert_func: Callable[..., Any] | None = None,
    retry_failed: bool = True,
    reclaim_after: float | None = DEFAULT_RECLAIM_AFTER,
    now: float | None = None,
) -> IndexWorkerReport:
    """Process a deterministic batch of pending chunk-index jobs.

    The provider receives all chunks from the claimed jobs in one call, and
    Chroma receives one ``upsert`` call for that batch.  Every SQLite success
    is guarded by the job's document hash and revision.
    """
    timestamp = float(time.time() if now is None else now)
    jobs = await _db_call(
        claim_index_jobs,
        db,
        limit=max(0, int(limit)),
        now=timestamp,
        reclaim_after=reclaim_after,
        retry_failed=retry_failed,
    )
    report = IndexWorkerReport(claimed=len(jobs))
    if not jobs:
        return report

    try:
        payloads, initial_stale, load_errors = await _db_call(_load_job_payloads, db, jobs)
    except Exception as exc:
        error_type = type(exc).__name__
        failed_ids = [index_job_report_identity(job) for job in jobs]
        errors = {job_id: error_type for job_id in failed_ids}
        await _db_call(
            _mark_jobs,
            db,
            jobs,
            "failed",
            errors,
            now=timestamp,
        )
        return IndexWorkerReport(
            claimed=len(jobs),
            failed=tuple(failed_ids),
            errors=errors,
        )

    stale_ids = list(initial_stale)
    error_map = dict(load_errors)
    if stale_ids:
        stale_set = set(stale_ids)
        await _db_call(
            _mark_jobs,
            db,
            (job for job in jobs if index_job_report_identity(job) in stale_set),
            "stale",
            error_map,
            now=timestamp,
        )

    payload_job_ids = {_payload_identity(payload) for payload in payloads}
    failed_without_payload = [
        job
        for job in jobs
        if _job_identity(job) not in payload_job_ids
        and index_job_report_identity(job) not in stale_ids
    ]
    if failed_without_payload:
        await _db_call(
            _mark_jobs,
            db,
            failed_without_payload,
            "failed",
            error_map,
            now=timestamp,
        )

    live_jobs = [
        job for job in jobs if _job_identity(job) in payload_job_ids
    ]
    if not live_jobs:
        return IndexWorkerReport(
            claimed=len(jobs),
            failed=tuple(index_job_report_identity(job) for job in failed_without_payload),
            stale=tuple(stale_ids),
            errors=error_map,
        )

    # The first load is intentionally not an outbound boundary: ensure the
    # current stored identity and every chunk are still identical immediately
    # before exposing text to the embedding provider.
    payloads, _, live_jobs, new_stale, new_errors = await _db_call(
        _revalidate_payloads,
        db,
        live_jobs,
        payloads,
        now=timestamp,
    )
    stale_ids.extend(new_stale)
    error_map.update(new_errors)
    if not live_jobs:
        return IndexWorkerReport(
            claimed=len(jobs),
            failed=tuple(index_job_report_identity(job) for job in failed_without_payload),
            stale=tuple(dict.fromkeys(stale_ids)),
            errors=error_map,
        )

    texts = [payload.content for payload in payloads]
    embedder = embed_texts_func or embed_texts
    try:
        embedding_value = await _call_external(embedder, texts)
        embedding_result = _normalize_embedding_result(embedding_value, len(texts))
    except Exception as exc:
        error_type = type(exc).__name__
        failed_ids = [index_job_report_identity(job) for job in live_jobs]
        await _db_call(
            _mark_jobs,
            db,
            live_jobs,
            "failed",
            {job_id: error_type for job_id in failed_ids},
            now=timestamp,
        )
        error_map.update({job_id: error_type for job_id in failed_ids})
        return IndexWorkerReport(
            claimed=len(jobs),
            failed=tuple(
                [
                    *failed_ids,
                    *(index_job_report_identity(job) for job in failed_without_payload),
                ]
            ),
            stale=tuple(stale_ids),
            errors=error_map,
        )

    model_name = embedding_result.model_name or "unknown"
    dimension = embedding_result.dimension
    embedded_chunk_count = len(texts)

    active_state = await _db_call(read_active_chunk_index_state, db)
    if active_state is not None and (
        active_state.version != CHUNK_INDEX_VERSION
        or active_state.model_name != model_name
        or active_state.dimension != dimension
    ):
        error_type = "ActiveCollectionIdentityMismatch"
        failed_ids = [index_job_report_identity(job) for job in live_jobs]
        await _db_call(
            _mark_jobs,
            db,
            live_jobs,
            "failed",
            {job_id: error_type for job_id in failed_ids},
            now=timestamp,
        )
        error_map.update({job_id: error_type for job_id in failed_ids})
        return IndexWorkerReport(
            claimed=len(jobs),
            embedded_chunks=embedded_chunk_count,
            failed=tuple(
                [
                    *failed_ids,
                    *(index_job_report_identity(job) for job in failed_without_payload),
                ]
            ),
            stale=tuple(dict.fromkeys(stale_ids)),
            model_name=model_name,
            dimension=dimension,
            errors=error_map,
        )

    payloads, vector_indices, live_jobs, new_stale, new_errors = await _db_call(
        _revalidate_payloads,
        db,
        live_jobs,
        payloads,
        now=timestamp,
    )
    embeddings = [embedding_result.embeddings[index] for index in vector_indices]
    stale_ids.extend(new_stale)
    error_map.update(new_errors)
    if not live_jobs:
        return IndexWorkerReport(
            claimed=len(jobs),
            embedded_chunks=embedded_chunk_count,
            failed=tuple(index_job_report_identity(job) for job in failed_without_payload),
            stale=tuple(dict.fromkeys(stale_ids)),
            model_name=model_name,
            dimension=dimension,
            errors=error_map,
        )

    try:
        if collection is None:
            if collection_resolver is not None:
                collection = await _resolve_collection(
                    collection_resolver,
                    model_name,
                    dimension,
                    chunk_collection_metadata(model_name, dimension),
                )
            elif db_path:
                collection = await get_chunk_collection(db_path, model_name, dimension)
        if collection is None:
            raise RuntimeError("CollectionUnavailable")
    except Exception as exc:
        error_type = (
            "CollectionUnavailable"
            if str(exc) == "CollectionUnavailable"
            else type(exc).__name__
        )
        failed_ids = [index_job_report_identity(job) for job in live_jobs]
        await _db_call(
            _mark_jobs,
            db,
            live_jobs,
            "failed",
            {job_id: error_type for job_id in failed_ids},
            now=timestamp,
        )
        error_map.update({job_id: error_type for job_id in failed_ids})
        return IndexWorkerReport(
            claimed=len(jobs),
            embedded_chunks=embedded_chunk_count,
            failed=tuple(
                [
                    *failed_ids,
                    *(index_job_report_identity(job) for job in failed_without_payload),
                ]
            ),
            stale=tuple(dict.fromkeys(stale_ids)),
            model_name=model_name,
            dimension=dimension,
            errors=error_map,
        )

    # Collection resolution can itself await I/O, so validate once more directly
    # before constructing the external upsert payload.
    payloads, vector_indices, live_jobs, new_stale, new_errors = await _db_call(
        _revalidate_payloads,
        db,
        live_jobs,
        payloads,
        now=timestamp,
    )
    embeddings = [embeddings[index] for index in vector_indices]
    stale_ids.extend(new_stale)
    error_map.update(new_errors)
    if not live_jobs:
        return IndexWorkerReport(
            claimed=len(jobs),
            embedded_chunks=embedded_chunk_count,
            failed=tuple(index_job_report_identity(job) for job in failed_without_payload),
            stale=tuple(dict.fromkeys(stale_ids)),
            model_name=model_name,
            dimension=dimension,
            errors=error_map,
        )

    active_collection_name = str(getattr(collection, "name", "") or "").strip()
    if not active_collection_name:
        active_collection_name = chunk_collection_name(model_name, dimension)
    if active_state is not None and active_state.collection_name != active_collection_name:
        error_type = "ActiveCollectionIdentityMismatch"
        failed_ids = [index_job_report_identity(job) for job in live_jobs]
        await _db_call(
            _mark_jobs,
            db,
            live_jobs,
            "failed",
            {job_id: error_type for job_id in failed_ids},
            now=timestamp,
        )
        error_map.update({job_id: error_type for job_id in failed_ids})
        return IndexWorkerReport(
            claimed=len(jobs),
            embedded_chunks=embedded_chunk_count,
            failed=tuple(
                [
                    *failed_ids,
                    *(index_job_report_identity(job) for job in failed_without_payload),
                ]
            ),
            stale=tuple(dict.fromkeys(stale_ids)),
            model_name=model_name,
            dimension=dimension,
            errors=error_map,
        )

    metadata = [
        {
            "collection_kind": "life_memory_chunk",
            "chunk_index_version": CHUNK_INDEX_VERSION,
            "node_id": payload.node_id,
            "file_path": payload.file_path,
            "title": payload.title,
            "chunk_hash": payload.chunk_hash,
            "document_hash": payload.document_hash,
            "index_revision": payload.index_revision,
            "embedding_model": model_name,
            "embedding_dimension": dimension,
            "chunk_index": payload.chunk_index,
        }
        for payload in payloads
    ]
    ids = [payload.chunk_id for payload in payloads]
    documents = [payload.content for payload in payloads]
    try:
        kwargs = {
            "ids": ids,
            "embeddings": embeddings,
            "documents": documents,
            "metadatas": metadata,
        }
        if collection_upsert_func is not None:
            await _call_external(collection_upsert_func, **kwargs)
        else:
            await asyncio.to_thread(collection.upsert, **kwargs)
    except Exception as exc:
        error_type = type(exc).__name__
        failed_ids = [index_job_report_identity(job) for job in live_jobs]
        await _db_call(
            _mark_jobs,
            db,
            live_jobs,
            "failed",
            {job_id: error_type for job_id in failed_ids},
            now=timestamp,
        )
        error_map.update({job_id: error_type for job_id in failed_ids})
        return IndexWorkerReport(
            claimed=len(jobs),
            embedded_chunks=embedded_chunk_count,
            failed=tuple(
                [
                    *failed_ids,
                    *(index_job_report_identity(job) for job in failed_without_payload),
                ]
            ),
            stale=tuple(dict.fromkeys(stale_ids)),
            model_name=model_name,
            dimension=dimension,
            errors=error_map,
        )

    completion_timestamp = float(time.time() if now is None else now)
    completed, post_stale, completion_errors = await _db_call(
        _complete_jobs,
        db,
        live_jobs,
        vector_chunk_ids={
            identity: tuple(
                payload.chunk_id
                for payload in payloads
                if _payload_identity(payload) == identity
            )
            for identity in {_job_identity(job) for job in live_jobs}
        },
        collection_name=active_collection_name,
        model_name=model_name,
        dimension=dimension,
        now=completion_timestamp,
    )
    stale_ids.extend(post_stale)
    error_map.update(completion_errors)

    if collection is not None:
        try:
            await consume_vector_tombstones(db, collection, now=timestamp)
        except Exception:  # noqa: BLE001
            pass

    return IndexWorkerReport(
        claimed=len(jobs),
        embedded_chunks=embedded_chunk_count,
        upserted_chunks=len(ids),
        completed=tuple(completed),
        failed=tuple(index_job_report_identity(job) for job in failed_without_payload),
        stale=tuple(dict.fromkeys(stale_ids)),
        model_name=model_name,
        dimension=dimension,
        errors=error_map,
    )


__all__ = [
    "CHUNK_COLLECTION_PREFIX",
    "CHUNK_INDEX_VERSION",
    "DEFAULT_RECLAIM_AFTER",
    "IndexWorkerReport",
    "chunk_collection_metadata",
    "chunk_collection_name",
    "consume_vector_tombstones",
    "get_chunk_collection",
    "get_named_chunk_collection",
    "process_index_jobs",
]
