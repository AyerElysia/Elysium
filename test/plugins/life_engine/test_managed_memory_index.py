"""Stable subject-file projection tests using only synthetic temporary data."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from plugins.life_engine.memory.indexing import (
    DocumentIdentityConflict,
    create_memory_schema,
    delete_document_rows_by_id,
    project_managed_document_rows,
    rekey_document_rows_by_id,
    transaction,
    upsert_document_rows,
)
from plugins.life_engine.memory.nodes import generate_file_node_id
from plugins.life_engine.memory.search import EmbeddingResult, search_memory_detailed
from plugins.life_engine.memory.worker import process_index_jobs
from plugins.life_engine.storage.memory.contracts import ManagedDocumentIndexSnapshot
from plugins.life_engine.storage.memory.local import create_local_memory_storage_bundle
from plugins.life_engine.storage.memory.mysql import MySQLDocumentIndexProjection
from plugins.life_engine.storage.memory.schema import (
    MEMORY_MIGRATIONS,
    MEMORY_SCHEMA_VERSION,
)
from plugins.life_engine.storage.migration.memory_copy import (
    TABLE_SPECS,
    normalize_target_row,
)
from plugins.life_engine.storage.migration.memory_export import (
    _create_source_schema,
    _source_row,
)
from test.plugins.life_engine.test_memory_snapshot_migration import _rows, _source


def _db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path), check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("CREATE TABLE IF NOT EXISTS memory_fts(node_id TEXT, title TEXT, content TEXT)")
    create_memory_schema(db)
    return db


def _snapshot(
    *, doc: str = "doc_one", version: str = "ver_one", path: str = "notes/alpha.md",
    revision: int = 1, binding: int = 1, content: str | None = "alpha current text",
    deleted: bool = False,
) -> ManagedDocumentIndexSnapshot:
    return ManagedDocumentIndexSnapshot(
        document_id=doc, version_id=version, path=path, document_revision=revision,
        binding_revision=binding, content_sha256=hashlib.sha256((content or "opaque").encode()).hexdigest(),
        content=content, deleted=deleted, title="Source title",
    )


def test_stable_node_rename_delete_reuse_retains_old_nodes_relations_and_replay(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    db = _db(path)
    legacy = upsert_document_rows(db, "notes/alpha.md", "legacy historical text")
    db.execute("CREATE TABLE relation_probe (edge_id TEXT PRIMARY KEY, source_id TEXT REFERENCES memory_nodes(node_id))")
    db.execute("INSERT INTO relation_probe VALUES ('old-edge', ?)", (legacy.node_id,))
    db.commit()
    first = _snapshot()
    result = project_managed_document_rows(db, first)
    assert result.node_id == "subject-file:doc_one"
    for protected in (result.node_id, legacy.node_id):
        with pytest.raises(DocumentIdentityConflict, match="cannot be hard deleted"):
            delete_document_rows_by_id(db, protected)
        with pytest.raises(DocumentIdentityConflict, match="cannot be rekeyed"):
            rekey_document_rows_by_id(db, protected, "notes/not-a-rename.md")
    assert db.execute("SELECT is_deleted FROM memory_nodes WHERE node_id = ?", (legacy.node_id,)).fetchone()[0] == 1
    with pytest.raises(DocumentIdentityConflict):
        upsert_document_rows(db, first.path, "must not revive legacy identity")
    renamed = replace(first, path="notes/renamed.md", document_revision=2)
    moved = project_managed_document_rows(db, renamed)
    assert moved.node_id == result.node_id
    deleted = replace(renamed, document_revision=3, binding_revision=2, content=None, deleted=True)
    project_managed_document_rows(db, deleted)
    new = _snapshot(doc="doc_new", version="ver_new", path=renamed.path, binding=3, content="new occupant")
    new_result = project_managed_document_rows(db, new)
    assert new_result.node_id != result.node_id
    assert db.execute("SELECT source_id FROM relation_probe").fetchone()[0] == legacy.node_id
    assert db.execute("SELECT COUNT(*) FROM memory_nodes").fetchone()[0] == 3
    assert db.execute("SELECT COUNT(*) FROM memory_chunks WHERE node_id = ?", (legacy.node_id,)).fetchone()[0] > 0
    db.close()
    db = _db(path)
    replay = project_managed_document_rows(db, deleted)
    assert replay.idempotent_replay and not replay.indexed
    assert db.execute("SELECT COUNT(*) FROM memory_nodes WHERE is_deleted = 0").fetchone()[0] == 1
    db.close()


def test_managed_projection_source_cas_and_opaque_current_bytes(tmp_path: Path) -> None:
    db = _db(tmp_path / "memory.db")
    first = _snapshot()
    project_managed_document_rows(db, first)
    before = tuple(db.execute("SELECT * FROM memory_nodes").fetchone())
    with pytest.raises(DocumentIdentityConflict, match="same managed revision"):
        project_managed_document_rows(db, replace(first, content="different text"))
    assert tuple(db.execute("SELECT * FROM memory_nodes").fetchone()) == before
    opaque = replace(first, path="notes/archive.bin", content=None, document_revision=2)
    assert not project_managed_document_rows(db, opaque).indexed
    with pytest.raises(DocumentIdentityConflict, match="stale"):
        project_managed_document_rows(db, first)
    assert db.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0] > 0
    assert db.execute("SELECT is_deleted FROM memory_nodes").fetchone()[0] == 1
    db.close()


@pytest.mark.asyncio
async def test_local_port_and_search_keep_exact_subject_refs_without_disk_dependency(tmp_path: Path) -> None:
    db = _db(tmp_path / "memory.db")
    port = create_local_memory_storage_bundle(lambda: db).document_index
    first = _snapshot()
    await port.project_managed_document(first)
    metadata = await port.get_document_metadata(first.path)
    assert metadata is not None and metadata.subject_document_id == first.document_id
    detailed = await search_memory_detailed(
        db, "alpha", None, top_k=3, enable_association=False, workspace_path=tmp_path / "absent",
    )
    assert len(detailed.results) == 1
    hit = detailed.results[0]
    assert (hit.node_id, hit.document_id, hit.version_id) == ("subject-file:doc_one", "doc_one", "ver_one")
    assert hit.document_revision == 1 and hit.binding_revision == 1
    assert hit.content_sha256 == first.content_sha256
    await port.project_managed_document(replace(first, document_revision=2, binding_revision=2, content=None, deleted=True))
    assert await port.get_document_metadata(first.path) is None
    nodes = await port.list_indexed_documents()
    assert len(nodes) == 1 and nodes[0].is_deleted and nodes[0].subject_document_id == "doc_one"
    historical = await create_local_memory_storage_bundle(lambda: db).legacy_graph.get_node_by_id("subject-file:doc_one")
    assert historical is not None and historical.is_deleted
    assert historical.subject_version_id == first.version_id
    db.close()


@pytest.mark.asyncio
async def test_stable_node_vector_worker_pins_source_and_does_not_export_retired_legacy(tmp_path: Path) -> None:
    db = _db(tmp_path / "memory.db")
    upsert_document_rows(db, "notes/alpha.md", "legacy old bytes")
    first = _snapshot()
    project_managed_document_rows(db, first)
    calls: list[dict[str, Any]] = []

    async def embed(texts: list[str]) -> EmbeddingResult:
        return EmbeddingResult([[1.0, 2.0] for _ in texts], model_name="fake/model")

    async def upsert(**kwargs: Any) -> None:
        calls.append(kwargs)

    report = await process_index_jobs(
        db, object(), embed_texts_func=embed, collection_upsert_func=upsert,
        reclaim_after=None, retry_failed=False,
    )
    assert report.completed and not report.failed
    assert calls and all(item["node_id"] != generate_file_node_id(first.path) for item in calls[0]["metadatas"])
    assert calls[0]["metadatas"][0]["document_id"] == first.document_id
    assert calls[0]["metadatas"][0]["version_id"] == first.version_id
    db.close()


def test_managed_identity_migration_keeps_retired_paths_without_current_claims() -> None:
    source = _source()
    first = _snapshot()
    project_managed_document_rows(source, first)
    project_managed_document_rows(source, replace(first, document_revision=2, binding_revision=2, content=None, deleted=True))
    project_managed_document_rows(source, _snapshot(doc="doc_new", version="ver_new", binding=3))
    rows = _rows(source, "memory_nodes")
    old = next(row for row in rows if row["subject_document_id"] == first.document_id)
    current = next(row for row in rows if row["subject_document_id"] == "doc_new")
    assert old["file_path"] == current["file_path"] == first.path
    assert old["file_path_sha256"] is None and current["file_path_sha256"] is not None
    assert normalize_target_row(TABLE_SPECS["memory_nodes"], old) == old
    destination = sqlite3.connect(":memory:")
    destination.row_factory = sqlite3.Row
    columns = _create_source_schema(source, destination)["memory_nodes"]
    restored = dict(zip(columns, _source_row("memory_nodes", old, columns), strict=True))
    assert restored["subject_document_id"] == first.document_id
    assert restored["subject_version_id"] == first.version_id
    assert restored["subject_document_revision"] == 2
    assert restored["document_content"] == first.content
    migration = MEMORY_MIGRATIONS[-1]
    assert migration.version == MEMORY_SCHEMA_VERSION == 15
    assert sum("PREPARE memory_identity_step FROM" in sql for sql in migration.statements) == 8
    assert migration.statements[-1] == "UPDATE memory_nodes SET file_path_sha256 = NULL WHERE is_deleted = TRUE"
    assert not any("DELETE FROM memory_" in sql for sql in migration.statements)
    destination.close()
    source.close()


class _SQLModelResult:
    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self.rowcount = cursor.rowcount
        self.rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []

    def mappings(self) -> _SQLModelResult:
        return self

    def one_or_none(self) -> dict[str, Any] | None:
        assert len(self.rows) <= 1
        return self.rows[0] if self.rows else None

    def all(self) -> list[dict[str, Any]]:
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class _MySQLDMLModel:
    """Execute MySQL adapter DML on temporary SQLite; not a MySQL lock test."""
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.statements: list[str] = []

    async def execute(self, statement: object, parameters: dict[str, Any] | None = None) -> _SQLModelResult:
        sql = str(statement).replace(" FOR UPDATE", "")
        self.statements.append(sql)
        return _SQLModelResult(self.db.execute(sql, parameters or {}))

    async def scalar(self, statement: object, parameters: dict[str, Any] | None = None) -> Any:
        result = await self.execute(statement, parameters)
        return next(iter(result.rows[0].values())) if result.rows else None


@pytest.mark.asyncio
async def test_mysql_stable_identity_dml_model_lifecycle_and_nullable_path_claim(tmp_path: Path) -> None:
    db = _db(tmp_path / "mysql-model.db")
    db.execute("ALTER TABLE memory_nodes ADD COLUMN file_path_sha256 TEXT")
    db.execute("ALTER TABLE memory_nodes ADD COLUMN legacy_fts_present INTEGER NOT NULL DEFAULT 0")
    db.execute("CREATE UNIQUE INDEX mysql_path_claim_model ON memory_nodes(file_path_sha256)")
    db.create_function("IF", 3, lambda condition, left, right: left if condition else right)
    legacy = upsert_document_rows(db, "notes/alpha.md", "legacy body")
    db.execute("UPDATE memory_nodes SET file_path_sha256 = ? WHERE node_id = ?", (hashlib.sha256(b"notes/alpha.md").hexdigest(), legacy.node_id))
    db.commit()
    model = _MySQLDMLModel(db)
    port = object.__new__(MySQLDocumentIndexProjection)

    async def write(operation: Any) -> Any:
        with transaction(db, immediate=True):
            return await operation(model)

    port._write = write
    first = _snapshot()
    initial = await port.project_managed_document(first)
    assert initial.node_id == "subject-file:doc_one"
    old = db.execute("SELECT * FROM memory_nodes WHERE node_id = ?", (legacy.node_id,)).fetchone()
    assert old["is_deleted"] and old["file_path_sha256"] is None
    with pytest.raises(DocumentIdentityConflict, match="cannot be hard deleted"):
        await port.delete_document(first.path)
    with pytest.raises(DocumentIdentityConflict, match="cannot be rekeyed"):
        await port.move_document(first.path, "notes/not-a-rename.md")
    renamed = replace(first, path="notes/renamed.md", document_revision=2)
    assert (await port.project_managed_document(renamed)).node_id == initial.node_id
    row = db.execute("SELECT * FROM memory_nodes WHERE node_id = ?", (initial.node_id,)).fetchone()
    assert row["file_path"] == renamed.path
    assert row["subject_document_revision"] == 2
    assert (await port.project_managed_document(renamed)).idempotent_replay
    deleted = replace(renamed, document_revision=3, binding_revision=2, content=None, deleted=True)
    await port.project_managed_document(deleted)
    await port.project_managed_document(_snapshot(doc="doc_new", path=renamed.path, binding=3))
    assert db.execute("SELECT COUNT(*) FROM memory_nodes WHERE file_path_sha256 IS NOT NULL").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM memory_nodes").fetchone()[0] == 3
    with pytest.raises(DocumentIdentityConflict):
        await port.project_managed_document(first)
    assert not any("DELETE FROM memory_nodes" in sql or "DELETE FROM memory_edges" in sql for sql in model.statements)
    db.close()
