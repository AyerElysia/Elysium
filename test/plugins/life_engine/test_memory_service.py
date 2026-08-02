"""life_engine memory_service 回归测试。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.memory import EdgeType, EmbeddingResult, LifeMemoryService
from plugins.life_engine.memory.nodes import (
    generate_file_node_id,
    generate_legacy_file_node_id,
)


@dataclass
class _DummyPlugin:
    config: LifeEngineConfig


class _FakeVectorService:
    """最小向量服务桩。"""

    def __init__(self, collection: Any) -> None:
        self._collection = collection
        self.calls = 0

    async def get_or_create_collection(self, name: str) -> Any:
        assert name == "life_memory"
        self.calls += 1
        return self._collection


def _make_service(tmp_path: Path) -> LifeMemoryService:
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    return LifeMemoryService(_DummyPlugin(config=config))


def test_get_chroma_collection_awaits_async_vector_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """应 await 异步 get_or_create_collection，并缓存集合实例。"""

    service = _make_service(tmp_path)
    fake_collection = SimpleNamespace(query=lambda **_: {"ids": [[]], "distances": [[]]})
    fake_vector_service = _FakeVectorService(fake_collection)

    monkeypatch.setattr(
        "src.kernel.vector_db.get_vector_db_service",
        lambda _path: fake_vector_service,
    )

    first = asyncio.run(service._get_chroma_collection())
    second = asyncio.run(service._get_chroma_collection())

    assert first is fake_collection
    assert second is fake_collection
    assert fake_vector_service.calls == 1


def test_workspace_path_override_works_with_path_input(tmp_path: Path) -> None:
    """当传入 Path 作为构造参数时，应使用该路径作为记忆库根目录。"""
    service = LifeMemoryService(tmp_path)
    db_path = service._get_db_path()
    assert db_path == tmp_path / ".memory" / "memory.db"


async def test_initialize_skips_chroma_when_vector_backend_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disabled vector backend must not enter native Chroma code."""
    service = LifeMemoryService(tmp_path, vector_backend_enabled=False)

    async def fail_if_called() -> Any:
        raise AssertionError("Chroma must not initialize while disabled")

    monkeypatch.setattr(service, "_get_chroma_collection", fail_if_called)

    await service.initialize()
    try:
        assert service._initialized is True
        assert service._chroma_collection is None
        results = await service.search_memory(
            "anything",
            return_bundles=False,
        )
        assert results == []
    finally:
        await service.close()


def test_migrate_file_path_keeps_edges_and_fts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """文件重命名后，节点 ID/边/FTS 应随之迁移。"""
    async def _run() -> None:
        service = _make_service(tmp_path)
        await service.initialize()

        class _FakeCollection:
            def get(self, **_: Any) -> dict[str, Any]:
                return {"ids": [], "embeddings": [], "documents": [], "metadatas": []}

            def upsert(self, **_: Any) -> None:
                return None

            def delete(self, **_: Any) -> None:
                return None

        async def _fake_get_collection() -> Any:
            return _FakeCollection()

        monkeypatch.setattr(service, "_get_chroma_collection", _fake_get_collection)

        source_node = await service.get_or_create_file_node(
            "notes/a.md",
            title="A",
            content="alpha content",
        )
        target_node = await service.get_or_create_file_node(
            "notes/b.md",
            title="B",
            content="beta content",
        )
        await service.create_or_update_edge(
            source_id=source_node.node_id,
            target_id=target_node.node_id,
            edge_type=EdgeType.RELATES,
            reason="test edge",
            strength=0.8,
            bidirectional=True,
        )

        migrated = await service.migrate_file_path("notes/a.md", "archive/a.md")
        assert migrated is True

        old_node = await service.get_node_by_file_path("notes/a.md")
        new_node = await service.get_node_by_file_path("archive/a.md")
        assert old_node is None
        assert new_node is not None
        assert new_node.file_path == "archive/a.md"

        edges = await service.get_edges_from(new_node.node_id)
        assert any(edge.target_id == target_node.node_id for edge in edges)

        cursor = service._db.cursor()
        cursor.execute("SELECT content FROM memory_fts WHERE node_id = ?", (new_node.node_id,))
        fts_row = cursor.fetchone()
        assert fts_row is not None
        assert "alpha content" in (fts_row["content"] or "")

        cursor.execute("SELECT content FROM memory_fts WHERE node_id = ?", (source_node.node_id,))
        assert cursor.fetchone() is None

    asyncio.run(_run())


