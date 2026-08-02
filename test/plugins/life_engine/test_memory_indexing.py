"""Tests for the SQLite document indexing foundation."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from plugins.life_engine.memory.indexing import (
    DocumentIdentityConflict,
    create_memory_schema,
    delete_document_rows,
    list_index_jobs,
    move_document_rows,
    read_active_chunk_index_state,
    transaction,
    upsert_document_rows,
    write_active_chunk_index_state,
)
from plugins.life_engine.memory.nodes import generate_file_node_id, increment_access


def _db(tmp_path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(tmp_path / "memory.db"))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute(
        """
        CREATE TABLE memory_fts (
            node_id TEXT,
            title TEXT,
            content TEXT
        )
        """
    )
    create_memory_schema(db, now=100.0)
    return db


def test_upsert_populates_chunks_fts_and_outbox(tmp_path: Path) -> None:
    db = _db(tmp_path)
    result = upsert_document_rows(
        db,
        "notes/2026-07-20.md",
        "alpha beta " * 120,
        "A note",
        source_mtime=12.5,
        now=101.0,
        max_chars=40,
    )

    assert result.node_id == generate_file_node_id("notes/2026-07-20.md")
    assert result.chunks
    assert db.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0] == len(result.chunks)
    assert db.execute("SELECT COUNT(*) FROM memory_chunks_fts").fetchone()[0] == len(result.chunks)
    assert db.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM memory_index_jobs WHERE status = 'pending'").fetchone()[0] == 1
    assert db.execute("SELECT tokenizer FROM memory_schema").fetchone()[0] in {"trigram", "unicode61"}

    # The transaction-backed FTS index is queryable without any embedding call.
    assert db.execute(
        "SELECT COUNT(*) FROM memory_chunks_fts WHERE memory_chunks_fts MATCH ?",
        ("alpha",),
    ).fetchone()[0] > 0


def test_empty_upsert_clears_old_content_and_outbox(tmp_path: Path) -> None:
    db = _db(tmp_path)
    indexed = upsert_document_rows(db, "notes/a.md", "old searchable content", "A", now=1.0)
    db.execute(
        "UPDATE memory_nodes SET embedding_synced = 1, embedding_content_hash = content_hash, "
        "embedding_model = 'old-model', embedding_updated_at = 1 WHERE node_id = ?",
        (indexed.node_id,),
    )
    db.commit()
    upsert_document_rows(db, "notes/a.md", "", "A", now=2.0)

    node = db.execute(
        "SELECT content_hash, embedding_synced, embedding_content_hash, embedding_model, "
        "embedding_updated_at FROM memory_nodes"
    ).fetchone()
    assert node["content_hash"] is None
    assert node["embedding_synced"] == 0
    assert node["embedding_content_hash"] is None
    assert node["embedding_model"] is None
    assert node["embedding_updated_at"] is None
    assert db.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM memory_chunks_fts").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM memory_index_jobs").fetchone()[0] == 0


def test_delete_and_move_retain_or_remove_expected_rows(tmp_path: Path) -> None:
    db = _db(tmp_path)
    upsert_document_rows(db, "notes/a.md", "move me", "A", now=1.0)
    assert delete_document_rows(db, "notes/missing.md") is False
    assert move_document_rows(db, "notes/a.md", "archive/a.md", now=2.0) is True
    assert db.execute(
        "SELECT file_path FROM memory_nodes WHERE node_id = ?",
        (generate_file_node_id("archive/a.md"),),
    ).fetchone()[0] == "archive/a.md"
    assert db.execute("SELECT COUNT(*) FROM memory_chunks_fts WHERE node_id = ?", (generate_file_node_id("archive/a.md"),)).fetchone()[0] > 0

    upsert_document_rows(db, "notes/b.md", "target", "B", now=3.0)
    with pytest.raises(FileExistsError, match="目标文档已存在"):
        move_document_rows(db, "archive/a.md", "notes/b.md")
    assert db.execute("SELECT content FROM memory_fts WHERE node_id = ?", (generate_file_node_id("archive/a.md"),)).fetchone()[0] == "move me"

    assert delete_document_rows(db, "archive/a.md") is True
    assert db.execute("SELECT COUNT(*) FROM memory_nodes WHERE file_path = 'archive/a.md'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM memory_chunks_fts").fetchone()[0] == 1
    remaining_jobs = list_index_jobs(db)
    assert len(remaining_jobs) == 1
    assert remaining_jobs[0].node_id == generate_file_node_id("notes/b.md")


def test_path_operations_reject_absolute_and_traversal_aliases(tmp_path: Path) -> None:
    db = _db(tmp_path)
    indexed = upsert_document_rows(db, "notes/a.md", "body", now=1.0)

    for path in ("/notes/a.md", "../notes/a.md"):
        with pytest.raises(ValueError):
            delete_document_rows(db, path)
        with pytest.raises(ValueError):
            move_document_rows(db, path, "archive/a.md")

    with pytest.raises(ValueError):
        move_document_rows(db, "notes/a.md", "/archive/a.md")
    assert db.execute(
        "SELECT file_path FROM memory_nodes WHERE node_id = ?", (indexed.node_id,)
    ).fetchone()[0] == "notes/a.md"


def test_upsert_rejects_another_node_claiming_the_canonical_path(tmp_path: Path) -> None:
    db = _db(tmp_path)
    now = 1.0
    db.execute(
        """
        INSERT INTO memory_nodes (
            node_id, node_type, file_path, content_hash, title,
            created_at, updated_at, embedding_synced
        ) VALUES (?, 'file', 'notes/a.md', '', 'legacy', ?, ?, 0)
        """,
        ("file:legacy-path", now, now),
    )
    db.commit()

    with pytest.raises(DocumentIdentityConflict, match="already claimed"):
        upsert_document_rows(db, "notes/a.md", "body", now=2.0)


def test_read_helpers_do_not_create_schema(tmp_path: Path) -> None:
    db = sqlite3.connect(str(tmp_path / "empty.db"))
    db.row_factory = sqlite3.Row

    assert read_active_chunk_index_state(db) is None
    assert list_index_jobs(db) == []
    assert db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
    ).fetchone()[0] == 0


def test_active_chunk_index_state_roundtrip(tmp_path: Path) -> None:
    db = _db(tmp_path)

    written = write_active_chunk_index_state(
        db,
        "life_memory_chunks_v1_fake_model_2",
        "fake/model",
        2,
        1,
        now=123.0,
    )

    assert read_active_chunk_index_state(db) == written
    assert written.updated_at == 123.0


def test_shared_connection_transactions_cannot_rollback_each_other(tmp_path: Path) -> None:
    db = sqlite3.connect(str(tmp_path / "shared-transaction.db"), check_same_thread=False)
    db.execute("CREATE TABLE events (name TEXT NOT NULL)")
    db.commit()
    first_entered = threading.Event()
    allow_first_rollback = threading.Event()
    second_entered = threading.Event()
    errors: list[BaseException] = []

    def rollback_first() -> None:
        try:
            with transaction(db):
                db.execute("INSERT INTO events(name) VALUES ('rolled-back')")
                first_entered.set()
                assert allow_first_rollback.wait(timeout=2.0)
                raise RuntimeError("expected rollback")
        except RuntimeError:
            return
        except BaseException as exc:  # pragma: no cover - assertion aid for threads
            errors.append(exc)

    def commit_second() -> None:
        try:
            assert first_entered.wait(timeout=2.0)
            with transaction(db):
                second_entered.set()
                db.execute("INSERT INTO events(name) VALUES ('committed')")
        except BaseException as exc:  # pragma: no cover - assertion aid for threads
            errors.append(exc)

    first = threading.Thread(target=rollback_first)
    second = threading.Thread(target=commit_second)
    first.start()
    assert first_entered.wait(timeout=2.0)
    second.start()

    # A second root scope must wait instead of becoming a savepoint within the
    # first scope, which would let the first rollback erase its committed work.
    assert second_entered.wait(timeout=0.1) is False
    allow_first_rollback.set()
    first.join(timeout=2.0)
    second.join(timeout=2.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert db.execute("SELECT name FROM events ORDER BY rowid").fetchall() == [
        ("committed",)
    ]


class _ExpectedRollback(Exception):
    """Marker raised inside ``with transaction(db):`` to force a rollback."""


def test_apply_decay_rollback_does_not_erase_concurrent_upsert_document(
    tmp_path: Path,
) -> None:
    """Regression: a rolled-back decay transaction must not erase another
    thread's committed upsert.

    ``apply_decay`` runs its writes inside ``with transaction(db):`` on a
    shared ``check_same_thread=False`` connection. Because ``transaction()``
    is guarded by the module-level ``_TRANSACTION_LOCK``, a concurrent
    ``upsert_document_rows`` call (which also opens its own ``transaction()``
    scope) must block until the decay transaction finishes, rather than
    nesting as a savepoint inside it. If it nested instead, a decay rollback
    would also roll back the concurrent upsert's savepoint, silently erasing
    a newly indexed document.
    """
    db = sqlite3.connect(str(tmp_path / "decay-vs-upsert.db"), check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute(
        """
        CREATE TABLE memory_fts (
            node_id TEXT,
            title TEXT,
            content TEXT
        )
        """
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
    create_memory_schema(db, now=100.0)
    upsert_document_rows(db, "notes/existing.md", "baseline content", now=100.0)

    decay_entered = threading.Event()
    allow_decay_rollback = threading.Event()
    upsert_entered = threading.Event()
    errors: list[BaseException] = []

    def apply_decay_and_rollback() -> None:
        try:
            with transaction(db):
                db.execute(
                    "UPDATE memory_nodes SET activation_strength = activation_strength * 0.5"
                )
                decay_entered.set()
                assert allow_decay_rollback.wait(timeout=5.0)
                raise _ExpectedRollback("expected decay rollback")
        except _ExpectedRollback:
            return
        except BaseException as exc:  # pragma: no cover - assertion aid for threads
            errors.append(exc)

    def upsert_new_document() -> None:
        try:
            assert decay_entered.wait(timeout=5.0)
            upsert_entered.set()
            upsert_document_rows(db, "notes/new.md", "brand new content", now=101.0)
        except BaseException as exc:  # pragma: no cover - assertion aid for threads
            errors.append(exc)

    decay_thread = threading.Thread(target=apply_decay_and_rollback)
    upsert_thread = threading.Thread(target=upsert_new_document)

    decay_thread.start()
    assert decay_entered.wait(timeout=5.0)
    upsert_thread.start()

    # The upsert must block behind ``_TRANSACTION_LOCK`` instead of nesting as
    # a savepoint inside the decay transaction while it is still open.
    assert upsert_entered.wait(timeout=5.0)
    allow_decay_rollback.set()

    decay_thread.join(timeout=15.0)
    upsert_thread.join(timeout=15.0)

    assert not decay_thread.is_alive()
    assert not upsert_thread.is_alive()
    assert errors == []

    row = db.execute(
        "SELECT node_id FROM memory_nodes WHERE file_path = ?",
        ("notes/new.md",),
    ).fetchone()
    assert row is not None


async def test_increment_access_cannot_commit_another_threads_transaction(
    tmp_path: Path,
) -> None:
    """Access accounting must wait for ownership of the shared writer."""
    db = sqlite3.connect(
        str(tmp_path / "access-vs-rollback.db"),
        check_same_thread=False,
    )
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE memory_fts (node_id TEXT, title TEXT, content TEXT)"
    )
    create_memory_schema(db, now=100.0)
    indexed = upsert_document_rows(
        db,
        "notes/access.md",
        "baseline",
        title="original",
        now=100.0,
    )

    transaction_entered = threading.Event()
    allow_rollback = threading.Event()
    errors: list[BaseException] = []

    def rollback_title_change() -> None:
        try:
            with transaction(db):
                db.execute(
                    "UPDATE memory_nodes SET title = 'must rollback' WHERE node_id = ?",
                    (indexed.node_id,),
                )
                transaction_entered.set()
                assert allow_rollback.wait(timeout=5.0)
                raise _ExpectedRollback("expected rollback")
        except _ExpectedRollback:
            return
        except BaseException as exc:  # pragma: no cover - thread assertion aid
            errors.append(exc)

    owner = threading.Thread(target=rollback_title_change)
    owner.start()
    assert transaction_entered.wait(timeout=5.0)

    access_task = asyncio.create_task(increment_access(db, indexed.node_id))
    await asyncio.sleep(0.05)
    assert not access_task.done()
    allow_rollback.set()
    await access_task
    owner.join(timeout=5.0)

    assert not owner.is_alive()
    assert errors == []
    row = db.execute(
        "SELECT title, access_count FROM memory_nodes WHERE node_id = ?",
        (indexed.node_id,),
    ).fetchone()
    assert row["title"] == "original"
    assert row["access_count"] == 1
