"""Tests for the SQLite document indexing foundation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from plugins.life_engine.memory.indexing import (
    create_memory_schema,
    delete_document_rows,
    list_index_jobs,
    move_document_rows,
    read_active_chunk_index_state,
    upsert_document_rows,
    write_active_chunk_index_state,
)
from plugins.life_engine.memory.nodes import generate_file_node_id


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