def test_unified_document_api_updates_sqlite_without_embedding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """统一文档 API 只写 SQLite/FTS/outbox，不触发向量请求。"""

    async def _run() -> None:
        service = _make_service(tmp_path)
        fake_collection = SimpleNamespace()

        async def _fake_get_collection() -> Any:
            return fake_collection

        monkeypatch.setattr(service, "_get_chroma_collection", _fake_get_collection)
        await service.initialize()

        first = await service.upsert_document(
            "notes/indexed.md",
            "first searchable body",
            title="Indexed",
            source_mtime=12.0,
        )
        second = await service.upsert_document(
            "notes/indexed.md",
            "second searchable body",
            title="Indexed",
            source_mtime=13.0,
        )
        assert first.node_id == second.node_id
        assert second.chunks
        assert (await service.list_index_jobs())

        await service.upsert_document("notes/indexed.md", "", title="Indexed")
        assert await service.list_index_jobs() == []
        assert service._db.execute(
            "SELECT COUNT(*) FROM memory_chunks_fts WHERE node_id = ?",
            (first.node_id,),
        ).fetchone()[0] == 0

        await service.upsert_document("notes/source.md", "source body", title="Source")
        await service.upsert_document("notes/target.md", "target body", title="Target")
        with pytest.raises(FileExistsError, match="目标文档已存在"):
            await service.move_document("notes/source.md", "notes/target.md")
        assert await service.delete_document("notes/target.md") is True

    asyncio.run(_run())


async def test_service_run_index_worker_uses_chunk_collection_and_updates_nodes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(tmp_path)
    legacy_collection = SimpleNamespace()

    async def fake_legacy_collection() -> Any:
        return legacy_collection

    monkeypatch.setattr(service, "_get_chroma_collection", fake_legacy_collection)
    await service.initialize()
    indexed = await service.upsert_document(
        "notes/service-worker.md",
        "service worker body",
        title="Service Worker",
    )

    class ChunkCollection:
        metadata = {"collection_kind": "life_memory_chunk"}
        name = "life_memory_chunks_v1_fake_2"

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def upsert(self, **kwargs: Any) -> None:
            self.calls.append(kwargs)

    chunk_collection = ChunkCollection()
    resolver_calls: list[tuple[str, int]] = []

    async def fake_chunk_collection(path: str, model_name: str, dimension: int) -> Any:
        assert path.endswith("/.memory/chroma")
        resolver_calls.append((model_name, dimension))
        return chunk_collection

    monkeypatch.setattr(
        "plugins.life_engine.memory.service.get_chunk_collection",
        fake_chunk_collection,
    )

    async def embed(texts: Sequence[str]) -> EmbeddingResult:
        return EmbeddingResult(
            embeddings=[[1.0, 2.0] for _ in texts],
            model_name="service/fake",
        )

    report = await service.run_index_worker(embed_texts_func=embed)

    assert report.completed == (indexed.job_id,)
    assert resolver_calls == [("service/fake", 2)]
    assert len(chunk_collection.calls) == 1
    assert service._chunk_collection is chunk_collection
    assert service._db.execute(
        "SELECT embedding_synced FROM memory_nodes WHERE node_id = ?",
        (indexed.node_id,),
    ).fetchone()[0] == 1


