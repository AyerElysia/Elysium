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
    IndexJob,
    claim_index_jobs,
    read_active_chunk_index_state,
    transaction,
    write_active_chunk_index_state,
)
from .nodes import NodeType, compute_content_hash, generate_file_node_id
from .search import EmbeddingResult, embed_texts

CHUNK_INDEX_VERSION = 1
CHUNK_COLLECTION_PREFIX = "life_memory_chunks"
DEFAULT_RECLAIM_AFTER = 600.0


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
        return await asyncio.to_thread(func, *args, **kwargs)
    except sqlite3.ProgrammingError as exc:
        if "created in a thread" not in str(exc):
            raise
        return func(*args, **kwargs)


def _tombstone_table_exists(db: sqlite3.Connection) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_vector_tombstones'",
    ).fetchone()
    return row is not None


def _read_pending_tombstones(db: sqlite3.Connection, *, limit: int) -> list[str]:
    """Return up to *limit* chunk_ids awaiting Chroma deletion."""
    if not _tombstone_table_exists(db):
        return []
    rows = db.execute(
        "SELECT chunk_id FROM memory_vector_tombstones ORDER BY created_at LIMIT ?",
        (max(0, int(limit)),),
    ).fetchall()
    return [str(row[0]) for row in rows if row[0]]


