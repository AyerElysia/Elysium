"""Chunk-vector worker regressions for Life Engine memory."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path
from typing import Any, Sequence

import pytest

from plugins.life_engine.memory.indexing import (
    claim_index_jobs,
    create_memory_schema,
    read_active_chunk_index_state,
    upsert_document_rows,
)
from plugins.life_engine.memory.search import EmbeddingResult
from plugins.life_engine.memory.worker import (
    chunk_collection_metadata,
    chunk_collection_name,
    get_named_chunk_collection,
    process_index_jobs,
)


class _RecordingUpsert:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.thread_id: int | None = None

    def upsert(self, **kwargs: Any) -> None:
        self.thread_id = threading.get_ident()
        self.calls.append(kwargs)


def _db(tmp_path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(tmp_path / "worker.db"), check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    create_memory_schema(db, now=1.0)
    return db


async def _embed_ok(texts: Sequence[str]) -> EmbeddingResult:
    return EmbeddingResult(
        embeddings=[[float(index), float(len(text))] for index, text in enumerate(texts)],
        model_name="fake/model-v1",
    )


def test_chunk_collection_identity_is_versioned_by_model_and_dimension() -> None:
    assert chunk_collection_name("vendor/model:1", 768) == (
        "life_memory_chunks_v1_vendor_model_1_768"
    )
    assert chunk_collection_metadata("vendor/model:1", 768) == {
        "collection_kind": "life_memory_chunk",
        "chunk_index_version": 1,
        "embedding_model": "vendor/model:1",
        "embedding_dimension": 768,
    }


@pytest.mark.asyncio
async def test_named_collection_lookup_is_exact_and_does_not_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = object()
    calls: list[str] = []

    class Client:
        def get_collection(self, *, name: str) -> object:
            calls.append(name)
            return collection

    vector_service = type(
        "VectorService",
        (),
        {"_initialized": True, "_client": Client(), "_collections": {}},
    )()
    monkeypatch.setattr(
        "src.kernel.vector_db.get_vector_db_service",
        lambda _path: vector_service,
    )

    restored = await get_named_chunk_collection("/tmp/chroma", "exact-name")

    assert restored is collection
    assert calls == ["exact-name"]
    assert vector_service._collections == {"exact-name": collection}


@pytest.mark.asyncio
async def test_worker_batches_embeddings_and_one_off_loop_upsert(tmp_path: Path) -> None:
    db = _db(tmp_path)
    second = upsert_document_rows(
        db,
        "notes/second.md",
        "second document has several chunks",
        "Second",
        now=3.0,
        max_chars=12,
        overlap_chars=2,
    )
    first = upsert_document_rows(
        db,
        "notes/first.md",
        "first document also has chunks",
        "First",
        now=2.0,
        max_chars=12,
        overlap_chars=2,
    )
    expected_rows = db.execute(
        "SELECT j.job_id, c.chunk_id, c.node_id, c.chunk_index, c.content_hash, "
        "c.content, n.file_path, n.title, n.content_hash AS document_hash, "
        "n.index_revision FROM memory_index_jobs j "
        "JOIN memory_chunks c ON c.node_id = j.node_id "
        "JOIN memory_nodes n ON n.node_id = j.node_id "
        "ORDER BY j.created_at, j.job_id, c.chunk_index, c.chunk_id"
    ).fetchall()
    expected_texts = [str(row["content"]) for row in expected_rows]
    embed_calls: list[list[str]] = []

    async def embed(texts: Sequence[str]) -> EmbeddingResult:
        embed_calls.append(list(texts))
        return await _embed_ok(texts)

    recorder = _RecordingUpsert()
    main_thread = threading.get_ident()
    report = await process_index_jobs(
        db,
        object(),
        limit=10,
        embed_texts_func=embed,
        collection_upsert_func=recorder.upsert,
        now=10.0,
    )

    assert embed_calls == [expected_texts]
    assert len(recorder.calls) == 1
    assert recorder.thread_id is not None
    assert recorder.thread_id != main_thread
    call = recorder.calls[0]
    assert call["ids"] == [str(row["chunk_id"]) for row in expected_rows]
    assert call["documents"] == expected_texts
    assert len(call["embeddings"]) == len(expected_rows)
    assert report.claimed == 2
    assert report.embedded_chunks == len(expected_rows)
    assert report.upserted_chunks == len(expected_rows)
    assert set(report.completed) == {first.job_id, second.job_id}
    assert report.failed == ()
    assert report.stale == ()
    assert report.model_name == "fake/model-v1"
    assert report.dimension == 2

    for row, metadata in zip(expected_rows, call["metadatas"]):
        assert metadata == {
            "collection_kind": "life_memory_chunk",
            "chunk_index_version": 1,
            "node_id": row["node_id"],
            "file_path": row["file_path"],
            "title": row["title"],
            "chunk_hash": row["content_hash"],
            "document_hash": row["document_hash"],
            "index_revision": row["index_revision"],
            "embedding_model": "fake/model-v1",
            "embedding_dimension": 2,
            "chunk_index": row["chunk_index"],
        }

    nodes = db.execute(
        "SELECT embedding_synced, embedding_content_hash, content_hash, "
        "embedding_model, embedding_updated_at FROM memory_nodes ORDER BY node_id"
    ).fetchall()
    assert all(int(row["embedding_synced"]) == 1 for row in nodes)
    assert all(row["embedding_content_hash"] == row["content_hash"] for row in nodes)
    assert all(row["embedding_model"] == "fake/model-v1" for row in nodes)
    assert all(row["embedding_updated_at"] == 10.0 for row in nodes)
    jobs = db.execute(
        "SELECT status, attempts, error FROM memory_index_jobs ORDER BY job_id"
    ).fetchall()
    assert [(row["status"], row["attempts"], row["error"]) for row in jobs] == [
        ("completed", 1, ""),
        ("completed", 1, ""),
    ]
    active_state = read_active_chunk_index_state(db)
    assert active_state is not None
    assert active_state.collection_name == "life_memory_chunks_v1_fake_model-v1_2"
    assert active_state.model_name == "fake/model-v1"
    assert active_state.dimension == 2
    assert active_state.version == 1


@pytest.mark.asyncio
async def test_worker_retries_failed_embedding_job(tmp_path: Path) -> None:
    db = _db(tmp_path)
    indexed = upsert_document_rows(db, "notes/retry.md", "retry body", "Retry", now=2.0)
    recorder = _RecordingUpsert()

    async def fail_embed(_: Sequence[str]) -> EmbeddingResult:
        raise RuntimeError("provider unavailable")

    failed = await process_index_jobs(
        db,
        object(),
        embed_texts_func=fail_embed,
        collection_upsert_func=recorder.upsert,
        now=10.0,
    )
    row = db.execute(
        "SELECT status, attempts, error FROM memory_index_jobs WHERE job_id = ?",
        (indexed.job_id,),
    ).fetchone()
    assert failed.failed == (indexed.job_id,)
    assert failed.errors[indexed.job_id] == "RuntimeError"
    assert (row["status"], row["attempts"], row["error"]) == (
        "failed",
        1,
        "RuntimeError",
    )
    assert recorder.calls == []

    completed = await process_index_jobs(
        db,
        object(),
        embed_texts_func=_embed_ok,
        collection_upsert_func=recorder.upsert,
        retry_failed=True,
        now=20.0,
    )
    row = db.execute(
        "SELECT status, attempts, error FROM memory_index_jobs WHERE job_id = ?",
        (indexed.job_id,),
    ).fetchone()
    assert completed.completed == (indexed.job_id,)
    assert (row["status"], row["attempts"], row["error"]) == (
        "completed",
        2,
        "",
    )
    assert len(recorder.calls) == 1


@pytest.mark.asyncio
async def test_worker_rejects_embedding_identity_drift_after_activation(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    first = upsert_document_rows(db, "notes/first.md", "first body", now=2.0)
    recorder = _RecordingUpsert()
    activated = await process_index_jobs(
        db,
        object(),
        embed_texts_func=_embed_ok,
        collection_upsert_func=recorder.upsert,
        now=10.0,
    )
    assert activated.completed == (first.job_id,)
    active_before = read_active_chunk_index_state(db)

    second = upsert_document_rows(db, "notes/second.md", "second body", now=11.0)

    async def different_model(texts: Sequence[str]) -> EmbeddingResult:
        return EmbeddingResult(
            embeddings=[[1.0, 2.0] for _ in texts],
            model_name="other/model-v2",
        )

    rejected = await process_index_jobs(
        db,
        object(),
        embed_texts_func=different_model,
        collection_upsert_func=recorder.upsert,
        retry_failed=False,
        now=20.0,
    )

    assert rejected.failed == (second.job_id,)
    assert rejected.errors[second.job_id] == "ActiveCollectionIdentityMismatch"
    assert read_active_chunk_index_state(db) == active_before
    assert len(recorder.calls) == 1


@pytest.mark.asyncio
async def test_worker_rejects_collection_name_drift_before_upsert(tmp_path: Path) -> None:
    db = _db(tmp_path)
    first = upsert_document_rows(db, "notes/first.md", "first body", now=2.0)
    recorder = _RecordingUpsert()
    activated = await process_index_jobs(
        db,
        object(),
        embed_texts_func=_embed_ok,
        collection_upsert_func=recorder.upsert,
        now=10.0,
    )
    assert activated.completed == (first.job_id,)
    active_before = read_active_chunk_index_state(db)

    second = upsert_document_rows(db, "notes/second.md", "second body", now=11.0)

    class WrongCollection:
        name = "life_memory_chunks_v1_wrong_2"

    rejected = await process_index_jobs(
        db,
        WrongCollection(),
        embed_texts_func=_embed_ok,
        collection_upsert_func=recorder.upsert,
        retry_failed=False,
        now=20.0,
    )

    assert rejected.failed == (second.job_id,)
    assert rejected.errors[second.job_id] == "ActiveCollectionIdentityMismatch"
    assert read_active_chunk_index_state(db) == active_before
    assert len(recorder.calls) == 1


@pytest.mark.asyncio
async def test_worker_reclaims_abandoned_processing_job(tmp_path: Path) -> None:
    db = _db(tmp_path)
    indexed = upsert_document_rows(db, "notes/reclaim.md", "reclaim body", now=1.0)
    claimed = claim_index_jobs(db, limit=1, now=2.0)
    assert [job.job_id for job in claimed] == [indexed.job_id]

    recorder = _RecordingUpsert()
    report = await process_index_jobs(
        db,
        object(),
        embed_texts_func=_embed_ok,
        collection_upsert_func=recorder.upsert,
        reclaim_after=10.0,
        now=100.0,
    )

    row = db.execute(
        "SELECT status, attempts FROM memory_index_jobs WHERE job_id = ?",
        (indexed.job_id,),
    ).fetchone()
    assert report.completed == (indexed.job_id,)
    assert (row["status"], row["attempts"]) == ("completed", 2)


@pytest.mark.asyncio
async def test_worker_marks_collection_resolver_errors_failed(tmp_path: Path) -> None:
    db = _db(tmp_path)
    indexed = upsert_document_rows(db, "notes/resolver.md", "resolver body", now=2.0)

    async def fail_resolver(*_: Any) -> Any:
        raise LookupError("cannot create collection")

    report = await process_index_jobs(
        db,
        embed_texts_func=_embed_ok,
        collection_resolver=fail_resolver,
        now=10.0,
    )

    row = db.execute(
        "SELECT status, error FROM memory_index_jobs WHERE job_id = ?",
        (indexed.job_id,),
    ).fetchone()
    assert report.failed == (indexed.job_id,)
    assert report.errors[indexed.job_id] == "LookupError"
    assert (row["status"], row["error"]) == ("failed", "LookupError")


@pytest.mark.asyncio
async def test_worker_drops_payload_updated_while_embedding(tmp_path: Path) -> None:
    db = _db(tmp_path)
    old = upsert_document_rows(
        db,
        "notes/revision.md",
        "old revision body",
        "Revision",
        now=2.0,
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_embed(texts: Sequence[str]) -> EmbeddingResult:
        started.set()
        await release.wait()
        return await _embed_ok(texts)

    recorder = _RecordingUpsert()
    task = asyncio.create_task(
        process_index_jobs(
            db,
            object(),
            embed_texts_func=delayed_embed,
            collection_upsert_func=recorder.upsert,
            now=10.0,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)
    new = await asyncio.to_thread(
        upsert_document_rows,
        db,
        "notes/revision.md",
        "new revision body",
        "Revision",
        None,
        now=11.0,
    )
    release.set()
    report = await task

    assert old.job_id in report.stale
    assert report.upserted_chunks == 0
    assert recorder.calls == []
    old_row = db.execute(
        "SELECT status FROM memory_index_jobs WHERE job_id = ?", (old.job_id,)
    ).fetchone()
    new_row = db.execute(
        "SELECT status, index_revision FROM memory_index_jobs WHERE job_id = ?",
        (new.job_id,),
    ).fetchone()
    node = db.execute(
        "SELECT embedding_synced, embedding_content_hash, embedding_model, "
        "embedding_updated_at, index_revision FROM memory_nodes WHERE node_id = ?",
        (new.node_id,),
    ).fetchone()
    assert old_row["status"] == "stale"
    assert new_row["status"] == "pending"
    assert new_row["index_revision"] == node["index_revision"]
    assert node["embedding_synced"] == 0
    assert node["embedding_content_hash"] is None
    assert node["embedding_model"] is None
    assert node["embedding_updated_at"] is None
