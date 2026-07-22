"""Life Engine 记忆查询只读止损测试。"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.life_engine.memory.edges import EdgeType
from plugins.life_engine.memory.nodes import generate_legacy_file_node_id
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


def _snapshot(db: sqlite3.Connection) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
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


@pytest.mark.asyncio
async def test_search_is_read_only_and_does_not_delete_stale_vectors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await _make_service(tmp_path)
    node_a = await service.get_or_create_file_node(
        "notes/a.md",
        title="A",
        content="alpha memory",
    )
    node_b = await service.get_or_create_file_node(
        "notes/b.md",
        title="B",
        content="beta memory",
    )
    await service.create_or_update_edge(
        node_a.node_id,
        node_b.node_id,
        EdgeType.RELATES,
        reason="explicit relation",
        strength=0.9,
        bidirectional=True,
    )
    await service.create_or_update_edge(
        node_a.node_id,
        node_b.node_id,
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


@pytest.mark.asyncio
async def test_spread_activation_defaults_to_explicit_relations(tmp_path: Path) -> None:
    service = await _make_service(tmp_path)
    node_a = await service.get_or_create_file_node("notes/a.md", title="A")
    node_b = await service.get_or_create_file_node("notes/b.md", title="B")
    node_c = await service.get_or_create_file_node("notes/c.md", title="C")
    await service.create_or_update_edge(
        node_a.node_id,
        node_b.node_id,
        EdgeType.ASSOCIATES,
        reason="learned",
        strength=1.0,
        bidirectional=False,
    )
    await service.create_or_update_edge(
        node_a.node_id,
        node_c.node_id,
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


@pytest.mark.asyncio
async def test_vector_search_filters_stale_ids_without_collection_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = _FakeCollection(["live", "stale"])

    async def _fake_embed(_: str) -> list[float]:
        return [0.1]

    async def _filter(scores: list[tuple[str, float]]) -> tuple[list[tuple[str, float]], list[str]]:
        return [scores[0]], ["stale"]

    monkeypatch.setattr("plugins.life_engine.memory.search.embed_text", _fake_embed)
    result = await vector_search("query", collection, filter_existing_func=_filter)

    assert result == [("live", pytest.approx(result[0][1]))]
    assert collection.delete_calls == []


@pytest.mark.asyncio
async def test_dream_walk_default_does_not_create_or_update_edges(tmp_path: Path) -> None:
    service = await _make_service(tmp_path)
    node_a = await service.get_or_create_file_node("notes/a.md", title="A")
    node_b = await service.get_or_create_file_node("notes/b.md", title="B")
    await service.create_or_update_edge(
        node_a.node_id,
        node_b.node_id,
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


@pytest.mark.asyncio
async def test_dream_walk_can_explicitly_persist_learning(tmp_path: Path) -> None:
    service = await _make_service(tmp_path)
    node_a = await service.get_or_create_file_node("notes/a.md", title="A")
    node_b = await service.get_or_create_file_node("notes/b.md", title="B")
    await service.create_or_update_edge(
        node_a.node_id,
        node_b.node_id,
        EdgeType.RELATES,
        reason="walkable",
        strength=1.0,
        bidirectional=False,
    )

    result = await service.dream_walk(
        num_seeds=1,
        seed_ids=[node_a.node_id],
        max_depth=1,
        decay_factor=1.0,
        persist_learning=True,
    )

    assert result["new_edges_created"] >= 1
    cursor = service._db.cursor()
    cursor.execute(
        "SELECT COUNT(*) AS count FROM memory_edges WHERE edge_type = ?",
        (EdgeType.ASSOCIATES.value,),
    )
    assert cursor.fetchone()["count"] >= 1


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_default_canonical_resolver_reads_legacy_lineage_without_migration(
    tmp_path: Path,
) -> None:
    service = await _make_service(tmp_path)
    old_path = "./notes/legacy.md"
    current_path = "notes/current.md"
    current_file = tmp_path / current_path
    current_file.parent.mkdir(parents=True)
    current_file.write_text("current", encoding="utf-8")

    legacy_node_id = generate_legacy_file_node_id(old_path)
    target = await service.get_or_create_file_node(current_path, title="current")
    now = time.time()
    service._db.execute(
        """
        INSERT INTO memory_nodes (
            node_id, node_type, file_path, content_hash, title,
            activation_strength, access_count, last_accessed_at,
            emotional_valence, emotional_arousal, importance,
            created_at, updated_at, embedding_synced
        ) VALUES (?, 'file', ?, '', 'legacy', 1.0, 0, NULL, 0.0, 0.0, 0.5, ?, ?, 0)
        """,
        (legacy_node_id, "notes/legacy.md", now, now),
    )
    service._db.execute(
        """
        INSERT INTO memory_edges (
            edge_id, source_id, target_id, edge_type, weight, base_strength,
            reinforcement, activation_count, last_activated_at, reason, created_at, bidirectional
        ) VALUES ('legacy-link', ?, ?, ?, 0.8, 0.8, 0.0, 0, NULL, 'legacy lineage', ?, 0)
        """,
        (legacy_node_id, target.node_id, EdgeType.RENAMES.value, now),
    )
    service._db.commit()

    before = _snapshot(service._db)
    result = await service.resolve_canonical_path(old_path)

    assert result["resolved"] is True
    assert result["resolved_path"] == current_path
    assert _snapshot(service._db) == before


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_canonical_resolver_rejects_runtime_path_without_reading_graph(
    tmp_path: Path,
) -> None:
    service = await _make_service(tmp_path)
    before = _snapshot(service._db)

    result = await service.resolve_canonical_path("runtime/state.json", allow_heuristic=True)

    assert result["resolved"] is False
    assert result["note"] == "不是可用于记忆演化的文档: unsupported_suffix"
    assert _snapshot(service._db) == before


@pytest.mark.asyncio
async def test_memory_bundles_exclude_legacy_runtime_lineage_nodes(tmp_path: Path) -> None:
    service = await _make_service(tmp_path)
    memory_path = "notes/kept.md"
    file_path = tmp_path / memory_path
    file_path.parent.mkdir(parents=True)
    file_path.write_text("kept", encoding="utf-8")
    primary = await service.get_or_create_file_node(
        memory_path,
        title="Kept",
        content="kept",
    )
    now = time.time()
    runtime_node_ids = ("file:legacy-runtime-out", "file:legacy-runtime-in")
    for node_id, runtime_path in zip(
        runtime_node_ids,
        ("runtime/outbound.json", "runtime/inbound.json"),
        strict=True,
    ):
        service._db.execute(
            """
            INSERT INTO memory_nodes (
                node_id, node_type, file_path, content_hash, title,
                created_at, updated_at, embedding_synced
            ) VALUES (?, 'file', ?, '', 'runtime', ?, ?, 0)
            """,
            (node_id, runtime_path, now, now),
        )
    service._db.commit()
    await service.create_or_update_edge(
        primary.node_id,
        runtime_node_ids[0],
        EdgeType.RENAMES,
        reason="legacy runtime target",
        strength=0.9,
        bidirectional=False,
    )
    await service.create_or_update_edge(
        runtime_node_ids[1],
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


@pytest.mark.asyncio
async def test_build_memory_bundles_does_not_use_filename_heuristic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await _make_service(tmp_path)
    old_path = "notes/topic_research.md"
    current_path = tmp_path / "notes" / "topic.md"
    current_path.parent.mkdir(parents=True)
    current_path.write_text("current", encoding="utf-8")
    await service.get_or_create_file_node(old_path, title="topic", content="old")

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


@pytest.mark.asyncio
async def test_scheduler_dream_walk_is_read_only_by_default() -> None:
    from plugins.life_engine.dream.scheduler import DreamScheduler

    memory = MagicMock()
    memory.dream_walk = AsyncMock(
        return_value={
            "nodes_activated": 1,
            "new_edges_created": 0,
            "seed_ids": ["seed"],
        }
    )
    memory.prune_weak_edges = AsyncMock(return_value=1)
    scheduler = DreamScheduler(
        memory_service=memory,
        rem_walk_rounds=1,
        rem_seeds_per_round=1,
    )

    report = await scheduler._run_rem(["seed"])

    assert report.new_edges_created == 0
    memory.dream_walk.assert_awaited_once()
    assert memory.dream_walk.await_args.kwargs["persist_learning"] is False
    memory.prune_weak_edges.assert_not_awaited()
