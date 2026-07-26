"""Read-only memory health and reconciliation checks."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from plugins.life_engine.memory.health import (
    collect_health_snapshot,
    health_snapshot,
)
from plugins.life_engine.memory.indexing import (
    create_memory_schema,
    delete_document_rows_by_id,
    upsert_document_rows,
)
from plugins.life_engine.memory.nodes import compute_content_hash, generate_file_node_id
from scripts.reconcile_life_memory import MemoryDatabaseInUseError
from scripts.reconcile_life_memory import main as reconcile_main
from scripts.reconcile_life_memory import reconcile


class _FakeCollection:
    def __init__(
        self,
        ids: list[str],
        *,
        fail: bool = False,
        chunk: bool = False,
    ) -> None:
        self.ids = ids
        self.fail = fail
        self.metadata = {"collection_kind": "life_memory_chunk"} if chunk else {}
        self.name = "life_memory_chunks_v1_fake_2" if chunk else "life_memory"
        self.count_thread_id: int | None = None
        self.get_thread_id: int | None = None

    def count(self) -> int:
        self.count_thread_id = threading.get_ident()
        if self.fail:
            raise RuntimeError("vector backend unavailable")
        return len(self.ids)

    def get(self, **_: Any) -> dict[str, list[list[str]]]:
        self.get_thread_id = threading.get_ident()
        if self.fail:
            raise RuntimeError("vector backend unavailable")
        return {"ids": [self.ids]}


def _db(tmp_path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(tmp_path / "memory.db"), check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute(
        "CREATE VIRTUAL TABLE memory_fts USING fts5("
        "node_id, title, content, tokenize='unicode61')"
    )
    create_memory_schema(db, now=1.0)
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
            FOREIGN KEY (source_id) REFERENCES memory_nodes(node_id) ON DELETE CASCADE,
            FOREIGN KEY (target_id) REFERENCES memory_nodes(node_id) ON DELETE CASCADE
        )
        """
    )
    db.commit()
    return db


def _insert_edge(
    db: sqlite3.Connection,
    edge_id: str,
    source_id: str,
    target_id: str,
    edge_type: str,
) -> None:
    db.execute(
        """
        INSERT INTO memory_edges
            (edge_id, source_id, target_id, edge_type, weight, base_strength,
             reinforcement, activation_count, last_activated_at, reason, created_at, bidirectional)
        VALUES (?, ?, ?, ?, 0.5, 0.5, 0.0, 0, NULL, '', 1.0, 0)
        """,
        (edge_id, source_id, target_id, edge_type),
    )


def _insert_active_file_node(db: sqlite3.Connection, path: str) -> str:
    node_id = generate_file_node_id(path)
    db.execute(
        """
        INSERT INTO memory_nodes
            (node_id, node_type, file_path, content_hash, title, created_at, updated_at, is_deleted)
        VALUES (?, 'file', ?, 'legacy-hash', 'Legacy', 1, 1, 0)
        """,
        (node_id, path),
    )
    return node_id


