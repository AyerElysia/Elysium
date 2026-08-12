"""Read-only compatibility tests for quarantined legacy graph rows."""

from __future__ import annotations

import sqlite3

from plugins.life_engine.memory.legacy_graph_projection import (
    read_legacy_graph_payload,
)


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
        "INSERT INTO memory_edges "
        "(edge_id, source_id, target_id, edge_type, weight) "
        "VALUES (?, ?, ?, 'relates', ?)",
        (edge_id, source_id, target_id, weight),
    )


async def test_projection_filters_invalid_deleted_rows_without_writing() -> None:
    db = _graph_db()
    _insert_node(db, "file:good", "file", "notes/good.md", activation=1.0)
    _insert_node(db, "concept:topic", "concept", None, activation=0.8)
    _insert_node(
        db,
        "file:noncanonical",
        "file",
        "./notes/noncanonical.md",
        activation=9.0,
    )
    _insert_node(
        db,
        "file:deleted",
        "file",
        "notes/deleted.md",
        activation=8.0,
        is_deleted=1,
    )
    _insert_edge(db, "good-concept", "file:good", "concept:topic")
    _insert_edge(db, "good-noncanonical", "file:good", "file:noncanonical")
    _insert_edge(db, "concept-deleted", "concept:topic", "file:deleted")
    db.commit()

    changes_before = db.total_changes
    payload = await read_legacy_graph_payload(
        db,
        limit_nodes=80,
        min_weight=0.15,
        focus_id=None,
    )

    nodes = {node["id"]: node for node in payload["nodes"]}
    assert set(nodes) == {"file:good", "concept:topic"}
    assert {link["id"] for link in payload["links"]} == {"good-concept"}
    assert nodes["file:good"]["degree"] == 1
    assert nodes["concept:topic"]["degree"] == 1
    assert db.total_changes == changes_before

    focused = await read_legacy_graph_payload(
        db,
        limit_nodes=80,
        min_weight=0.15,
        focus_id="file:good",
    )
    assert [node["id"] for node in focused["nodes"]] == [
        "file:good",
        "concept:topic",
    ]
    invalid = await read_legacy_graph_payload(
        db,
        limit_nodes=80,
        min_weight=0.15,
        focus_id="file:noncanonical",
    )
    assert invalid == {"nodes": [], "links": []}


async def test_projection_reads_schema_without_deleted_marker() -> None:
    db = _graph_db(include_deleted=False)
    db.execute(
        "INSERT INTO memory_nodes "
        "(node_id, node_type, file_path, title, activation_strength) "
        "VALUES ('file:legacy', 'file', 'notes/legacy.md', 'Legacy', 1.0)"
    )
    db.commit()

    payload = await read_legacy_graph_payload(
        db,
        limit_nodes=80,
        min_weight=0.15,
        focus_id=None,
    )

    assert [node["id"] for node in payload["nodes"]] == ["file:legacy"]
    assert payload["links"] == []
