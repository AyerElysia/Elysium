"""Regression tests for rebuildable MySQL Memory projection recovery."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.memory.indexing import IndexJob, chunk_document
from plugins.life_engine.memory.nodes import (
    canonical_file_node_id,
    compute_content_hash,
)
from plugins.life_engine.storage.memory.mysql import MySQLDocumentIndexProjection


class _Mappings:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def one_or_none(self) -> dict[str, Any] | None:
        if not self._rows:
            return None
        assert len(self._rows) == 1
        return self._rows[0]

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._rows)


class _Result:
    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        *,
        rowcount: int = 1,
    ) -> None:
        self._rows = rows or []
        self.rowcount = rowcount

    def mappings(self) -> _Mappings:
        return _Mappings(self._rows)


class _UpsertSession:
    def __init__(
        self,
        existing: dict[str, Any],
        *,
        existing_job: dict[str, Any] | None = None,
        chunks_missing: bool = False,
    ) -> None:
        self.existing = existing
        self.existing_job = existing_job
        self.chunks_missing = chunks_missing
        self.statements: list[str] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(
        self,
        statement: object,
        _parameters: object | None = None,
    ) -> _Result:
        sql = " ".join(str(statement).split())
        self.statements.append(sql)
        self.calls.append((sql, dict(_parameters or {})))
        if sql.startswith("SELECT * FROM memory_nodes WHERE node_id"):
            return _Result([self.existing])
        if sql.startswith("SELECT node_id, file_path FROM memory_nodes"):
            return _Result(
                [
                    {
                        "node_id": self.existing["node_id"],
                        "file_path": self.existing["file_path"],
                    }
                ]
            )
        if sql.startswith("SELECT * FROM memory_chunks"):
            if self.chunks_missing:
                return _Result([])
            chunks = chunk_document(
                str(self.existing["node_id"]),
                str(self.existing["document_content"]),
                str(self.existing["title"]),
                max_chars=1000,
                overlap_chars=100,
            )
            return _Result(
                [
                    {
                        "chunk_id": chunk.chunk_id,
                        "node_id": chunk.node_id,
                        "chunk_index": chunk.chunk_index,
                        "content_hash": chunk.content_hash,
                        "content": chunk.content,
                        "title": chunk.title,
                    }
                    for chunk in chunks
                ]
            )
        if sql.startswith("SELECT * FROM memory_index_jobs WHERE node_id"):
            return _Result(
                [self.existing_job] if self.existing_job is not None else []
            )
        return _Result()

    async def scalar(
        self,
        _statement: object,
        _parameters: object | None = None,
    ) -> str | None:
        if self.existing_job is None:
            return None
        return str(self.existing_job["job_id"])


def _existing_document(*, deleted: bool, fts_hash: str) -> dict[str, Any]:
    path = "notes/recover-me.md"
    body = "projection source remains authoritative"
    _, node_id = canonical_file_node_id(path)
    digest = compute_content_hash(body)
    return {
        "node_id": node_id,
        "node_type": "file",
        "file_path": path,
        "content_hash": digest,
        "document_content": body,
        "title": "recover-me",
        "created_at": 1.0,
        "source_mtime": 10.0,
        "index_revision": 7,
        "is_deleted": deleted,
        "fts_content_hash": fts_hash,
        "embedding_synced": True,
        "embedding_content_hash": digest,
        "legacy_fts_present": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deleted", "fts_hash"),
    [(True, ""), (False, "stale-fts")],
)
async def test_mysql_upsert_revives_tombstones_and_repairs_hash_drift(
    deleted: bool,
    fts_hash: str,
) -> None:
    existing = _existing_document(deleted=deleted, fts_hash=fts_hash)
    session = _UpsertSession(existing)
    port = object.__new__(MySQLDocumentIndexProjection)

    result = await port._upsert_in_session(
        session,  # type: ignore[arg-type]
        existing["file_path"],
        existing["document_content"],
        existing["title"],
        existing["source_mtime"],
        max_chars=1000,
        overlap_chars=100,
    )

    update = next(
        sql for sql in session.statements if sql.startswith("UPDATE memory_nodes")
    )
    assert "is_deleted = FALSE" in update
    assert "fts_content_hash =" in update
    assert "embedding_content_hash = NULL" in update
    assert result.job_id == f"{existing['node_id']}:{existing['content_hash']}"


@pytest.mark.asyncio
async def test_mysql_upsert_mtime_only_updates_observation_metadata() -> None:
    existing = _existing_document(deleted=False, fts_hash="")
    existing["fts_content_hash"] = existing["content_hash"]
    session = _UpsertSession(existing)
    port = object.__new__(MySQLDocumentIndexProjection)

    result = await port._upsert_in_session(
        session,  # type: ignore[arg-type]
        existing["file_path"],
        existing["document_content"],
        existing["title"],
        25.0,
        max_chars=1000,
        overlap_chars=100,
    )

    writes = [sql for sql in session.statements if not sql.startswith("SELECT")]
    assert writes == [
        "UPDATE memory_nodes SET source_mtime = :source_mtime, "
        "updated_at = :now WHERE node_id = :node_id "
        "AND index_revision = :revision AND content_hash = :content_hash"
    ]
    observation_params = next(
        params
        for sql, params in session.calls
        if sql.startswith("UPDATE memory_nodes SET source_mtime")
    )
    assert observation_params["revision"] == 7
    assert observation_params["source_mtime"] == 25.0
    assert result.source_mtime == 25.0
    assert result.content_hash == existing["content_hash"]


@pytest.mark.asyncio
async def test_mysql_upsert_recreates_missing_current_embedding_job_only() -> None:
    existing = _existing_document(deleted=False, fts_hash="")
    existing["fts_content_hash"] = existing["content_hash"]
    existing["embedding_synced"] = False
    existing["embedding_content_hash"] = None
    session = _UpsertSession(existing)
    port = object.__new__(MySQLDocumentIndexProjection)

    result = await port._upsert_in_session(
        session,  # type: ignore[arg-type]
        existing["file_path"],
        existing["document_content"],
        existing["title"],
        existing["source_mtime"],
        max_chars=1000,
        overlap_chars=100,
    )

    writes = [sql for sql in session.statements if not sql.startswith("SELECT")]
    assert len(writes) == 1
    assert writes[0].startswith("INSERT INTO memory_index_jobs")
    assert not any("memory_chunks" in sql for sql in writes)
    assert not any(sql.startswith("UPDATE memory_nodes") for sql in writes)
    insert_params = next(
        params
        for sql, params in session.calls
        if sql.startswith("INSERT INTO memory_index_jobs")
    )
    assert insert_params["revision"] == 7
    assert result.job_id == f"{existing['node_id']}:{existing['content_hash']}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    ["pending", "processing", "completed", "failed"],
)
async def test_mysql_upsert_preserves_existing_current_embedding_job(
    status: str,
) -> None:
    existing = _existing_document(deleted=False, fts_hash="")
    existing["fts_content_hash"] = existing["content_hash"]
    existing["embedding_synced"] = False
    existing["embedding_content_hash"] = None
    existing_job = {
        "job_id": f"{existing['node_id']}:{existing['content_hash']}",
        "node_id": existing["node_id"],
        "content_hash": existing["content_hash"],
        "status": status,
        "created_at": 1.0,
        "updated_at": 2.0,
        "attempts": 3,
        "error": "preserve-me",
        "index_revision": 7,
        "claim_token": "lease" if status == "processing" else "",
    }
    session = _UpsertSession(existing, existing_job=existing_job)
    port = object.__new__(MySQLDocumentIndexProjection)

    result = await port._upsert_in_session(
        session,  # type: ignore[arg-type]
        existing["file_path"],
        existing["document_content"],
        existing["title"],
        existing["source_mtime"],
        max_chars=1000,
        overlap_chars=100,
    )

    assert all(sql.startswith("SELECT") for sql in session.statements)
    assert result.job_id == existing_job["job_id"]
    assert existing_job["status"] == status
    assert existing_job["attempts"] == 3
    assert existing_job["error"] == "preserve-me"


@pytest.mark.asyncio
async def test_mysql_completed_embedding_mismatch_requires_explicit_recovery() -> None:
    existing = _existing_document(deleted=False, fts_hash="")
    existing["fts_content_hash"] = existing["content_hash"]
    existing["embedding_synced"] = False
    existing_job = {
        "job_id": f"{existing['node_id']}:{existing['content_hash']}",
        "node_id": existing["node_id"],
        "content_hash": existing["content_hash"],
        "status": "completed",
        "created_at": 1.0,
        "updated_at": 2.0,
        "attempts": 1,
        "error": "",
        "index_revision": 7,
        "claim_token": "",
    }

    class _RecoverySession:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def execute(
            self,
            statement: object,
            parameters: dict[str, Any] | None = None,
        ) -> _Result:
            sql = " ".join(str(statement).split())
            params = dict(parameters or {})
            self.calls.append((sql, params))
            if sql.startswith(
                "SELECT node_id, content_hash, index_revision FROM memory_nodes"
            ):
                return _Result([existing])
            if sql.startswith("SELECT * FROM memory_index_jobs WHERE node_id"):
                return _Result([existing_job])
            return _Result(rowcount=1)

    session = _RecoverySession()
    port = object.__new__(MySQLDocumentIndexProjection)

    async def _write(operation):  # type: ignore[no-untyped-def]
        return await operation(session)

    port._write = _write  # type: ignore[method-assign]

    recovered = await port.invalidate_vector_projection()

    assert recovered == 1
    requeue_sql, requeue_params = next(
        (sql, params)
        for sql, params in session.calls
        if sql.startswith("UPDATE memory_index_jobs SET status = 'pending'")
    )
    assert "index_revision = :revision" in requeue_sql
    assert requeue_params["revision"] == 7
    assert requeue_params["expected_status"] == "completed"


class _IndexJobSession:
    def __init__(
        self,
        *,
        node: dict[str, Any] | None = None,
        existing_job: dict[str, Any] | None = None,
        existing_jobs: list[dict[str, Any]] | None = None,
        pending_jobs: list[dict[str, Any]] | None = None,
    ) -> None:
        self.node = node
        self.existing_jobs = (
            list(existing_jobs)
            if existing_jobs is not None
            else ([existing_job] if existing_job is not None else [])
        )
        self.pending_jobs = pending_jobs or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(
        self,
        statement: object,
        parameters: dict[str, Any],
    ) -> _Result:
        sql = " ".join(str(statement).split())
        self.calls.append((sql, dict(parameters)))
        if sql.startswith("SELECT node_id, content_hash, index_revision FROM memory_nodes"):
            return _Result([self.node] if self.node is not None else [])
        if sql.startswith("SELECT * FROM memory_index_jobs WHERE node_id"):
            return _Result(
                [
                    job
                    for job in self.existing_jobs
                    if job["node_id"] == parameters["node_id"]
                    and job["index_revision"] == parameters["revision"]
                ]
            )
        if sql.startswith("SELECT j.* FROM memory_index_jobs j"):
            return _Result(self.pending_jobs)
        if sql.startswith("SELECT index_revision"):
            return _Result(self.pending_jobs)
        return _Result(rowcount=1)


def _job_row(*, status: str, revision: int = 7) -> dict[str, Any]:
    return {
        "job_id": "legacy-job-id",
        "node_id": "node-1",
        "content_hash": "a" * 64,
        "status": status,
        "created_at": 1.0,
        "updated_at": 2.0,
        "attempts": 3,
        "error": "preserve-me",
        "index_revision": revision,
        "claim_token": "existing-lease" if status == "processing" else "",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "processing"])
async def test_mysql_duplicate_enqueue_preserves_existing_job_state(
    status: str,
) -> None:
    existing = _job_row(status=status)
    session = _IndexJobSession(
        node={
            "node_id": "node-1",
            "content_hash": "a" * 64,
            "index_revision": 7,
        },
        existing_job=existing,
    )
    port = object.__new__(MySQLDocumentIndexProjection)

    async def _write(operation):  # type: ignore[no-untyped-def]
        return await operation(session)

    port._write = _write  # type: ignore[method-assign]

    job_id = await port.enqueue_job("node-1", "a" * 64)

    assert job_id == "legacy-job-id"
    assert all(sql.startswith("SELECT") for sql, _params in session.calls)
    assert existing["status"] == status
    assert existing["attempts"] == 3
    assert existing["error"] == "preserve-me"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["stale", "failed", "completed"])
async def test_mysql_explicit_enqueue_requeues_current_terminal_job(
    status: str,
) -> None:
    existing = _job_row(status=status)
    session = _IndexJobSession(
        node={
            "node_id": "node-1",
            "content_hash": "a" * 64,
            "index_revision": 7,
        },
        existing_job=existing,
    )
    port = object.__new__(MySQLDocumentIndexProjection)

    async def _write(operation):  # type: ignore[no-untyped-def]
        return await operation(session)

    port._write = _write  # type: ignore[method-assign]

    job_id = await port.enqueue_job("node-1", "a" * 64)

    assert job_id == "legacy-job-id"
    requeue_sql, requeue_params = next(
        (sql, params)
        for sql, params in session.calls
        if sql.startswith("UPDATE memory_index_jobs SET status = 'pending'")
    )
    assert "index_revision = :revision" in requeue_sql
    assert "status = :expected_status" in requeue_sql
    assert "claim_token = :expected_claim_token" in requeue_sql
    assert "attempts" not in requeue_sql
    assert requeue_params["revision"] == 7
    assert requeue_params["expected_status"] == status
    assert requeue_params["expected_claim_token"] == ""


@pytest.mark.asyncio
async def test_mysql_explicit_enqueue_never_requeues_a_b_a_old_revision() -> None:
    digest = "a" * 64
    job_id = "node-1:" + digest
    old_job = {
        **_job_row(status="stale", revision=1),
        "job_id": job_id,
    }
    current_job = {
        **_job_row(status="completed", revision=3),
        "job_id": job_id,
    }
    session = _IndexJobSession(
        node={
            "node_id": "node-1",
            "content_hash": digest,
            "index_revision": 3,
        },
        existing_jobs=[old_job, current_job],
    )
    port = object.__new__(MySQLDocumentIndexProjection)

    async def _write(operation):  # type: ignore[no-untyped-def]
        return await operation(session)

    port._write = _write  # type: ignore[method-assign]

    assert await port.enqueue_job("node-1", digest) == job_id

    updates = [
        params
        for sql, params in session.calls
        if sql.startswith("UPDATE memory_index_jobs SET status = 'pending'")
    ]
    assert len(updates) == 1
    assert updates[0]["revision"] == 3
    assert updates[0]["expected_status"] == "completed"
    assert all(params.get("revision") != 1 for params in updates)


@pytest.mark.asyncio
async def test_mysql_new_revision_gets_an_independent_job_identity() -> None:
    session = _IndexJobSession(
        node={
            "node_id": "node-1",
            "content_hash": "a" * 64,
            "index_revision": 8,
        }
    )
    port = object.__new__(MySQLDocumentIndexProjection)

    async def _write(operation):  # type: ignore[no-untyped-def]
        return await operation(session)

    port._write = _write  # type: ignore[method-assign]

    job_id = await port.enqueue_job("node-1", "a" * 64)

    assert job_id == f"node-1:{'a' * 64}"
    insert_sql, insert_params = next(
        (sql, params)
        for sql, params in session.calls
        if sql.startswith("INSERT INTO memory_index_jobs")
    )
    assert "index_revision, claim_token" in insert_sql
    assert insert_params["revision"] == 8


@pytest.mark.asyncio
async def test_mysql_claim_binds_a_new_token_to_exact_revision() -> None:
    pending = _job_row(status="pending")
    session = _IndexJobSession(pending_jobs=[pending])
    port = object.__new__(MySQLDocumentIndexProjection)

    async def _write(operation):  # type: ignore[no-untyped-def]
        return await operation(session)

    port._write = _write  # type: ignore[method-assign]

    claimed = await port.claim_jobs(limit=1)

    assert len(claimed) == 1
    assert claimed[0].claim_token
    assert len(claimed[0].claim_token) == 32
    claim_sql, claim_params = next(
        (sql, params)
        for sql, params in session.calls
        if sql.startswith("UPDATE memory_index_jobs SET status = 'processing'")
    )
    assert "index_revision = :revision" in claim_sql
    assert "status = 'pending'" in claim_sql
    assert claim_params["revision"] == 7
    assert claim_params["claim_token"] == claimed[0].claim_token


@pytest.mark.asyncio
async def test_mysql_terminal_status_requires_exact_claim_token() -> None:
    session = _IndexJobSession()
    port = object.__new__(MySQLDocumentIndexProjection)

    async def _write(operation):  # type: ignore[no-untyped-def]
        return await operation(session)

    port._write = _write  # type: ignore[method-assign]

    assert not await port.set_job_status("job-1", "failed", error="boom")
    assert session.calls == []

    claimed = IndexJob(
        job_id="job-1",
        node_id="node-1",
        content_hash="a" * 64,
        status="processing",
        created_at=1.0,
        updated_at=2.0,
        index_revision=7,
        claim_token="lease-1",
    )
    assert await port.set_job_status(claimed, "failed", error="boom")
    status_sql, status_params = session.calls[0]
    assert "status = 'processing'" in status_sql
    assert "claim_token = :claim_token" in status_sql
    assert status_params["revision"] == 7
    assert status_params["claim_token"] == "lease-1"


@pytest.mark.asyncio
async def test_mysql_legacy_status_calls_fail_closed_without_lease_identity() -> None:
    single = _IndexJobSession(
        pending_jobs=[
            {
                "index_revision": 7,
                "status": "failed",
                "claim_token": "",
            }
        ]
    )
    port = object.__new__(MySQLDocumentIndexProjection)

    async def _single_write(operation):  # type: ignore[no-untyped-def]
        return await operation(single)

    port._write = _single_write  # type: ignore[method-assign]
    assert not await port.set_job_status("legacy-job", "pending")
    assert single.calls == []

    ambiguous = _IndexJobSession(
        pending_jobs=[
            {"index_revision": 7, "status": "failed", "claim_token": ""},
            {"index_revision": 9, "status": "failed", "claim_token": ""},
        ]
    )

    async def _ambiguous_write(operation):  # type: ignore[no-untyped-def]
        return await operation(ambiguous)

    port._write = _ambiguous_write  # type: ignore[method-assign]
    assert not await port.set_job_status("legacy-job", "pending")
    assert ambiguous.calls == []


@pytest.mark.asyncio
async def test_mysql_nonterminal_status_cannot_bypass_processing_lease() -> None:
    processing = _IndexJobSession(
        pending_jobs=[
            {
                "index_revision": 7,
                "status": "processing",
                "claim_token": "lease-current",
            }
        ]
    )
    port = object.__new__(MySQLDocumentIndexProjection)

    async def _write(operation):  # type: ignore[no-untyped-def]
        return await operation(processing)

    port._write = _write  # type: ignore[method-assign]

    assert not await port.set_job_status("legacy-job", "processing")
    assert processing.calls == []
    assert not await port.set_job_status("legacy-job", "pending")
    assert processing.calls == []

    claimed = IndexJob(
        job_id="legacy-job",
        node_id="node-1",
        content_hash="a" * 64,
        status="processing",
        created_at=1.0,
        updated_at=2.0,
        index_revision=7,
        claim_token="lease-current",
    )
    assert await port.set_job_status(claimed, "pending")
    release_sql, release_params = processing.calls[-1]
    assert "status = 'processing'" in release_sql
    assert "claim_token = :claim_token" in release_sql
    assert release_params["revision"] == 7
    assert release_params["claim_token"] == "lease-current"


class _Connection:
    def __init__(
        self,
        node: dict[str, Any],
        chunk: dict[str, Any] | list[dict[str, Any]],
    ) -> None:
        self.node = node
        self.chunks = chunk if isinstance(chunk, list) else [chunk]

    async def execute(
        self,
        statement: object,
        _parameters: object | None = None,
    ) -> _Result:
        sql = " ".join(str(statement).split())
        if sql.startswith("SELECT * FROM memory_nodes"):
            return _Result([self.node])
        if sql.startswith("SELECT * FROM memory_chunks"):
            return _Result(self.chunks)
        raise AssertionError(sql)


class _ConnectContext:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Engine:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def connect(self) -> _ConnectContext:
        return _ConnectContext(self.connection)


class _CompletionSession:
    def __init__(
        self,
        *,
        locked_job: dict[str, Any] | None = None,
        locked_node: dict[str, Any] | None = None,
        existing_revision_job: dict[str, Any] | None = None,
        index_state: dict[str, Any] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.locked_job = locked_job
        self.locked_node = locked_node
        self.existing_revision_job = existing_revision_job
        self.index_state = index_state

    async def execute(
        self,
        statement: object,
        parameters: dict[str, Any],
    ) -> _Result:
        sql = " ".join(str(statement).split())
        self.calls.append((sql, dict(parameters)))
        if sql.startswith("SELECT job_id, node_id, content_hash, status"):
            return _Result([self.locked_job] if self.locked_job is not None else [])
        if sql.startswith("SELECT node_id, node_type, content_hash"):
            return _Result([self.locked_node] if self.locked_node is not None else [])
        if sql.startswith("SELECT * FROM memory_index_jobs WHERE node_id"):
            return _Result(
                [self.existing_revision_job]
                if self.existing_revision_job is not None
                else []
            )
        if sql.startswith("SELECT collection_name, model_name, dimension, version"):
            return _Result([self.index_state] if self.index_state is not None else [])
        return _Result(rowcount=1)


class _RetrySession:
    def __init__(
        self,
        *,
        node: dict[str, Any],
        jobs: list[dict[str, Any]],
    ) -> None:
        self.node = node
        self.jobs = jobs
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(
        self,
        statement: object,
        parameters: dict[str, Any],
    ) -> _Result:
        sql = " ".join(str(statement).split())
        params = dict(parameters)
        self.calls.append((sql, params))
        if sql.startswith("SELECT j.job_id, j.node_id, j.index_revision"):
            candidates: list[dict[str, Any]] = []
            for job in self.jobs:
                failed = "j.status = 'failed'" in sql and job["status"] == "failed"
                abandoned = bool(
                    "j.status = 'processing'" in sql
                    and job["status"] == "processing"
                    and float(job["updated_at"])
                    <= float(params.get("reclaim_cutoff", -1.0))
                )
                if failed or abandoned:
                    candidates.append(job)
            return _Result(candidates)
        if sql.startswith("SELECT node_id, content_hash, index_revision, node_type"):
            return _Result([self.node])
        if sql.startswith("SELECT job_id, node_id, content_hash, status, updated_at"):
            matching = [
                job
                for job in self.jobs
                if job["job_id"] == params["job_id"]
                and job["index_revision"] == params["revision"]
            ]
            return _Result(matching)
        if sql.startswith("SELECT * FROM memory_index_jobs WHERE node_id"):
            matching = [
                job
                for job in self.jobs
                if job["node_id"] == params["node_id"]
                and job["index_revision"] == params["revision"]
            ]
            return _Result(matching)
        if sql.startswith("UPDATE memory_index_jobs SET status = 'stale'"):
            matching = [
                job
                for job in self.jobs
                if job["job_id"] == params["job_id"]
                and job["index_revision"] == params["revision"]
                and job["status"] == "processing"
                and job["claim_token"] == params["expected_claim_token"]
            ]
            for job in matching:
                job.update(
                    status="stale",
                    claim_token="",
                    error="HistoricalLeaseExpired",
                )
            return _Result(rowcount=len(matching))
        if sql.startswith("UPDATE memory_index_jobs SET status = 'pending'"):
            matching = [
                job
                for job in self.jobs
                if job["job_id"] == params["job_id"]
                and job["index_revision"] == params["revision"]
                and job["status"] == params["expected_status"]
                and job["claim_token"] == params["expected_claim_token"]
            ]
            for job in matching:
                job.update(status="pending", claim_token="")
            return _Result(rowcount=len(matching))
        return _Result(rowcount=1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "retry_failed", "reclaim_after", "claim_token"),
    [
        ("failed", True, None, ""),
        ("processing", False, 60.0, "lease-current"),
    ],
)
async def test_mysql_retry_requeues_current_and_converges_expired_history(
    status: str,
    retry_failed: bool,
    reclaim_after: float | None,
    claim_token: str,
) -> None:
    digest = compute_content_hash("A")
    job_id = f"node-aba:{digest}"
    node = {
        "node_id": "node-aba",
        "node_type": "file",
        "content_hash": digest,
        "index_revision": 3,
        "is_deleted": False,
    }
    jobs = [
        {
            "job_id": job_id,
            "node_id": "node-aba",
            "content_hash": digest,
            "status": status,
            "updated_at": 0.0,
            "index_revision": 1,
            "claim_token": "lease-old" if status == "processing" else "",
        },
        {
            "job_id": job_id,
            "node_id": "node-aba",
            "content_hash": digest,
            "status": status,
            "updated_at": 0.0,
            "index_revision": 3,
            "claim_token": claim_token,
        },
    ]
    session = _RetrySession(node=node, jobs=jobs)
    port = object.__new__(MySQLDocumentIndexProjection)
    port.claim_jobs = lambda **_kwargs: _async_value([])  # type: ignore[method-assign]

    async def _write(operation):  # type: ignore[no-untyped-def]
        return await operation(session)

    port._write = _write  # type: ignore[method-assign]

    report = await port.run_index_worker(
        limit=2,
        collection=None,
        embed_texts_func=None,
        collection_resolver=None,
        collection_upsert_func=None,
        retry_failed=retry_failed,
        reclaim_after=reclaim_after,
    )

    assert report.claimed == 0
    updates = [
        (sql, params)
        for sql, params in session.calls
        if sql.startswith("UPDATE memory_index_jobs SET status = 'pending'")
    ]
    assert len(updates) == 1
    update_sql, update_params = updates[0]
    assert "index_revision = :revision" in update_sql
    assert "claim_token = :expected_claim_token" in update_sql
    assert "attempts" not in update_sql
    assert "error =" not in update_sql
    assert update_params["revision"] == 3
    assert update_params["expected_status"] == status
    assert update_params["expected_claim_token"] == claim_token
    old_job = next(job for job in jobs if job["index_revision"] == 1)
    if status == "failed":
        assert old_job["status"] == "failed"
    else:
        assert old_job["status"] == "stale"
        assert old_job["error"] == "HistoricalLeaseExpired"
        assert any(
            sql.startswith("UPDATE memory_vector_tombstones SET consumed_at = NULL")
            for sql, _params in session.calls
        )
        assert any(
            sql.startswith("UPDATE memory_nodes SET embedding_synced = FALSE")
            for sql, _params in session.calls
        )


@pytest.mark.asyncio
async def test_mysql_worker_completes_under_locked_job_and_node_cas() -> None:
    body = "one exact chunk"
    digest = compute_content_hash(body)
    node = {
        "node_id": "node-1",
        "node_type": "file",
        "file_path": "notes/one.md",
        "title": "one",
        "content_hash": digest,
        "index_revision": 4,
        "is_deleted": False,
    }
    chunk = {
        "chunk_id": f"node-1:0:{digest}",
        "node_id": "node-1",
        "chunk_index": 0,
        "content_hash": digest,
        "content": body,
        "title": "one",
    }
    job = IndexJob(
        job_id=f"node-1:{digest}",
        node_id="node-1",
        content_hash=digest,
        status="processing",
        created_at=1.0,
        updated_at=1.0,
        attempts=1,
        error="",
        index_revision=4,
        claim_token="lease-1",
    )
    connection = _Connection(node, chunk)

    async def _validate_writer() -> None:
        return None

    runtime = SimpleNamespace(
        engine=_Engine(connection),
        validate_writer=_validate_writer,
    )
    port = object.__new__(MySQLDocumentIndexProjection)
    port.runtime = runtime
    port.claim_jobs = lambda **_kwargs: _async_value([job])  # type: ignore[method-assign]
    port.read_chunk_index_state = lambda: _async_value(None)  # type: ignore[method-assign]
    completion = _CompletionSession(
        locked_job={
            "job_id": job.job_id,
            "node_id": job.node_id,
            "content_hash": job.content_hash,
            "status": "processing",
            "index_revision": job.index_revision,
            "claim_token": job.claim_token,
        },
        locked_node=node,
    )

    async def _write(operation):  # type: ignore[no-untyped-def]
        return await operation(completion)

    port._write = _write  # type: ignore[method-assign]

    class _Collection:
        name = "memory-test"

        async def upsert(self, **_kwargs: Any) -> None:
            return None

    async def _embed(_texts: list[str]) -> tuple[list[list[float]], str]:
        return [[0.1, 0.2]], "test/model-v1"

    report = await port.run_index_worker(
        limit=1,
        collection=_Collection(),
        embed_texts_func=_embed,
        collection_resolver=None,
        collection_upsert_func=None,
        retry_failed=False,
        reclaim_after=None,
    )

    sqls = [sql for sql, _params in completion.calls]
    assert any(
        sql.startswith("SELECT job_id, node_id, content_hash, status")
        and sql.endswith("FOR UPDATE")
        for sql in sqls
    )
    assert any(
        sql.startswith("SELECT node_id, node_type, content_hash, index_revision")
        and sql.endswith("FOR UPDATE")
        for sql in sqls
    )
    node_lock_index = next(
        index
        for index, sql in enumerate(sqls)
        if sql.startswith("SELECT node_id, node_type, content_hash, index_revision")
    )
    job_lock_index = next(
        index
        for index, sql in enumerate(sqls)
        if sql.startswith("SELECT job_id, node_id, content_hash, status")
    )
    assert node_lock_index < job_lock_index
    assert not any(sql.startswith("UPDATE memory_index_jobs j") for sql in sqls)
    node_sql, node_params = next(
        (sql, params)
        for sql, params in completion.calls
        if sql.startswith("UPDATE memory_nodes SET embedding_synced = TRUE")
    )
    job_sql, job_params = next(
        (sql, params)
        for sql, params in completion.calls
        if sql.startswith("UPDATE memory_index_jobs SET status = 'completed'")
    )
    assert "index_revision = :revision" in node_sql
    assert node_params["model_name"] == "test/model-v1"
    assert "claim_token = :claim_token" in job_sql
    assert job_params["claim_token"] == "lease-1"
    assert report.completed == (f"{job.job_id}@index_revision=4",)
    assert report.stale == ()


@pytest.mark.asyncio
async def test_mysql_worker_disambiguates_a_b_a_jobs_in_one_claim_batch() -> None:
    body = "A"
    digest = compute_content_hash(body)
    job_id = f"node-aba:{digest}"
    node = {
        "node_id": "node-aba",
        "node_type": "file",
        "file_path": "notes/aba.md",
        "title": "aba",
        "content_hash": digest,
        "index_revision": 3,
        "is_deleted": False,
    }
    chunk = {
        "chunk_id": f"node-aba:0:{digest}",
        "node_id": "node-aba",
        "chunk_index": 0,
        "content_hash": digest,
        "content": body,
        "title": "aba",
    }
    old_job = IndexJob(
        job_id=job_id,
        node_id="node-aba",
        content_hash=digest,
        status="processing",
        created_at=1.0,
        updated_at=1.0,
        attempts=1,
        index_revision=1,
        claim_token="lease-r1",
    )
    current_job = IndexJob(
        job_id=job_id,
        node_id="node-aba",
        content_hash=digest,
        status="processing",
        created_at=3.0,
        updated_at=3.0,
        attempts=1,
        index_revision=3,
        claim_token="lease-r3",
    )

    async def _validate_writer() -> None:
        return None

    port = object.__new__(MySQLDocumentIndexProjection)
    port.runtime = SimpleNamespace(
        engine=_Engine(_Connection(node, chunk)),
        validate_writer=_validate_writer,
    )
    port.claim_jobs = lambda **_kwargs: _async_value(  # type: ignore[method-assign]
        [old_job, current_job]
    )
    port.read_chunk_index_state = lambda: _async_value(None)  # type: ignore[method-assign]
    completion = _CompletionSession(
        locked_job={
            "job_id": current_job.job_id,
            "node_id": current_job.node_id,
            "content_hash": current_job.content_hash,
            "status": "processing",
            "index_revision": current_job.index_revision,
            "claim_token": current_job.claim_token,
        },
        locked_node=node,
    )

    async def _write(operation):  # type: ignore[no-untyped-def]
        return await operation(completion)

    port._write = _write  # type: ignore[method-assign]

    class _Collection:
        name = "memory-test"

        async def upsert(self, **_kwargs: Any) -> None:
            return None

    async def _embed(_texts: list[str]) -> tuple[list[list[float]], str]:
        return [[0.1, 0.2]], "test/model-v1"

    report = await port.run_index_worker(
        limit=2,
        collection=_Collection(),
        embed_texts_func=_embed,
        collection_resolver=None,
        collection_upsert_func=None,
        retry_failed=False,
        reclaim_after=None,
    )

    old_report_id = f"{job_id}@index_revision=1"
    current_report_id = f"{job_id}@index_revision=3"
    assert report.stale == (old_report_id,)
    assert report.completed == (current_report_id,)
    assert report.errors == {old_report_id: "StaleRevision"}

    stale_updates = [
        params
        for sql, params in completion.calls
        if sql.startswith("UPDATE memory_index_jobs SET status = :status")
        and params["status"] == "stale"
    ]
    assert len(stale_updates) == 1
    assert stale_updates[0]["revision"] == 1
    assert stale_updates[0]["claim_token"] == "lease-r1"

    completed_updates = [
        params
        for sql, params in completion.calls
        if sql.startswith("UPDATE memory_index_jobs SET status = 'completed'")
    ]
    assert len(completed_updates) == 1
    assert completed_updates[0]["revision"] == 3
    assert completed_updates[0]["claim_token"] == "lease-r3"


@pytest.mark.asyncio
async def test_mysql_worker_rejects_a_whole_job_when_any_chunk_is_corrupt() -> None:
    first_body = "valid first chunk"
    second_body = "corrupt second chunk"
    first_hash = compute_content_hash(first_body)
    second_hash = compute_content_hash(second_body)
    document_hash = compute_content_hash(first_body + second_body)
    node = {
        "node_id": "node-atomic",
        "node_type": "file",
        "file_path": "notes/atomic.md",
        "title": "atomic",
        "content_hash": document_hash,
        "index_revision": 8,
        "is_deleted": False,
    }
    chunks = [
        {
            "chunk_id": f"node-atomic:0:{first_hash}",
            "node_id": "node-atomic",
            "chunk_index": 0,
            "content_hash": first_hash,
            "content": first_body,
            "title": "atomic",
        },
        {
            "chunk_id": f"node-atomic:2:{second_hash}",
            "node_id": "node-atomic",
            "chunk_index": 2,
            "content_hash": second_hash,
            "content": second_body,
            "title": "atomic",
        },
    ]
    job = IndexJob(
        job_id=f"node-atomic:{document_hash}",
        node_id="node-atomic",
        content_hash=document_hash,
        status="processing",
        created_at=1.0,
        updated_at=1.0,
        attempts=1,
        index_revision=8,
        claim_token="lease-atomic",
    )

    async def _validate_writer() -> None:
        return None

    port = object.__new__(MySQLDocumentIndexProjection)
    port.runtime = SimpleNamespace(
        engine=_Engine(_Connection(node, chunks)),
        validate_writer=_validate_writer,
    )
    port.claim_jobs = lambda **_kwargs: _async_value([job])  # type: ignore[method-assign]
    status_calls: list[tuple[IndexJob, str, str]] = []

    async def _set_status(
        claimed: IndexJob,
        status: str,
        *,
        error: str = "",
    ) -> bool:
        status_calls.append((claimed, status, error))
        return True

    port.set_job_status = _set_status  # type: ignore[method-assign]

    async def _reject_embed(_texts: list[str]) -> tuple[list[list[float]], str]:
        pytest.fail("a partial job payload reached the embedding provider")

    report = await port.run_index_worker(
        limit=1,
        collection=SimpleNamespace(name="memory-test"),
        embed_texts_func=_reject_embed,
        collection_resolver=None,
        collection_upsert_func=None,
        retry_failed=False,
        reclaim_after=None,
    )

    identity = f"{job.job_id}@index_revision=8"
    assert report.embedded_chunks == 0
    assert report.upserted_chunks == 0
    assert report.stale == (identity,)
    assert report.errors == {identity: "InvalidChunkIdentity"}
    assert status_calls == [(job, "stale", "InvalidChunkIdentity")]


@pytest.mark.asyncio
async def test_mysql_worker_n_plus_one_race_persists_vector_compensation() -> None:
    old_body = "old revision"
    old_digest = compute_content_hash(old_body)
    new_digest = compute_content_hash("new revision")
    initial_node = {
        "node_id": "node-race",
        "node_type": "file",
        "file_path": "notes/race.md",
        "title": "race",
        "content_hash": old_digest,
        "index_revision": 4,
        "is_deleted": False,
    }
    chunk = {
        "chunk_id": f"node-race:0:{old_digest}",
        "node_id": "node-race",
        "chunk_index": 0,
        "content_hash": old_digest,
        "content": old_body,
        "title": "race",
    }
    job = IndexJob(
        job_id=f"node-race:{old_digest}",
        node_id="node-race",
        content_hash=old_digest,
        status="processing",
        created_at=1.0,
        updated_at=1.0,
        attempts=1,
        index_revision=4,
        claim_token="lease-old",
    )
    current_node = {
        "node_id": "node-race",
        "node_type": "file",
        "content_hash": new_digest,
        "index_revision": 5,
        "is_deleted": False,
    }
    current_job = {
        "job_id": f"node-race:{new_digest}",
        "node_id": "node-race",
        "content_hash": new_digest,
        "status": "completed",
        "created_at": 2.0,
        "updated_at": 2.0,
        "attempts": 1,
        "error": "",
        "index_revision": 5,
        "claim_token": "",
    }
    connection = _Connection(initial_node, chunk)

    async def _validate_writer() -> None:
        return None

    port = object.__new__(MySQLDocumentIndexProjection)
    port.runtime = SimpleNamespace(
        engine=_Engine(connection),
        validate_writer=_validate_writer,
    )
    port.claim_jobs = lambda **_kwargs: _async_value([job])  # type: ignore[method-assign]
    port.read_chunk_index_state = lambda: _async_value(None)  # type: ignore[method-assign]
    completion = _CompletionSession(
        locked_job={
            "job_id": job.job_id,
            "node_id": job.node_id,
            "content_hash": job.content_hash,
            "status": "processing",
            "index_revision": 4,
            "claim_token": "lease-old",
        },
        locked_node=current_node,
        existing_revision_job=current_job,
    )

    async def _write(operation):  # type: ignore[no-untyped-def]
        return await operation(completion)

    port._write = _write  # type: ignore[method-assign]

    class _Collection:
        name = "memory-test"

        async def upsert(self, **_kwargs: Any) -> None:
            return None

    async def _embed(_texts: list[str]) -> tuple[list[list[float]], str]:
        return [[0.1, 0.2]], "test/model-v1"

    report = await port.run_index_worker(
        limit=1,
        collection=_Collection(),
        embed_texts_func=_embed,
        collection_resolver=None,
        collection_upsert_func=None,
        retry_failed=False,
        reclaim_after=None,
    )

    assert report.completed == ()
    report_id = f"{job.job_id}@index_revision=4"
    assert report.stale == (report_id,)
    assert report.errors[report_id] == "StaleRevision"
    assert any(
        sql.startswith("INSERT INTO memory_vector_tombstones")
        and "force_delete" in sql
        and params["node_id"] == "node-race"
        for sql, params in completion.calls
    )
    assert any(
        sql.startswith("UPDATE memory_nodes SET embedding_synced = FALSE")
        and params["revision"] == 5
        for sql, params in completion.calls
    )
    assert any(
        sql.startswith("UPDATE memory_index_jobs SET status = 'pending'")
        and params["job_id"] == current_job["job_id"]
        and params["expected_status"] == "completed"
        for sql, params in completion.calls
    )


@pytest.mark.asyncio
async def test_mysql_compensation_keeps_and_consumes_each_collection_identity() -> None:
    tombstones: list[dict[str, Any]] = [
        {
            "tombstone_id": 1,
            "node_id": "node-cross-collection",
            "chunk_id": "chunk-shared",
            "collection_name": "collection-a",
            "created_at": 1.0,
            "consumed_at": None,
            "force_delete": True,
        }
    ]

    class _CompensationSession:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def execute(
            self,
            statement: object,
            parameters: dict[str, Any],
        ) -> _Result:
            sql = " ".join(str(statement).split())
            params = dict(parameters)
            self.calls.append((sql, params))
            if sql.startswith(
                "UPDATE memory_vector_tombstones SET consumed_at = NULL"
            ):
                assert "collection_name = :collection_name" in sql
                matching = [
                    row
                    for row in tombstones
                    if row["node_id"] == params["node_id"]
                    and row["collection_name"] == params["collection_name"]
                ]
                for row in matching:
                    row.update(
                        consumed_at=None,
                        force_delete=True,
                        created_at=params["now"],
                    )
                return _Result(rowcount=len(matching))
            if sql.startswith("INSERT INTO memory_vector_tombstones"):
                assert "t.collection_name = :collection_name" in sql
                assert "SELECT historical.node_id, historical.chunk_id" in sql
                candidate_ids = {"chunk-shared"}
                candidate_ids.update(
                    str(row["chunk_id"])
                    for row in tombstones
                    if row["node_id"] == params["node_id"]
                )
                inserted = 0
                for chunk_id in sorted(candidate_ids):
                    already_pending = any(
                        row["node_id"] == params["node_id"]
                        and row["chunk_id"] == chunk_id
                        and row["collection_name"] == params["collection_name"]
                        and row["consumed_at"] is None
                        and bool(row["force_delete"])
                        for row in tombstones
                    )
                    if already_pending:
                        continue
                    tombstones.append(
                        {
                            "tombstone_id": max(
                                (int(row["tombstone_id"]) for row in tombstones),
                                default=0,
                            )
                            + 1,
                            "node_id": params["node_id"],
                            "chunk_id": chunk_id,
                            "collection_name": params["collection_name"],
                            "created_at": params["now"],
                            "consumed_at": None,
                            "force_delete": True,
                        }
                    )
                    inserted += 1
                return _Result(rowcount=inserted)
            if sql.startswith("SELECT n.node_id, n.content_hash"):
                return _Result([])
            if sql.startswith(
                "UPDATE memory_vector_tombstones SET consumed_at = :consumed_at"
            ):
                matching = [
                    row
                    for row in tombstones
                    if row["tombstone_id"] == params["tombstone_id"]
                    and row["consumed_at"] is None
                ]
                for row in matching:
                    row["consumed_at"] = params["consumed_at"]
                return _Result(rowcount=len(matching))
            raise AssertionError(sql)

    session = _CompensationSession()
    await MySQLDocumentIndexProjection._schedule_vector_compensation(
        session,  # type: ignore[arg-type]
        node_id="node-cross-collection",
        locked_node=None,
        collection_name="collection-b",
        now=2.0,
    )
    await MySQLDocumentIndexProjection._schedule_vector_compensation(
        session,  # type: ignore[arg-type]
        node_id="node-cross-collection",
        locked_node=None,
        collection_name="collection-b",
        now=3.0,
    )

    assert [row["collection_name"] for row in tombstones] == [
        "collection-a",
        "collection-b",
    ]

    class _TombstoneConnection:
        async def execute(
            self,
            statement: object,
            parameters: dict[str, Any],
        ) -> _Result:
            sql = " ".join(str(statement).split())
            assert "t.collection_name = :collection_name" in sql
            rows = [
                {
                    **row,
                    "is_live": False,
                }
                for row in tombstones
                if row["collection_name"] == parameters["collection_name"]
                and row["consumed_at"] is None
            ]
            return _Result(rows)

    port = object.__new__(MySQLDocumentIndexProjection)
    port.runtime = SimpleNamespace(engine=_Engine(_TombstoneConnection()))

    async def _write(operation):  # type: ignore[no-untyped-def]
        return await operation(session)

    port._write = _write  # type: ignore[method-assign]
    deleted: list[tuple[str, tuple[str, ...]]] = []

    class _Collection:
        def __init__(self, name: str) -> None:
            self.name = name

        async def delete(self, *, ids: list[str]) -> None:
            deleted.append((self.name, tuple(ids)))

    assert await port.consume_vector_tombstones(_Collection("collection-b")) == 1
    assert tombstones[0]["consumed_at"] is None
    assert tombstones[1]["consumed_at"] is not None
    assert await port.consume_vector_tombstones(_Collection("collection-a")) == 1
    assert tombstones[0]["consumed_at"] is not None
    assert deleted == [
        ("collection-b", ("chunk-shared",)),
        ("collection-a", ("chunk-shared",)),
    ]


@pytest.mark.asyncio
async def test_mysql_tombstones_never_delete_a_current_live_chunk() -> None:
    rows = [
        {
            "tombstone_id": 1,
            "chunk_id": "chunk-live",
            "is_live": True,
            "force_delete": False,
            "collection_name": "",
        },
        {
            "tombstone_id": 2,
            "chunk_id": "chunk-old",
            "is_live": False,
            "force_delete": False,
            "collection_name": "",
        },
        {
            "tombstone_id": 3,
            "chunk_id": "chunk-old",
            "is_live": False,
            "force_delete": False,
            "collection_name": "",
        },
    ]

    class _TombstoneConnection:
        async def execute(
            self,
            statement: object,
            _parameters: object | None = None,
        ) -> _Result:
            sql = " ".join(str(statement).split())
            assert "EXISTS(SELECT 1 FROM memory_chunks" in sql
            return _Result(rows)

    connection = _TombstoneConnection()
    completion = _CompletionSession()
    port = object.__new__(MySQLDocumentIndexProjection)
    port.runtime = SimpleNamespace(engine=_Engine(connection))

    async def _write(operation):  # type: ignore[no-untyped-def]
        return await operation(completion)

    port._write = _write  # type: ignore[method-assign]
    deleted_ids: list[str] = []

    class _Collection:
        name = "memory-test"

        async def delete(self, *, ids: list[str]) -> None:
            deleted_ids.extend(ids)

    consumed = await port.consume_vector_tombstones(_Collection())

    assert consumed == 3
    assert deleted_ids == ["chunk-old"]
    assert sum(
        sql.startswith("UPDATE memory_vector_tombstones")
        for sql, _params in completion.calls
    ) == 3


@pytest.mark.asyncio
async def test_mysql_tombstone_race_requeues_a_newly_live_chunk() -> None:
    rows = [
        {
            "tombstone_id": 1,
            "chunk_id": "chunk-race",
            "is_live": False,
            "force_delete": False,
            "collection_name": "memory-test",
        }
    ]

    class _TombstoneConnection:
        async def execute(
            self,
            statement: object,
            _parameters: object | None = None,
        ) -> _Result:
            sql = " ".join(str(statement).split())
            assert "EXISTS(SELECT 1 FROM memory_chunks" in sql
            return _Result(rows)

    class _RevivalSession(_CompletionSession):
        async def execute(
            self,
            statement: object,
            parameters: dict[str, Any],
        ) -> _Result:
            sql = " ".join(str(statement).split())
            self.calls.append((sql, dict(parameters)))
            if sql.startswith("SELECT n.node_id, n.content_hash"):
                return _Result(
                    [
                        {
                            "node_id": "node-race",
                            "content_hash": "a" * 16,
                            "index_revision": 9,
                        }
                    ]
                )
            return _Result(rowcount=1)

    connection = _TombstoneConnection()
    completion = _RevivalSession()
    port = object.__new__(MySQLDocumentIndexProjection)
    port.runtime = SimpleNamespace(engine=_Engine(connection))

    async def _write(operation):  # type: ignore[no-untyped-def]
        return await operation(completion)

    port._write = _write  # type: ignore[method-assign]
    deleted_ids: list[str] = []

    class _Collection:
        name = "memory-test"

        async def delete(self, *, ids: list[str]) -> None:
            deleted_ids.extend(ids)

    consumed = await port.consume_vector_tombstones(_Collection())

    assert consumed == 1
    assert deleted_ids == ["chunk-race"]
    assert any(
        sql.startswith("UPDATE memory_nodes SET embedding_synced = FALSE")
        for sql, _params in completion.calls
    )
    assert any(
        sql.startswith("INSERT INTO memory_index_jobs")
        and params["job_id"] == f"node-race:{'a' * 16}"
        for sql, params in completion.calls
    )


@pytest.mark.asyncio
async def test_mysql_tombstone_does_not_cross_collection_identity() -> None:
    class _TombstoneConnection:
        async def execute(
            self,
            statement: object,
            parameters: dict[str, Any],
        ) -> _Result:
            sql = " ".join(str(statement).split())
            assert "t.collection_name = :collection_name" in sql
            assert parameters["collection_name"] == "collection-b"
            # MySQL applies the WHERE clause: collection-a is not returned.
            return _Result([])

    completion = _CompletionSession()
    port = object.__new__(MySQLDocumentIndexProjection)
    port.runtime = SimpleNamespace(engine=_Engine(_TombstoneConnection()))

    async def _write(operation):  # type: ignore[no-untyped-def]
        return await operation(completion)

    port._write = _write  # type: ignore[method-assign]
    deleted_ids: list[str] = []

    class _Collection:
        name = "collection-b"

        async def delete(self, *, ids: list[str]) -> None:
            deleted_ids.extend(ids)

    assert await port.consume_vector_tombstones(_Collection()) == 0
    assert deleted_ids == []
    assert completion.calls == []


@pytest.mark.asyncio
async def test_mysql_tombstone_without_collection_identity_fails_closed() -> None:
    port = object.__new__(MySQLDocumentIndexProjection)

    class _Collection:
        async def delete(self, *, ids: list[str]) -> None:
            raise AssertionError(ids)

    assert await port.consume_vector_tombstones(_Collection()) == 0


async def _async_value(value: Any) -> Any:
    return value