async def test_service_restart_restores_persisted_chunk_collection_and_close_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(tmp_path)
    legacy_collection = SimpleNamespace()

    async def fake_legacy_collection() -> Any:
        return legacy_collection

    monkeypatch.setattr(service, "_get_chroma_collection", fake_legacy_collection)
    await service.initialize()
    indexed = await service.upsert_document("notes/restart.md", "restart body")

    class ChunkCollection:
        name = "life_memory_chunks_v1_service_fake_2"
        metadata = {
            "collection_kind": "life_memory_chunk",
            "chunk_index_version": 1,
            "embedding_model": "service/fake",
            "embedding_dimension": 2,
        }

        def upsert(self, **_: Any) -> None:
            return None

    chunk_collection = ChunkCollection()

    async def embed(texts: Sequence[str]) -> EmbeddingResult:
        return EmbeddingResult(
            embeddings=[[1.0, 2.0] for _ in texts],
            model_name="service/fake",
        )

    report = await service.run_index_worker(
        collection=chunk_collection,
        embed_texts_func=embed,
    )
    assert report.completed == (indexed.job_id,)
    await service.close()
    await service.close()
    assert service._db is None
    assert service._initialized is False

    restored = _make_service(tmp_path)
    monkeypatch.setattr(restored, "_get_chroma_collection", fake_legacy_collection)
    named_calls: list[str] = []

    async def fake_named_collection(path: str, name: str) -> Any:
        assert path.endswith("/.memory/chroma")
        named_calls.append(name)
        return chunk_collection

    monkeypatch.setattr(
        "plugins.life_engine.memory.service.get_named_chunk_collection",
        fake_named_collection,
    )
    await restored.initialize()

    assert named_calls == [chunk_collection.name]
    assert restored._chunk_collection is chunk_collection
    assert restored._chunk_collection_identity == ("service/fake", 2)
    await restored.close()


async def test_service_rejects_invalid_active_marker_before_backend_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.life_engine.memory import write_active_chunk_index_state

    legacy_collection = SimpleNamespace()

    async def fake_legacy_collection() -> Any:
        return legacy_collection

    service = _make_service(tmp_path)
    monkeypatch.setattr(service, "_get_chroma_collection", fake_legacy_collection)
    await service.initialize()
    write_active_chunk_index_state(
        service._db,
        "invalid-collection-name",
        "service/fake",
        2,
        1,
        now=10.0,
    )
    await service.close()

    named_calls: list[str] = []

    async def fake_named_collection(_path: str, name: str) -> Any:
        named_calls.append(name)
        return SimpleNamespace(name=name)

    monkeypatch.setattr(
        "plugins.life_engine.memory.service.get_named_chunk_collection",
        fake_named_collection,
    )
    restored = _make_service(tmp_path)
    monkeypatch.setattr(restored, "_get_chroma_collection", fake_legacy_collection)
    await restored.initialize()

    assert named_calls == []
    assert restored._chunk_collection is None
    assert restored._chroma_collection is legacy_collection
    await restored.close()


async def test_service_rejects_restored_collection_without_identity_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.life_engine.memory import write_active_chunk_index_state

    legacy_collection = SimpleNamespace()

    async def fake_legacy_collection() -> Any:
        return legacy_collection

    service = _make_service(tmp_path)
    monkeypatch.setattr(service, "_get_chroma_collection", fake_legacy_collection)
    await service.initialize()
    collection_name = "life_memory_chunks_v1_service_fake_2"
    write_active_chunk_index_state(
        service._db,
        collection_name,
        "service/fake",
        2,
        1,
        now=10.0,
    )
    await service.close()

    named_calls: list[str] = []

    async def fake_named_collection(_path: str, name: str) -> Any:
        named_calls.append(name)
        return SimpleNamespace(name=name, metadata={})

    monkeypatch.setattr(
        "plugins.life_engine.memory.service.get_named_chunk_collection",
        fake_named_collection,
    )
    restored = _make_service(tmp_path)
    monkeypatch.setattr(restored, "_get_chroma_collection", fake_legacy_collection)
    await restored.initialize()

    assert named_calls == [collection_name]
    assert restored._chunk_collection is None
    assert restored._chroma_collection is legacy_collection

    create_calls: list[tuple[str, int]] = []

    async def fake_create_collection(
        _path: str,
        model_name: str,
        dimension: int,
    ) -> Any:
        create_calls.append((model_name, dimension))
        return SimpleNamespace()

    monkeypatch.setattr(
        "plugins.life_engine.memory.service.get_chunk_collection",
        fake_create_collection,
    )
    indexed = await restored.upsert_document("notes/missing-active.md", "body")

    async def embed(texts: Sequence[str]) -> EmbeddingResult:
        return EmbeddingResult(
            embeddings=[[1.0, 2.0] for _ in texts],
            model_name="service/fake",
        )

    report = await restored.run_index_worker(embed_texts_func=embed)

    assert report.failed == (indexed.job_id,)
    assert report.errors[indexed.job_id] == "ValueError"
    assert named_calls == [collection_name, collection_name]
    assert create_calls == []
    await restored.close()