async def test_health_reports_integrity_schema_counts_orphans_jobs_and_edges(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    notes = tmp_path / "notes"
    notes.mkdir()
    live = notes / "live.md"
    live.write_text("live body", encoding="utf-8")
    indexed = upsert_document_rows(db, "notes/live.md", "live body", "Live", now=2.0)
    missing = upsert_document_rows(db, "notes/missing.md", "old body", "Missing", now=2.0)
    db.execute(
        "UPDATE memory_nodes SET embedding_synced = 1 WHERE node_id = ?",
        (indexed.node_id,),
    )
    db.execute(
        "DELETE FROM memory_index_jobs WHERE node_id IN (?, ?)",
        (indexed.node_id, missing.node_id),
    )
    db.execute(
        "INSERT INTO memory_index_jobs"
        "(job_id, node_id, content_hash, status, created_at, updated_at, attempts, error)"
        "VALUES (?, ?, ?, 'processing', 1, 1, 1, '')",
        ("job-processing", indexed.node_id, "hash-processing"),
    )
    db.execute(
        "INSERT INTO memory_index_jobs"
        "(job_id, node_id, content_hash, status, created_at, updated_at, attempts, error)"
        "VALUES (?, ?, ?, 'failed', 1, 1, 1, 'failed')",
        ("job-failed", indexed.node_id, "hash-failed"),
    )
    _insert_edge(db, "edge-assoc", indexed.node_id, missing.node_id, "associates")
    _insert_edge(db, "edge-self", indexed.node_id, indexed.node_id, "relates")
    db.commit()

    # Create intentionally inconsistent rows after the normal schema setup.
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute(
        "INSERT INTO memory_chunks"
        "(chunk_id, node_id, chunk_index, content_hash, content, title, created_at, updated_at)"
        "VALUES ('orphan-chunk', 'missing-node', 0, 'hash', 'body', 'title', 1, 1)"
    )
    db.execute(
        "INSERT INTO memory_chunks_fts(chunk_id, node_id, content, title)"
        "VALUES ('orphan-fts', 'missing-node', 'body', 'title')"
    )
    db.execute("INSERT INTO memory_fts(node_id, title, content) VALUES ('missing-node', 'T', 'body')")
    db.execute(
        "INSERT INTO memory_edges"
        "(edge_id, source_id, target_id, edge_type, weight, base_strength, reinforcement, "
        "activation_count, last_activated_at, reason, created_at, bidirectional) "
        "VALUES ('edge-orphan', 'missing-node', ?, 'relates', 0.5, 0.5, 0, 0, NULL, '', 1, 0)",
        (indexed.node_id,),
    )
    db.execute(
        "CREATE TABLE memory_corrections ("
        "correction_id TEXT PRIMARY KEY, related_node_id TEXT, "
        "FOREIGN KEY (related_node_id) REFERENCES memory_nodes(node_id) ON DELETE SET NULL)"
    )
    db.execute(
        "INSERT INTO memory_corrections(correction_id, related_node_id) VALUES (?, ?)",
        ("orphan-correction", "missing-node"),
    )
    db.execute(
        "INSERT INTO memory_index_jobs"
        "(job_id, node_id, content_hash, status, created_at, updated_at, attempts, error) "
        "VALUES (?, ?, ?, 'pending', 1, 1, 0, '')",
        ("orphan-job", "missing-node", "hash-orphan"),
    )
    db.commit()
    db.execute("PRAGMA foreign_keys = ON")

    collection = _FakeCollection([indexed.node_id, "stale-vector-id"])
    snapshot = await health_snapshot(db, tmp_path, collection)

    assert snapshot["integrity_check"] == "ok"
    assert snapshot["foreign_key_check_count"] >= 2
    assert snapshot["schema_version"] == 4
    assert snapshot["tokenizer"] in {"trigram", "unicode61"}
    assert snapshot["counts"]["nodes"] == 2
    assert snapshot["counts"]["chunks"] == 3
    assert snapshot["counts"]["chunk_fts"] == 3
    assert snapshot["counts"]["legacy_fts"] == 3
    assert snapshot["fts"]["legacy_fts_orphan_count"] == 1
    assert snapshot["fts"]["chunk_orphan_count"] == 1
    assert snapshot["fts"]["chunk_fts_orphan_count"] == 1
    assert snapshot["outbox"]["processing"] == 1
    assert snapshot["outbox"]["failed"] == 1
    assert snapshot["outbox"]["orphan_node_count"] == 1
    assert snapshot["corrections"]["orphan_related_node_count"] == 1
    assert snapshot["edges"]["orphan_count"] == 1
    assert snapshot["edges"]["self_loop_count"] == 1
    assert snapshot["edges"]["associates_ratio"] == pytest.approx(1 / 3)
    assert snapshot["workspace"]["missing_node_paths"] == ["notes/missing.md"]
    assert snapshot["vector"]["orphan_count"] == 1
    assert snapshot["vector_degraded"] is False
    assert collection.count_thread_id != threading.get_ident()
    assert collection.get_thread_id != threading.get_ident()
    json.dumps(snapshot)


def test_sync_health_reports_workspace_hash_and_ignores_memory_files(tmp_path: Path) -> None:
    db = _db(tmp_path)
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "same.txt").write_text("new content", encoding="utf-8")
    (notes / "unindexed.md").write_text("new document", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("ignored", encoding="utf-8")
    (tmp_path / ".memory").mkdir()
    (tmp_path / ".memory" / "ignored.md").write_text("ignored", encoding="utf-8")
    upsert_document_rows(db, "notes/same.txt", "old content", "Same", now=2.0)

    snapshot = collect_health_snapshot(db, tmp_path)

    assert snapshot["workspace_file_count"] == 2
    assert snapshot["index"]["hash_mismatch_paths"] == ["notes/same.txt"]
    assert snapshot["index"]["unindexed_paths"] == ["notes/unindexed.md"]
    assert snapshot["index"]["coverage"] == pytest.approx(0.5)
    assert snapshot["hash_mismatch_count"] == 1
    assert snapshot["vector_degraded"] is True
    json.dumps(snapshot)


async def test_health_handles_unavailable_vector_and_minimal_legacy_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    db = sqlite3.connect(str(db_path), check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE memory_nodes ("
        "node_id TEXT PRIMARY KEY, file_path TEXT, node_type TEXT, content_hash TEXT)"
    )
    db.commit()

    collection = _FakeCollection([], fail=True)
    snapshot = await health_snapshot(db, tmp_path, collection)

    assert snapshot["integrity_check"] == "ok"
    assert snapshot["schema_version"] is None
    assert snapshot["tokenizer"] is None
    assert snapshot["counts"]["chunks"] == 0
    assert snapshot["counts"]["chunk_fts"] == 0
    assert snapshot["vector_degraded"] is True
    assert snapshot["vector"]["error_types"] == ["RuntimeError", "RuntimeError"]
    json.dumps(snapshot)

    empty = await health_snapshot(None, tmp_path, None)
    assert empty["status"] == "unavailable"
    assert empty["sqlite"]["integrity_check"] == "unavailable"
    json.dumps(empty)


async def test_health_compares_chunk_collection_with_chunk_ids(tmp_path: Path) -> None:
    db = _db(tmp_path)
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "chunk-health.md").write_text("abcdefghij", encoding="utf-8")
    indexed = upsert_document_rows(
        db,
        "notes/chunk-health.md",
        "abcdefghij",
        "Chunk Health",
        now=2.0,
        max_chars=5,
        overlap_chars=0,
    )
    db.execute(
        "UPDATE memory_nodes SET embedding_synced = 1 WHERE node_id = ?",
        (indexed.node_id,),
    )
    db.execute("DELETE FROM memory_index_jobs WHERE node_id = ?", (indexed.node_id,))
    db.commit()
    collection = _FakeCollection(
        [indexed.chunks[0].chunk_id, "orphan-vector-chunk"],
        chunk=True,
    )

    snapshot = await health_snapshot(db, tmp_path, collection)

    vector = snapshot["vector"]
    assert vector["kind"] == "chunk"
    assert vector["orphan_chunk_count"] == 1
    assert vector["orphan_chunk_ids"] == ["orphan-vector-chunk"]
    assert vector["missing_chunk_count"] == 1
    assert vector["missing_chunk_ids"] == [indexed.chunks[1].chunk_id]
    assert vector["orphan_count"] == 1
    assert vector["missing_embedded_node_count"] is None
    assert indexed.node_id not in vector["orphan_ids"]
    json.dumps(snapshot)


async def test_health_requires_exact_stored_identity_for_coverage_and_vectors(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "valid.md").write_text("valid", encoding="utf-8")
    (notes / "noncanonical.md").write_text("noncanonical", encoding="utf-8")
    (notes / "wrong-id.md").write_text("wrong id", encoding="utf-8")
    (notes / "deleted.md").write_text("deleted", encoding="utf-8")
    (notes / "concept.md").write_text("concept", encoding="utf-8")

    valid = upsert_document_rows(db, "notes/valid.md", "valid", now=1.0)
    noncanonical = upsert_document_rows(db, "notes/noncanonical.md", "noncanonical", now=2.0)
    wrong_id = upsert_document_rows(db, "notes/wrong-id.md", "wrong id", now=3.0)
    deleted = upsert_document_rows(db, "notes/deleted.md", "deleted", now=4.0)
    concept = upsert_document_rows(db, "notes/concept.md", "concept", now=5.0)
    db.execute(
        "UPDATE memory_nodes SET embedding_synced = 1 WHERE node_id IN (?, ?, ?, ?, ?)",
        (valid.node_id, noncanonical.node_id, wrong_id.node_id, deleted.node_id, concept.node_id),
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
    db.commit()
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute(
        "UPDATE memory_nodes SET node_id = ? WHERE node_id = ?",
        ("file:wrong-id", wrong_id.node_id),
    )
    db.execute(
        "UPDATE memory_chunks SET node_id = ? WHERE node_id = ?",
        ("file:wrong-id", wrong_id.node_id),
    )
    db.execute(
        "UPDATE memory_chunks_fts SET node_id = ? WHERE node_id = ?",
        ("file:wrong-id", wrong_id.node_id),
    )
    db.execute(
        "UPDATE memory_index_jobs SET node_id = ? WHERE node_id = ?",
        ("file:wrong-id", wrong_id.node_id),
    )
    db.commit()
    db.execute("PRAGMA foreign_keys = ON")

    collection = _FakeCollection(
        [
            valid.chunks[0].chunk_id,
            noncanonical.chunks[0].chunk_id,
            wrong_id.chunks[0].chunk_id,
            deleted.chunks[0].chunk_id,
            concept.chunks[0].chunk_id,
        ],
        chunk=True,
    )
    snapshot = await health_snapshot(db, tmp_path, collection)

    assert snapshot["index"]["coverage"] == pytest.approx(0.2)
    assert snapshot["index"]["unindexed_path_count"] == 4
    assert snapshot["index"]["unindexed_paths"] == [
        "notes/concept.md",
        "notes/deleted.md",
        "notes/noncanonical.md",
        "notes/wrong-id.md",
    ]
    assert snapshot["index"]["ineligible_indexed_node_count"] == 2
    assert snapshot["index"]["ineligible_indexed_node_paths"] == [
        "./notes/noncanonical.md",
        "notes/wrong-id.md",
    ]
    assert snapshot["index"]["ineligible_indexed_reason_counts"] == {
        "node_id_mismatch": 1,
        "noncanonical_path": 1,
    }
    assert snapshot["vector"]["orphan_chunk_count"] == 4
    assert snapshot["vector"]["orphan_chunk_ids"] == sorted(
        [
            noncanonical.chunks[0].chunk_id,
            wrong_id.chunks[0].chunk_id,
            deleted.chunks[0].chunk_id,
            concept.chunks[0].chunk_id,
        ]
    )


def test_health_and_reconcile_exclude_runtime_and_hidden_trace_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    notes = workspace / "notes"
    runtime = workspace / "runtime"
    trace = workspace / ".life_trace"
    notes.mkdir(parents=True)
    runtime.mkdir()
    trace.mkdir()
    (notes / "kept.md").write_text("kept", encoding="utf-8")
    (runtime / "state.json").write_text("{}", encoding="utf-8")
    (trace / "trace.txt").write_text("trace", encoding="utf-8")

    memory_dir = workspace / ".memory"
    memory_dir.mkdir()
    db = sqlite3.connect(str(memory_dir / "memory.db"), check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    create_memory_schema(db)
    upsert_document_rows(db, "notes/kept.md", "kept", "Kept")
    _insert_active_file_node(db, "runtime/state.json")
    _insert_active_file_node(db, ".life_trace/trace.txt")
    db.commit()

    health = collect_health_snapshot(db, workspace)
    reconciliation = reconcile(workspace)

    assert health["workspace_file_count"] == 1
    assert health["index"]["unindexed_path_count"] == 0
    assert health["index"]["ineligible_indexed_node_count"] == 2
    assert health["index"]["ineligible_indexed_node_paths"] == [
        ".life_trace/trace.txt",
        "runtime/state.json",
    ]
    assert health["eligibility"]["rejected_reason_counts"]["blocked_directory"] == 1
    assert health["eligibility"]["rejected_reason_counts"]["hidden_directory"] == 1
    assert reconciliation["scanned_count"] == 1
    assert reconciliation["new_count"] == 0
    assert reconciliation["ineligible_indexed_node_count"] == 2
    assert reconciliation["eligibility"]["rejected_reason_counts"]["blocked_directory"] == 1
    assert reconciliation["eligibility"]["rejected_reason_counts"]["hidden_directory"] == 1
    json.dumps(health)
    json.dumps(reconciliation)
    db.close()


def test_reconcile_dry_run_and_apply_are_sqlite_only(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    notes = workspace / "notes"
    notes.mkdir(parents=True)
    (notes / "note.md").write_text("first", encoding="utf-8")
    (workspace / "ignored.bin").write_bytes(b"ignored")

    dry_run = reconcile(workspace)
    assert dry_run["mode"] == "dry-run"
    assert dry_run["new_paths"] == ["notes/note.md"]
    assert dry_run["database_exists"] is False
    assert dry_run["backup"] is None
    assert not (workspace / ".memory").exists()
    json.dumps(dry_run)

    applied = reconcile(workspace, apply=True)
    assert applied["mode"] == "apply"
    assert applied["applied_paths"] == ["notes/note.md"]
    assert applied["backup"] is None
    db_path = workspace / ".memory" / "memory.db"
    assert db_path.exists()

    db = sqlite3.connect(str(db_path))
    assert (
        db.execute("SELECT content_hash FROM memory_nodes WHERE file_path = 'notes/note.md'").fetchone()[0]
        == compute_content_hash("first")
    )
    assert db.execute("SELECT status FROM memory_index_jobs").fetchone()[0] == "pending"
    db.close()

    (notes / "note.md").write_text("second", encoding="utf-8")
    before = reconcile(workspace)
    assert before["changed_paths"] == ["notes/note.md"]
    applied_again = reconcile(workspace, apply=True)
    assert applied_again["applied_paths"] == ["notes/note.md"]
    assert applied_again["backup"] is not None
    assert Path(applied_again["backup"]).is_file()

    db = sqlite3.connect(str(db_path))
    assert (
        db.execute("SELECT content_hash FROM memory_nodes WHERE file_path = 'notes/note.md'").fetchone()[0]
        == compute_content_hash("second")
    )
    db.close()


def test_reconcile_apply_refuses_an_active_database_holder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    notes = workspace / "notes"
    notes.mkdir(parents=True)
    (notes / "note.md").write_text("body", encoding="utf-8")
    reconcile(workspace, apply=True)

    db_path = workspace / ".memory" / "memory.db"
    baseline = db_path.read_bytes()
    monkeypatch.setattr(
        "scripts.reconcile_life_memory._database_holders",
        lambda _: (4242,),
    )

    with pytest.raises(MemoryDatabaseInUseError):
        reconcile(workspace, apply=True, rebuild=True)

    assert db_path.read_bytes() == baseline
    assert not (db_path.parent / "backups").exists()


def test_health_and_reconcile_mark_present_symlink_and_oversized_nodes_ineligible(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    notes = workspace / "notes"
    notes.mkdir(parents=True)
    target = notes / "target.md"
    target.write_text("target", encoding="utf-8")
    (notes / "linked.md").symlink_to(target)
    oversized = notes / "oversized.md"
    oversized.write_bytes(b"x" * (4 * 1024 * 1024 + 1))

    memory_dir = workspace / ".memory"
    memory_dir.mkdir()
    db = sqlite3.connect(str(memory_dir / "memory.db"), check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    create_memory_schema(db)
    _insert_active_file_node(db, "notes/linked.md")
    _insert_active_file_node(db, "notes/oversized.md")
    db.commit()

    health = collect_health_snapshot(db, workspace)
    reconciliation = reconcile(workspace)

    expected_reasons = {"symlink": 1, "too_large": 1}
    assert health["index"]["ineligible_indexed_node_count"] == 2
    assert health["index"]["ineligible_indexed_reason_counts"] == expected_reasons
    assert reconciliation["ineligible_indexed_node_count"] == 2
    assert reconciliation["ineligible_indexed_reason_counts"] == expected_reasons
    assert reconciliation["eligibility"]["rejected_reason_counts"] == expected_reasons
    db.close()


def test_reconcile_rebuild_repairs_missing_chunks_for_unchanged_document(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    notes = workspace / "notes"
    notes.mkdir(parents=True)
    (notes / "note.md").write_text("stable body", encoding="utf-8")
    reconcile(workspace, apply=True)

    db_path = workspace / ".memory" / "memory.db"
    node_id = generate_file_node_id("notes/note.md")
    db = sqlite3.connect(str(db_path))
    db.execute("DELETE FROM memory_chunks_fts WHERE node_id = ?", (node_id,))
    db.execute("DELETE FROM memory_chunks WHERE node_id = ?", (node_id,))
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM memory_chunks WHERE node_id = ?", (node_id,)).fetchone()[0] == 0
    db.close()

    dry_run = reconcile(workspace)
    assert dry_run["unchanged_paths"] == ["notes/note.md"]
    assert dry_run["applied_count"] == 0

    rebuilt = reconcile(workspace, apply=True, rebuild=True)
    assert rebuilt["changed_count"] == 0
    assert rebuilt["rebuild_count"] == 1
    assert rebuilt["rebuild_paths"] == ["notes/note.md"]
    assert rebuilt["applied_paths"] == ["notes/note.md"]
    assert rebuilt["backup"] is not None

    db = sqlite3.connect(str(db_path))
    assert db.execute("SELECT COUNT(*) FROM memory_chunks WHERE node_id = ?", (node_id,)).fetchone()[0] > 0
    db.close()


def test_reconcile_prunes_only_ineligible_indexed_nodes_and_dry_run_writes_nothing(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    notes = workspace / "notes"
    notes.mkdir(parents=True)
    kept = notes / "kept.md"
    missing = notes / "missing.md"
    kept.write_text("kept", encoding="utf-8")
    missing.write_text("missing", encoding="utf-8")
    reconcile(workspace, apply=True)
    missing.unlink()

    db_path = workspace / ".memory" / "memory.db"
    db = sqlite3.connect(str(db_path))
    _insert_active_file_node(db, "runtime/state.json")
    _insert_active_file_node(db, ".life_trace/trace.txt")
    db.commit()
    before_node_count = db.execute("SELECT COUNT(*) FROM memory_nodes").fetchone()[0]
    db.close()

    dry_run = reconcile(workspace)
    assert dry_run["mode"] == "dry-run"
    assert dry_run["ineligible_indexed_node_count"] == 2
    assert dry_run["missing_node_paths"] == ["notes/missing.md"]
    assert dry_run["applied_count"] == 0
    assert dry_run["deletions"] == 0

    db = sqlite3.connect(str(db_path))
    assert db.execute("SELECT COUNT(*) FROM memory_nodes").fetchone()[0] == before_node_count
    db.close()

    pruned = reconcile(workspace, apply=True, prune_ineligible=True)
    assert pruned["prune_ineligible_count"] == 2
    assert pruned["prune_ineligible_paths"] == [".life_trace/trace.txt", "runtime/state.json"]
    assert pruned["deletions"] == 2
    assert pruned["backup"] is not None

    db = sqlite3.connect(str(db_path))
    remaining_paths = {
        row[0] for row in db.execute("SELECT file_path FROM memory_nodes WHERE is_deleted = 0")
    }
    assert "notes/missing.md" in remaining_paths
    assert "runtime/state.json" not in remaining_paths
    assert ".life_trace/trace.txt" not in remaining_paths
    db.close()


def test_reconcile_rejects_and_prunes_noncanonical_stored_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    notes = workspace / "notes"
    notes.mkdir(parents=True)
    (notes / "note.md").write_text("canonical body", encoding="utf-8")

    memory_dir = workspace / ".memory"
    memory_dir.mkdir()
    db_path = memory_dir / "memory.db"
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    create_memory_schema(db)
    _insert_active_file_node(db, "./notes/note.md")
    db.commit()
    db.close()

    dry_run = reconcile(workspace)
    assert dry_run["new_paths"] == ["notes/note.md"]
    assert dry_run["unchanged_count"] == 0
    assert dry_run["ineligible_indexed_node_paths"] == ["./notes/note.md"]
    assert dry_run["ineligible_indexed_reason_counts"] == {"noncanonical_path": 1}

    db = sqlite3.connect(str(db_path))
    assert db.execute("SELECT file_path FROM memory_nodes").fetchone()[0] == "./notes/note.md"
    db.close()

    applied = reconcile(workspace, apply=True, prune_ineligible=True)
    assert applied["prune_ineligible_paths"] == ["./notes/note.md"]
    assert applied["applied_paths"] == ["notes/note.md"]

    db = sqlite3.connect(str(db_path))
    assert db.execute("SELECT file_path FROM memory_nodes").fetchone()[0] == "notes/note.md"
    assert db.execute("SELECT status FROM memory_index_jobs").fetchone()[0] == "pending"
    db.close()


def test_reconcile_apply_rolls_back_pruning_and_prior_upserts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    notes = workspace / "notes"
    notes.mkdir(parents=True)
    (notes / "first.md").write_text("first", encoding="utf-8")
    (notes / "second.md").write_text("second", encoding="utf-8")

    memory_dir = workspace / ".memory"
    memory_dir.mkdir()
    db_path = memory_dir / "memory.db"
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    create_memory_schema(db)
    _insert_active_file_node(db, "runtime/state.json")
    db.commit()
    db.close()

    calls: list[str] = []

    def fail_after_first_upsert(
        db: sqlite3.Connection,
        file_path: str,
        content: str,
        title: str = "",
        source_mtime: float | None = None,
    ) -> Any:
        calls.append(file_path)
        if file_path == "notes/second.md":
            raise RuntimeError("injected reconciliation failure")
        return upsert_document_rows(db, file_path, content, title, source_mtime)

    monkeypatch.setattr(
        "scripts.reconcile_life_memory._load_indexing_helpers",
        lambda: (create_memory_schema, fail_after_first_upsert, delete_document_rows_by_id),
    )

    with pytest.raises(RuntimeError, match="injected reconciliation failure"):
        reconcile(workspace, apply=True, prune_ineligible=True)

    assert calls == ["notes/first.md", "notes/second.md"]
    db = sqlite3.connect(str(db_path))
    assert db.execute("SELECT file_path FROM memory_nodes ORDER BY file_path").fetchall() == [
        ("runtime/state.json",)
    ]
    assert db.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM memory_index_jobs").fetchone()[0] == 0
    db.close()


def test_reconcile_reports_missing_nodes_without_deleting_them(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    notes = workspace / "notes"
    notes.mkdir(parents=True)
    gone = notes / "gone.md"
    gone.write_text("gone", encoding="utf-8")
    reconcile(workspace, apply=True)
    gone.unlink()

    summary = reconcile(workspace)
    assert summary["missing_node_paths"] == ["notes/gone.md"]
    assert summary["deletions"] == 0
    db = sqlite3.connect(str(workspace / ".memory" / "memory.db"))
    assert (
        db.execute("SELECT COUNT(*) FROM memory_nodes WHERE file_path = 'notes/gone.md'").fetchone()[0]
        == 1
    )
    db.close()


def test_reconcile_repair_orphans_is_explicit_and_repairs_sqlite_only(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    notes = workspace / "notes"
    notes.mkdir(parents=True)
    (notes / "live.md").write_text("live", encoding="utf-8")
    reconcile(workspace, apply=True)

    db_path = workspace / ".memory" / "memory.db"
    live_node = generate_file_node_id("notes/live.md")
    db = sqlite3.connect(str(db_path))
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(
        """
        CREATE TABLE memory_edges (
            edge_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            edge_type TEXT NOT NULL,
            FOREIGN KEY (source_id) REFERENCES memory_nodes(node_id) ON DELETE CASCADE,
            FOREIGN KEY (target_id) REFERENCES memory_nodes(node_id) ON DELETE CASCADE
        );
        CREATE TABLE memory_corrections (
            correction_id TEXT PRIMARY KEY,
            topic TEXT NOT NULL,
            message TEXT NOT NULL,
            related_node_id TEXT,
            FOREIGN KEY (related_node_id) REFERENCES memory_nodes(node_id) ON DELETE SET NULL
        );
        CREATE VIRTUAL TABLE memory_fts USING fts5(node_id, title, content);
        """
    )
    db.commit()
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute(
        "INSERT INTO memory_edges(edge_id, source_id, target_id, edge_type) VALUES (?, ?, ?, ?)",
        ("orphan-edge", "missing-node", live_node, "associates"),
    )
    db.execute(
        "INSERT INTO memory_corrections(correction_id, topic, message, related_node_id) "
        "VALUES (?, ?, ?, ?)",
        ("orphan-correction", "topic", "message", "missing-node"),
    )
    db.execute(
        "INSERT INTO memory_fts(node_id, title, content) VALUES (?, ?, ?)",
        ("missing-node", "legacy", "body"),
    )
    db.execute(
        "INSERT INTO memory_chunks"
        "(chunk_id, node_id, chunk_index, content_hash, content, title, created_at, updated_at) "
        "VALUES (?, ?, 0, ?, ?, ?, 1, 1)",
        ("orphan-chunk", "missing-node", "hash", "body", "chunk"),
    )
    db.execute(
        "INSERT INTO memory_chunks_fts(chunk_id, node_id, content, title) VALUES (?, ?, ?, ?)",
        ("orphan-chunk", "missing-node", "body", "chunk"),
    )
    db.execute(
        "INSERT INTO memory_index_jobs"
        "(job_id, node_id, content_hash, status, created_at, updated_at, attempts, error, index_revision) "
        "VALUES (?, ?, ?, 'pending', 1, 1, 0, '', 0)",
        ("orphan-job", "missing-node", "hash"),
    )
    db.commit()
    db.execute("PRAGMA foreign_keys = ON")
    assert db.execute("PRAGMA foreign_key_check").fetchall()
    db.close()

    dry_run = reconcile(workspace)
    assert dry_run["repair_orphans_requested"] is False
    assert dry_run["repair_orphan_candidate_count"] == 6
    assert dry_run["repair_orphan_count"] == 0
    assert dry_run["deletions"] == 0

    db = sqlite3.connect(str(db_path))
    assert db.execute("PRAGMA foreign_key_check").fetchall()
    assert db.execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0] == 1
    db.close()

    repaired = reconcile(workspace, apply=True, repair_orphans=True)
    assert repaired["repair_orphans_requested"] is True
    assert repaired["repair_orphan_candidate_count"] == 6
    assert repaired["repair_orphan_count"] == 6
    assert repaired["repair_orphans"] == {
        "edge_orphan_count": 1,
        "correction_orphan_count": 1,
        "legacy_fts_orphan_count": 1,
        "chunk_orphan_count": 1,
        "chunk_fts_orphan_count": 1,
        "index_job_orphan_count": 1,
        "total_count": 6,
    }
    assert repaired["backup"] is not None

    db = sqlite3.connect(str(db_path))
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0] == 0
    assert (
        db.execute(
            "SELECT related_node_id FROM memory_corrections WHERE correction_id = 'orphan-correction'"
        ).fetchone()[0]
        is None
    )
    assert db.execute("SELECT COUNT(*) FROM memory_fts WHERE node_id = 'missing-node'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM memory_chunks WHERE node_id = 'missing-node'").fetchone()[0] == 0
    assert (
        db.execute("SELECT COUNT(*) FROM memory_chunks_fts WHERE node_id = 'missing-node'").fetchone()[0]
        == 0
    )
    assert (
        db.execute("SELECT COUNT(*) FROM memory_index_jobs WHERE node_id = 'missing-node'").fetchone()[0]
        == 0
    )
    db.close()


@pytest.mark.parametrize(
    "argv",
    [
        ["--rebuild"],
        ["--prune-ineligible"],
        ["--repair-orphans"],
        ["--rebuild", "--prune-ineligible", "--repair-orphans"],
    ],
)
def test_reconcile_repair_flags_require_apply(argv: list[str], tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires --apply"):
        reconcile(
            tmp_path / "workspace",
            rebuild="--rebuild" in argv,
            prune_ineligible="--prune-ineligible" in argv,
            repair_orphans="--repair-orphans" in argv,
        )
    with pytest.raises(SystemExit) as error:
        reconcile_main(argv)
    assert error.value.code == 2
