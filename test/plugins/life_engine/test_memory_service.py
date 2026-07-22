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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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
