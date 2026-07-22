"""Regression tests for Life Engine memory relation invariants."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from typing import Any

import pytest

from plugins.life_engine.memory.decay import (
    apply_decay,
    dream_walk,
    get_file_relations,
    prune_weak_edges,
)
from plugins.life_engine.memory.edges import (
    EdgeType,
    create_or_update_edge,
    delete_edge,
    get_edges_from,
    get_edges_to,
    reinforce_coactivated,
)
from plugins.life_engine.memory.nodes import (
    generate_file_node_id,
    get_node_by_file_path,
)
from plugins.life_engine.memory.search import get_node_by_id, spread_activation


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE memory_nodes (
            node_id TEXT PRIMARY KEY,
            node_type TEXT NOT NULL,
            file_path TEXT,
            content_hash TEXT,
            title TEXT,
            activation_strength REAL NOT NULL DEFAULT 1.0,
            access_count INTEGER NOT NULL DEFAULT 0,
            last_accessed_at REAL,
            emotional_valence REAL NOT NULL DEFAULT 0.0,
            emotional_arousal REAL NOT NULL DEFAULT 0.0,
            importance REAL NOT NULL DEFAULT 0.5,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            embedding_synced INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE memory_edges (
            edge_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            edge_type TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 0.5,
            base_strength REAL NOT NULL DEFAULT 0.5,
            reinforcement REAL NOT NULL DEFAULT 0.0,
            activation_count INTEGER NOT NULL DEFAULT 0,
            last_activated_at REAL,
            reason TEXT,
            created_at REAL NOT NULL,
            bidirectional INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (source_id) REFERENCES memory_nodes(node_id) ON DELETE CASCADE,
            FOREIGN KEY (target_id) REFERENCES memory_nodes(node_id) ON DELETE CASCADE,
            UNIQUE(source_id, target_id, edge_type)
        );
        """
    )
    yield connection
    connection.close()


def _add_file_node(db: sqlite3.Connection, file_path: str) -> str:
    node_id = generate_file_node_id(file_path)
    now = time.time()
    db.execute(
        """
        INSERT INTO memory_nodes (
            node_id, node_type, file_path, content_hash, title,
            activation_strength, access_count, last_accessed_at,
            emotional_valence, emotional_arousal, importance,
            created_at, updated_at, embedding_synced
        ) VALUES (?, 'file', ?, '', ?, 1.0, 0, NULL, 0.0, 0.0, 0.5, ?, ?, 0)
        """,
        (node_id, file_path, file_path.rsplit("/", 1)[-1], now, now),
    )
    db.commit()
    return node_id


