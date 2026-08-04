"""记忆索引修复模块契约测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.memory import LifeMemoryService
from plugins.life_engine.memory.indexing import (
    canonical_file_node_id,
    upsert_document_rows,
)
from plugins.life_engine.memory.repair import (
    ensure_self_loop_guards,
    repair_document_index,
)


class _FakeCollection:
    def query(self, **_: Any) -> dict[str, list[list[Any]]]:
        return {"ids": [[]], "distances": [[]]}

    def get(self, **_: Any) -> dict[str, list[Any]]:
        return {"ids": [], "embeddings": [], "documents": [], "metadatas": []}

    def upsert(self, **_: Any) -> None:
        return None

    def delete(self, **_: Any) -> None:
        return None


async def _make_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> LifeMemoryService:
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    plugin = type("DummyPlugin", (), {"config": config})()
    service = LifeMemoryService(plugin)

    async def _fake_get_collection() -> Any:
        return _FakeCollection()

    monkeypatch.setattr(service, "_get_chroma_collection", _fake_get_collection)
    await service.initialize()
    return service


def _stored_hash(db: sqlite3.Connection, file_path: str) -> str | None:
    row = db.execute(
        "SELECT content_hash FROM memory_nodes WHERE file_path = ? AND is_deleted = 0",
        (file_path,),
    ).fetchone()
    return None if row is None else row["content_hash"]


def test_repair_rebuilds_drifted_documents_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        service = await _make_service(tmp_path, monkeypatch)
        db = service._require_local_db()
        note = tmp_path / "notes" / "life.md"
        note.parent.mkdir(parents=True)
        note.write_text("# 旧版本\n\n她最初的理解。\n", encoding="utf-8")
        upsert_document_rows(
            db, "notes/life.md", "# 旧版本\n\n她最初的理解。\n", "life"
        )

        note.write_text("# 新版本\n\n世界已经变化，她也知道了。\n", encoding="utf-8")
        from plugins.life_engine.memory.nodes import compute_content_hash

        assert _stored_hash(db, "notes/life.md") != compute_content_hash(
            "# 新版本\n\n世界已经变化，她也知道了。\n"
        )

        first = repair_document_index(db, tmp_path)
        assert first.rebuilt_documents == 1
        assert first.rebuilt_paths == ("notes/life.md",)
        assert first.integrity_check == "ok"
        assert first.foreign_key_errors == 0
        assert first.pending_jobs >= 1
        assert _stored_hash(db, "notes/life.md") == compute_content_hash(
            "# 新版本\n\n世界已经变化，她也知道了。\n"
        )

        second = repair_document_index(db, tmp_path)
        assert second.rebuilt_documents == 0
        assert second.integrity_check == "ok"

        await service.close()

    import asyncio

    asyncio.run(_run())


def test_repair_empty_document_never_enqueues_vector_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        service = await _make_service(tmp_path, monkeypatch)
        db = service._require_local_db()
        empty = tmp_path / "notes" / "empty.md"
        empty.parent.mkdir(parents=True)
        empty.write_text("# 曾经有内容\n", encoding="utf-8")
        upsert_document_rows(db, "notes/empty.md", "# 曾经有内容\n", "empty")

        empty.write_text("", encoding="utf-8")
        from plugins.life_engine.memory.indexing import canonical_file_node_id

        _, node_id = canonical_file_node_id("notes/empty.md")
        report = repair_document_index(db, tmp_path)
        assert report.rebuilt_documents == 1
        remaining = db.execute(
            "SELECT COUNT(*) FROM memory_index_jobs WHERE node_id = ?",
            (node_id,),
        ).fetchone()[0]
        assert remaining == 0
        assert _stored_hash(db, "notes/empty.md") is None

        await service.close()

    import asyncio

    asyncio.run(_run())


def test_repair_removes_legacy_self_loops_and_blocks_new_ones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        service = await _make_service(tmp_path, monkeypatch)
        db = service._require_local_db()
        note = tmp_path / "notes" / "loop.md"
        note.parent.mkdir(parents=True)
        note.write_text("# 自环遗留\n", encoding="utf-8")
        upsert_document_rows(db, "notes/loop.md", "# 自环遗留\n", "loop")
        _, node_id = canonical_file_node_id("notes/loop.md")

        # 模拟防线建立之前写入的历史自环：先移除触发器再插入。
        db.execute("DROP TRIGGER IF EXISTS memory_edges_no_self_loop_insert")
        db.execute("DROP TRIGGER IF EXISTS memory_edges_no_self_loop_update")
        db.execute(
            "INSERT INTO memory_edges "
            "(edge_id, source_id, target_id, edge_type, weight, created_at) "
            "VALUES ('legacy-loop', ?, ?, 'continues', 0.5, 1.0)",
            (node_id, node_id),
        )
        report = repair_document_index(db, tmp_path)
        assert report.removed_self_loops == 1
        assert report.remaining_self_loops == 0

        with pytest.raises(sqlite3.IntegrityError, match="MemoryEdgeSelfLoop"):
            db.execute(
                "INSERT INTO memory_edges "
                "(edge_id, source_id, target_id, edge_type, weight, created_at) "
                "VALUES ('new-loop', ?, ?, 'continues', 0.5, 1.0)",
                (node_id, node_id),
            )
        db.rollback()

        ensure_self_loop_guards(db)
        await service.close()

    import asyncio

    asyncio.run(_run())
