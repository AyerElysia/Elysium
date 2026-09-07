"""Opportunity-managed Learning keeps shared procedural state independent."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.life_engine.learning.maintenance import LearningMaintenanceEvent
from plugins.life_engine.learning.scheduler import LearningScheduler
from plugins.life_engine.learning.selectable import LearningMutationContext
from plugins.life_engine.learning.shared_runtime import SharedLearningProjectionRuntime
from plugins.life_engine.learning.skill_store import SkillPattern
from plugins.life_engine.learning.store import InsightStore
from plugins.life_engine.storage.learning_contracts import (
    LearningCommitResult,
    LearningEventDraft,
    LearningEventRecord,
    LearningProjection,
    LearningProjectionWrite,
)
from src.kernel.storage import canonical_json


class _MemoryLearningStore:
    def __init__(self) -> None:
        self.runtime = object()
        self.projections: dict[str, LearningProjection] = {}
        self.commits: list[
            tuple[list[LearningEventDraft], list[LearningProjectionWrite]]
        ] = []
        self.position = 0

    async def commit(
        self,
        *,
        events: list[LearningEventDraft],
        projections: list[LearningProjectionWrite],
    ) -> LearningCommitResult:
        self.commits.append((list(events), list(projections)))
        self.position += len(events)
        committed: list[LearningProjection] = []
        for write in projections:
            previous = self.projections.get(write.projection_name)
            assert write.expected_revision == (previous.revision if previous else 0)
            assert write.expected_source_frontier == (
                previous.source_frontier if previous else 0
            )
            payload = dict(write.payload)
            projection = LearningProjection(
                projection_name=write.projection_name,
                revision=write.expected_revision + 1,
                source_frontier=self.position,
                schema_version=write.schema_version,
                projector_version=write.projector_version,
                rebuild_state=write.rebuild_state,
                payload=payload,
                projection_sha256=hashlib.sha256(
                    canonical_json(payload).encode("utf-8")
                ).hexdigest(),
                updated_at="2026-09-05T00:00:00+00:00",
            )
            self.projections[write.projection_name] = projection
            committed.append(projection)
        return LearningCommitResult(events=(), projections=tuple(committed))

    async def read_events(
        self,
        after_position: int,
        *,
        limit: int = 100,
        event_kinds: tuple[str, ...] = (),
    ) -> list[LearningEventRecord]:
        return []

    async def event_by_occurrence(
        self,
        occurrence_id: str,
    ) -> LearningEventRecord | None:
        return None

    async def get_projection(
        self,
        projection_name: str,
    ) -> LearningProjection | None:
        return self.projections.get(projection_name)

    async def list_projections(self) -> list[LearningProjection]:
        return list(self.projections.values())

    async def health_snapshot(self) -> dict[str, object]:
        return {"status": "healthy"}


class _MemoryMaintenanceJournal:
    def __init__(self) -> None:
        self.initialized = 0
        self.events: list[LearningMaintenanceEvent] = []

    async def initialize(self) -> None:
        self.initialized += 1

    async def append(self, event: LearningMaintenanceEvent) -> None:
        self.events.append(event)

    def health_snapshot(self) -> dict[str, object]:
        return {"status": "healthy", "event_count": len(self.events)}


class _SubjectAuthority:
    async def current_subject_revision(self) -> str:
        return "b" * 64


def _skill() -> SkillPattern:
    return SkillPattern.create(
        name="bounded-review",
        description="Review one bounded artifact before deciding.",
        instructions="Read the exact artifact and preserve its provenance.",
    )


async def test_unclaimed_selected_state_hydrates_but_rejects_before_memory_change(
    tmp_path: Path,
) -> None:
    backend = _MemoryLearningStore()
    shared = SharedLearningProjectionRuntime(
        tmp_path,
        learning_store=backend,
        writer_instance_id="reader-only",
        writable=False,
    )

    await shared.initialize()

    assert shared.skill_store.list_skills() == []
    assert shared.store.load_state() == {}
    assert shared.decision_ledger is not None
    assert shared.storage_runtime is backend.runtime
    with pytest.raises(RuntimeError, match="SelectedLearningProjectionReadOnly"):
        shared.skill_store.add_skill(_skill())
    with pytest.raises(RuntimeError, match="SelectedLearningProjectionReadOnly"):
        shared.store.save_state({"would_have_been": "phantom"})
    assert shared.skill_store.list_skills() == []
    assert shared.store.load_state() == {}
    assert backend.commits == []


async def test_scheduler_borrows_shared_owner_and_delegates_explicit_flush(
    tmp_path: Path,
) -> None:
    backend = _MemoryLearningStore()
    shared = SharedLearningProjectionRuntime(
        tmp_path,
        learning_store=backend,
        writer_instance_id="service-owner",
        writable=True,
    )
    await shared.initialize()
    journal = _MemoryMaintenanceJournal()
    scheduler = LearningScheduler(
        workspace_path=tmp_path,
        shared_state=shared,
        maintenance_journal=journal,
        opportunity_managed=True,
        subject_review_enabled=False,
    )

    await scheduler.initialize()
    assert journal.initialized == 1
    with shared.mutation_context(
        LearningMutationContext(
            source="subject.skill_tool",
            actor_consciousness_instance_id="chat_global",
            subject_revision="a" * 64,
            provenance={"tool_call_id": "tool:1"},
        )
    ):
        assert scheduler.skill_store.add_skill(_skill()) is True
    await scheduler.flush()

    assert len(backend.commits) == 1
    assert backend.commits[0][0][0].actor_consciousness_instance_id == "chat_global"
    assert backend.commits[0][1][0].projection_name == "learning_skills"
    await scheduler.close()
    assert shared.closed is False
    assert shared.skill_store.get_skill_by_name("bounded-review") is not None
    await shared.close()
    assert shared.closed is True


async def test_scheduler_borrows_subject_revision_from_shared_owner(
    tmp_path: Path,
) -> None:
    shared = SharedLearningProjectionRuntime(
        tmp_path,
        subject_authority=_SubjectAuthority(),  # type: ignore[arg-type]
    )
    await shared.initialize()
    scheduler = LearningScheduler(
        workspace_path=tmp_path,
        shared_state=shared,
        maintenance_journal=_MemoryMaintenanceJournal(),
        opportunity_managed=True,
        subject_review_enabled=False,
    )

    await scheduler.initialize()

    assert await scheduler.current_subject_revision() == "b" * 64


async def test_opportunity_managed_scheduler_never_runs_automatic_semantic_work(
    tmp_path: Path,
) -> None:
    shared = SharedLearningProjectionRuntime(tmp_path)
    await shared.initialize()
    scheduler = LearningScheduler(
        workspace_path=tmp_path,
        shared_state=shared,
        maintenance_journal=_MemoryMaintenanceJournal(),
        opportunity_managed=True,
        subject_review_enabled=False,
    )
    await scheduler.initialize()
    scheduler.enqueue_reflection = AsyncMock(return_value="explicit-job")  # type: ignore[method-assign]
    scheduler._run_pending_reflection = AsyncMock(  # type: ignore[method-assign]
        return_value=("explicit-job", [])
    )
    scheduler.reconcile_subject_review_outcomes = AsyncMock()  # type: ignore[method-assign]

    await scheduler.on_interaction_end(interaction_text="automatic")
    await scheduler.on_thought_closed(thought_summary="automatic")
    await scheduler.on_attention_thread_closed(
        public_statement="automatic",
        source_event_ids=["event:1"],
        actor_consciousness_instance_id="chat_global",
    )
    await scheduler.on_heartbeat()
    await asyncio.wait_for(scheduler.run(asyncio.Event()), timeout=0.1)

    scheduler.enqueue_reflection.assert_not_awaited()
    scheduler.reconcile_subject_review_outcomes.assert_not_awaited()

    result = await scheduler.submit_reflection(
        reflection_kind="interaction",
        reflection_text="explicit subject operation",
        actor_consciousness_instance_id="chat_global",
    )
    assert result == []
    scheduler.enqueue_reflection.assert_awaited_once()
    scheduler._run_pending_reflection.assert_awaited_once_with(
        job_id="explicit-job"
    )
    state = scheduler.get_state()
    assert state["mode"] == "opportunity_managed"
    assert state["automatic_semantic_work"] is False
    assert state["worker"]["status"] == "disabled"


async def test_explicit_cognitive_stages_do_not_chain_into_each_other(
    tmp_path: Path,
) -> None:
    shared = SharedLearningProjectionRuntime(tmp_path)
    await shared.initialize()
    scheduler = LearningScheduler(
        workspace_path=tmp_path,
        shared_state=shared,
        maintenance_journal=_MemoryMaintenanceJournal(),
        opportunity_managed=True,
        subject_review_enabled=False,
    )
    await scheduler.initialize()
    scheduler._run_pending_reflection = AsyncMock(  # type: ignore[method-assign]
        return_value=("reflection:1", [])
    )
    scheduler.auditor.run_audit_cycle = AsyncMock(return_value=[])
    scheduler.compressor.run_compression = AsyncMock(return_value=True)
    scheduler.distiller.run_distillation = AsyncMock(return_value=True)
    scheduler._snapshot_metrics_now = Mock()  # type: ignore[method-assign]

    assert await scheduler.run_next_reflection_once() == ("reflection:1", [])
    scheduler._run_pending_reflection.assert_awaited_once()
    scheduler.auditor.run_audit_cycle.assert_not_awaited()
    scheduler.compressor.run_compression.assert_not_awaited()
    scheduler.distiller.run_distillation.assert_not_awaited()

    assert await scheduler.run_independent_audit_once() == []
    scheduler.auditor.run_audit_cycle.assert_awaited_once()
    scheduler.compressor.run_compression.assert_not_awaited()
    scheduler.distiller.run_distillation.assert_not_awaited()

    assert await scheduler.propose_knowledge_candidate_once() is True
    scheduler.compressor.run_compression.assert_awaited_once()
    scheduler.distiller.run_distillation.assert_not_awaited()

    assert await scheduler.distill_skill_candidate_once() is True
    scheduler.distiller.run_distillation.assert_awaited_once()


async def test_scheduler_close_and_quiesce_do_not_quiesce_borrowed_shared_state(
    tmp_path: Path,
) -> None:
    shared = SharedLearningProjectionRuntime(tmp_path)
    await shared.initialize()
    scheduler = LearningScheduler(
        workspace_path=tmp_path,
        shared_state=shared,
        maintenance_journal=_MemoryMaintenanceJournal(),
        opportunity_managed=True,
    )
    await scheduler.initialize()

    scheduler.quiesce_opportunity(reason="capability paused")
    assert shared.writable is True
    assert shared.closed is False
    await scheduler.close()
    assert shared.writable is True
    assert shared.closed is False


def test_legacy_scheduler_constructor_remains_available(tmp_path: Path) -> None:
    scheduler = LearningScheduler(
        workspace_path=tmp_path,
        subject_review_enabled=False,
    )

    assert isinstance(scheduler.store, InsightStore)
    assert scheduler.decision_ledger is None
    assert scheduler._shared_state is None


def test_scheduler_rejects_two_projection_owners(tmp_path: Path) -> None:
    backend = _MemoryLearningStore()
    shared = SharedLearningProjectionRuntime(
        tmp_path,
        learning_store=backend,
    )

    with pytest.raises(
        ValueError,
        match="LearningSchedulerSharedStateCannotAlsoConstructLearningStore",
    ):
        LearningScheduler(
            workspace_path=tmp_path,
            shared_state=shared,
            learning_store=backend,
        )