def _remove_processed_tombstones(
    db: sqlite3.Connection,
    chunk_ids: Sequence[str],
) -> None:
    """Delete consumed tombstone rows inside an already-open transaction."""
    if not chunk_ids or not _tombstone_table_exists(db):
        return
    placeholders = ",".join("?" for _ in chunk_ids)
    db.execute(
        f"DELETE FROM memory_vector_tombstones WHERE chunk_id IN ({placeholders})",
        list(chunk_ids),
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
        expected = int(job.index_revision or revision)
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
        node = db.execute(
            "SELECT node_id, node_type, file_path, title, content_hash, index_revision, is_deleted "
            "FROM memory_nodes WHERE node_id = ?",
            (job.node_id,),
        ).fetchone()
        if not _is_active_embeddable_file_node(node):
            stale.append(job.job_id)
            errors[job.job_id] = "InvalidDocumentIdentity"
            continue
        if _job_identity_error(job) is not None:
            stale.append(job.job_id)
            errors[job.job_id] = "InvalidJobIdentity"
            continue
        assert node is not None
        job_revision, revision_error = _current_revision(node, job)
        if revision_error:
            stale.append(job.job_id)
            errors[job.job_id] = revision_error
            continue
        assert job_revision is not None
        if (
            str(node["content_hash"] or "") != job.content_hash
            or int(node["index_revision"] or 0) != job_revision
        ):
            stale.append(job.job_id)
            errors[job.job_id] = "StaleRevision"
            continue
        job_payloads, payload_error = _payloads_for_job(
            db,
            job,
            node,
            index_revision=job_revision,
        )
        if payload_error == "EmptyDocument":
            errors[job.job_id] = payload_error
            continue
        if payload_error:
            stale.append(job.job_id)
            errors[job.job_id] = payload_error
            continue
        payloads.extend(job_payloads)
    return payloads, stale, errors


def _mark_jobs(
    db: sqlite3.Connection,
    job_ids: Iterable[str],
    status: str,
    errors: Mapping[str, str] | None = None,
    *,
    now: float | None = None,
) -> None:
    """Update only jobs still owned by this worker pass."""
    ids = list(dict.fromkeys(str(job_id) for job_id in job_ids))
    if not ids:
        return
    timestamp = float(time.time() if now is None else now)
    with transaction(db):
        db.executemany(
            "UPDATE memory_index_jobs SET status = ?, updated_at = ?, error = ? "
            "WHERE job_id = ? AND status = 'processing'",
            [
                (status, timestamp, str((errors or {}).get(job_id, "")), job_id)
                for job_id in ids
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
    payloads_by_job: dict[str, list[_ChunkPayload]] = {}
    for payload in payloads:
        payloads_by_job.setdefault(payload.job_id, []).append(payload)

    valid_job_ids: set[str] = set()
    stale: list[str] = []
    errors: dict[str, str] = {}
    with transaction(db):
        for job in jobs:
            node = db.execute(
                "SELECT node_id, node_type, file_path, title, content_hash, index_revision, is_deleted "
                "FROM memory_nodes WHERE node_id = ?",
                (job.node_id,),
            ).fetchone()
            job_row = db.execute(
                "SELECT job_id, node_id, status, content_hash, index_revision "
                "FROM memory_index_jobs WHERE job_id = ?",
                (job.job_id,),
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
                elif job_row is None or str(job_row["status"] or "") != "processing":
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
                    elif current_payloads != payloads_by_job.get(job.job_id, []):
                        error_type = "StalePayload"

            if error_type:
                if job_row is not None and str(job_row["status"] or "") == "processing":
                    db.execute(
                        "UPDATE memory_index_jobs SET status = 'stale', updated_at = ?, error = ? "
                        "WHERE job_id = ? AND status = 'processing'",
                        (now, error_type, job.job_id),
                    )
                stale.append(job.job_id)
                errors[job.job_id] = error_type
                continue

            assert job_row is not None and job_revision is not None
            if int(job_row["index_revision"] or 0) == 0 and job_revision:
                db.execute(
                    "UPDATE memory_index_jobs SET index_revision = ? "
                    "WHERE job_id = ? AND status = 'processing' AND index_revision = 0",
                    (job_revision, job.job_id),
                )
            valid_job_ids.add(job.job_id)

    valid_payloads: list[_ChunkPayload] = []
    embedding_indices: list[int] = []
    for index, payload in enumerate(payloads):
        if payload.job_id in valid_job_ids:
            valid_payloads.append(payload)
            embedding_indices.append(index)
    valid_jobs = [job for job in jobs if job.job_id in valid_job_ids]
    return valid_payloads, embedding_indices, valid_jobs, stale, errors


def _complete_jobs(
    db: sqlite3.Connection,
    jobs: Sequence[IndexJob],
    *,
    collection_name: str,
    model_name: str,
    dimension: int,
    now: float,
) -> tuple[list[str], list[str], dict[str, str]]:
    """Atomically guard node/job completion against concurrent document writes."""
    completed: list[str] = []
    stale: list[str] = []
    errors: dict[str, str] = {}
    with transaction(db):
        for job in jobs:
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
                    "WHERE job_id = ? AND status = 'processing'",
                    (now, error_type, job.job_id),
                )
                stale.append(job.job_id)
                errors[job.job_id] = error_type
                continue

            assert expected_revision is not None
            job_cursor = db.execute(
                "UPDATE memory_index_jobs SET status = 'completed', updated_at = ?, error = '' "
                "WHERE job_id = ? AND node_id = ? AND status = 'processing' AND content_hash = ? "
                "AND index_revision = ? AND EXISTS ("
                "SELECT 1 FROM memory_nodes WHERE node_id = ? AND node_type = ? "
                "AND is_deleted = 0 AND file_path = ? AND content_hash = ? "
                "AND index_revision = ?)",
                (
                    now,
                    job.job_id,
                    job.node_id,
                    job.content_hash,
                    expected_revision,
                    job.node_id,
                    NodeType.FILE.value,
                    str(node_row["file_path"]),
                    job.content_hash,
                    expected_revision,
                ),
            )
            if job_cursor.rowcount != 1:
                stale.append(job.job_id)
                errors[job.job_id] = "JobStateChanged"
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
                completed.append(job.job_id)
            else:
                db.execute(
                    "UPDATE memory_index_jobs SET status = 'stale', error = 'StaleRevision' "
                    "WHERE job_id = ? AND status = 'completed'",
                    (job.job_id,),
                )
                stale.append(job.job_id)
                errors[job.job_id] = "StaleRevision"
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
    chunk_ids = await _db_call(_read_pending_tombstones, db, limit=limit)
    if not chunk_ids:
        return 0

    delete_func = getattr(collection, "delete", None)
    if not callable(delete_func):
        return 0

    try:
        await _call_external(delete_func, ids=chunk_ids)
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger("life_engine.memory.worker").warning(
            f"向量 tombstone 清理失败 ({len(chunk_ids)} ids): {type(exc).__name__}: {exc}"
        )
        return 0

    def _clear(db_: sqlite3.Connection) -> None:
        with transaction(db_):
            _remove_processed_tombstones(db_, chunk_ids)

    await _db_call(_clear, db)
    return len(chunk_ids)


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
        failed_ids = [job.job_id for job in jobs]
        errors = {job_id: error_type for job_id in failed_ids}
        await _db_call(
            _mark_jobs,
            db,
            failed_ids,
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
        await _db_call(
            _mark_jobs,
            db,
            stale_ids,
            "stale",
            error_map,
            now=timestamp,
        )

    payload_job_ids = {payload.job_id for payload in payloads}
    failed_without_payload = [
        job
        for job in jobs
        if job.job_id not in payload_job_ids and job.job_id not in stale_ids
    ]
    if failed_without_payload:
        await _db_call(
            _mark_jobs,
            db,
            (job.job_id for job in failed_without_payload),
            "failed",
            error_map,
            now=timestamp,
        )

    live_jobs = [job for job in jobs if job.job_id in payload_job_ids]
    if not live_jobs:
        return IndexWorkerReport(
            claimed=len(jobs),
            failed=tuple(job.job_id for job in failed_without_payload),
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
            failed=tuple(job.job_id for job in failed_without_payload),
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
        failed_ids = [job.job_id for job in live_jobs]
        await _db_call(
            _mark_jobs,
            db,
            failed_ids,
            "failed",
            {job_id: error_type for job_id in failed_ids},
            now=timestamp,
        )
        error_map.update({job_id: error_type for job_id in failed_ids})
        return IndexWorkerReport(
            claimed=len(jobs),
            failed=tuple(failed_ids + [job.job_id for job in failed_without_payload]),
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
        failed_ids = [job.job_id for job in live_jobs]
        await _db_call(
            _mark_jobs,
            db,
            failed_ids,
            "failed",
            {job_id: error_type for job_id in failed_ids},
            now=timestamp,
        )
        error_map.update({job_id: error_type for job_id in failed_ids})
        return IndexWorkerReport(
            claimed=len(jobs),
            embedded_chunks=embedded_chunk_count,
            failed=tuple(failed_ids + [job.job_id for job in failed_without_payload]),
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
            failed=tuple(job.job_id for job in failed_without_payload),
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
        failed_ids = [job.job_id for job in live_jobs]
        await _db_call(
            _mark_jobs,
            db,
            failed_ids,
            "failed",
            {job_id: error_type for job_id in failed_ids},
            now=timestamp,
        )
        error_map.update({job_id: error_type for job_id in failed_ids})
        return IndexWorkerReport(
            claimed=len(jobs),
            embedded_chunks=embedded_chunk_count,
            failed=tuple(failed_ids + [job.job_id for job in failed_without_payload]),
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
            failed=tuple(job.job_id for job in failed_without_payload),
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
        failed_ids = [job.job_id for job in live_jobs]
        await _db_call(
            _mark_jobs,
            db,
            failed_ids,
            "failed",
            {job_id: error_type for job_id in failed_ids},
            now=timestamp,
        )
        error_map.update({job_id: error_type for job_id in failed_ids})
        return IndexWorkerReport(
            claimed=len(jobs),
            embedded_chunks=embedded_chunk_count,
            failed=tuple(failed_ids + [job.job_id for job in failed_without_payload]),
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
        failed_ids = [job.job_id for job in live_jobs]
        await _db_call(
            _mark_jobs,
            db,
            failed_ids,
            "failed",
            {job_id: error_type for job_id in failed_ids},
            now=timestamp,
        )
        error_map.update({job_id: error_type for job_id in failed_ids})
        return IndexWorkerReport(
            claimed=len(jobs),
            embedded_chunks=embedded_chunk_count,
            failed=tuple(failed_ids + [job.job_id for job in failed_without_payload]),
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
        failed=tuple(job.job_id for job in failed_without_payload),
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
