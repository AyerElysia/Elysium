"""Characterization tests for safe Life Memory snapshot migration."""

from __future__ import annotations

import sqlite3

from plugins.life_engine.memory.epistemic import create_epistemic_schema
from plugins.life_engine.memory.experience import create_life_memory_schema
from plugins.life_engine.memory.indexing import create_memory_schema
from plugins.life_engine.memory.living import create_living_memory_schema
from plugins.life_engine.storage.migration.memory_copy import (
    TABLE_SPECS,
    _insert_statement,
    _source_context,
    iter_transformed_source_rows,
    normalize_target_row,
)


def _source() -> sqlite3.Connection:
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    create_memory_schema(database)
    create_life_memory_schema(database)
    create_epistemic_schema(database)
    create_living_memory_schema(database)
    database.executescript(
        """
        CREATE VIRTUAL TABLE memory_fts USING fts5(
            node_id,
            title,
            content,
            tokenize='unicode61'
        );
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
            UNIQUE(source_id, target_id, edge_type)
        );
        CREATE TABLE memory_corrections (
            correction_id TEXT PRIMARY KEY,
            topic TEXT NOT NULL,
            message TEXT NOT NULL,
            source TEXT DEFAULT 'user',
            created_at REAL NOT NULL,
            related_node_id TEXT,
            query TEXT DEFAULT '',
            stream_id TEXT
        );
        """
    )
    return database


def _rows(
    database: sqlite3.Connection,
    table: str,
) -> list[dict[str, object]]:
    context = _source_context(database)
    batches = iter_transformed_source_rows(
        database,
        TABLE_SPECS[table],
        context,
        batch_size=10,
    )
    return [row for batch in batches for row in batch]


def test_deleted_nodes_and_their_edges_are_preserved() -> None:
    database = _source()
    try:
        database.executemany(
            """INSERT INTO memory_nodes (
                node_id, node_type, file_path, content_hash, title,
                created_at, updated_at, is_deleted, event_date,
                fts_content_hash, embedding_content_hash, embedding_model,
                embedding_updated_at, index_revision
            ) VALUES (?, 'file', ?, ?, ?, 1.0, 2.0, ?, ?, ?, ?, ?, ?, ?)""",
            (
                (
                    "file:live",
                    "notes/live.md",
                    "live-hash",
                    "live",
                    0,
                    "2026-08-04",
                    "fts-live",
                    "embedding-live",
                    "BAAI/bge-m3",
                    3.0,
                    2,
                ),
                (
                    "file:deleted",
                    "notes/deleted.md",
                    "deleted-hash",
                    "deleted",
                    1,
                    "2026-08-03",
                    "fts-deleted",
                    "embedding-deleted",
                    "BAAI/bge-m3",
                    4.0,
                    3,
                ),
            ),
        )
        database.execute(
            "INSERT INTO memory_fts (node_id, title, content) VALUES (?, ?, ?)",
            ("file:deleted", "deleted", "不可丢弃的历史正文"),
        )
        database.execute(
            """INSERT INTO memory_edges (
                edge_id, source_id, target_id, edge_type, weight,
                base_strength, reinforcement, activation_count,
                last_activated_at, reason, created_at, bidirectional
            ) VALUES ('edge-1', 'file:deleted', 'file:live', 'associates',
                0.8, 0.5, 0.3, 4, 5.0, '历史关联', 1.0, 1)"""
        )

        nodes = {row["node_id"]: row for row in _rows(database, "memory_nodes")}
        edges = _rows(database, "memory_edges")

        assert nodes["file:deleted"]["is_deleted"] is True
        assert nodes["file:deleted"]["legacy_fts_present"] is True
        assert nodes["file:deleted"]["document_content"] == "不可丢弃的历史正文"
        assert nodes["file:deleted"]["embedding_content_hash"] == "embedding-deleted"
        assert edges == [
            {
                "edge_id": "edge-1",
                "source_id": "file:deleted",
                "target_id": "file:live",
                "edge_type": "associates",
                "weight": 0.8,
                "base_strength": 0.5,
                "reinforcement": 0.3,
                "activation_count": 4,
                "last_activated_at": 5.0,
                "reason": "历史关联",
                "created_at": 1.0,
                "bidirectional": True,
            }
        ]
    finally:
        database.close()


def test_legacy_experience_identity_remains_replay_compatible() -> None:
    database = _source()
    try:
        database.execute(
            """INSERT INTO memory_experiences (
                event_id, source_event_id, sequence, occurred_at, recorded_at,
                source, channel, event_type, content, stream_id,
                consciousness_instance_id, actor, visibility, valid_from,
                valid_to, metadata_json
            ) VALUES (
                'event-1', '', 7, '2026-08-04T01:00:00+00:00',
                '2026-08-04T01:00:01+00:00', 'qq', 'group', 'message',
                '一次经验', 'qq:group:1', 'core', 'user:1', 'private',
                '2026-08-04T01:00:00+00:00', '', '{"occurrence":"legacy"}'
            )"""
        )

        [record] = _rows(database, "memory_experiences")

        assert record["source_event_id"] == ""
        assert len(str(record["payload_sha256"])) == 64
        assert record["metadata_json"] == '{"occurrence":"legacy"}'
    finally:
        database.close()


def test_mysql_integer_normalization_covers_cursors_and_graph_history() -> None:
    witness = normalize_target_row(
        TABLE_SPECS["memory_witness_state"],
        {
            "consciousness_instance_id": "memory_witness",
            "last_sequence": "86093",
            "revision": "1",
            "last_run_at": "2026-08-04T14:10:17+08:00",
            "last_success_at": "2026-08-04T14:10:17+08:00",
            "last_error": "",
            "updated_at": "2026-08-04T14:10:17+08:00",
        },
    )
    edge = normalize_target_row(
        TABLE_SPECS["memory_edges"],
        {
            "edge_id": "edge-1",
            "source_id": "node-1",
            "target_id": "node-2",
            "edge_type": "associates",
            "weight": "0.8",
            "base_strength": "0.5",
            "reinforcement": "0.3",
            "activation_count": "4",
            "last_activated_at": "5.0",
            "reason": "history",
            "created_at": "1.0",
            "bidirectional": "1",
        },
    )

    assert witness["last_sequence"] == 86093
    assert witness["revision"] == 1
    assert edge["activation_count"] == 4


def test_failed_candidate_replay_repairs_only_matching_immutable_json() -> None:
    statement = str(_insert_statement(TABLE_SPECS["memory_artifact_versions"]))

    assert "memory_artifact_versions.payload_sha256 = new.payload_sha256" in statement
    assert "new.metadata_json" in statement
    assert "new.parent_artifact_ids_json" in statement