def _edge_rows(
    db: sqlite3.Connection,
    edge_type: EdgeType,
) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT source_id, target_id, weight, base_strength, reinforcement,
               activation_count, last_activated_at, reason, bidirectional
        FROM memory_edges
        WHERE edge_type = ?
        ORDER BY source_id, target_id
        """,
        (edge_type.value,),
    ).fetchall()


def _database_snapshot(db: sqlite3.Connection) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    nodes = [
        tuple(row)
        for row in db.execute(
            """
            SELECT node_id, node_type, file_path, content_hash, title,
                   activation_strength, access_count, last_accessed_at,
                   emotional_valence, emotional_arousal, importance,
                   created_at, updated_at, embedding_synced
            FROM memory_nodes ORDER BY node_id
            """
        ).fetchall()
    ]
    edges = [
        tuple(row)
        for row in db.execute(
            """
            SELECT edge_id, source_id, target_id, edge_type, weight, base_strength,
                   reinforcement, activation_count, last_activated_at, reason,
                   created_at, bidirectional
            FROM memory_edges ORDER BY edge_id
            """
        ).fetchall()
    ]
    return nodes, edges


@pytest.mark.asyncio
async def test_create_or_update_edge_validates_and_syncs_bidirectional_rows(
    db: sqlite3.Connection,
) -> None:
    node_a = _add_file_node(db, "notes/a.md")
    node_b = _add_file_node(db, "notes/b.md")

    with pytest.raises(ValueError, match="相同"):
        await create_or_update_edge(db, node_a, node_a, EdgeType.RELATES)
    with pytest.raises(ValueError, match="endpoint"):
        await create_or_update_edge(db, node_a, "missing", EdgeType.RELATES)
    for strength in (-0.01, 1.01, float("nan"), float("inf"), "invalid"):
        with pytest.raises(ValueError, match="strength"):
            await create_or_update_edge(
                db,
                node_a,
                node_b,
                EdgeType.RELATES,
                strength=strength,
            )

    await create_or_update_edge(
        db,
        node_a,
        node_b,
        EdgeType.RELATES,
        reason="first",
        strength=0.4,
        bidirectional=True,
    )
    await create_or_update_edge(
        db,
        node_b,
        node_a,
        EdgeType.RELATES,
        reason="updated",
        strength=0.8,
        bidirectional=True,
    )

    relation_rows = _edge_rows(db, EdgeType.RELATES)
    assert {(row["source_id"], row["target_id"]) for row in relation_rows} == {
        (node_a, node_b),
        (node_b, node_a),
    }
    assert {
        (
            row["weight"],
            row["base_strength"],
            row["reinforcement"],
            row["activation_count"],
            row["reason"],
            row["bidirectional"],
        )
        for row in relation_rows
    } == {(0.8, 0.8, 0.0, 0, "updated", 1)}

    await create_or_update_edge(
        db,
        node_a,
        node_b,
        EdgeType.RELATES,
        strength=0.6,
        bidirectional=False,
    )
    assert [
        (row["source_id"], row["target_id"], row["bidirectional"])
        for row in _edge_rows(db, EdgeType.RELATES)
    ] == [(node_a, node_b, 0)]

    await create_or_update_edge(
        db,
        node_a,
        node_b,
        EdgeType.CAUSES,
        strength=0.7,
        bidirectional=True,
    )
    directional_rows = _edge_rows(db, EdgeType.CAUSES)
    assert [(row["source_id"], row["target_id"], row["bidirectional"])
            for row in directional_rows] == [(node_a, node_b, 0)]


@pytest.mark.asyncio
async def test_delete_edge_uses_total_deleted_rows_and_preserves_directional_reverse(
    db: sqlite3.Connection,
) -> None:
    node_a = _add_file_node(db, "notes/a.md")
    node_b = _add_file_node(db, "notes/b.md")

    await create_or_update_edge(
        db,
        node_a,
        node_b,
        EdgeType.CONTRASTS,
        strength=0.6,
        bidirectional=False,
    )
    assert await delete_edge(db, "notes/a.md", "notes/b.md", EdgeType.CONTRASTS)
    assert _edge_rows(db, EdgeType.CONTRASTS) == []

    await create_or_update_edge(
        db,
        node_a,
        node_b,
        EdgeType.RELATES,
        strength=0.6,
        bidirectional=True,
    )
    assert await delete_edge(db, "notes/a.md", "notes/b.md", EdgeType.RELATES)
    assert _edge_rows(db, EdgeType.RELATES) == []

    await create_or_update_edge(db, node_a, node_b, EdgeType.CAUSES, strength=0.6)
    await create_or_update_edge(db, node_b, node_a, EdgeType.CAUSES, strength=0.6)
    assert await delete_edge(db, "notes/a.md", "notes/b.md", EdgeType.CAUSES)
    directional_rows = _edge_rows(db, EdgeType.CAUSES)
    assert [(row["source_id"], row["target_id"])
            for row in directional_rows] == [(node_b, node_a)]


@pytest.mark.asyncio
async def test_reinforcement_and_dream_persist_real_bidirectional_associations(
    db: sqlite3.Connection,
) -> None:
    node_a = _add_file_node(db, "notes/a.md")
    node_b = _add_file_node(db, "notes/b.md")
    node_c = _add_file_node(db, "notes/c.md")
    node_d = _add_file_node(db, "notes/d.md")

    await reinforce_coactivated(db, [node_a, node_b])
    await reinforce_coactivated(db, [node_a, node_b])
    association_rows = _edge_rows(db, EdgeType.ASSOCIATES)
    assert {(row["source_id"], row["target_id"], row["bidirectional"])
            for row in association_rows} == {
        (node_a, node_b, 1),
        (node_b, node_a, 1),
    }
    assert len(
        {
            (
                row["weight"],
                row["base_strength"],
                row["reinforcement"],
                row["activation_count"],
                row["reason"],
                row["bidirectional"],
            )
            for row in association_rows
        }
    ) == 1

    await create_or_update_edge(
        db,
        node_c,
        node_d,
        EdgeType.CAUSES,
        strength=1.0,
    )
    dream_result = await dream_walk(
        db,
        num_seeds=1,
        seed_ids=[node_c],
        max_depth=1,
        decay_factor=1.0,
        persist_learning=True,
    )
    assert dream_result["new_edges_created"] == 1
    dream_rows = db.execute(
        """
        SELECT source_id, target_id, bidirectional
        FROM memory_edges
        WHERE edge_type = ?
          AND ((source_id = ? AND target_id = ?) OR (source_id = ? AND target_id = ?))
        ORDER BY source_id, target_id
        """,
        (EdgeType.ASSOCIATES.value, node_c, node_d, node_d, node_c),
    ).fetchall()
    assert {tuple(row) for row in dream_rows} == {
        (node_c, node_d, 1),
        (node_d, node_c, 1),
    }


@pytest.mark.asyncio
async def test_association_decay_and_pruning_preserve_pair_invariants(
    db: sqlite3.Connection,
) -> None:
    node_a = _add_file_node(db, "notes/a.md")
    node_b = _add_file_node(db, "notes/b.md")
    await reinforce_coactivated(db, [node_a, node_b])
    await reinforce_coactivated(db, [node_a, node_b])

    old_timestamp = time.time() - 86400
    db.execute(
        """
        UPDATE memory_edges
        SET weight = CASE WHEN source_id = ? THEN 0.99 ELSE 0.01 END,
            last_activated_at = ?
        WHERE edge_type = ?
        """,
        (node_a, old_timestamp, EdgeType.ASSOCIATES.value),
    )
    db.commit()
    await apply_decay(db)

    decayed_rows = _edge_rows(db, EdgeType.ASSOCIATES)
    assert len(decayed_rows) == 2
    assert len(
        {
            (
                row["weight"],
                row["base_strength"],
                row["reinforcement"],
                row["activation_count"],
                row["last_activated_at"],
                row["reason"],
                row["bidirectional"],
            )
            for row in decayed_rows
        }
    ) == 1

    db.execute(
        """
        UPDATE memory_edges
        SET weight = CASE WHEN source_id = ? THEN 0.05 ELSE 0.5 END
        WHERE edge_type = ?
        """,
        (node_a, EdgeType.ASSOCIATES.value),
    )
    db.commit()
    assert await prune_weak_edges(db, threshold=0.1) == 2
    assert _edge_rows(db, EdgeType.ASSOCIATES) == []


@pytest.mark.asyncio
async def test_association_maintenance_keeps_null_timestamp_pairs_and_outer_transactions(
    db: sqlite3.Connection,
) -> None:
    node_a = _add_file_node(db, "notes/a.md")
    node_b = _add_file_node(db, "notes/b.md")
    await create_or_update_edge(
        db,
        node_a,
        node_b,
        EdgeType.ASSOCIATES,
        strength=0.2,
        bidirectional=True,
    )

    await apply_decay(db)
    null_timestamp_rows = _edge_rows(db, EdgeType.ASSOCIATES)
    assert len(null_timestamp_rows) == 2
    assert {row["bidirectional"] for row in null_timestamp_rows} == {1}
    assert {row["last_activated_at"] for row in null_timestamp_rows} == {None}

    db.execute("BEGIN")
    db.execute("UPDATE memory_nodes SET title = 'during-decay' WHERE node_id = ?", (node_a,))
    await apply_decay(db)
    db.rollback()
    assert db.execute(
        "SELECT title FROM memory_nodes WHERE node_id = ?", (node_a,)
    ).fetchone()["title"] != "during-decay"

    db.execute("BEGIN")
    db.execute("UPDATE memory_nodes SET title = 'uncommitted' WHERE node_id = ?", (node_a,))
    assert await prune_weak_edges(db, threshold=0.3) == 2
    db.rollback()

    assert db.execute(
        "SELECT title FROM memory_nodes WHERE node_id = ?", (node_a,)
    ).fetchone()["title"] != "uncommitted"
    assert len(_edge_rows(db, EdgeType.ASSOCIATES)) == 2


class _BoundRelationCallbacks:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.file_paths: list[str] = []

    async def get_node_by_file_path(
        self,
        file_path: str,
        migrate_identity: bool = True,
    ) -> Any:
        self.file_paths.append(file_path)
        return await get_node_by_file_path(self.db, file_path)

    async def get_edges_from(self, node_id: str, min_weight: float = 0.0) -> Any:
        return await get_edges_from(self.db, node_id, min_weight)

    async def get_edges_to(self, node_id: str, min_weight: float = 0.0) -> Any:
        return await get_edges_to(self.db, node_id, min_weight)

    async def get_node_by_id(self, node_id: str) -> Any:
        return await get_node_by_id(self.db, node_id)


@pytest.mark.asyncio
async def test_file_relations_uses_bound_or_module_callbacks_with_bfs_depths(
    db: sqlite3.Connection,
) -> None:
    node_a = _add_file_node(db, "notes/a.md")
    node_b = _add_file_node(db, "notes/b.md")
    node_c = _add_file_node(db, "notes/c.md")
    await create_or_update_edge(db, node_a, node_b, EdgeType.CAUSES, strength=0.9)
    await create_or_update_edge(db, node_b, node_c, EdgeType.CAUSES, strength=0.9)
    await create_or_update_edge(db, node_c, node_b, EdgeType.CAUSES, strength=0.9)

    callbacks = _BoundRelationCallbacks(db)
    bound_result = await get_file_relations(
        db,
        "notes/a.md",
        depth=2,
        get_node_by_file_path_func=callbacks.get_node_by_file_path,
        get_edges_from_func=callbacks.get_edges_from,
        get_edges_to_func=callbacks.get_edges_to,
        get_node_by_id_func=callbacks.get_node_by_id,
    )
    module_result = await get_file_relations(
        db,
        "notes/a.md",
        depth=2,
        get_node_by_file_path_func=get_node_by_file_path,
        get_edges_from_func=get_edges_from,
        get_edges_to_func=get_edges_to,
        get_node_by_id_func=get_node_by_id,
    )

    assert callbacks.file_paths == ["notes/a.md"]
    assert bound_result == module_result
    outgoing = bound_result["outgoing"]
    assert {(item["file_path"], item["depth"]) for item in outgoing} == {
        ("notes/b.md", 1),
        ("notes/c.md", 2),
    }
    assert all(item["depth"] <= 2 and item["edge_id"] for item in outgoing)

    zero_depth = await get_file_relations(
        db,
        "notes/a.md",
        depth=0,
        get_node_by_file_path_func=callbacks.get_node_by_file_path,
        get_edges_from_func=callbacks.get_edges_from,
        get_edges_to_func=callbacks.get_edges_to,
        get_node_by_id_func=callbacks.get_node_by_id,
    )
    assert zero_depth["outgoing"] == []
    assert zero_depth["incoming"] == []


@pytest.mark.asyncio
async def test_spread_activation_uses_single_hop_decay_and_strongest_acyclic_path(
    db: sqlite3.Connection,
) -> None:
    node_a = _add_file_node(db, "notes/a.md")
    node_b = _add_file_node(db, "notes/b.md")
    node_c = _add_file_node(db, "notes/c.md")
    node_d = _add_file_node(db, "notes/d.md")
    for source_id, target_id, strength in (
        (node_a, node_b, 0.2),
        (node_a, node_c, 1.0),
        (node_c, node_b, 1.0),
        (node_b, node_d, 1.0),
        (node_d, node_a, 1.0),
    ):
        await create_or_update_edge(
            db,
            source_id,
            target_id,
            EdgeType.CAUSES,
            strength=strength,
        )

    before = _database_snapshot(db)
    results = await spread_activation(
        db,
        [node_a],
        max_depth=3,
        max_results=10,
        spread_decay=0.5,
        spread_threshold=0.01,
    )
    assert _database_snapshot(db) == before

    result_by_id = {node_id: (score, path, reason) for node_id, score, path, reason in results}
    assert result_by_id[node_b][0] == pytest.approx(0.25)
    assert result_by_id[node_b][1] == [node_a, node_c, node_b]
    assert result_by_id[node_d][0] == pytest.approx(0.125)
    assert result_by_id[node_d][1] == [node_a, node_c, node_b, node_d]
    assert node_a not in result_by_id
    assert all(len(path) == len(set(path)) for _, path, _ in result_by_id.values())