async def test_service_worker_does_not_block_concurrent_document_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(tmp_path)

    async def fake_legacy_collection() -> Any:
        return SimpleNamespace()

    monkeypatch.setattr(service, "_get_chroma_collection", fake_legacy_collection)
    await service.initialize()
    old = await service.upsert_document("notes/concurrent.md", "old body")
    started = asyncio.Event()
    release = asyncio.Event()
    upserts: list[dict[str, Any]] = []

    async def delayed_embed(texts: Sequence[str]) -> EmbeddingResult:
        started.set()
        await release.wait()
        return EmbeddingResult(
            embeddings=[[1.0, 2.0] for _ in texts],
            model_name="service/fake",
        )

    worker = asyncio.create_task(
        service.run_index_worker(
            collection=SimpleNamespace(upsert=lambda **kwargs: upserts.append(kwargs)),
            embed_texts_func=delayed_embed,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)
    new = await asyncio.wait_for(
        service.upsert_document("notes/concurrent.md", "new body"),
        timeout=1.0,
    )
    release.set()
    report = await worker

    assert old.job_id in report.stale
    assert report.upserted_chunks == 0
    assert upserts == []
    assert service._db.execute(
        "SELECT status FROM memory_index_jobs WHERE job_id = ?", (new.job_id,)
    ).fetchone()[0] == "pending"


async def test_service_health_snapshot_uses_isolated_committed_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(tmp_path)

    async def fake_legacy_collection() -> Any:
        return SimpleNamespace(count=lambda: 0, get=lambda **_: {"ids": []})

    monkeypatch.setattr(service, "_get_chroma_collection", fake_legacy_collection)
    await service.initialize()
    committed = await service.upsert_document("notes/committed.md", "committed")
    db = service._db
    assert db is not None

    db.execute(
        "INSERT INTO memory_nodes "
        "(node_id, node_type, file_path, content_hash, title, created_at, updated_at) "
        "VALUES (?, 'file', ?, 'transient', 'Transient', 2, 2)",
        (generate_file_node_id("notes/transient.md"), "notes/transient.md"),
    )
    assert db.in_transaction is True

    snapshot = await service.health_snapshot()

    assert snapshot["counts"]["nodes"] == 1
    assert snapshot["workspace"]["file_count"] == 0
    assert db.in_transaction is True
    assert db.execute("SELECT COUNT(*) FROM memory_nodes").fetchone()[0] == 2
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM memory_nodes").fetchone()[0] == 1
    assert committed.node_id == generate_file_node_id("notes/committed.md")
    await service.close()


async def test_get_or_create_document_uses_sqlite_outbox_without_chroma_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(tmp_path)
    collection = SimpleNamespace(upsert=lambda **_: pytest.fail("unexpected Chroma write"))

    async def fake_collection() -> Any:
        return collection

    monkeypatch.setattr(service, "_get_chroma_collection", fake_collection)
    await service.initialize()

    node = await service.get_or_create_file_node(
        "notes/compat.md",
        title="Compat",
        content="transaction backed content",
    )

    assert service._db.execute(
        "SELECT COUNT(*) FROM memory_chunks WHERE node_id = ?", (node.node_id,)
    ).fetchone()[0] > 0
    assert service._db.execute(
        "SELECT COUNT(*) FROM memory_chunks_fts WHERE node_id = ?", (node.node_id,)
    ).fetchone()[0] > 0
    assert service._db.execute(
        "SELECT status FROM memory_index_jobs WHERE node_id = ?", (node.node_id,)
    ).fetchone()[0] == "pending"


async def test_legacy_node_lookup_is_read_only(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    await service.initialize()
    path = "notes/legacy.md"
    legacy_id = generate_legacy_file_node_id(f"./{path}")
    service._db.execute(
        """
        INSERT INTO memory_nodes (
            node_id, node_type, file_path, content_hash, title,
            created_at, updated_at, embedding_synced
        ) VALUES (?, 'file', ?, '', 'legacy', 1, 1, 0)
        """,
        (legacy_id, path),
    )
    service._db.commit()
    before = [tuple(row) for row in service._db.execute("SELECT * FROM memory_nodes")]

    node = await service.get_node_by_file_path(path)

    assert node is not None
    assert node.node_id == legacy_id
    assert [tuple(row) for row in service._db.execute("SELECT * FROM memory_nodes")] == before


async def test_read_rejects_noncanonical_stored_path(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    await service.initialize()
    path = "notes/noncanonical.md"
    node_id = generate_file_node_id(path)
    service._db.execute(
        """
        INSERT INTO memory_nodes (
            node_id, node_type, file_path, content_hash, title,
            created_at, updated_at, embedding_synced
        ) VALUES (?, 'file', './notes/noncanonical.md', '', 'bad', 1, 1, 0)
        """,
        (node_id,),
    )
    service._db.commit()

    assert await service.get_node_by_file_path(path) is None


async def test_record_memory_correction_rejects_absolute_related_path(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    await service.initialize()

    with pytest.raises(ValueError, match="absolute_path"):
        await service.record_memory_correction(
            "topic",
            "correction",
            related_paths=["/notes/a.md"],
        )


# ── 启动恢复 ────────────────────────────────────────────────


async def test_startup_recovery_indexes_unindexed_files(tmp_path: Path) -> None:
    """工作区中已存在但尚未入索引的文件，启动时应该被发现并入队。"""
    service = _make_service(tmp_path)
    await service.initialize()

    # 模拟"外部写入"的文件——不在索引里，但工作区存在
    workspace_file = tmp_path / "notes" / "external_note.md"
    workspace_file.parent.mkdir(parents=True, exist_ok=True)
    workspace_file.write_text("# 外部写入的笔记\n这是一段内容", encoding="utf-8")

    # 确认此时工作区有文件，但索引里没有
    rows = service._db.execute(
        "SELECT node_id FROM memory_nodes WHERE file_path = 'notes/external_note.md'"
    ).fetchall()
    assert len(rows) == 0, "文件不应在启动前就被索引"

    # 触发启动恢复
    await service._startup_recovery()

    # 验证文件已被入队索引
    rows = service._db.execute(
        "SELECT node_id, file_path FROM memory_nodes WHERE file_path = 'notes/external_note.md'"
    ).fetchall()
    assert len(rows) == 1, "文件应该被启动恢复扫描到"
    assert rows[0]["file_path"] == "notes/external_note.md"

    # 验证索引任务已入队
    job_rows = service._db.execute(
        "SELECT node_id, status FROM memory_index_jobs WHERE status = 'pending'"
    ).fetchall()
    assert any(r["node_id"] == rows[0]["node_id"] for r in job_rows), "文件应产生待处理索引任务"


async def test_startup_recovery_marks_ghost_nodes_as_deleted(tmp_path: Path) -> None:
    """索引里有但工作区已删除的文件，应被标记为 is_deleted=1。"""
    service = _make_service(tmp_path)
    await service.initialize()

    # 创建一个节点，不创建对应文件（ghost 节点）
    ghost_node_id = "file:0000000000001"
    service._db.execute(
        """
        INSERT INTO memory_nodes
        (node_id, node_type, file_path, content_hash, title, created_at, updated_at, is_deleted)
        VALUES (?, 'file', 'notes/deleted_note.md', '', 'deleted', 1, 1, 0)
        """,
        (ghost_node_id,),
    )
    service._db.commit()

    assert service._db.execute(
        "SELECT is_deleted FROM memory_nodes WHERE node_id = ?", (ghost_node_id,)
    ).fetchone()["is_deleted"] == 0, "启动前不应标记删除"

    # 触发启动恢复
    await service._startup_recovery()

    # 验证 ghost 节点已被标记删除
    row = service._db.execute(
        "SELECT is_deleted FROM memory_nodes WHERE node_id = ?", (ghost_node_id,)
    ).fetchone()
    assert row is not None, "ghost 节点应保留在表中"
    assert row["is_deleted"] == 1, "ghost 节点应被标记为已删除"


async def test_startup_recovery_deletes_orphan_edges(tmp_path: Path) -> None:
    """指向已删除节点的孤立边，应该被启动恢复清理掉。"""
    service = _make_service(tmp_path)
    await service.initialize()

    # 创建两个节点，然后只删除一个——制造孤立边
    node_a = "file:000000000000a"
    node_b = "file:000000000000b"
    service._db.execute(
        "INSERT INTO memory_nodes (node_id, node_type, file_path, created_at, updated_at) VALUES (?, 'file', 'notes/a.md', 1, 1)",
        (node_a,),
    )
    service._db.execute(
        "INSERT INTO memory_nodes (node_id, node_type, file_path, created_at, updated_at) VALUES (?, 'file', 'notes/b.md', 1, 1)",
        (node_b,),
    )
    service._db.commit()

    # 创建边：A→B
    service._db.execute(
        "INSERT INTO memory_edges (edge_id, source_id, target_id, edge_type, weight, created_at) VALUES (?, ?, ?, 'relates', 0.5, 1)",
        ("edge_a_to_b", node_a, node_b),
    )
    service._db.commit()
    # 确保 service 连接完全释放锁
    _ = service._db.execute("SELECT 1").fetchone()
    # 创建孤立边：X→B，其中 X 不存在
    # 用独立的 sqlite3 连接绕过 service 的 FK enforcement
    import sqlite3

    db_path = service._get_db_path()
    with sqlite3.connect(db_path, timeout=10.0) as raw_conn:
        raw_conn.execute("PRAGMA foreign_keys = OFF")
        raw_conn.execute(
            "INSERT INTO memory_edges (edge_id, source_id, target_id, edge_type, weight, created_at) VALUES (?, ?, ?, 'relates', 0.5, 1)",
            ("edge_orphan", "file:000000000000x", node_b),
        )
        raw_conn.commit()

    # 确认孤立边存在
    orphan_count = service._db.execute(
        "SELECT COUNT(*) FROM memory_edges WHERE edge_id = 'edge_orphan'"
    ).fetchone()[0]
    assert orphan_count == 1

    # 触发启动恢复
    await service._startup_recovery()

    # 验证孤立边已删除
    orphan_count = service._db.execute(
        "SELECT COUNT(*) FROM memory_edges WHERE edge_id = 'edge_orphan'"
    ).fetchone()[0]
    assert orphan_count == 0, "孤立边应该被启动恢复删除"

    # 正常边仍然保留
    normal_count = service._db.execute(
        "SELECT COUNT(*) FROM memory_edges WHERE edge_id = 'edge_a_to_b'"
    ).fetchone()[0]
    assert normal_count == 1, "正常边应该保留"


async def test_startup_recovery_noop_when_everything_clean(tmp_path: Path) -> None:
    """索引和工作区完全一致时，不应有任何副作用。"""
    service = _make_service(tmp_path)
    await service.initialize()

    # 创建一个与工作区文件对应的节点
    workspace_file = tmp_path / "notes" / "clean.md"
    workspace_file.parent.mkdir(parents=True, exist_ok=True)
    workspace_file.write_text("# 干净的文件\n内容", encoding="utf-8")

    node_id = "file:000000000000c"
    service._db.execute(
        "INSERT INTO memory_nodes (node_id, node_type, file_path, created_at, updated_at, is_deleted) VALUES (?, 'file', 'notes/clean.md', 1, 1, 0)",
        (node_id,),
    )
    service._db.commit()
    initial_node_count = service._db.execute("SELECT COUNT(*) FROM memory_nodes").fetchone()[0]

    await service._startup_recovery()

    # 不应新增节点，不应标记任何删除，不应删除任何边
    final_node_count = service._db.execute("SELECT COUNT(*) FROM memory_nodes").fetchone()[0]
    assert final_node_count == initial_node_count


async def test_startup_recovery_requeues_unembedded_nodes_without_jobs(tmp_path: Path) -> None:
    """有节点但从无入队任务（历史遗留）的未同步节点，启动时应被补入队。"""
    service = _make_service(tmp_path)
    await service.initialize()

    # 创建工作区文件
    workspace_file = tmp_path / "notes" / "legacy_note.md"
    workspace_file.parent.mkdir(parents=True, exist_ok=True)
    workspace_file.write_text("# 历史遗留笔记\n内容", encoding="utf-8")

    # 直接插入节点（模拟旧版本写入，未入队）
    node_id = "file:legacy000001"
    service._db.execute(
        """INSERT INTO memory_nodes
           (node_id, node_type, file_path, content_hash, created_at, updated_at, embedding_synced)
           VALUES (?, 'file', 'notes/legacy_note.md', 'abc123hash', 1, 1, 0)""",
        (node_id,),
    )
    service._db.commit()

    # 确认没有对应任务
    job_count = service._db.execute(
        "SELECT COUNT(*) FROM memory_index_jobs WHERE node_id = ?", (node_id,)
    ).fetchone()[0]
    assert job_count == 0, "遗留节点不应有任何任务"

    # 触发启动恢复
    await service._startup_recovery()

    # 应该补入队了一个 pending 任务
    pending_count = service._db.execute(
        "SELECT COUNT(*) FROM memory_index_jobs WHERE node_id = ? AND status = 'pending'",
        (node_id,),
    ).fetchone()[0]
    assert pending_count == 1, "遗留节点应被补入队一个 pending 任务"


# ── 启动恢复的执行位置 ──────────────────────────────────────


async def test_startup_recovery_never_runs_on_the_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启动恢复的工作量与工作区规模成正比，必须完全离开事件循环。

    三次全表扫描、一次目录树遍历、每个待补文件一次读取——原实现全部直接跑在
    循环上，启动期间适配器收发、心跳和调度一起停摆。这里把目录遍历和文件读取
    都替换成"记录线程名并真的睡一会儿"的桩：如果它们回到循环上，并发运行的
    计时协程就推不动。
    """
    import threading
    import time

    from plugins.life_engine.memory import service as service_module

    service = _make_service(tmp_path)
    await service.initialize()

    for index in range(3):
        note = tmp_path / "notes" / f"n{index}.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(f"# 笔记 {index}\n内容", encoding="utf-8")

    loop_thread = threading.current_thread().name
    scan_threads: list[str] = []
    read_threads: list[str] = []

    real_scan = service_module.scan_workspace_documents
    real_read = service_module._read_documents

    def _slow_scan(workspace: Any) -> Any:
        """记录扫描所在线程，并模拟一次真实规模的目录遍历。"""
        scan_threads.append(threading.current_thread().name)
        time.sleep(0.15)
        return real_scan(workspace)

    def _slow_read(workspace: Any, paths: Any) -> Any:
        """记录文件读取所在线程，并模拟一次真实规模的批量读。"""
        read_threads.append(threading.current_thread().name)
        time.sleep(0.15)
        return real_read(workspace, paths)

    monkeypatch.setattr(service_module, "scan_workspace_documents", _slow_scan)
    monkeypatch.setattr(service_module, "_read_documents", _slow_read)

    ticks = 0

    async def _tick() -> None:
        """恢复期间持续推进，用来证明循环没有被占住。"""
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.ensure_future(_tick())
    try:
        await service._startup_recovery()
    finally:
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass
        await service.close()

    # 桩确实被调用了，否则下面的断言是空的
    assert scan_threads, "启动恢复没有扫描工作区"
    assert read_threads, "启动恢复没有读取待补索引的文件"
    assert all(name != loop_thread for name in scan_threads + read_threads)

    # 至少 0.3s 的同步耗时，循环仍然在以 10ms 的节奏推进
    assert ticks >= 10, f"事件循环在启动恢复期间被阻塞（仅推进 {ticks} 次）"
