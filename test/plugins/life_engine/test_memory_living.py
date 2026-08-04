"""Contracts for traceable versions, interpretations, and living recall."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from plugins.life_engine.memory.living import (
    ArtifactHeadConflict,
    CoRecallEvent,
    InterpretationSource,
    MemoryDerivation,
    MemoryInterpretation,
    RecallEvent,
    append_artifact_version,
    append_corecall_event,
    append_interpretation,
    append_recall_events,
    begin_recall_episode,
    create_living_memory_schema,
    get_artifact_head,
    get_artifact_head_state,
    list_artifact_history,
    list_association_evidence,
    list_interpretations,
    new_artifact_version,
    rebuild_association_projection,
    search_interpretations,
)
from plugins.life_engine.memory.health import collect_health_snapshot
from plugins.life_engine.memory.search import SearchResult
from plugins.life_engine.memory.service import LifeMemoryService


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    create_living_memory_schema(db)
    return db


def test_artifact_revisions_preserve_old_content_and_open_provenance() -> None:
    db = _db()
    old = new_artifact_version(
        logical_key="MEMORY.md",
        artifact_kind="memory_document",
        content="我曾经认为离开意味着失去。",
        authored_by="elysia",
        recorded_at="2026-08-01T01:00:00+08:00",
    )
    append_artifact_version(db, old)
    current = new_artifact_version(
        logical_key="MEMORY.md",
        artifact_kind="memory_document",
        content="现在我认为离开也可能是保留选择。",
        parent_artifact_ids=(old.artifact_id,),
        authored_by="elysia",
        recorded_at="2026-08-02T01:00:00+08:00",
    )
    append_artifact_version(
        db,
        current,
        derivations=(
            MemoryDerivation(
                derivation_id="derive-1",
                generated_artifact_id=current.artifact_id,
                used_artifact_id=old.artifact_id,
                predicate="在这次经历后重新理解",
                reason="新的经历改变了我看待离开的方式",
                actor="elysia",
                recorded_at="2026-08-02T01:00:00+08:00",
            ),
        ),
    )

    history = list_artifact_history(db, "MEMORY.md")
    assert [item.content for item in history] == [old.content, current.content]
    assert get_artifact_head(db, "MEMORY.md") == current
    assert db.execute(
        "SELECT predicate FROM memory_artifact_derivations"
    ).fetchone()[0] == "在这次经历后重新理解"
    with pytest.raises(sqlite3.IntegrityError, match="LivingMemoryRecordImmutable"):
        db.execute(
            "UPDATE memory_artifact_versions SET content = '覆盖' WHERE artifact_id = ?",
            (old.artifact_id,),
        )


def test_artifact_head_uses_expected_revision_cas() -> None:
    db = _db()
    first = new_artifact_version(
        logical_key="MEMORY.md",
        artifact_kind="memory_document",
        content="first",
    )
    append_artifact_version(db, first, expected_head_revision=0)
    assert get_artifact_head_state(db, "MEMORY.md").revision == 1

    second = new_artifact_version(
        logical_key="MEMORY.md",
        artifact_kind="memory_document",
        content="second",
        parent_artifact_ids=(first.artifact_id,),
    )
    with pytest.raises(ArtifactHeadConflict, match="expected=0, actual=1"):
        append_artifact_version(db, second, expected_head_revision=0)
    assert list_artifact_history(db, "MEMORY.md") == [first]

    append_artifact_version(db, second, expected_head_revision=1)
    head = get_artifact_head_state(db, "MEMORY.md")
    assert head is not None
    assert head.artifact_id == second.artifact_id
    assert head.revision == 2

    append_artifact_version(db, second, expected_head_revision=2)
    assert get_artifact_head_state(db, "MEMORY.md").revision == 2


def test_interpretations_can_evolve_without_resolving_each_other() -> None:
    db = _db()
    first = MemoryInterpretation(
        interpretation_id="interpretation-1",
        subject_id="experience:one",
        content="当时我把沉默理解成疏远。",
        authored_by="elysia",
        consciousness_instance_id="chat_global",
        recorded_at="2026-08-01T01:00:00+08:00",
    )
    second = MemoryInterpretation(
        interpretation_id="interpretation-2",
        subject_id="experience:one",
        content="后来我也能把那段沉默理解成彼此休息。",
        authored_by="elysia",
        consciousness_instance_id="chat_global",
        recorded_at="2026-08-02T01:00:00+08:00",
    )
    append_interpretation(
        db,
        first,
        sources=(
            InterpretationSource(
                interpretation_id=first.interpretation_id,
                entity_ref="experience:one",
                predicate="回望",
            ),
        ),
    )
    append_interpretation(
        db,
        second,
        sources=(
            InterpretationSource(
                interpretation_id=second.interpretation_id,
                entity_ref=f"interpretation:{first.interpretation_id}",
                predicate="重新框定",
            ),
        ),
    )

    assert list_interpretations(db, "experience:one") == [first, second]
    assert list_interpretations(
        db,
        "experience:one",
        recorded_as_of="2026-08-01T12:00:00+08:00",
    ) == [first]


def test_interpretation_retrieval_returns_provenance_and_respects_recorded_time() -> None:
    db = _db()
    for identifier, content, recorded_at in (
        ("old", "我曾把沉默理解成疏远", "2026-08-01T01:00:00+08:00"),
        ("new", "后来沉默也可以意味着休息", "2026-08-02T01:00:00+08:00"),
    ):
        interpretation = MemoryInterpretation(
            interpretation_id=identifier,
            subject_id="topic:沉默",
            content=content,
            authored_by="elysia",
            consciousness_instance_id="chat_global",
            recorded_at=recorded_at,
        )
        append_interpretation(
            db,
            interpretation,
            sources=(
                InterpretationSource(
                    interpretation_id=identifier,
                    entity_ref=f"life_event:{identifier}",
                    predicate="draws_from",
                ),
            ),
        )

    results = search_interpretations(
        db,
        "沉默",
        top_k=5,
        stream_scope=None,
        visibility=("private",),
        recorded_as_of="2026-08-01T12:00:00+08:00",
    )

    assert [item.interpretation.interpretation_id for item in results] == ["old"]
    assert results[0].sources[0].entity_ref == "life_event:old"
    assert results[0].retrieval_source in {
        "interpretation_fts",
        "interpretation_substring",
    }


def test_corecall_hyperedges_keep_signals_separate_and_rebuildable() -> None:
    db = _db()
    episode = begin_recall_episode(
        db,
        query="为什么那天让我想到后来那次谈话？",
        retrieval_intent="自由回望，不急着裁决",
        consciousness_instance_id="chat_global",
        stream_scope="private:ayer",
        context_key="chat_global/private:ayer",
        policy_version="living-recall-test",
        random_seed=42,
        context={"mood": "quiet"},
        episode_id="recall-1",
        recorded_at="2026-08-02T02:00:00+08:00",
    )
    append_recall_events(
        db,
        (
            RecallEvent(
                event_id="recall-event-1",
                episode_id=episode.episode_id,
                action="被意识看到",
                entity_ref="experience:one",
                ordinal=0,
                source="fts",
                recorded_at="2026-08-02T02:00:01+08:00",
            ),
            RecallEvent(
                event_id="recall-event-2",
                episode_id=episode.episode_id,
                action="被带入这次表达",
                entity_ref="witness:two",
                ordinal=1,
                source="contextual_association",
                recorded_at="2026-08-02T02:00:02+08:00",
            ),
        ),
    )
    append_corecall_event(
        db,
        CoRecallEvent(
            corecall_id="corecall-1",
            episode_id=episode.episode_id,
            context_key=episode.context_key,
            signal="共同进入意识",
            entity_refs=("experience:one", "witness:two", "claim:three"),
            actor="chat_global",
            reason="同一次回望中一起出现",
            recorded_at="2026-08-02T02:00:03+08:00",
        ),
    )
    append_corecall_event(
        db,
        CoRecallEvent(
            corecall_id="corecall-2",
            episode_id=episode.episode_id,
            context_key=episode.context_key,
            signal="共同用于表达",
            entity_refs=("experience:one", "witness:two"),
            actor="chat_global",
            reason="两段记忆共同支撑了表达",
            recorded_at="2026-08-02T02:00:04+08:00",
        ),
    )

    evidence = list_association_evidence(
        db,
        "experience:one",
        context_key=episode.context_key,
    )
    assert {item.signal for item in evidence} == {
        "共同进入意识",
        "共同用于表达",
    }
    assert not any(hasattr(item, "truth_score") for item in evidence)
    before = [
        tuple(row)
        for row in db.execute(
            """SELECT * FROM memory_association_projection
            ORDER BY source_ref, target_ref, signal"""
        )
    ]
    assert rebuild_association_projection(db) == 2
    after = [
        tuple(row)
        for row in db.execute(
            """SELECT * FROM memory_association_projection
            ORDER BY source_ref, target_ref, signal"""
        )
    ]
    assert after == before
    assert episode.random_seed == 42


def test_recall_actions_are_open_vocabulary_and_append_only() -> None:
    db = _db()
    episode = begin_recall_episode(
        db,
        query="想起一段旧事",
        episode_id="recall-open",
        random_seed=7,
    )
    event = RecallEvent(
        event_id="event-open",
        episode_id=episode.episode_id,
        action="她暂时把它放在心边但没有采用",
        entity_ref="artifact:anything",
        recorded_at="2026-08-02T03:00:00+08:00",
    )
    assert append_recall_events(db, (event,)) == (event,)
    with pytest.raises(sqlite3.IntegrityError, match="LivingMemoryRecordImmutable"):
        db.execute(
            "UPDATE memory_recall_events SET action = 'changed' WHERE event_id = ?",
            (event.event_id,),
        )


async def test_corecall_can_bring_an_interpretation_back_into_retrieval(
    tmp_path: Path,
) -> None:
    service = LifeMemoryService(tmp_path)
    service._vector_backend_enabled = False
    await service.initialize()
    interpretation = MemoryInterpretation(
        interpretation_id="associated-interpretation",
        subject_id="topic:rain",
        content="雨声让我重新理解那段等待",
        authored_by="elysia",
        consciousness_instance_id="life_engine",
        recorded_at="2026-08-02T04:00:00+08:00",
    )
    await service.record_memory_interpretation(
        interpretation,
        sources=(
            InterpretationSource(
                interpretation_id=interpretation.interpretation_id,
                entity_ref="life_event:rain",
            ),
        ),
    )
    episode = await service.begin_memory_recall(
        query="seed",
        context_key="life_engine/test",
        random_seed=12,
    )
    await service.append_memory_corecall(
        CoRecallEvent(
            corecall_id="corecall-associated-interpretation",
            episode_id=episode.episode_id,
            context_key=episode.context_key,
            signal="曾共同被想起",
            entity_refs=(
                "document:notes/seed.md",
                f"memory_interpretation:{interpretation.interpretation_id}",
            ),
            actor="life_engine",
            reason="一次旧的共同回忆",
            recorded_at="2026-08-02T04:01:00+08:00",
        )
    )

    results = await service.search_evidence_aware(
        "seed",
        top_k=5,
        document_results=[
            SearchResult(
                file_path="notes/seed.md",
                title="seed",
                snippet="seed",
                relevance=1.0,
                source="direct",
            )
        ],
        association_context_key=episode.context_key,
        association_random_seed=episode.random_seed,
    )

    associated = next(
        item for item in results if item.record_id == interpretation.interpretation_id
    )
    assert associated.source == "contextual_corecall"
    assert associated.metadata["association_signals"] == ["曾共同被想起"]
    assert associated.metadata["epistemic_note"] == (
        "co-recall changes accessibility, not truth"
    )
    await service.close()


async def test_document_association_expansion_loads_snippet_from_node_storage(
    tmp_path: Path,
) -> None:
    service = LifeMemoryService(tmp_path)
    service._vector_backend_enabled = False
    await service.initialize()
    await service.upsert_document("notes/seed.md", "seed body", title="Seed")
    target = await service.upsert_document(
        "notes/associated.md",
        "associated body that must be returned as the snippet",
        title="Associated",
    )
    episode = await service.begin_memory_recall(
        query="seed",
        context_key="life_engine/document-association",
        random_seed=19,
    )
    await service.append_memory_corecall(
        CoRecallEvent(
            corecall_id="corecall-associated-document",
            episode_id=episode.episode_id,
            context_key=episode.context_key,
            signal="共同进入意识",
            entity_refs=(
                "document:notes/seed.md",
                "document:notes/associated.md",
            ),
            actor="life_engine",
            reason="同一次回忆中一起出现",
            recorded_at="2026-08-03T11:36:00+08:00",
        )
    )

    expanded = await service.expand_living_document_associations(
        [
            SearchResult(
                file_path="notes/seed.md",
                title="Seed",
                snippet="seed body",
                relevance=1.0,
                source="direct",
            )
        ],
        context_key=episode.context_key,
        random_seed=episode.random_seed,
        limit=3,
    )

    associated = next(item for item in expanded if item.file_path == target.file_path)
    assert "associated body" in associated.snippet
    assert associated.source == "associated"
    await service.close()


def test_health_reports_living_ledgers_and_projection_drift(tmp_path: Path) -> None:
    db = _db()
    interpretation = MemoryInterpretation(
        interpretation_id="health-interpretation",
        subject_id="topic:health",
        content="一条可追溯解释",
        authored_by="elysia",
        consciousness_instance_id="life_engine",
        recorded_at="2026-08-02T05:00:00+08:00",
    )
    append_interpretation(db, interpretation)
    episode = begin_recall_episode(
        db,
        query="health",
        episode_id="health-recall",
        random_seed=9,
    )
    append_corecall_event(
        db,
        CoRecallEvent(
            corecall_id="health-corecall",
            episode_id=episode.episode_id,
            context_key="health",
            signal="co_exposed",
            entity_refs=("document:a.md", "memory_interpretation:health-interpretation"),
            actor="life_engine",
            reason="health test",
            recorded_at="2026-08-02T05:01:00+08:00",
        ),
    )

    snapshot = collect_health_snapshot(db, tmp_path)

    living = snapshot["living_memory"]
    assert living["counts"]["memory_interpretations"] == 1
    assert living["counts"]["memory_interpretation_fts"] == 1
    assert living["counts"]["memory_corecall_events"] == 1
    assert living["association_projection_drift"] is False
    db.execute("UPDATE memory_association_projection SET event_count = 2")
    assert collect_health_snapshot(db, tmp_path)["living_memory"][
        "association_projection_drift"
    ] is True
