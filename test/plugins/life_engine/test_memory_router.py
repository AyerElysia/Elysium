"""Focused visibility tests for the read-only memory router."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from plugins.life_engine.memory.router import MemoryRouter, _read_graph_payload


def _graph_db(*, include_deleted: bool = True) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    deleted_column = ", is_deleted INTEGER DEFAULT 0" if include_deleted else ""
    db.execute(
        "CREATE TABLE memory_nodes ("
        "node_id TEXT PRIMARY KEY, node_type TEXT, file_path TEXT, title TEXT, "
        "activation_strength REAL DEFAULT 0, access_count INTEGER DEFAULT 0, "
        "last_accessed_at REAL, emotional_valence REAL DEFAULT 0, "
        "emotional_arousal REAL DEFAULT 0, importance REAL DEFAULT 0, "
        "updated_at REAL"
        f"{deleted_column}"
        ")"
    )
    db.execute(
        "CREATE TABLE memory_edges ("
        "edge_id TEXT PRIMARY KEY, source_id TEXT, target_id TEXT, edge_type TEXT, "
        "weight REAL, base_strength REAL DEFAULT 0, reinforcement REAL DEFAULT 0, "
        "activation_count INTEGER DEFAULT 0, last_activated_at REAL, reason TEXT"
        ")"
    )
    return db


def _insert_node(
    db: sqlite3.Connection,
    node_id: str,
    node_type: str,
    file_path: str | None,
    *,
    activation: float = 0.0,
    is_deleted: int = 0,
) -> None:
    db.execute(
        "INSERT INTO memory_nodes "
        "(node_id, node_type, file_path, title, activation_strength, is_deleted) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (node_id, node_type, file_path, node_id, activation, is_deleted),
    )


def _insert_edge(
    db: sqlite3.Connection,
    edge_id: str,
    source_id: str,
    target_id: str,
    *,
    weight: float = 0.9,
) -> None:
    db.execute(
        "INSERT INTO memory_edges (edge_id, source_id, target_id, edge_type, weight) "
        "VALUES (?, ?, ?, 'relates', ?)",
        (edge_id, source_id, target_id, weight),
    )


async def test_graph_filters_invalid_deleted_nodes_links_degrees_and_invalid_focus() -> None:
    db = _graph_db()
    _insert_node(db, "file:good", "file", "notes/good.md", activation=1.0)
    _insert_node(db, "concept:topic", "concept", None, activation=0.8)
    _insert_node(db, "file:noncanonical", "file", "./notes/noncanonical.md", activation=9.0)
    _insert_node(db, "file:deleted", "file", "notes/deleted.md", activation=8.0, is_deleted=1)
    _insert_edge(db, "good-concept", "file:good", "concept:topic")
    _insert_edge(db, "good-noncanonical", "file:good", "file:noncanonical")
    _insert_edge(db, "concept-deleted", "concept:topic", "file:deleted")
    db.commit()

    changes_before = db.total_changes
    payload = await _read_graph_payload(db, limit_nodes=80, min_weight=0.15, focus_id=None)

    nodes = {node["id"]: node for node in payload["nodes"]}
    assert set(nodes) == {"file:good", "concept:topic"}
    assert nodes["file:good"]["path"] == "notes/good.md"
    assert nodes["concept:topic"]["path"] is None
    assert {link["id"] for link in payload["links"]} == {"good-concept"}
    assert nodes["file:good"]["degree"] == 1
    assert nodes["concept:topic"]["degree"] == 1
    assert db.total_changes == changes_before

    focused = await _read_graph_payload(
        db,
        limit_nodes=80,
        min_weight=0.15,
        focus_id="file:good",
    )
    assert [node["id"] for node in focused["nodes"]] == ["file:good", "concept:topic"]
    assert [link["id"] for link in focused["links"]] == ["good-concept"]

    invalid_focus = await _read_graph_payload(
        db,
        limit_nodes=80,
        min_weight=0.15,
        focus_id="file:noncanonical",
    )
    assert invalid_focus == {"nodes": [], "links": []}


async def test_graph_supports_legacy_schema_without_is_deleted() -> None:
    db = _graph_db(include_deleted=False)
    db.execute(
        "INSERT INTO memory_nodes (node_id, node_type, file_path, title, activation_strength) "
        "VALUES ('file:legacy', 'file', 'notes/legacy.md', 'Legacy', 1.0)"
    )
    db.commit()

    payload = await _read_graph_payload(db, limit_nodes=80, min_weight=0.15, focus_id=None)

    assert [node["id"] for node in payload["nodes"]] == ["file:legacy"]
    assert payload["links"] == []


class _FakeMemory:
    def __init__(self) -> None:
        self._db = None
        self.search_calls: list[tuple[str, int]] = []
        self.activation_calls: list[tuple[list[str], int, int]] = []
        self.nodes = {
            "file:good": SimpleNamespace(
                node_type="file",
                file_path="notes/good.md",
                title="Good",
            ),
            "file:noncanonical": SimpleNamespace(
                node_type="file",
                file_path="./notes/noncanonical.md",
                title="Noncanonical",
            ),
            "concept:topic": SimpleNamespace(
                node_type="concept",
                file_path=None,
                title="Topic",
            ),
        }

    async def search_memory(self, query: str, *, top_k: int, return_bundles: bool = True) -> list[Any]:
        self.search_calls.append((query, top_k))
        return [
            SimpleNamespace(
                file_path="notes/good.md",
                title="Good",
                snippet="good",
                relevance=1.0,
                source="direct",
                association_path=[],
                association_reason="",
            ),
            SimpleNamespace(
                file_path="./notes/noncanonical.md",
                title="Noncanonical",
                snippet="bad",
                relevance=0.9,
                source="direct",
                association_path=[],
                association_reason="",
            ),
            SimpleNamespace(
                file_path="/tmp/outside.md",
                title="Outside",
                snippet="outside",
                relevance=0.8,
                source="direct",
                association_path=[],
                association_reason="",
            ),
        ]

    async def spread_activation(
        self,
        seed_ids: list[str],
        *,
        max_depth: int,
        max_results: int,
    ) -> list[tuple[str, float, list[str], str]]:
        self.activation_calls.append((seed_ids, max_depth, max_results))
        return [
            ("file:good", 1.0, ["seed", "file:good"], "good"),
            ("file:noncanonical", 0.9, ["seed", "file:noncanonical"], "bad"),
            ("concept:topic", 0.8, ["seed", "concept:topic"], "concept"),
        ]

    async def _get_node_by_id(self, node_id: str) -> Any:
        return self.nodes.get(node_id)


def test_search_and_activation_filter_malformed_file_rows_after_backend_calls() -> None:
    memory = _FakeMemory()
    plugin = SimpleNamespace(service=SimpleNamespace(_memory_service=memory))
    router = MemoryRouter(plugin=plugin)

    with TestClient(router.app) as client:
        search = client.get("/api/search", params={"query": "memory", "top_k": 7})
        activation = client.post(
            "/api/activate",
            json={"seed_ids": ["seed"], "max_depth": 3, "max_results": 4},
        )

    assert search.status_code == 200
    assert [item["file_path"] for item in search.json()] == ["notes/good.md"]
    assert memory.search_calls == [("memory", 7)]

    assert activation.status_code == 200
    assert [item["id"] for item in activation.json()] == ["file:good", "concept:topic"]
    assert memory.activation_calls == [(["seed"], 3, 4)]


def test_health_endpoint_exports_response_schema() -> None:
    plugin = SimpleNamespace(service=SimpleNamespace(_memory_service=None))
    router = MemoryRouter(plugin=plugin)

    openapi = router.app.openapi()
    response_schema = openapi["paths"]["/api/health"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]

    assert response_schema["$ref"].endswith("/MemoryHealthResponse")
    health_schema = openapi["components"]["schemas"]["MemoryHealthResponse"]
    assert {"status", "sqlite", "index", "outbox", "living_memory", "vector"} <= set(
        health_schema["properties"]
    )
