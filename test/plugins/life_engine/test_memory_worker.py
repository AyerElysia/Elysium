"""Chunk-vector worker regressions for Life Engine memory."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path
from typing import Any, Sequence

import pytest

from plugins.life_engine.memory import worker as worker_module
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


@pytest.mark.asyncio
async def test_worker_exports_only_valid_document_bodies(tmp_path: Path) -> None:
    db = _db(tmp_path)
    valid_body = "ordinary valid body reaches the worker outputs"
    noncanonical_body = "noncanonical body must never leave sqlite"
    deleted_body = "deleted body must never leave sqlite"
    concept_body = "concept body must never leave sqlite"
    wrong_identity_body = "wrong identity body must never leave sqlite"
    valid = upsert_document_rows(db, "notes/valid.md", valid_body, now=1.0)
    noncanonical = upsert_document_rows(
        db,
        "notes/noncanonical.md",
        noncanonical_body,
        now=2.0,
    )
    deleted = upsert_document_rows(db, "notes/deleted.md", deleted_body, now=3.0)
    concept = upsert_document_rows(db, "notes/concept.md", concept_body, now=4.0)
    wrong_identity = upsert_document_rows(
        db,
        "notes/wrong-identity.md",
        wrong_identity_body,
        now=5.0,
    )
    db.execute(
        "UPDATE memory_nodes SET file_path = ? WHERE node_id = ?",
        ("./notes/noncanonical.md", noncanonical.node_id),
    )
    db.execute(
        "UPDATE memory_nodes SET is_deleted = 1 WHERE node_id = ?",
        (deleted.node_id,),
    )
    db.execute(
        "UPDATE memory_nodes SET node_type = 'concept' WHERE node_id = ?",
        (concept.node_id,),
    )
    # Keep every related row structurally consistent under a file ID that does
    # not belong to this canonical path, so the node predicate is the boundary
    # being exercised rather than a foreign-key or chunk-ID failure.
    assert wrong_identity.content_hash is not None
    wrong_node_id = "file:000000000000"
    wrong_job_id = f"{wrong_node_id}:{wrong_identity.content_hash}"
    chunk_rows = db.execute(
        "SELECT chunk_id, chunk_index, content_hash FROM memory_chunks WHERE node_id = ?",
        (wrong_identity.node_id,),
    ).fetchall()
    db.commit()
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute(
        "UPDATE memory_nodes SET node_id = ? WHERE node_id = ?",
        (wrong_node_id, wrong_identity.node_id),
    )
    for chunk in chunk_rows:
        chunk_index = int(chunk["chunk_index"])
        chunk_hash = str(chunk["content_hash"])
        db.execute(
            "UPDATE memory_chunks SET node_id = ?, chunk_id = ? WHERE chunk_id = ?",
            (
                wrong_node_id,
                f"{wrong_node_id}:{chunk_index}:{chunk_hash}",
                chunk["chunk_id"],
            ),
        )
    db.execute(
        "UPDATE memory_index_jobs SET job_id = ?, node_id = ? WHERE job_id = ?",
        (wrong_job_id, wrong_node_id, wrong_identity.job_id),
    )
    db.commit()
    db.execute("PRAGMA foreign_keys = ON")

    embed_calls: list[list[str]] = []

    async def embed(texts: Sequence[str]) -> EmbeddingResult:
        embed_calls.append(list(texts))
        return await _embed_ok(texts)

    recorder = _RecordingUpsert()
    report = await process_index_jobs(
        db,
        object(),
        limit=10,
        embed_texts_func=embed,
        collection_upsert_func=recorder.upsert,
        now=10.0,
    )

    embedded_texts = [text for call in embed_calls for text in call]
    upserted_documents = [
        document for call in recorder.calls for document in call["documents"]
    ]
    invalid_bodies = (
        noncanonical_body,
        deleted_body,
        concept_body,
        wrong_identity_body,
    )
    assert valid_body in embedded_texts
    assert valid_body in upserted_documents
    assert all(body not in embedded_texts for body in invalid_bodies)
    assert all(body not in upserted_documents for body in invalid_bodies)
    assert report.completed == (valid.job_id,)
    assert report.failed == ()
    invalid_job_ids = {
        noncanonical.job_id,
        deleted.job_id,
        concept.job_id,
        wrong_job_id,
    }
    assert set(report.stale) == invalid_job_ids

    for job_id in invalid_job_ids:
        row = db.execute(
            "SELECT status, error FROM memory_index_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        assert report.errors[job_id] == "InvalidDocumentIdentity"
        assert row is not None
        assert (row["status"], row["error"]) == (
            "stale",
            "InvalidDocumentIdentity",
        )


@pytest.mark.asyncio
async def test_worker_never_exports_malformed_chunk_identity(tmp_path: Path) -> None:
    db = _db(tmp_path)
    indexed = upsert_document_rows(
        db,
        "notes/chunk-boundary.md",
        "must never leave sqlite",
        now=2.0,
    )
    db.execute(
        "UPDATE memory_chunks SET chunk_id = 'malformed-chunk' WHERE node_id = ?",
        (indexed.node_id,),
    )
    db.commit()
    recorder = _RecordingUpsert()

    async def reject_embed(_: Sequence[str]) -> EmbeddingResult:
        pytest.fail("malformed chunk content reached the embedder")

    report = await process_index_jobs(
        db,
        object(),
        embed_texts_func=reject_embed,
        collection_upsert_func=recorder.upsert,
        now=10.0,
    )

    assert report.stale == (indexed.job_id,)
    assert report.errors[indexed.job_id] == "InvalidChunkIdentity"
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_worker_revalidates_invalid_identity_after_delayed_embedding(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    body = "legal body must not reach chroma after identity mutation"
    indexed = upsert_document_rows(
        db,
        "notes/delayed-identity.md",
        body,
        now=2.0,
    )
    started = asyncio.Event()
    release = asyncio.Event()
    embed_calls: list[list[str]] = []

    async def delayed_embed(texts: Sequence[str]) -> EmbeddingResult:
        embed_calls.append(list(texts))
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
    db.execute(
        "UPDATE memory_nodes SET node_type = 'concept' WHERE node_id = ?",
        (indexed.node_id,),
    )
    db.commit()
    release.set()
    report = await task

    row = db.execute(
        "SELECT status, error FROM memory_index_jobs WHERE job_id = ?",
        (indexed.job_id,),
    ).fetchone()
    assert embed_calls == [[body]]
    assert report.stale == (indexed.job_id,)
    assert report.errors[indexed.job_id] == "InvalidDocumentIdentity"
    assert report.upserted_chunks == 0
    assert recorder.calls == []
    assert row is not None
    assert (row["status"], row["error"]) == ("stale", "InvalidDocumentIdentity")


@pytest.mark.asyncio
async def test_worker_revalidates_deleted_identity_before_embedding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db(tmp_path)
    indexed = upsert_document_rows(
        db,
        "notes/pre-embed-identity.md",
        "body must not reach the embedder after preflight mutation",
        now=2.0,
    )
    original_load = worker_module._load_job_payloads

    def load_then_delete(
        connection: sqlite3.Connection,
        jobs: Sequence[Any],
    ) -> tuple[list[Any], list[str], dict[str, str]]:
        result = original_load(connection, jobs)
        connection.execute(
            "UPDATE memory_nodes SET is_deleted = 1 WHERE node_id = ?",
            (indexed.node_id,),
        )
        connection.commit()
        return result

    monkeypatch.setattr(worker_module, "_load_job_payloads", load_then_delete)
    recorder = _RecordingUpsert()

    async def reject_embed(_: Sequence[str]) -> EmbeddingResult:
        pytest.fail("deleted document content reached the embedder")

    report = await process_index_jobs(
        db,
        object(),
        embed_texts_func=reject_embed,
        collection_upsert_func=recorder.upsert,
        now=10.0,
    )

    row = db.execute(
        "SELECT status, error FROM memory_index_jobs WHERE job_id = ?",
        (indexed.job_id,),
    ).fetchone()
    assert report.stale == (indexed.job_id,)
    assert report.errors[indexed.job_id] == "InvalidDocumentIdentity"
    assert report.embedded_chunks == 0
    assert report.upserted_chunks == 0
    assert recorder.calls == []
    assert row is not None
    assert (row["status"], row["error"]) == ("stale", "InvalidDocumentIdentity")


@pytest.mark.asyncio
async def test_tombstones_are_created_on_document_update(tmp_path: Path) -> None:
    """Updating a document writes old chunk IDs to memory_vector_tombstones."""
    db = _db(tmp_path)
    upsert_document_rows(db, "notes/doc.md", "first version content here", "Doc")
    rows = db.execute("SELECT chunk_id FROM memory_vector_tombstones").fetchall()
    assert rows == []
    upsert_document_rows(db, "notes/doc.md", "completely different content now", "Doc")
    rows = db.execute("SELECT chunk_id FROM memory_vector_tombstones").fetchall()
    assert len(rows) > 0


@pytest.mark.asyncio
async def test_tombstones_are_created_on_document_delete(tmp_path: Path) -> None:
    """Deleting a document writes chunk IDs to memory_vector_tombstones."""
    from plugins.life_engine.memory.indexing import delete_document_rows
    db = _db(tmp_path)
    upsert_document_rows(db, "notes/del.md", "content to delete later", "Del")
    delete_document_rows(db, "notes/del.md")
    rows = db.execute("SELECT chunk_id FROM memory_vector_tombstones").fetchall()
    assert len(rows) > 0


@pytest.mark.asyncio
async def test_consume_tombstones_calls_collection_delete(tmp_path: Path) -> None:
    """consume_vector_tombstones forwards tombstoned IDs to collection.delete."""
    from plugins.life_engine.memory.indexing import delete_document_rows
    from plugins.life_engine.memory.worker import consume_vector_tombstones

    db = _db(tmp_path)
    upsert_document_rows(db, "notes/tomb.md", "old content to be superseded here", "Tomb")
    old_ids = [
        row["chunk_id"]
        for row in db.execute("SELECT chunk_id FROM memory_chunks WHERE 1=1").fetchall()
    ]
    assert old_ids
    delete_document_rows(db, "notes/tomb.md")

    deleted_ids: list[str] = []

    class FakeCollection:
        def delete(self, *, ids: list[str]) -> None:
            deleted_ids.extend(ids)

    cleared = await consume_vector_tombstones(db, FakeCollection())
    assert cleared == len(old_ids)
    assert set(deleted_ids) == set(old_ids)
    rows = db.execute("SELECT chunk_id FROM memory_vector_tombstones").fetchall()
    assert rows == []


@pytest.mark.asyncio
async def test_consume_tombstones_handles_collection_delete_error(tmp_path: Path) -> None:
    """consume_vector_tombstones swallows collection.delete errors gracefully."""
    from plugins.life_engine.memory.indexing import delete_document_rows
    from plugins.life_engine.memory.worker import consume_vector_tombstones

    db = _db(tmp_path)
    upsert_document_rows(db, "notes/errortomb.md", "content that will be deleted", "ErrTomb")
    delete_document_rows(db, "notes/errortomb.md")

    class FailingCollection:
        def delete(self, *, ids: list[str]) -> None:
            raise RuntimeError("Chroma is down")

    cleared = await consume_vector_tombstones(db, FailingCollection())
    assert cleared == 0
    rows = db.execute("SELECT chunk_id FROM memory_vector_tombstones").fetchall()
    assert len(rows) > 0
