"""Characterization tests for safe Life Memory snapshot migration."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from plugins.life_engine.memory.epistemic import create_epistemic_schema
from plugins.life_engine.memory.experience import create_life_memory_schema
from plugins.life_engine.memory.indexing import create_memory_schema
from plugins.life_engine.memory.living import create_living_memory_schema
from plugins.life_engine.storage.contracts import StorageWriterRole
from plugins.life_engine.storage.migration import memory_copy as memory_copy_module
from plugins.life_engine.storage.migration.memory_copy import (
    TABLE_SPECS,
    _insert_statement,
    _source_context,
    ensure_memory_copy_schema,
    iter_transformed_source_rows,
    normalize_target_row,
)
from plugins.life_engine.storage.migration.memory_export import (
    _create_source_schema,
    _source_row,
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


@pytest.mark.asyncio
@pytest.mark.parametrize("writer_frozen", [False, True])
async def test_copy_schema_binds_trigger_policy_to_frozen_run(
    monkeypatch: pytest.MonkeyPatch,
    writer_frozen: bool,
) -> None:
    calls: list[dict[str, object]] = []

    class _Registry:
        async def get_run(self, run_id: str) -> dict[str, object]:
            assert run_id == "copy-run"
            return {"writer_frozen": writer_frozen}

    async def _ensure(_runtime: object, **kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(memory_copy_module, "ensure_memory_storage_schema", _ensure)
    runtime = SimpleNamespace(writer_role=StorageWriterRole.CANDIDATE_COPY)
    token = SimpleNamespace(run_id="copy-run")

    assert (
        await ensure_memory_copy_schema(
            runtime,  # type: ignore[arg-type]
            copy_registry=_Registry(),  # type: ignore[arg-type]
            token=token,  # type: ignore[arg-type]
        )
        is writer_frozen
    )
    assert calls == [
        {
            "require_database_immutability": writer_frozen,
            "writer_frozen": writer_frozen,
        }
    ]


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


def test_index_recovery_copy_preserves_compensation_and_resets_foreign_lease() -> None:
    database = _source()
    try:
        database.execute(
            """INSERT INTO memory_nodes (
                node_id, node_type, file_path, content_hash, title,
                created_at, updated_at, index_revision
            ) VALUES ('file:index', 'file', 'notes/index.md', 'doc-hash',
                'index', 1.0, 2.0, 7)"""
        )
        database.execute(
            """INSERT INTO memory_index_jobs (
                job_id, node_id, content_hash, status, created_at, updated_at,
                attempts, error, index_revision, claim_token
            ) VALUES ('job-index', 'file:index', 'doc-hash', 'processing',
                1.0, 2.0, 4, 'provider-timeout', 7, 'foreign-lease')"""
        )
        database.execute(
            """INSERT INTO memory_vector_tombstones (
                node_id, chunk_id, created_at, collection_name, force_delete
            ) VALUES ('file:index', 'file:index:0:chunk-hash',
                3.0, 'chunks-v1', 1)"""
        )

        [job] = _rows(database, "memory_index_jobs")
        [tombstone] = _rows(database, "memory_vector_tombstones")

        assert TABLE_SPECS["memory_index_jobs"].key_columns == (
            "job_id",
            "index_revision",
        )
        assert job["status"] == "pending"
        assert job["claim_token"] == ""
        assert job["attempts"] == 4
        assert job["error"] == "provider-timeout|RecoveryLeaseReset"
        assert tombstone["collection_name"] == "chunks-v1"
        assert tombstone["force_delete"] is True
    finally:
        database.close()


def test_reverse_export_round_trip_preserves_job_and_tombstone_histories() -> None:
    template = _source()
    exported = sqlite3.connect(":memory:")
    exported.row_factory = sqlite3.Row
    try:
        columns = _create_source_schema(template, exported)
        exported.execute("PRAGMA foreign_keys = ON")
        exported.execute(
            """INSERT INTO memory_nodes (
                node_id, node_type, file_path, title, created_at, updated_at
            ) VALUES ('node-1', 'file', 'notes/node-1.md', 'node-1', 1.0, 1.0)"""
        )
        job_spec = TABLE_SPECS["memory_index_jobs"]
        job_rows = (
            {
                "job_id": "node-1:hash-a",
                "node_id": "node-1",
                "content_hash": "hash-a",
                "status": "completed",
                "created_at": 1.0,
                "updated_at": 2.0,
                "attempts": 1,
                "error": "",
                "index_revision": 1,
                "claim_token": "",
            },
            {
                "job_id": "node-1:hash-a",
                "node_id": "node-1",
                "content_hash": "hash-a",
                "status": "processing",
                "created_at": 3.0,
                "updated_at": 4.0,
                "attempts": 6,
                "error": "late-worker",
                "index_revision": 3,
                "claim_token": "foreign-lease",
            },
        )
        job_insert = (
            "INSERT INTO memory_index_jobs ("
            + ", ".join(columns["memory_index_jobs"])
            + ") VALUES ("
            + ", ".join("?" for _ in columns["memory_index_jobs"])
            + ")"
        )
        exported.executemany(
            job_insert,
            [
                _source_row(
                    "memory_index_jobs",
                    normalize_target_row(job_spec, row),
                    columns["memory_index_jobs"],
                )
                for row in job_rows
            ],
        )

        tombstone_spec = TABLE_SPECS["memory_vector_tombstones"]
        tombstone_rows = (
            {
                "tombstone_id": 10,
                "node_id": "node-1",
                "chunk_id": "node-1:0:chunk-a",
                "collection_name": "chunks-v1",
                "created_at": 5.0,
                "consumed_at": 6.0,
                "force_delete": False,
            },
            {
                "tombstone_id": 11,
                "node_id": "node-1",
                "chunk_id": "node-1:0:chunk-a",
                "collection_name": "chunks-v2",
                "created_at": 7.0,
                "consumed_at": None,
                "force_delete": True,
            },
        )
        tombstone_insert = (
            "INSERT INTO memory_vector_tombstones ("
            + ", ".join(columns["memory_vector_tombstones"])
            + ") VALUES ("
            + ", ".join("?" for _ in columns["memory_vector_tombstones"])
            + ")"
        )
        exported.executemany(
            tombstone_insert,
            [
                _source_row(
                    "memory_vector_tombstones",
                    normalize_target_row(tombstone_spec, row),
                    columns["memory_vector_tombstones"],
                )
                for row in tombstone_rows
            ],
        )
        exported.commit()

        round_trip_jobs = _rows(exported, "memory_index_jobs")
        round_trip_tombstones = _rows(exported, "memory_vector_tombstones")
        assert [(row["job_id"], row["index_revision"]) for row in round_trip_jobs] == [
            ("node-1:hash-a", 1),
            ("node-1:hash-a", 3),
        ]
        assert round_trip_jobs[1]["status"] == "pending"
        assert round_trip_jobs[1]["claim_token"] == ""
        assert round_trip_jobs[1]["attempts"] == 6
        assert round_trip_jobs[1]["error"] == "late-worker|RecoveryLeaseReset"
        assert [row["tombstone_id"] for row in round_trip_tombstones] == [10, 11]
        assert [row["chunk_id"] for row in round_trip_tombstones] == [
            "node-1:0:chunk-a",
            "node-1:0:chunk-a",
        ]
        assert round_trip_tombstones[0]["consumed_at"] == 6.0
        assert round_trip_tombstones[1]["collection_name"] == "chunks-v2"
        assert round_trip_tombstones[1]["force_delete"] is True

        job_pk = {
            str(row["name"]): int(row["pk"])
            for row in exported.execute("PRAGMA table_info(memory_index_jobs)")
            if int(row["pk"] or 0) > 0
        }
        tombstone_pk = {
            str(row["name"]): int(row["pk"])
            for row in exported.execute(
                "PRAGMA table_info(memory_vector_tombstones)"
            )
            if int(row["pk"] or 0) > 0
        }
        assert job_pk == {"job_id": 1, "index_revision": 2}
        assert tombstone_pk == {"tombstone_id": 1}
        job_foreign_keys = [
            dict(row)
            for row in exported.execute("PRAGMA foreign_key_list(memory_index_jobs)")
        ]
        assert len(job_foreign_keys) == 1
        assert job_foreign_keys[0]["table"] == "memory_nodes"
        assert job_foreign_keys[0]["from"] == "node_id"
        assert job_foreign_keys[0]["to"] == "node_id"
        assert job_foreign_keys[0]["on_delete"] == "CASCADE"

        exported.execute("DELETE FROM memory_nodes WHERE node_id = 'node-1'")
        assert exported.execute(
            "SELECT COUNT(*) FROM memory_index_jobs WHERE node_id = 'node-1'"
        ).fetchone()[0] == 0

        exported.execute(
            "DELETE FROM memory_vector_tombstones WHERE tombstone_id = 11"
        )
        exported.execute(
            """INSERT INTO memory_vector_tombstones (
                node_id, chunk_id, collection_name, created_at, force_delete
            ) VALUES ('node-1', 'node-1:1:chunk-b', 'chunks-v3', 8.0, 1)"""
        )
        assert exported.execute(
            "SELECT MAX(tombstone_id) FROM memory_vector_tombstones"
        ).fetchone()[0] == 12
    finally:
        exported.close()
        template.close()


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
