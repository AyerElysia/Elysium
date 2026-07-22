"""Life Engine memory search v2 regressions."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from plugins.life_engine.memory.indexing import create_memory_schema, upsert_document_rows
from plugins.life_engine.memory.search import (
    chunk_fts_search,
    embed_texts,
    search_memory,
    search_memory_detailed,
    vector_search,
)


class _FakeCollection:
    def __init__(
        self,
        ids: list[str] | None = None,
        *,
        distances: list[float] | None = None,
        metadatas: list[dict[str, Any]] | None = None,
        fail: bool = False,
        gate: threading.Event | None = None,
        started: threading.Event | None = None,
        chunk: bool = False,
    ) -> None:
        self.ids = ids or []
        self.distances = distances or [0.1 for _ in self.ids]
        self.metadatas = metadatas or [{} for _ in self.ids]
        self.fail = fail
        self.gate = gate
        self.started = started
        self.metadata = {"collection_kind": "life_memory_chunk"} if chunk else {}
        self.name = "life_memory_chunks_v1_fake_1" if chunk else "life_memory"
        self.delete_calls: list[list[str]] = []
        self.query_thread_id: int | None = None

    def query(self, **_: Any) -> dict[str, list[list[Any]]]:
        self.query_thread_id = threading.get_ident()
        if self.started is not None:
            self.started.set()
        if self.gate is not None:
            assert self.gate.wait(timeout=2.0)
        if self.fail:
            raise RuntimeError("vector backend unavailable")
        return {
            "ids": [self.ids],
            "distances": [self.distances],
            "metadatas": [self.metadatas],
        }

    def delete(self, ids: list[str]) -> None:
        self.delete_calls.append(ids)


def _db(tmp_path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(tmp_path / "memory-v2.db"), check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute(
        "CREATE VIRTUAL TABLE memory_fts USING fts5("
        "node_id, title, content, tokenize='unicode61')"
    )
    db.execute(
        """
        CREATE TABLE memory_edges (
            edge_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            edge_type TEXT NOT NULL,
            weight REAL DEFAULT 0.5,
            base_strength REAL DEFAULT 0.5,
            reinforcement REAL DEFAULT 0.0,
            activation_count INTEGER DEFAULT 0,
            last_activated_at REAL,
            reason TEXT,
            created_at REAL NOT NULL,
            bidirectional INTEGER DEFAULT 1,
            UNIQUE(source_id, target_id, edge_type)
        )
        """
    )
    create_memory_schema(db, now=1.0)
    return db


async def _no_embed(_: str) -> list[float]:
    return [0.1]


def _snapshot(db: sqlite3.Connection) -> tuple[list[tuple[Any, ...]], ...]:
    queries = (
        "SELECT * FROM memory_nodes ORDER BY node_id",
        "SELECT * FROM memory_chunks ORDER BY chunk_id",
        "SELECT * FROM memory_chunks_fts ORDER BY rowid",
        "SELECT * FROM memory_fts ORDER BY rowid",
        "SELECT * FROM memory_edges ORDER BY edge_id",
        "SELECT * FROM memory_index_jobs ORDER BY job_id",
    )
    return tuple([tuple(row) for row in db.execute(query).fetchall()] for query in queries)


@pytest.mark.asyncio
async def test_chinese_chunk_fts_hits_late_content_and_multiterm_is_or(tmp_path: Path) -> None:
    db = _db(tmp_path)
    content = "前文无关。" * 180 + "后半段出现神经可塑性关键结论，另有星海协议。"
    indexed = upsert_document_rows(
        db,
        "notes/long.md",
        content,
        "Long",
        now=2.0,
        max_chars=120,
        overlap_chars=20,
    )

    chinese = await chunk_fts_search(db, "神经可塑性", top_k=10)
    assert len(chinese) == 1
    assert chinese[0].node_id == indexed.node_id
    assert chinese[0].chunk_index > 0
    assert "神经可塑性" in chinese[0].snippet
    assert len(chinese[0].snippet) <= 300

    # OR semantics: the document need not contain every query term.
    multi = await chunk_fts_search(db, "星海协议 不存在词", top_k=10)
    assert [item.node_id for item in multi] == [indexed.node_id]


@pytest.mark.asyncio
async def test_fts_and_vector_run_in_parallel_and_vector_query_is_off_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db(tmp_path)
    indexed = upsert_document_rows(db, "notes/parallel.md", "parallel marker", "P", now=2.0)
    started = threading.Event()
    release = threading.Event()
    collection = _FakeCollection([indexed.node_id], gate=release, started=started)
    monkeypatch.setattr("plugins.life_engine.memory.search.embed_text", _no_embed)
    main_thread = threading.get_ident()

    task = asyncio.create_task(
        search_memory_detailed(
            db,
            "parallel marker",
            collection,
            top_k=2,
            enable_association=False,
        )
    )
    assert await asyncio.to_thread(started.wait, 1.0)
    assert not task.done()
    assert db.execute(
        "SELECT COUNT(*) FROM memory_chunks_fts WHERE memory_chunks_fts MATCH ?",
        ('"parallel" OR "marker"',),
    ).fetchone()[0] > 0
    release.set()
    detailed = await task

    assert detailed.fts_success is True
    assert detailed.vector_success is True
    assert detailed.results
    assert collection.query_thread_id is not None
    assert collection.query_thread_id != main_thread


@pytest.mark.asyncio
async def test_vector_failure_returns_fts_and_marks_degraded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db(tmp_path)
    upsert_document_rows(db, "notes/fallback.md", "fallback keyword", "Fallback", now=2.0)
    collection = _FakeCollection(fail=True)
    monkeypatch.setattr("plugins.life_engine.memory.search.embed_text", _no_embed)

    detailed = await search_memory_detailed(
        db,
        "fallback keyword",
        collection,
        top_k=2,
        enable_association=False,
    )

    assert [item.file_path for item in detailed.results] == ["notes/fallback.md"]
    assert detailed.degraded is True
    assert detailed.fts_success is True
    assert detailed.vector_success is False
    assert detailed.diagnostics.error_types["vector"] == "RuntimeError"
    assert collection.delete_calls == []


@pytest.mark.asyncio
async def test_strict_file_type_and_workspace_missing_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db(tmp_path)
    for path in ("notes/live.md", "notes/not-md.md", "notes/missing.md"):
        upsert_document_rows(db, path, "strict suffix marker", Path(path).name, now=2.0)
    # Simulate an old index created before document eligibility existed.
    db.execute(
        "UPDATE memory_nodes SET file_path = ? WHERE file_path = ?",
        ("notes/not-md.md.bak", "notes/not-md.md"),
    )
    db.commit()
    live = tmp_path / "notes" / "live.md"
    live.parent.mkdir(parents=True)
    live.write_text("strict suffix marker", encoding="utf-8")
    (tmp_path / "notes" / "not-md.md.bak").write_text("strict suffix marker", encoding="utf-8")
    monkeypatch.setattr("plugins.life_engine.memory.search.embed_text", _no_embed)

    results = await search_memory(
        db,
        "strict suffix marker",
        _FakeCollection(),
        top_k=10,
        enable_association=False,
        file_types=["md"],
        workspace_path=tmp_path,
    )

    assert [item.file_path for item in results] == ["notes/live.md"]


@pytest.mark.asyncio
async def test_explicit_relative_date_and_time_range_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db(tmp_path)
    now = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    for path in (
        "notes/2026-07-18.md",
        "notes/2026-07-19.md",
        "notes/2026-07-20.md",
        "notes/2026-07-10.md",
    ):
        upsert_document_rows(db, path, "dated marker", Path(path).stem, now=2.0)
    monkeypatch.setattr("plugins.life_engine.memory.search.embed_text", _no_embed)

    explicit = await search_memory(
        db,
        "昨天 dated marker",
        _FakeCollection(),
        top_k=10,
        enable_association=False,
        now=now,
    )
    assert [item.file_path for item in explicit] == ["notes/2026-07-19.md"]

    ranged = await search_memory(
        db,
        "dated marker",
        _FakeCollection(),
        top_k=10,
        enable_association=False,
        time_range_days=2,
        now=now,
    )
    assert {item.file_path for item in ranged} == {
        "notes/2026-07-18.md",
        "notes/2026-07-19.md",
        "notes/2026-07-20.md",
    }


@pytest.mark.asyncio
async def test_same_node_multi_chunk_dedup_and_search_is_fully_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db(tmp_path)
    content = ("repeat marker " * 80) + ("other text " * 80)
    indexed = upsert_document_rows(
        db,
        "notes/repeated.md",
        content,
        "Repeated",
        now=2.0,
        max_chars=100,
        overlap_chars=20,
    )
    db.execute(
        "UPDATE memory_nodes SET access_count = 7, activation_strength = 0.42 "
        "WHERE node_id = ?",
        (indexed.node_id,),
    )
    db.commit()
    before = _snapshot(db)
    monkeypatch.setattr("plugins.life_engine.memory.search.embed_text", _no_embed)

    results = await search_memory(
        db,
        "repeat marker",
        _FakeCollection([indexed.node_id]),
        top_k=10,
        enable_association=True,
    )

    assert [item.file_path for item in results] == ["notes/repeated.md"]
    assert results[0].source == "direct"
    assert results[0].score_kind == "rank"
    assert _snapshot(db) == before


@pytest.mark.asyncio
async def test_embed_texts_sends_one_batch_and_retains_provider_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class _Request:
        async def send(self) -> Any:
            return type(
                "Response",
                (),
                {
                    "embeddings": [[1, 2], [3.5, 4.5]],
                    "model_name": "provider/embedding-v2",
                },
            )()

    def create_request(**kwargs: Any) -> _Request:
        calls.append(kwargs)
        return _Request()

    monkeypatch.setattr(
        "src.app.plugin_system.api.llm_api.get_model_set_by_task",
        lambda task: f"set:{task}",
    )
    monkeypatch.setattr(
        "src.app.plugin_system.api.llm_api.create_embedding_request",
        create_request,
    )

    result = await embed_texts(["first", "second"])

    assert calls == [
        {
            "model_set": "set:embedding",
            "request_name": "life_memory_embedding",
            "inputs": ["first", "second"],
        }
    ]
    assert result.embeddings == [[1.0, 2.0], [3.5, 4.5]]
    assert result.model_name == "provider/embedding-v2"
    assert result.dimension == 2


@pytest.mark.asyncio
async def test_chunk_vector_search_deduplicates_nodes_and_ignores_stale_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db(tmp_path)
    first = upsert_document_rows(
        db,
        "notes/first-vector.md",
        "abcdefghij",
        "First",
        now=2.0,
        max_chars=5,
        overlap_chars=0,
    )
    second = upsert_document_rows(
        db,
        "notes/second-vector.md",
        "klmno",
        "Second",
        now=3.0,
        max_chars=5,
        overlap_chars=0,
    )
    monkeypatch.setattr("plugins.life_engine.memory.search.embed_text", _no_embed)
    chunks = _FakeCollection(
        [first.chunks[0].chunk_id, first.chunks[1].chunk_id, second.chunks[0].chunk_id, "old"],
        distances=[0.4, 0.1, 0.2, 0.01],
        chunk=True,
    )

    results = await vector_search(
        "query",
        None,
        db=db,
        chunk_collection=chunks,
    )

    assert dict(results) == {
        first.node_id: pytest.approx(1.0 / 1.1),
        second.node_id: pytest.approx(1.0 / 1.2),
    }
    assert chunks.delete_calls == []


@pytest.mark.asyncio
async def test_chunk_vector_failure_uses_degraded_legacy_node_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db(tmp_path)
    indexed = upsert_document_rows(
        db,
        "notes/legacy-fallback.md",
        "legacy body",
        "Legacy",
        now=2.0,
    )
    monkeypatch.setattr("plugins.life_engine.memory.search.embed_text", _no_embed)
    legacy = _FakeCollection([indexed.node_id], distances=[0.2])
    chunks = _FakeCollection(fail=True, chunk=True)

    detailed = await search_memory_detailed(
        db,
        "not-an-fts-hit",
        legacy,
        top_k=2,
        enable_association=False,
        chunk_collection=chunks,
    )

    assert [item.file_path for item in detailed.results] == ["notes/legacy-fallback.md"]
    assert detailed.vector_success is True
    assert detailed.degraded is True
    assert detailed.error_types["vector"] == "LegacyVectorFallback"
    assert chunks.query_thread_id is not None
    assert legacy.query_thread_id is not None
