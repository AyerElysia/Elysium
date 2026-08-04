"""Shared contract tests for selectable Life Memory storage."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from plugins.life_engine.memory.edges import EdgeType
from plugins.life_engine.memory.epistemic import MemoryClaim, create_epistemic_schema
from plugins.life_engine.memory.experience import (
    ExperienceRecord,
    create_life_memory_schema,
)
from plugins.life_engine.memory.indexing import create_memory_schema
from plugins.life_engine.memory.living import (
    ArtifactHeadConflict,
    create_living_memory_schema,
    new_artifact_version,
)
from plugins.life_engine.storage.memory import (
    DocumentIndexProjection,
    EpistemicMemoryStore,
    ExperienceLedgerStore,
    LegacyGraphStore,
    LivingMemoryStore,
    MemoryStoreRole,
    WitnessLedgerStore,
    create_local_memory_storage_bundle,
    memory_store_characterizations,
)
from plugins.life_engine.storage.memory.schema import (
    MEMORY_MIGRATIONS,
    MEMORY_SCHEMA_VERSION,
)
from src.kernel.storage import CursorConflict


def _database() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    create_memory_schema(db)
    create_life_memory_schema(db)
    create_epistemic_schema(db)
    create_living_memory_schema(db)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS memory_edges (
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
        CREATE TABLE IF NOT EXISTS memory_corrections (
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
    return db


def _experience(event_id: str = "event-1", sequence: int = 1) -> ExperienceRecord:
    return ExperienceRecord(
        event_id=event_id,
        source_event_id="producer-1",
        sequence=sequence,
        occurred_at="2026-08-04T10:00:00+08:00",
        recorded_at="2026-08-04T10:00:01+08:00",
        source="qq",
        channel="group",
        event_type="message",
        content="她记得这一刻",
        stream_id="qq:group:1",
        consciousness_instance_id="core",
        actor="user:1",
        metadata={"occurrence_id": event_id},
    )


def test_memory_characterization_is_ordered_and_engineering_only() -> None:
    items = memory_store_characterizations()

    assert [item.migration_order for item in items] == [10, 20, 30, 40, 50, 60]
    assert items[0].role == MemoryStoreRole.REBUILDABLE_PROJECTION
    assert items[-1].role == MemoryStoreRole.COMPATIBILITY_HISTORY
    assert all(item.name for item in items)
    assert not any(
        token in item.name
        for item in items
        for token in ("truth", "important", "emotion", "mature", "identity")
    )


def test_mysql_memory_migrations_are_explicit_and_ordered() -> None:
    assert MEMORY_SCHEMA_VERSION == 6
    assert tuple(item.version for item in MEMORY_MIGRATIONS) == (1, 2, 3, 4, 5, 6)
    ddl = "\n".join(
        statement
        for migration in MEMORY_MIGRATIONS
        for statement in migration.statements
    )
    for table in (
        "memory_nodes",
        "memory_experiences",
        "memory_witnesses",
        "memory_artifact_versions",
        "memory_claims",
        "memory_edges",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in ddl
    assert "universal" not in ddl.lower()


@pytest.mark.asyncio
async def test_local_memory_bundle_satisfies_every_public_port() -> None:
    db = _database()
    try:
        bundle = create_local_memory_storage_bundle(lambda: db)

        assert isinstance(bundle.document_index, DocumentIndexProjection)
        assert isinstance(bundle.experiences, ExperienceLedgerStore)
        assert isinstance(bundle.witnesses, WitnessLedgerStore)
        assert isinstance(bundle.living, LivingMemoryStore)
        assert isinstance(bundle.epistemic, EpistemicMemoryStore)
        assert isinstance(bundle.legacy_graph, LegacyGraphStore)

        indexed = await bundle.document_index.upsert_document(
            "notes/contract.md",
            "可追溯的记忆正文",
            "contract",
        )
        replay = await bundle.document_index.upsert_document(
            "notes/contract.md",
            "可追溯的记忆正文",
            "contract",
        )
        assert indexed.chunks
        assert indexed.job_id
        assert replay.job_id == indexed.job_id
        assert len(await bundle.document_index.claim_jobs(limit=1)) == 1

        report = await bundle.experiences.append((_experience(),))
        replay_report = await bundle.experiences.append((_experience(),))
        assert report.inserted_count == 1
        assert replay_report.inserted_count == 0
        assert replay_report.existing[0].event_id == "event-1"

        witness = await bundle.witnesses.append(
            witness_id="witness-1",
            content="我见证了这一刻",
            consciousness_instance_id="core",
            perspective_subject_id="elysia",
            epistemic_kind="subjective_witness",
            source_kind="experience_window",
            stream_scope="qq:group:1",
            visibility="private",
            valid_from="2026-08-04T10:00:00+08:00",
            valid_to="2026-08-04T10:00:00+08:00",
            source_event_ids=("event-1",),
            source_sequence_start=1,
            source_sequence_end=1,
            projection_path="notes/witness-1.md",
        )
        assert witness.source_event_ids == ("event-1",)
        state = await bundle.witnesses.compare_and_advance_state(
            "core",
            expected_sequence=0,
            expected_revision=0,
            next_sequence=1,
        )
        assert (state["last_sequence"], state["revision"]) == (1, 1)
        with pytest.raises(CursorConflict):
            await bundle.witnesses.compare_and_advance_state(
                "core",
                expected_sequence=0,
                expected_revision=0,
                next_sequence=2,
            )

        first = new_artifact_version(
            logical_key="memory:self-view",
            artifact_kind="self_narrative",
            content="旧的理解",
        )
        await bundle.living.append_artifact(first, expected_head_revision=0)
        head = await bundle.living.get_artifact_head("memory:self-view")
        assert head is not None and head.revision == 1
        second = new_artifact_version(
            logical_key="memory:self-view",
            artifact_kind="self_narrative",
            content="新的理解",
            parent_artifact_ids=(first.artifact_id,),
        )
        with pytest.raises(ArtifactHeadConflict):
            await bundle.living.append_artifact(second, expected_head_revision=0)

        claim = MemoryClaim(
            claim_id="claim-1",
            subject_key="user:name",
            content="名字仍需保留来源",
            claim_kind="identity_claim",
            source="explicit_user",
            authority="explicit_user",
            valid_from="2026-08-04T10:00:00+08:00",
            valid_to="",
            recorded_at="2026-08-04T10:00:02+08:00",
        )
        assert await bundle.epistemic.append_claim(claim) == claim

        left = await bundle.legacy_graph.get_or_create_file_node(
            "notes/left.md",
            "left",
            "左侧记忆",
        )
        right = await bundle.legacy_graph.get_or_create_file_node(
            "notes/right.md",
            "right",
            "右侧记忆",
        )
        edge = await bundle.legacy_graph.create_or_update_edge(
            left.node_id,
            right.node_id,
            EdgeType.RELATES.value,
            strength=0.7,
        )
        assert edge.weight == pytest.approx(0.7)
        assert (await bundle.legacy_graph.get_edges_from(left.node_id))[0].target_id == right.node_id
    finally:
        await asyncio.to_thread(db.close)
