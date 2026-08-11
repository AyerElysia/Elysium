"""Regression tests for rebuildable MySQL Memory projection recovery."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.memory.indexing import IndexJob
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
    def __init__(self, existing: dict[str, Any]) -> None:
        self.existing = existing
        self.statements: list[str] = []

    async def execute(
        self,
        statement: object,
        _parameters: object | None = None,
    ) -> _Result:
        sql = " ".join(str(statement).split())
        self.statements.append(sql)
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
            return _Result([])
        return _Result()

    async def scalar(
        self,
        _statement: object,
        _parameters: object | None = None,
    ) -> None:
        return None


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


class _Connection:
    def __init__(self, node: dict[str, Any], chunk: dict[str, Any]) -> None:
        self.node = node
        self.chunk = chunk

    async def execute(
        self,
        statement: object,
        _parameters: object | None = None,
    ) -> _Result:
        sql = " ".join(str(statement).split())
        if sql.startswith("SELECT * FROM memory_nodes"):
            return _Result([self.node])
        if sql.startswith("SELECT * FROM memory_chunks"):
            return _Result([self.chunk])
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
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(
        self,
        statement: object,
        parameters: dict[str, Any],
    ) -> _Result:
        sql = " ".join(str(statement).split())
        self.calls.append((sql, dict(parameters)))
        return _Result(rowcount=1)


@pytest.mark.asyncio
async def test_mysql_worker_records_exact_embedding_provenance() -> None:
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
        "chunk_id": "chunk-1",
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
    completion = _CompletionSession()

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

    completion_sql, completion_params = next(
        (sql, params)
        for sql, params in completion.calls
        if sql.startswith("UPDATE memory_index_jobs j")
    )
    assert "n.embedding_content_hash =" in completion_sql
    assert "n.embedding_model =" in completion_sql
    assert "n.embedding_updated_at =" in completion_sql
    assert completion_params["model_name"] == "test/model-v1"
    assert report.completed == (job.job_id,)


@pytest.mark.asyncio
async def test_mysql_tombstones_never_delete_a_current_live_chunk() -> None:
    rows = [
        {"tombstone_id": 1, "chunk_id": "chunk-live", "is_live": True},
        {"tombstone_id": 2, "chunk_id": "chunk-old", "is_live": False},
        {"tombstone_id": 3, "chunk_id": "chunk-old", "is_live": False},
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
    rows = [{"tombstone_id": 1, "chunk_id": "chunk-race", "is_live": False}]

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


async def _async_value(value: Any) -> Any:
    return value
