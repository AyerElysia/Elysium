"""Life Engine 记忆查询只读止损测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from plugins.life_engine.memory.edges import EdgeType
from plugins.life_engine.memory.nodes import MemoryNode
from plugins.life_engine.memory.search import SearchResult, search_memory, vector_search
from plugins.life_engine.memory.service import LifeMemoryService


class _FakeCollection:
    def __init__(self, ids: list[str], distances: list[float] | None = None) -> None:
        self.ids = ids
        self.distances = distances or [0.1 for _ in ids]
        self.delete_calls: list[list[str]] = []

    def query(self, **_: Any) -> dict[str, list[list[Any]]]:
        return {"ids": [self.ids], "distances": [self.distances]}

    def delete(self, ids: list[str]) -> None:
        self.delete_calls.append(ids)


def _snapshot(
    db: sqlite3.Connection,
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    cursor = db.cursor()
    cursor.execute(
        """
        SELECT node_id, access_count, activation_strength, last_accessed_at
        FROM memory_nodes ORDER BY node_id
        """
    )
    nodes = [tuple(row) for row in cursor.fetchall()]
    cursor.execute(
        """
        SELECT edge_id, source_id, target_id, edge_type, weight, base_strength,
               reinforcement, activation_count, last_activated_at, reason, created_at,
               bidirectional
        FROM memory_edges ORDER BY edge_id
        """
    )
    edges = [tuple(row) for row in cursor.fetchall()]
    return nodes, edges


async def _make_service(tmp_path: Path) -> LifeMemoryService:
    service = LifeMemoryService(tmp_path)
    await service.initialize()
    return service


async def _seed_legacy_migration_node(
    service: LifeMemoryService,
    file_path: str,
    *,
    title: str = "",
    content: str = "",
) -> MemoryNode:
    """Seed one historical graph row through the offline-migration adapter."""
    return await service._require_memory_storage().legacy_graph.get_or_create_file_node(
        file_path,
        title,
        content,
    )


async def _seed_legacy_migration_edge(
    service: LifeMemoryService,
    source: MemoryNode,
    target: MemoryNode,
    edge_type: EdgeType,
    *,
    reason: str,
    strength: float,
    bidirectional: bool,
) -> None:
    """Seed historical edge evidence without reopening a runtime write path."""
    await _seed_legacy_migration_edge_ids(
        service,
        source.node_id,
        target.node_id,
        edge_type,
        reason=reason,
        strength=strength,
        bidirectional=bidirectional,
    )


async def _seed_legacy_migration_edge_ids(
    service: LifeMemoryService,
    source_id: str,
    target_id: str,
    edge_type: EdgeType,
    *,
    reason: str,
    strength: float,
    bidirectional: bool,
) -> None:
    """Seed an edge when a malformed historical node has no modern DTO."""
    await service._require_memory_storage().legacy_graph.create_or_update_edge(
        source_id,
        target_id,
        edge_type.value,
        reason=reason,
        strength=strength,
        bidirectional=bidirectional,
    )


def _seed_legacy_migration_unindexable_node(
    service: LifeMemoryService,
    *,
    node_id: str,
    file_path: str,
) -> None:
    """Seed malformed pre-validation evidence that the modern adapter rejects."""
    service._db.execute(
        """
        INSERT INTO memory_nodes (
            node_id, node_type, file_path, content_hash, title,
            created_at, updated_at, embedding_synced
        ) VALUES (?, 'file', ?, '', 'migration-fixture', 1.0, 1.0, 0)
        """,
        (node_id, file_path),
    )
    service._db.commit()


async def test_search_is_read_only_and_does_not_delete_stale_vectors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await _make_service(tmp_path)
    node_a = await _seed_legacy_migration_node(
        service,
        "notes/a.md",
        title="A",
        content="alpha memory",
    )
    node_b = await _seed_legacy_migration_node(
        service,
        "notes/b.md",
        title="B",
        content="beta memory",
    )
    await _seed_legacy_migration_edge(
        service,
        node_a,
        node_b,
        EdgeType.RELATES,
        reason="explicit relation",
        strength=0.9,
        bidirectional=True,
    )
    await _seed_legacy_migration_edge(
        service,
        node_a,
        node_b,
        EdgeType.ASSOCIATES,
        reason="old learned relation",
        strength=0.9,
        bidirectional=False,
    )

    collection = _FakeCollection([node_a.node_id, "stale-vector-id"])

    async def _fake_embed(_: str) -> list[float]:
        return [0.1]

    monkeypatch.setattr("plugins.life_engine.memory.search.embed_text", _fake_embed)
    before = _snapshot(service._db)
    increment_access = AsyncMock()
    reinforce = AsyncMock()

    results = await search_memory(
        db=service._db,
        query="alpha",
        collection=collection,
        top_k=1,
        increment_access_func=increment_access,
        reinforce_coactivated_func=reinforce,
    )

    assert results
    assert increment_access.await_count == 0
    assert reinforce.await_count == 0
    assert collection.delete_calls == []
    assert _snapshot(service._db) == before


async def test_spread_activation_defaults_to_explicit_relations(tmp_path: Path) -> None:
    service = await _make_service(tmp_path)
    node_a = await _seed_legacy_migration_node(service, "notes/a.md", title="A")
    node_b = await _seed_legacy_migration_node(service, "notes/b.md", title="B")
    node_c = await _seed_legacy_migration_node(service, "notes/c.md", title="C")
    await _seed_legacy_migration_edge(
        service,
        node_a,
        node_b,
        EdgeType.ASSOCIATES,
        reason="learned",
        strength=1.0,
        bidirectional=False,
    )
    await _seed_legacy_migration_edge(
        service,
        node_a,
        node_c,
        EdgeType.RELATES,
        reason="explicit",
        strength=1.0,
        bidirectional=False,
    )

    default_results = await service.spread_activation([node_a.node_id], max_depth=1)
    default_ids = {node_id for node_id, *_ in default_results}
    assert node_b.node_id not in default_ids
    assert node_c.node_id in default_ids

    associates_results = await service.spread_activation(
        [node_a.node_id],
        max_depth=1,
        allowed_edge_types=[EdgeType.ASSOCIATES],
    )
    assert node_b.node_id in {node_id for node_id, *_ in associates_results}


async def test_vector_search_filters_stale_ids_without_collection_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = _FakeCollection(["live", "stale"])

    async def _fake_embed(_: str) -> list[float]:
        return [0.1]

    async def _filter(
        scores: list[tuple[str, float]],
    ) -> tuple[list[tuple[str, float]], list[str]]:
        return [scores[0]], ["stale"]

    monkeypatch.setattr("plugins.life_engine.memory.search.embed_text", _fake_embed)
    result = await vector_search("query", collection, filter_existing_func=_filter)

    assert result == [("live", pytest.approx(result[0][1]))]
    assert collection.delete_calls == []


async def test_dream_walk_default_does_not_create_or_update_edges(
    tmp_path: Path,
) -> None:
    service = await _make_service(tmp_path)
    node_a = await _seed_legacy_migration_node(service, "notes/a.md", title="A")
    node_b = await _seed_legacy_migration_node(service, "notes/b.md", title="B")
    await _seed_legacy_migration_edge(
        service,
        node_a,
        node_b,
        EdgeType.RELATES,
        reason="walkable",
        strength=1.0,
        bidirectional=False,
    )
    before = _snapshot(service._db)

    result = await service.dream_walk(
        num_seeds=1,
        seed_ids=[node_a.node_id],
        max_depth=1,
        decay_factor=1.0,
    )

    assert result["new_edges_created"] == 0
    assert _snapshot(service._db) == before


async def test_dream_walk_rejects_legacy_relation_mutation(tmp_path: Path) -> None:
    service = await _make_service(tmp_path)
    node_a = await _seed_legacy_migration_node(service, "notes/a.md", title="A")
    node_b = await _seed_legacy_migration_node(service, "notes/b.md", title="B")
    await _seed_legacy_migration_edge(
        service,
        node_a,
        node_b,
        EdgeType.RELATES,
        reason="walkable",
        strength=1.0,
        bidirectional=False,
    )

    before = _snapshot(service._db)

    with pytest.raises(RuntimeError, match="LegacyDreamMutationRetired"):
        await service.dream_walk(
            num_seeds=1,
            seed_ids=[node_a.node_id],
            max_depth=1,
            decay_factor=1.0,
            persist_learning=True,
        )

    assert _snapshot(service._db) == before
    cursor = service._db.cursor()
    cursor.execute(
        "SELECT COUNT(*) AS count FROM memory_edges WHERE edge_type = ?",
        (EdgeType.ASSOCIATES.value,),
    )
    assert cursor.fetchone()["count"] == 0


async def test_default_canonical_resolver_is_read_only_and_does_not_scan_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await _make_service(tmp_path)
    current_path = tmp_path / "notes" / "topic.md"
    current_path.parent.mkdir(parents=True)
    current_path.write_text("current", encoding="utf-8")

    def _unexpected_scan(_: str) -> str | None:
        raise AssertionError("default canonical resolution must not scan the workspace")

    monkeypatch.setattr(service, "_find_missing_file_candidate", _unexpected_scan)
    monkeypatch.setattr(
        Path,
        "rglob",
        lambda *_: (_ for _ in ()).throw(AssertionError("workspace scan is forbidden")),
    )
    before = _snapshot(service._db)
    result = await service.resolve_canonical_path("notes/topic_research.md")

    assert result["resolved"] is False
    assert result["resolved_path"] == "notes/topic_research.md"
    assert _snapshot(service._db) == before


async def test_default_canonical_resolver_reads_legacy_lineage_without_migration(
    tmp_path: Path,
) -> None:
    service = await _make_service(tmp_path)
    old_path = "./notes/legacy.md"
    current_path = "notes/current.md"
    current_file = tmp_path / current_path
    current_file.parent.mkdir(parents=True)
    current_file.write_text("current", encoding="utf-8")

    source = await _seed_legacy_migration_node(
        service,
        old_path,
        title="legacy",
    )
    target = await _seed_legacy_migration_node(
        service,
        current_path,
        title="current",
        content="current",
    )
    await _seed_legacy_migration_edge(
        service,
        source,
        target,
        EdgeType.RENAMES,
        reason="legacy lineage",
        strength=0.8,
        bidirectional=False,
    )

    before = _snapshot(service._db)
    result = await service.resolve_canonical_path(old_path)

    assert result["resolved"] is True
    assert result["resolved_path"] == current_path
    assert _snapshot(service._db) == before


async def test_heuristic_resolution_is_opt_in_and_read_only_without_persistence(
    tmp_path: Path,
) -> None:
    service = await _make_service(tmp_path)
    current_path = tmp_path / "notes" / "topic.md"
    current_path.parent.mkdir(parents=True)
    current_path.write_text("current", encoding="utf-8")

    before = _snapshot(service._db)
    result = await service.resolve_canonical_path(
        "notes/topic_research.md",
        allow_heuristic=True,
    )

    assert result["resolved"] is True
    assert result["resolved_path"] == "notes/topic.md"
    assert _snapshot(service._db) == before


async def test_heuristic_resolution_requires_a_unique_candidate(tmp_path: Path) -> None:
    service = await _make_service(tmp_path)
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "topic.md").write_text("first", encoding="utf-8")
    diaries = tmp_path / "diaries"
    diaries.mkdir()
    (diaries / "topic.md").write_text("second", encoding="utf-8")

    result = await service.resolve_canonical_path(
        "notes/topic_research.md",
        allow_heuristic=True,
    )

    assert result["resolved"] is False


async def test_canonical_resolver_rejects_runtime_path_without_reading_graph(
    tmp_path: Path,
) -> None:
    service = await _make_service(tmp_path)
    before = _snapshot(service._db)

    result = await service.resolve_canonical_path(
        "runtime/state.json", allow_heuristic=True
    )

    assert result["resolved"] is False
    assert result["note"] == "不是可用于记忆演化的文档: unsupported_suffix"
    assert _snapshot(service._db) == before


async def test_memory_bundles_exclude_legacy_runtime_lineage_nodes(
    tmp_path: Path,
) -> None:
    service = await _make_service(tmp_path)
    memory_path = "notes/kept.md"
    file_path = tmp_path / memory_path
    file_path.parent.mkdir(parents=True)
    file_path.write_text("kept", encoding="utf-8")
    primary = await _seed_legacy_migration_node(
        service,
        memory_path,
        title="Kept",
        content="kept",
    )
    runtime_out_id = "file:legacy-runtime-out"
    runtime_in_id = "file:legacy-runtime-in"
    _seed_legacy_migration_unindexable_node(
        service,
        node_id=runtime_out_id,
        file_path="runtime/outbound.json",
    )
    _seed_legacy_migration_unindexable_node(
        service,
        node_id=runtime_in_id,
        file_path="runtime/inbound.json",
    )
    await _seed_legacy_migration_edge_ids(
        service,
        primary.node_id,
        runtime_out_id,
        EdgeType.RENAMES,
        reason="legacy runtime target",
        strength=0.9,
        bidirectional=False,
    )
    await _seed_legacy_migration_edge_ids(
        service,
        runtime_in_id,
        primary.node_id,
        EdgeType.REFINES,
        reason="legacy runtime source",
        strength=0.9,
        bidirectional=False,
    )

    bundles = await service.build_memory_bundles(
        "kept",
        [
            SearchResult(
                file_path=memory_path,
                title="Kept",
                snippet="kept",
                relevance=1.0,
                source="direct",
            )
        ],
    )

    assert len(bundles) == 1
    bundle = bundles[0]
    assert bundle.primary_path == memory_path
    assert {item.file_path for item in bundle.evidence} == {memory_path}
    assert bundle.history_trace == []


async def test_build_memory_bundles_does_not_use_filename_heuristic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await _make_service(tmp_path)
    old_path = "notes/topic_research.md"
    current_path = tmp_path / "notes" / "topic.md"
    current_path.parent.mkdir(parents=True)
    current_path.write_text("current", encoding="utf-8")
    await _seed_legacy_migration_node(
        service,
        old_path,
        title="topic",
        content="old",
    )

    def _unexpected_scan(_: str) -> str | None:
        raise AssertionError("memory bundles must not use filename heuristics")

    async def _unexpected_public_resolver(*_: Any, **__: Any) -> dict[str, Any]:
        raise AssertionError("memory bundles must not use the public resolver fallback")

    monkeypatch.setattr(service, "_find_missing_file_candidate", _unexpected_scan)
    monkeypatch.setattr(service, "resolve_canonical_path", _unexpected_public_resolver)
    bundles = await service.build_memory_bundles(
        "topic",
        [
            SearchResult(
                file_path=old_path,
                title="topic",
                snippet="old",
                relevance=1.0,
                source="direct",
            )
        ],
    )

    assert bundles
    assert bundles[0].primary_path == old_path


async def test_memory_bundles_batch_lineage_reads_and_path_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """记忆包的往返次数必须与检索结果数成正比，而不是与血缘边数成正比。

    原实现每条血缘边打两次库（取节点、取摘要）再做几次同步 stat。一次
    召回 10 条结果、每条十几条边，就是上百次串行往返，而 stat 直接跑在
    事件循环上。本用例给一个节点挂 12 条边，断言节点批量查询只发生一次、
    路径判定只发生常数次，并且判定不在事件循环线程上执行。
    """
    import threading

    from plugins.life_engine.memory import service as service_module

    service = await _make_service(tmp_path)
    memory_path = "notes/primary.md"
    (tmp_path / "notes").mkdir(parents=True)
    (tmp_path / memory_path).write_text("primary", encoding="utf-8")
    primary = await _seed_legacy_migration_node(
        service,
        memory_path,
        title="Primary",
        content="primary",
    )

    edge_count = 12
    for index in range(edge_count):
        neighbour_path = f"notes/neighbour_{index}.md"
        (tmp_path / neighbour_path).write_text(f"n{index}", encoding="utf-8")
        neighbour = await _seed_legacy_migration_node(
            service,
            neighbour_path,
            title=f"Neighbour {index}",
            content=f"n{index}",
        )
        # 一半出边一半入边，两个方向都要被同一批查询覆盖
        if index % 2 == 0:
            source_id, target_id = primary.node_id, neighbour.node_id
        else:
            source_id, target_id = neighbour.node_id, primary.node_id
        await service._require_memory_storage().legacy_graph.create_or_update_edge(
            source_id,
            target_id,
            EdgeType.REFINES.value,
            reason=f"lineage {index}",
            strength=0.9,
            bidirectional=False,
        )

    view_calls = 0
    assess_threads: list[str] = []
    loop_thread = threading.current_thread().name

    graph_store = service._require_memory_storage().legacy_graph
    real_views = graph_store.get_lineage_node_views
    real_assess = service_module._assess_bundle_paths

    async def _counting_views(node_ids: Any) -> Any:
        """统计血缘节点批量查询次数。"""
        nonlocal view_calls
        view_calls += 1
        return await real_views(node_ids)

    def _recording_assess(workspace: Any, paths: Any) -> Any:
        """记录路径判定所在线程。"""
        assess_threads.append(threading.current_thread().name)
        return real_assess(workspace, paths)

    monkeypatch.setattr(graph_store, "get_lineage_node_views", _counting_views)
    monkeypatch.setattr(service_module, "_assess_bundle_paths", _recording_assess)

    bundles = await service.build_memory_bundles(
        "primary",
        [
            SearchResult(
                file_path=memory_path,
                title="Primary",
                snippet="primary",
                relevance=1.0,
                source="direct",
            )
        ],
    )

    # 血缘确实被组装进来了，否则下面的次数断言毫无意义
    assert len(bundles) == 1
    assert len(bundles[0].history_trace) == edge_count

    # 12 条边共用一次节点批量查询
    assert view_calls == 1
    # 检索结果一批、血缘邻居一批、规范路径一批——与边数无关的常数
    assert 1 <= len(assess_threads) <= 3
    assert all(name != loop_thread for name in assess_threads)
