"""Upgrade/recovery gaps only, using synthetic SQLite and real copy fences.

No application process, formal database, copied ACTIVE registry, or model is used.
The v4 fixture contains a complete old history, unlike a single-version shape probe.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text

from plugins.life_engine.memory import indexing
from plugins.life_engine.memory.nodes import generate_file_node_id
from plugins.life_engine.storage.contracts import StorageBackendRuntime
from plugins.life_engine.storage.memory.contracts import ManagedDocumentIndexSnapshot
from plugins.life_engine.storage.migration import subject_history as history
from plugins.life_engine.storage.subject_adapters import SQLSubjectDocumentStore
from plugins.life_engine.storage.subject_contracts import AppendSubjectDocumentVersion
from plugins.life_engine.storage.subject_schema import (
    LOCAL_SUBJECT_SCHEMA_STATEMENTS,
    SUBJECT_SCHEMA_VERSION,
    ensure_subject_document_schema,
)
from scripts.bootstrap_local_selectable import _open_local_copy_runtime
from test.plugins.life_engine.test_subject_full_history_migration import _rows

_NOTE = "life_engine_workspace/fixture-note.md"
_V4_HEAD = """CREATE TABLE subject_documents (
    document_id TEXT PRIMARY KEY,
    logical_path TEXT NOT NULL UNIQUE,
    declared_owner TEXT NULL,
    current_version_id TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 0
)"""
_V4_OUTBOX_COLUMNS = (
    "outbox_id",
    "head_event_id",
    "document_id",
    "logical_path",
    "version_id",
    "content_hash",
    "state",
    "attempt_count",
    "created_at",
    "confirmed_at",
    "last_error",
    "lease_owner",
    "lease_until",
    "revision",
)


@asynccontextmanager
async def _runtime(
    path: Path, *, initialize: bool = True
) -> AsyncIterator[StorageBackendRuntime]:
    # Reopen uses a new isolated lease, never the source's ACTIVE authority.
    authority = path.with_name(f"{path.stem}-copy-authority-{uuid4().hex}.json")
    runtime, registry, token = await _open_local_copy_runtime(path, authority)
    try:
        if initialize:
            await ensure_subject_document_schema(runtime)
        yield runtime
    finally:
        try:
            await runtime.close()
        finally:
            await registry.revoke(token)


async def _seed_v4(runtime: StorageBackendRuntime) -> None:
    """Freeze the real v4 table shape and complete two-version synthetic history."""
    source = copy.deepcopy(_rows())
    document = source["subject_documents"][0]
    document = {
        key: document[key]
        for key in (
            "document_id",
            "logical_path",
            "declared_owner",
            "current_version_id",
            "revision",
        )
    }
    document.update(logical_path=_NOTE, revision=2, declared_owner=None)
    versions = [
        row
        for row in source["subject_document_versions"]
        if row["document_id"] == "old"
    ]
    for version in versions:
        version["logical_path"] = _NOTE
    event_ids = {"event-1", "event-3"}
    events = [
        row
        for row in source["subject_document_head_events"]
        if row["head_event_id"] in event_ids
    ]
    outbox = [
        {key: row[key] for key in _V4_OUTBOX_COLUMNS}
        for row in source["subject_projection_outbox"]
        if row["head_event_id"] in event_ids
    ]
    for row in outbox:
        row["logical_path"] = _NOTE
    tables = {
        "subject_documents": [document],
        "subject_document_versions": versions,
        "subject_document_head_events": events,
        "subject_projection_outbox": outbox,
    }
    async with runtime.unit_of_work() as uow:
        for statement in (_V4_HEAD, *LOCAL_SUBJECT_SCHEMA_STATEMENTS[1:]):
            await uow.session.execute(text(statement))
        for table, rows in tables.items():
            for row in rows:
                names = tuple(row)
                await uow.session.execute(
                    text(
                        f"INSERT INTO {table} ({', '.join(names)}) "
                        f"VALUES ({', '.join(':' + name for name in names)})"
                    ),
                    {
                        name: json.dumps(value) if name.endswith("_json") else value
                        for name, value in row.items()
                    },
                )


async def _old_history(
    runtime: StorageBackendRuntime,
) -> dict[str, list[tuple[Any, ...]]]:
    """Compare original records without including added projection columns."""
    assert runtime.engine is not None
    async with runtime.engine.connect() as connection:
        result = {}
        for table in (
            "subject_document_versions",
            "subject_document_head_events",
            "subject_authority_decisions",
            "subject_projection_outbox",
        ):
            columns = (
                ", ".join(_V4_OUTBOX_COLUMNS)
                if table == "subject_projection_outbox"
                else "*"
            )
            rows = await connection.execute(
                text(f"SELECT {columns} FROM {table} ORDER BY 1")
            )
            names = tuple(rows.keys())
            result[table] = [
                tuple(
                    json.loads(value) if name.endswith("_json") else value
                    for name, value in zip(names, row, strict=True)
                )
                for row in rows
            ]
        return result


async def test_v4_complete_history_upgrades_reopens_and_restores_without_rollback_loss(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "v4-source.sqlite3"
    target_path = tmp_path / "restored-candidate.sqlite3"
    async with _runtime(source_path, initialize=False) as source:
        await _seed_v4(source)
        store = SQLSubjectDocumentStore(source)
        originals = [await store.get_version(name) for name in ("old-v1", "old-v2")]
        before = await _old_history(source)
        await ensure_subject_document_schema(source)
        assert SUBJECT_SCHEMA_VERSION == 5
        assert await _old_history(source) == before
        head = await store.get_head(_NOTE)
        assert head is not None
        assert (head.document_id, head.revision, head.binding_revision) == ("old", 2, 1)
        assert head.declared_owner is None
        upgraded = history.verify_subject_history_bundle(
            await history.capture_subject_history(source)
        )
        assert upgraded.table_counts["subject_document_versions"] == 2
        assert upgraded.table_counts["subject_document_head_events"] == 2
        assert upgraded.table_counts["subject_document_operations"] == 0
        assert upgraded.table_counts["subject_document_path_events"] == 1

    async with _runtime(source_path) as reopened:
        assert await _old_history(reopened) == before
        payload = await history.capture_subject_history(reopened)
        assert (
            history.verify_subject_history_bundle(payload).table_roots
            == upgraded.table_roots
        )
        async with _runtime(target_path) as target:
            imported = await history.import_subject_history_bundle(payload, target)
            assert imported.table_counts == upgraded.table_counts
            assert imported.table_roots == upgraded.table_roots

    async with _runtime(target_path) as restored:
        store = SQLSubjectDocumentStore(restored)
        assert await _old_history(restored) == before
        for old in originals:
            assert old.semantic_actor_id is None and old.semantic_source_id is None
            assert old.occurred_at is None
            assert await store.get_version(old.version_id) == old
        assert (
            await history.import_subject_history_bundle(payload, restored)
        ).idempotent_replay
        head = await store.get_head(_NOTE)
        assert head is not None
        command = AppendSubjectDocumentVersion(
            logical_path=_NOTE,
            expected_revision=head.revision,
            expected_head_version_id=head.current_version_id,
            expected_document_id=head.document_id,
            expected_binding_revision=head.binding_revision,
            content_bytes=b"synthetic post-restore version\r\n\x00",
            occurrence_id="synthetic:after-candidate-restore",
            recorded_by="synthetic-upgrade-test",
            recorded_source="test:post-restore-append",
            declared_owner=None,
            semantic_actor_id=None,
            semantic_source_id=None,
            provenance_status="semantic_source_missing",
        )
        later = await store.append_version(command)
        current = history.verify_subject_history_bundle(
            await history.capture_subject_history(restored)
        )
        assert current.table_counts["subject_document_versions"] == 3
        with pytest.raises(history.SubjectHistoryError, match="extra or conflicting"):
            await history.import_subject_history_bundle(payload, restored)
        retained = history.verify_subject_history_bundle(
            await history.capture_subject_history(restored)
        )
        assert retained.table_counts == current.table_counts
        assert retained.table_roots == current.table_roots
        assert await store.get_version(later.version.version_id) == later.version

    async with _runtime(target_path) as reopened:
        store = SQLSubjectDocumentStore(reopened)
        replay = await store.append_version(command)
        assert replay.version.version_id == later.version.version_id
        assert await store.get_document_head("old") == later.head
        final = history.verify_subject_history_bundle(
            await history.capture_subject_history(reopened)
        )
        assert final.table_roots == current.table_roots
        for old in originals:
            assert await store.get_version(old.version_id) == old


@pytest.mark.parametrize("cancelled", [False, True])
async def test_v4_upgrade_final_fence_failure_rolls_back_ddl_and_reopens(
    tmp_path: Path, cancelled: bool
) -> None:
    path = tmp_path / "v4-failure.sqlite3"
    async with _runtime(path, initialize=False) as runtime:
        await _seed_v4(runtime)
        before = await _old_history(runtime)
        original_validator = runtime._writer_validator
        assert original_validator is not None
        checks = 0

        async def fail_final_validation() -> None:
            nonlocal checks
            await original_validator()
            checks += 1
            if checks == 2:
                if cancelled:
                    raise asyncio.CancelledError(
                        "synthetic final migration cancellation"
                    )
                raise RuntimeError("synthetic final migration fence expired")

        runtime._writer_validator = fail_final_validation
        failure = asyncio.CancelledError if cancelled else RuntimeError
        with pytest.raises(failure):
            await ensure_subject_document_schema(runtime)
        assert checks == 2
        runtime._writer_validator = original_validator
        assert await _old_history(runtime) == before
        assert runtime.engine is not None
        async with runtime.engine.connect() as connection:
            columns = {
                row[1]
                for row in await connection.execute(
                    text("PRAGMA table_info(subject_documents)")
                )
            }
            assert columns == {
                "document_id",
                "logical_path",
                "declared_owner",
                "current_version_id",
                "revision",
            }
            assert await connection.scalar(text("PRAGMA foreign_keys")) == 1
            assert (
                await connection.execute(text("PRAGMA foreign_key_check"))
            ).first() is None
            names = set(
                (
                    await connection.execute(text("SELECT name FROM sqlite_master"))
                ).scalars()
            )
            assert not names & {
                "subject_documents_v5",
                "subject_document_path_events",
                "subject_document_path_bindings",
                "subject_document_operations",
            }

    async with _runtime(path) as reopened:
        assert await _old_history(reopened) == before
        verified = history.verify_subject_history_bundle(
            await history.capture_subject_history(reopened)
        )
        assert verified.table_counts["subject_document_versions"] == 2
        assert verified.table_counts["subject_document_path_events"] == 1


# Frozen v6 shapes: new v7 identity columns are absent, not merely a version label.
_V6_MEMORY_SCHEMA = (
    """CREATE TABLE memory_nodes (
        node_id TEXT PRIMARY KEY, node_type TEXT NOT NULL, file_path TEXT,
        content_hash TEXT, title TEXT, activation_strength REAL DEFAULT 1.0,
        access_count INTEGER DEFAULT 0, last_accessed_at REAL,
        emotional_valence REAL DEFAULT 0.0, emotional_arousal REAL DEFAULT 0.0,
        importance REAL DEFAULT 0.5, created_at REAL NOT NULL,
        updated_at REAL NOT NULL, embedding_synced INTEGER DEFAULT 0,
        source_mtime REAL, event_date TEXT, is_deleted INTEGER NOT NULL DEFAULT 0,
        fts_content_hash TEXT, embedding_content_hash TEXT, embedding_model TEXT,
        embedding_updated_at REAL, index_revision INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE memory_schema (
        schema_name TEXT PRIMARY KEY, version INTEGER NOT NULL,
        tokenizer TEXT NOT NULL, updated_at REAL NOT NULL
    )""",
    """CREATE TABLE memory_chunks (
        chunk_id TEXT PRIMARY KEY, node_id TEXT NOT NULL,
        chunk_index INTEGER NOT NULL, content_hash TEXT NOT NULL,
        content TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        FOREIGN KEY(node_id) REFERENCES memory_nodes(node_id) ON DELETE CASCADE
    )""",
    """CREATE TABLE memory_index_jobs (
        job_id TEXT NOT NULL, node_id TEXT NOT NULL, content_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL,
        updated_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '', index_revision INTEGER NOT NULL DEFAULT 0,
        claim_token TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(job_id, index_revision), UNIQUE(node_id, index_revision),
        FOREIGN KEY(node_id) REFERENCES memory_nodes(node_id) ON DELETE CASCADE
    )""",
    """CREATE TABLE memory_index_state (
        state_key TEXT PRIMARY KEY, collection_name TEXT NOT NULL,
        model_name TEXT NOT NULL, dimension INTEGER NOT NULL,
        version INTEGER NOT NULL, updated_at REAL NOT NULL
    )""",
    """CREATE TABLE memory_vector_tombstones (
        tombstone_id INTEGER PRIMARY KEY AUTOINCREMENT, node_id TEXT NOT NULL,
        chunk_id TEXT NOT NULL, collection_name TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL, consumed_at REAL,
        force_delete INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE memory_edges (
        edge_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, target_id TEXT NOT NULL,
        edge_type TEXT NOT NULL, weight REAL DEFAULT 0.5,
        base_strength REAL DEFAULT 0.5, reinforcement REAL DEFAULT 0.0,
        activation_count INTEGER DEFAULT 0, last_activated_at REAL, reason TEXT,
        created_at REAL NOT NULL, bidirectional INTEGER DEFAULT 1,
        FOREIGN KEY(source_id) REFERENCES memory_nodes(node_id) ON DELETE CASCADE,
        FOREIGN KEY(target_id) REFERENCES memory_nodes(node_id) ON DELETE CASCADE,
        UNIQUE(source_id, target_id, edge_type)
    )""",
    "CREATE TABLE memory_fts (node_id TEXT, title TEXT, content TEXT)",
    (
        "CREATE VIRTUAL TABLE memory_chunks_fts USING fts5("
        "chunk_id, node_id, content, title, tokenize='unicode61')"
    ),
)
_V7_COLUMNS = {
    "document_content",
    "subject_document_id",
    "subject_version_id",
    "subject_document_revision",
    "subject_binding_revision",
    "subject_content_sha256",
    "subject_projection_sha256",
    "subject_projection_state",
}


def _connect_memory(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def _seed_memory_v6(db: sqlite3.Connection) -> None:
    for statement in _V6_MEMORY_SCHEMA:
        db.execute(statement)
    db.execute(
        "INSERT INTO memory_schema VALUES ('document_index', 6, 'unicode61', 10)"
    )
    for suffix, content in (("a", "synthetic old alpha"), ("b", "synthetic old beta")):
        path = f"notes/{suffix}.md"
        node_id = generate_file_node_id(path)
        digest = hashlib.sha256(content.encode()).hexdigest()
        chunk_id = indexing.chunk_document(node_id, content)[0].chunk_id
        db.execute(
            "INSERT INTO memory_nodes "
            "(node_id, node_type, file_path, content_hash, title, created_at, updated_at) "
            "VALUES (?, 'file', ?, ?, NULL, 10, 11)",
            (node_id, path, digest),
        )
        db.execute(
            "INSERT INTO memory_chunks VALUES (?, ?, 0, ?, ?, '', 10, 11)",
            (chunk_id, node_id, digest, content),
        )
        db.execute(
            "INSERT INTO memory_chunks_fts VALUES (?, ?, ?, '')",
            (chunk_id, node_id, content),
        )
        db.execute("INSERT INTO memory_fts VALUES (?, '', ?)", (node_id, content))
    db.execute(
        "INSERT INTO memory_edges "
        "(edge_id, source_id, target_id, edge_type, reason, created_at) "
        "VALUES ('old-edge', ?, ?, 'synthetic-test-relation', NULL, 10)",
        (generate_file_node_id("notes/a.md"), generate_file_node_id("notes/b.md")),
    )
    db.execute(
        "INSERT INTO memory_index_jobs VALUES "
        "('old-job', ?, 'old-hash', 'completed', 10, 11, 1, '', 1, '')",
        (generate_file_node_id("notes/a.md"),),
    )
    db.execute(
        "INSERT INTO memory_index_state VALUES "
        "('active_chunk_collection', 'old-collection', 'synthetic-model', 2, 6, 10)"
    )
    db.execute(
        "INSERT INTO memory_vector_tombstones VALUES "
        "(9, ?, 'older-chunk', 'old-collection', 8, 9, 0)",
        (generate_file_node_id("notes/a.md"),),
    )
    db.commit()


def _memory_columns(db: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in db.execute("PRAGMA table_info(memory_nodes)"))


def _memory_history(
    db: sqlite3.Connection, node_columns: tuple[str, ...]
) -> dict[str, list[tuple[Any, ...]]]:
    result = {}
    for table in (
        "memory_nodes",
        "memory_chunks",
        "memory_edges",
        "memory_index_jobs",
        "memory_index_state",
        "memory_vector_tombstones",
        "memory_fts",
        "memory_chunks_fts",
    ):
        columns = ", ".join(node_columns) if table == "memory_nodes" else "*"
        result[table] = [
            tuple(row)
            for row in db.execute(f"SELECT {columns} FROM {table} ORDER BY 1")
        ]
    return result


def test_memory_v6_upgrades_restores_and_keeps_exact_legacy_ids_and_refs(
    tmp_path: Path,
) -> None:
    path = tmp_path / "memory-v6.sqlite3"
    restored_path = tmp_path / "memory-v7-restored.sqlite3"
    with closing(_connect_memory(path)) as db:
        _seed_memory_v6(db)
        old_columns = _memory_columns(db)
        assert not set(old_columns) & _V7_COLUMNS
        before = _memory_history(db, old_columns)
        indexing.create_memory_schema(db, now=20)
        assert indexing.INDEX_SCHEMA_VERSION == 7
        assert set(_memory_columns(db)) == set(old_columns) | _V7_COLUMNS
        assert _memory_history(db, old_columns) == before
        assert db.execute("SELECT version FROM memory_schema").fetchone()[0] == 7
    with closing(_connect_memory(path)) as reopened:
        indexing.create_memory_schema(reopened, now=21)
        assert _memory_history(reopened, old_columns) == before
        with closing(_connect_memory(restored_path)) as destination:
            reopened.backup(destination)

    with closing(_connect_memory(restored_path)) as restored:
        indexing.create_memory_schema(restored, now=22)
        assert _memory_history(restored, old_columns) == before
        assert restored.execute("PRAGMA foreign_key_check").fetchone() is None
        original_edge = before["memory_edges"]
        content = "synthetic managed replacement"
        snapshot = ManagedDocumentIndexSnapshot(
            document_id="restored-subject-doc",
            version_id="restored-subject-version",
            path="notes/a.md",
            document_revision=1,
            binding_revision=1,
            content_sha256=hashlib.sha256(content.encode()).hexdigest(),
            content=content,
            title="Synthetic restored source",
        )
        projected = indexing.project_managed_document_rows(restored, snapshot, now=23)
        assert projected.node_id == "subject-file:restored-subject-doc"
        old_a = generate_file_node_id("notes/a.md")
        row = restored.execute(
            "SELECT is_deleted, subject_document_id, title FROM memory_nodes WHERE node_id=?",
            (old_a,),
        ).fetchone()
        assert tuple(row) == (1, None, None)
        assert [
            tuple(row) for row in restored.execute("SELECT * FROM memory_edges")
        ] == original_edge
        assert (
            restored.execute(
                "SELECT content FROM memory_chunks WHERE node_id=?", (old_a,)
            ).fetchone()[0]
            == "synthetic old alpha"
        )
        with pytest.raises(indexing.DocumentIdentityConflict):
            indexing.delete_document_rows_by_id(restored, old_a)
        assert indexing.project_managed_document_rows(
            restored, snapshot, now=24
        ).idempotent_replay
        assert restored.execute("SELECT COUNT(*) FROM memory_nodes").fetchone()[0] == 3


def test_memory_v6_upgrade_failure_does_not_leave_partial_schema_or_lose_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "memory-v6-failure.sqlite3"
    with closing(_connect_memory(path)) as db:
        _seed_memory_v6(db)
        old_columns = _memory_columns(db)
        before = _memory_history(db, old_columns)

        def fail_before_schema_marker(_db: sqlite3.Connection) -> str:
            raise sqlite3.OperationalError("synthetic schema finalization failure")

        with monkeypatch.context() as patch:
            patch.setattr(indexing, "_try_create_chunks_fts", fail_before_schema_marker)
            with pytest.raises(sqlite3.OperationalError, match="synthetic"):
                indexing.create_memory_schema(db, now=20)
        assert _memory_columns(db) == old_columns
        assert _memory_history(db, old_columns) == before
        assert db.execute("SELECT version FROM memory_schema").fetchone()[0] == 6
        assert (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='uq_memory_nodes_subject_document'"
            ).fetchone()
            is None
        )
    with closing(_connect_memory(path)) as reopened:
        indexing.create_memory_schema(reopened, now=21)
        assert _memory_history(reopened, old_columns) == before
        assert reopened.execute("SELECT version FROM memory_schema").fetchone()[0] == 7
        assert reopened.execute("PRAGMA foreign_key_check").fetchone() is None
