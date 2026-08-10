"""Resilience and content-free health contracts for learning maintenance."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from plugins.life_engine.learning.auditor import (
    InsightAuditor,
)
from plugins.life_engine.learning.auditor import (
    _resolve_timeout_seconds as _resolve_audit_timeout_seconds,
)
from plugins.life_engine.learning.knowledge import SelfKnowledgeCompressor
from plugins.life_engine.learning.maintenance import (
    LearningMaintenanceEvent,
    LearningPhase,
    LearningPhaseOutcome,
    LocalLearningMaintenanceJournal,
)
from plugins.life_engine.learning.models import (
    Insight,
    InsightNextAction,
    InsightStatus,
)
from plugins.life_engine.learning.reflection import (
    ReflectionEngine,
    _resolve_timeout_seconds,
)
from plugins.life_engine.learning.reflection_queue import (
    MAX_PENDING_REFLECTIONS,
    LearningReflectionJob,
)
from plugins.life_engine.learning.scheduler import LearningScheduler
from plugins.life_engine.learning.skill_distiller import SkillDistiller
from plugins.life_engine.learning.skill_store import SkillPattern, SkillStore
from plugins.life_engine.learning.store import InsightStore
from plugins.life_engine.storage.learning_contracts import (
    LearningCommitResult,
    LearningEventRecord,
)


class _MemoryJournal:
    def __init__(self) -> None:
        self.events: list[LearningMaintenanceEvent] = []

    async def initialize(self) -> None:
        return None

    async def append(self, event: LearningMaintenanceEvent) -> None:
        self.events.append(event)

    def health_snapshot(self) -> dict[str, Any]:
        return {"status": "healthy", "event_count": len(self.events)}


class _SharedLearningEventStore:
    """Minimal append-only event port; projection writes are forbidden."""

    def __init__(self) -> None:
        self.records: list[LearningEventRecord] = []
        self.projection_write_count = 0
        self.read_calls: list[tuple[int, int, tuple[str, ...]]] = []

    async def commit(self, *, events, projections) -> LearningCommitResult:
        self.projection_write_count += len(projections)
        if projections:
            raise AssertionError("enqueue evidence must not write a projection")
        committed: list[LearningEventRecord] = []
        for draft in events:
            existing = next(
                (
                    record
                    for record in self.records
                    if record.occurrence_id == draft.occurrence_id
                ),
                None,
            )
            if existing is not None:
                committed.append(existing)
                continue
            record = LearningEventRecord(
                position=len(self.records) + 1,
                occurrence_id=draft.occurrence_id,
                event_kind=draft.event_kind,
                occurred_at=draft.occurred_at,
                recorded_at=draft.occurred_at,
                source=draft.source,
                actor_consciousness_instance_id=(
                    draft.actor_consciousness_instance_id
                ),
                subject_revision=draft.subject_revision,
                provenance=dict(draft.provenance),
                payload=dict(draft.payload),
                event_sha256="0" * 64,
            )
            self.records.append(record)
            committed.append(record)
        return LearningCommitResult(events=tuple(committed), projections=())

    async def read_events(
        self,
        after_position: int,
        *,
        limit: int = 100,
        event_kinds: tuple[str, ...] = (),
    ) -> list[LearningEventRecord]:
        self.read_calls.append((after_position, limit, event_kinds))
        rows = [
            record
            for record in self.records
            if record.position > after_position
            and (not event_kinds or record.event_kind in event_kinds)
        ]
        return rows[:limit]

    async def health_snapshot(self) -> dict[str, Any]:
        return {
            "status": "healthy",
            "event_count": len(self.records),
            "event_frontier": len(self.records),
        }


def _scheduler(tmp_path: Path, journal: Any) -> LearningScheduler:
    scheduler = LearningScheduler(
        workspace_path=tmp_path,
        maintenance_journal=journal,
    )
    scheduler._epistemic_backfilled = True
    return scheduler


def _set_due_phases(scheduler: LearningScheduler) -> None:
    scheduler._should_audit = lambda: True  # type: ignore[method-assign]
    scheduler.compressor.should_compress = lambda: True  # type: ignore[method-assign]
    scheduler.distiller.should_distill = lambda: True  # type: ignore[method-assign]
    scheduler._should_snapshot_metrics = lambda: True  # type: ignore[method-assign]
    scheduler._should_check_staleness = lambda: True  # type: ignore[method-assign]


async def test_phase_failure_does_not_starve_later_maintenance(
    tmp_path: Path,
) -> None:
    journal = _MemoryJournal()
    scheduler = _scheduler(tmp_path, journal)
    _set_due_phases(scheduler)
    calls: list[str] = []

    async def audit() -> None:
        calls.append("audit")

    async def compression() -> None:
        calls.append("compression")
        raise RuntimeError("private prompt fragment must not enter health")

    async def distillation() -> None:
        calls.append("distillation")

    async def metrics() -> None:
        calls.append("metrics")

    async def staleness() -> None:
        calls.append("staleness")

    scheduler._maybe_run_audit = audit  # type: ignore[method-assign]
    scheduler._maybe_run_compression = compression  # type: ignore[method-assign]
    scheduler._maybe_run_distillation = distillation  # type: ignore[method-assign]
    scheduler._maybe_snapshot_metrics = metrics  # type: ignore[method-assign]
    scheduler._maybe_check_staleness = staleness  # type: ignore[method-assign]

    await scheduler.on_heartbeat()

    assert calls == [
        "audit",
        "compression",
        "distillation",
        "metrics",
        "staleness",
    ]
    final_by_phase = {event.phase: event for event in journal.events}
    assert final_by_phase["compression"].outcome == "failed"
    assert final_by_phase["compression"].error_type == "RuntimeError"
    assert len(final_by_phase["compression"].error_fingerprint) == 64
    assert final_by_phase["distillation"].outcome == "succeeded"
    assert "private prompt fragment" not in json.dumps(
        [event.to_dict() for event in journal.events]
    )


async def test_missing_start_evidence_fails_only_that_phase_closed(
    tmp_path: Path,
) -> None:
    class _RejectCompressionStart(_MemoryJournal):
        async def append(self, event: LearningMaintenanceEvent) -> None:
            if (
                event.phase == LearningPhase.COMPRESSION.value
                and event.outcome == LearningPhaseOutcome.STARTED.value
            ):
                raise OSError("injected journal outage")
            await super().append(event)

    journal = _RejectCompressionStart()
    scheduler = _scheduler(tmp_path, journal)
    scheduler._should_audit = lambda: False  # type: ignore[method-assign]
    scheduler.compressor.should_compress = lambda: True  # type: ignore[method-assign]
    scheduler.distiller.should_distill = lambda: True  # type: ignore[method-assign]
    scheduler._should_snapshot_metrics = lambda: False  # type: ignore[method-assign]
    scheduler._should_check_staleness = lambda: False  # type: ignore[method-assign]
    calls: list[str] = []

    async def compression() -> None:
        calls.append("compression")

    async def distillation() -> None:
        calls.append("distillation")

    scheduler._maybe_run_compression = compression  # type: ignore[method-assign]
    scheduler._maybe_run_distillation = distillation  # type: ignore[method-assign]

    await scheduler.on_heartbeat()

    assert calls == ["distillation"]
    assert journal.events[-1].phase == LearningPhase.DISTILLATION.value
    assert journal.events[-1].outcome == LearningPhaseOutcome.SUCCEEDED.value


async def test_overlapping_heartbeats_are_serialized(tmp_path: Path) -> None:
    journal = _MemoryJournal()
    scheduler = _scheduler(tmp_path, journal)
    scheduler._should_audit = lambda: False  # type: ignore[method-assign]
    scheduler.compressor.should_compress = lambda: False  # type: ignore[method-assign]
    scheduler.distiller.should_distill = lambda: False  # type: ignore[method-assign]
    scheduler._should_snapshot_metrics = lambda: True  # type: ignore[method-assign]
    scheduler._should_check_staleness = lambda: False  # type: ignore[method-assign]
    active = 0
    maximum_active = 0

    async def metrics() -> None:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0)
        active -= 1

    scheduler._maybe_snapshot_metrics = metrics  # type: ignore[method-assign]

    await asyncio.gather(scheduler.on_heartbeat(), scheduler.on_heartbeat())

    assert maximum_active == 1
    succeeded = [
        event
        for event in journal.events
        if event.outcome == LearningPhaseOutcome.SUCCEEDED.value
    ]
    assert len(succeeded) == 2
    assert len({event.run_id for event in succeeded}) == 2


async def test_local_journal_restores_latest_content_free_health(
    tmp_path: Path,
) -> None:
    journal = LocalLearningMaintenanceJournal(tmp_path)
    started_at = datetime.now(UTC)
    await journal.append(
        LearningMaintenanceEvent.started(
            run_id="run-contract",
            phase=LearningPhase.AUDIT,
            started_at=started_at,
            pending_count=3,
        )
    )
    await journal.append(
        LearningMaintenanceEvent.succeeded(
            run_id="run-contract",
            phase=LearningPhase.AUDIT,
            started_at=started_at,
            pending_count=3,
        )
    )

    restarted = LocalLearningMaintenanceJournal(tmp_path)
    await restarted.initialize()
    health = restarted.health_snapshot()

    assert health["status"] == "healthy"
    assert health["latest_by_phase"]["audit"]["outcome"] == "succeeded"
    assert health["latest_by_phase"]["audit"]["pending_count"] == 3
    assert health["observed_events"] == 2


async def test_failed_reflection_is_preserved_and_retried_without_error_content(
    tmp_path: Path,
    monkeypatch,
) -> None:
    scheduler = _scheduler(tmp_path, _MemoryJournal())
    calls = 0

    async def call_llm(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("private prompt fragment")
        return '{"insights": []}'

    monkeypatch.setattr(scheduler.reflection, "_call_llm", call_llm)
    try:
        await scheduler.submit_reflection(
            reflection_kind="interaction",
            reflection_text="private interaction",
            context="private context",
            source_event_ids=["life-event:one"],
        )
    except TimeoutError:
        pass
    else:  # pragma: no cover - contract assertion
        raise AssertionError("reflection failure must remain observable")

    state = scheduler.store.load_state()
    pending = state["pending_reflections_v1"]
    assert len(pending) == 1
    assert pending[0]["attempt_count"] == 1
    assert pending[0]["last_error_type"] == "TimeoutError"
    assert "private prompt fragment" not in json.dumps(pending)
    health = scheduler.get_state()["reflection_queue"]
    assert health["status"] == "degraded"
    assert health["pending_count"] == 1
    assert "private" not in json.dumps(health)

    now = datetime.now(UTC).isoformat()
    pending[0]["next_attempt_at"] = now
    state["reflection_runtime_v1"]["global_next_attempt_at"] = now
    scheduler.store.save_state(state)
    result = await scheduler._run_pending_reflection()

    assert result is not None
    assert result[1] == []
    assert scheduler.get_state()["reflection_queue"]["pending_count"] == 0
    assert calls == 2


async def test_live_interaction_only_enqueues_and_wakes_background_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Foreground expression must never await a reflection model round trip."""

    scheduler = _scheduler(tmp_path, _MemoryJournal())
    called = False

    async def forbidden_call(_prompt: str) -> str:
        nonlocal called
        called = True
        raise AssertionError("live interaction must not call the learning model")

    monkeypatch.setattr(scheduler.reflection, "_call_llm", forbidden_call)

    await asyncio.wait_for(
        scheduler.on_interaction_end(
            interaction_text="one completed situated interaction",
            context="bounded context",
            source_event_ids=["life-event:foreground"],
            actor_consciousness_instance_id="consciousness:chat",
        ),
        timeout=1,
    )

    assert called is False
    assert scheduler._maintenance_wakeup.is_set()
    pending = scheduler.store.load_state()["pending_reflections_v1"]
    assert len(pending) == 1
    assert pending[0]["actor_consciousness_instance_id"] == "consciousness:chat"


async def test_cross_node_enqueue_is_event_only_and_owner_projects_once(
    tmp_path: Path,
) -> None:
    """Any node may append evidence; the owner alone advances queue state."""

    event_store = _SharedLearningEventStore()
    producer = LearningScheduler(
        workspace_path=tmp_path / "producer",
        learning_event_store=event_store,
    )
    owner = LearningScheduler(
        workspace_path=tmp_path / "owner",
        learning_event_store=event_store,
    )
    flush_calls = 0

    async def _flush() -> None:
        nonlocal flush_calls
        flush_calls += 1

    owner.flush = _flush  # type: ignore[method-assign]

    job_id = await producer.enqueue_reflection(
        reflection_kind="interaction",
        reflection_text="one cross-node situated experience",
        context="bounded",
        source_event_ids=["life-event:cross-node"],
        actor_consciousness_instance_id="chat_global",
    )

    assert producer._reflection_jobs() == []
    assert event_store.projection_write_count == 0
    assert len(event_store.records) == 1
    assert event_store.records[0].occurrence_id == job_id

    assert await owner._ingest_reflection_events() == 1
    projected = owner._reflection_jobs()
    assert len(projected) == 1
    assert projected[0].job_id == job_id
    assert projected[0].actor_consciousness_instance_id == "chat_global"
    assert flush_calls == 1
    assert await owner._ingest_reflection_events() == 0
    assert flush_calls == 1
    assert len(owner._reflection_jobs()) == 1
    assert (
        owner.store.load_state()["reflection_runtime_v1"]["event_cursor"] == 1
    )
    assert all(
        event_kinds == ("reflection.enqueued",)
        for _, _, event_kinds in event_store.read_calls
    )


async def test_legacy_missing_cursor_filters_large_unrelated_events(
    tmp_path: Path,
) -> None:
    """A legacy projection never materializes unrelated snapshot payloads."""

    event_store = _SharedLearningEventStore()
    event_store.records.append(
        LearningEventRecord(
            position=1,
            occurrence_id="snapshot-1",
            event_kind="learning_insights.snapshot",
            occurred_at="2026-08-10T00:00:00+00:00",
            recorded_at="2026-08-10T00:00:00+00:00",
            source="learning.projector",
            actor_consciousness_instance_id="",
            subject_revision="",
            provenance={},
            payload={"oversized": "x" * (1024 * 1024)},
            event_sha256="0" * 64,
        )
    )
    producer = LearningScheduler(
        workspace_path=tmp_path / "producer",
        learning_event_store=event_store,
    )
    owner = LearningScheduler(
        workspace_path=tmp_path / "owner",
        learning_event_store=event_store,
    )
    job_id = await producer.enqueue_reflection(
        reflection_kind="interaction",
        reflection_text="one bounded reflection",
        source_event_ids=["life-event:filtered"],
        actor_consciousness_instance_id="chat_global",
    )

    assert await owner._ingest_reflection_events() == 1
    assert owner._reflection_jobs()[0].job_id == job_id
    assert event_store.read_calls == [(0, 500, ("reflection.enqueued",))]
    assert owner.store.load_state()["reflection_event_cursor_v1"] == 2


async def test_reflection_cursor_uses_opaque_source_frontier(
    tmp_path: Path,
) -> None:
    """Sparse positions advance to the captured authority high-water."""

    event_store = _SharedLearningEventStore()
    producer = LearningScheduler(
        workspace_path=tmp_path / "producer",
        learning_event_store=event_store,
    )
    owner = LearningScheduler(
        workspace_path=tmp_path / "owner",
        learning_event_store=event_store,
    )
    await producer.enqueue_reflection(
        reflection_kind="interaction",
        reflection_text="one sparse-position reflection",
        source_event_ids=["life-event:sparse"],
        actor_consciousness_instance_id="chat_global",
    )
    event_store.records[0] = replace(event_store.records[0], position=800)

    async def sparse_health() -> dict[str, Any]:
        return {"status": "healthy", "event_count": 1, "event_frontier": 900}

    event_store.health_snapshot = sparse_health  # type: ignore[method-assign]

    assert await owner._ingest_reflection_events() == 1
    state = owner.store.load_state()
    assert state["reflection_event_cursor_v1"] == 900
    assert state["reflection_runtime_v1"]["event_frontier"] == 900


async def test_concurrent_reflection_after_frontier_waits_for_next_pass(
    tmp_path: Path,
) -> None:
    """A post-frontier append cannot leak into the captured delivery window."""

    event_store = _SharedLearningEventStore()
    event_store.records.append(
        LearningEventRecord(
            position=1,
            occurrence_id="unrelated-1",
            event_kind="learning_skills.snapshot",
            occurred_at="2026-08-10T00:00:00+00:00",
            recorded_at="2026-08-10T00:00:00+00:00",
            source="learning.projector",
            actor_consciousness_instance_id="",
            subject_revision="",
            provenance={},
            payload={},
            event_sha256="0" * 64,
        )
    )
    producer = LearningScheduler(
        workspace_path=tmp_path / "producer",
        learning_event_store=event_store,
    )
    owner = LearningScheduler(
        workspace_path=tmp_path / "owner",
        learning_event_store=event_store,
    )
    original_read = event_store.read_events
    appended_job_id = ""

    async def append_during_first_read(
        after_position: int,
        *,
        limit: int = 100,
        event_kinds: tuple[str, ...] = (),
    ) -> list[LearningEventRecord]:
        nonlocal appended_job_id
        if not appended_job_id:
            appended_job_id = await producer.enqueue_reflection(
                reflection_kind="interaction",
                reflection_text="one concurrent reflection",
                source_event_ids=["life-event:concurrent"],
                actor_consciousness_instance_id="chat_global",
            )
        return await original_read(
            after_position,
            limit=limit,
            event_kinds=event_kinds,
        )

    event_store.read_events = append_during_first_read  # type: ignore[method-assign]

    assert await owner._ingest_reflection_events() == 0
    assert owner.store.load_state()["reflection_event_cursor_v1"] == 1
    assert owner._reflection_jobs() == []
    assert await owner._ingest_reflection_events() == 1
    assert owner._reflection_jobs()[0].job_id == appended_job_id
    assert owner.store.load_state()["reflection_event_cursor_v1"] == 2


async def test_event_cursor_stops_before_capacity_without_losing_evidence(
    tmp_path: Path,
) -> None:
    """A full derived queue never acknowledges an unprojected immutable fact."""

    event_store = _SharedLearningEventStore()
    producer = LearningScheduler(
        workspace_path=tmp_path / "producer",
        learning_event_store=event_store,
    )
    owner = LearningScheduler(
        workspace_path=tmp_path / "owner",
        learning_event_store=event_store,
    )
    for index in range(MAX_PENDING_REFLECTIONS + 1):
        await producer.enqueue_reflection(
            reflection_kind="interaction",
            reflection_text=f"bounded experience {index}",
            source_event_ids=[f"life-event:capacity:{index}"],
            actor_consciousness_instance_id="chat_global",
        )

    assert await owner._ingest_reflection_events() == MAX_PENDING_REFLECTIONS
    state = owner.store.load_state()
    assert len(owner._reflection_jobs()) == MAX_PENDING_REFLECTIONS
    assert state["reflection_event_cursor_v1"] == MAX_PENDING_REFLECTIONS
    assert len(event_store.records) == MAX_PENDING_REFLECTIONS + 1
    queue_health = owner.get_state()["reflection_queue"]
    assert queue_health["event_cursor"] == MAX_PENDING_REFLECTIONS
    assert queue_health["event_frontier"] == MAX_PENDING_REFLECTIONS + 1
    assert queue_health["unprojected_event_count"] == 1
    assert queue_health["total_pending_evidence_count"] == (
        MAX_PENDING_REFLECTIONS + 1
    )
    assert "event_projection_lag" in queue_health["reasons"]

    _queue_jobs(owner, owner._reflection_jobs()[1:])
    assert await owner._ingest_reflection_events() == 1
    state = owner.store.load_state()
    assert len(owner._reflection_jobs()) == MAX_PENDING_REFLECTIONS
    assert state["reflection_event_cursor_v1"] == MAX_PENDING_REFLECTIONS + 1
    assert len(event_store.records) == MAX_PENDING_REFLECTIONS + 1
    queue_health = owner.get_state()["reflection_queue"]
    assert queue_health["unprojected_event_count"] == 0
    assert "event_projection_lag" not in queue_health["reasons"]


async def test_independent_worker_runs_without_foreground_heartbeat(
    tmp_path: Path,
) -> None:
    """The service-owned worker performs maintenance after an explicit wake."""

    scheduler = _scheduler(tmp_path, _MemoryJournal())
    stop_event = asyncio.Event()
    entered = asyncio.Event()
    cycles = 0

    async def cycle() -> None:
        nonlocal cycles
        cycles += 1
        entered.set()
        stop_event.set()

    scheduler.on_heartbeat = cycle  # type: ignore[method-assign]

    await asyncio.wait_for(
        scheduler.run(stop_event, poll_interval_seconds=60),
        timeout=1,
    )

    assert entered.is_set()
    assert cycles == 1
    assert scheduler._worker_running is False
    assert scheduler._worker_last_completed_at


def test_old_never_attempted_backlog_degrades_whole_learning_health(
    tmp_path: Path,
) -> None:
    """A stalled queue cannot be hidden by otherwise healthy projections."""

    scheduler = _scheduler(tmp_path, _MemoryJournal())
    old = LearningReflectionJob.create(
        reflection_kind="interaction",
        reflection_text="an experience still waiting to be understood",
        context="",
        source_event_ids=["life-event:stalled"],
        created_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
    )
    _queue_jobs(scheduler, [old])

    state = scheduler.get_state()
    queue = state["reflection_queue"]

    assert state["status"] == "degraded"
    assert queue["status"] == "degraded"
    assert queue["due_count"] == 1
    assert queue["never_attempted_count"] == 1
    assert queue["oldest_age_seconds"] >= 7_199
    assert "oldest_job_stalled" in queue["reasons"]
    assert queue["capacity"] == 512
    assert queue["nominal_drain_capacity_per_day"] == 288.0


async def test_reflection_requested_during_cooldown_stays_queued(
    tmp_path: Path,
) -> None:
    scheduler = _scheduler(tmp_path, _MemoryJournal())
    scheduler.reflection._last_reflection_at = datetime.now(UTC).timestamp()

    result = await scheduler.submit_reflection(
        reflection_kind="introspection",
        reflection_text="preserve this request",
        source_event_ids=["life-event:cooldown"],
    )

    assert result is None
    assert scheduler.get_state()["reflection_queue"]["pending_count"] == 1


def test_corrupt_learning_state_fails_closed_without_overwrite(tmp_path: Path) -> None:
    store = InsightStore(tmp_path)
    store._ensure_dirs()
    original = b'{"pending_reflections_v1": ['
    store.state_path.write_bytes(original)

    try:
        store.load_state()
    except RuntimeError as exc:
        assert str(exc) == "LearningStateUnavailable"
    else:  # pragma: no cover - contract assertion
        raise AssertionError("corrupt durable state must not become an empty state")
    assert store.state_path.read_bytes() == original


def _queue_jobs(
    scheduler: LearningScheduler,
    jobs: list[LearningReflectionJob],
) -> None:
    state = scheduler.store.load_state()
    state["pending_reflections_v1"] = [job.to_dict() for job in jobs]
    scheduler.store.save_state(state)


async def test_ready_reflection_outranks_older_backed_off_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Selection follows readiness, not the durable list's insertion order."""

    scheduler = _scheduler(tmp_path, _MemoryJournal())
    now = datetime.now(UTC)
    backed_off = replace(
        LearningReflectionJob.create(
            reflection_kind="interaction",
            reflection_text="oldest experience, just retried",
            context="",
            source_event_ids=["life-event:head"],
            created_at=(now - timedelta(seconds=600)).isoformat(),
        ),
        attempt_count=40,
        next_attempt_at=(now - timedelta(seconds=5)).isoformat(),
    )
    never_tried = LearningReflectionJob.create(
        reflection_kind="interaction",
        reflection_text="never attempted experience",
        context="",
        source_event_ids=["life-event:tail"],
        created_at=(now - timedelta(seconds=300)).isoformat(),
    )
    # Insertion order is the failure mode: the backed-off job sits at the head.
    _queue_jobs(scheduler, [backed_off, never_tried])
    seen: list[str] = []

    async def call_llm(prompt: str) -> str:
        seen.append(prompt)
        return '{"insights": []}'

    monkeypatch.setattr(scheduler.reflection, "_call_llm", call_llm)

    result = await scheduler._run_pending_reflection()

    assert result is not None
    assert result[0] == never_tried.job_id
    assert len(seen) == 1
    assert "never attempted experience" in seen[0]
    remaining = scheduler.store.load_state()["pending_reflections_v1"]
    assert [item["job_id"] for item in remaining] == [backed_off.job_id]


async def test_submit_does_not_block_behind_an_inflight_reflection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A live interaction never waits on another reflection's LLM call."""

    scheduler = _scheduler(tmp_path, _MemoryJournal())
    released = asyncio.Event()
    entered = asyncio.Event()

    async def call_llm(_prompt: str) -> str:
        entered.set()
        await released.wait()
        return '{"insights": []}'

    monkeypatch.setattr(scheduler.reflection, "_call_llm", call_llm)
    _queue_jobs(
        scheduler,
        [
            LearningReflectionJob.create(
                reflection_kind="introspection",
                reflection_text="in flight experience",
                context="",
                source_event_ids=["life-event:inflight"],
            )
        ],
    )
    runner = asyncio.create_task(scheduler._run_pending_reflection())
    await asyncio.wait_for(entered.wait(), timeout=1)

    # The queue lock must be free while the LLM call is outstanding.
    submitted = await asyncio.wait_for(
        scheduler.submit_reflection(
            reflection_kind="interaction",
            reflection_text="fresh interaction",
            source_event_ids=["life-event:fresh"],
        ),
        timeout=1,
    )

    assert submitted is None
    pending = scheduler.store.load_state()["pending_reflections_v1"]
    assert len(pending) == 2

    released.set()
    assert await asyncio.wait_for(runner, timeout=1) is not None
    still_pending = scheduler.store.load_state()["pending_reflections_v1"]
    assert [item["reflection_text"] for item in still_pending] == ["fresh interaction"]


async def test_declined_reflection_stays_queued(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """An engine that declined to run must not retire the experience."""

    scheduler = _scheduler(tmp_path, _MemoryJournal())
    _queue_jobs(
        scheduler,
        [
            LearningReflectionJob.create(
                reflection_kind="interaction",
                reflection_text="must survive a declined run",
                context="",
                source_event_ids=["life-event:declined"],
            )
        ],
    )

    async def declined(**_kwargs: Any) -> list[Any]:
        # Same return value the engine uses for "cooling down, did nothing".
        return []

    monkeypatch.setattr(scheduler.reflection, "reflect_on_interaction", declined)

    result = await scheduler._run_pending_reflection()

    assert result is None
    pending = scheduler.store.load_state()["pending_reflections_v1"]
    assert len(pending) == 1
    assert pending[0]["attempt_count"] == 0


def test_reflection_timeout_reads_deployment_override(monkeypatch) -> None:
    """The timeout is deployment-tunable and fails loudly when malformed."""

    monkeypatch.delenv("ELYSIUM_REFLECTION_TIMEOUT_SECONDS", raising=False)
    assert _resolve_timeout_seconds(None) == 180.0

    monkeypatch.setenv("ELYSIUM_REFLECTION_TIMEOUT_SECONDS", "240")
    assert _resolve_timeout_seconds(None) == 240.0

    monkeypatch.setenv("ELYSIUM_REFLECTION_TIMEOUT_SECONDS", "0")
    try:
        _resolve_timeout_seconds(None)
    except ValueError as exc:
        assert "positive" in str(exc)
    else:  # pragma: no cover - contract assertion
        raise AssertionError("a non-positive timeout must not be silently accepted")

    monkeypatch.setenv("ELYSIUM_REFLECTION_TIMEOUT_SECONDS", "not-a-number")
    try:
        _resolve_timeout_seconds(None)
    except ValueError:
        pass
    else:  # pragma: no cover - contract assertion
        raise AssertionError("a malformed timeout must not fall back silently")


async def test_reflection_timeout_is_one_deadline_across_both_waits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Two sequential waits share one budget instead of doubling it."""

    store = InsightStore(tmp_path)
    engine = ReflectionEngine(
        store=store,
        workspace_path=tmp_path,
        timeout_seconds=10.0,
    )
    budgets: list[float | None] = []
    real_wait_for = asyncio.wait_for

    async def recording_wait_for(awaitable: Any, timeout: float | None = None) -> Any:
        budgets.append(timeout)
        return await real_wait_for(awaitable, timeout)

    async def send(**_kwargs: Any) -> Any:
        await asyncio.sleep(0.05)

        async def _response() -> str:
            return '{"insights": []}'

        return _response()

    class _Request:
        def add_payload(self, _payload: Any) -> None:
            return None

        def send(self, **kwargs: Any) -> Any:
            return send(**kwargs)

    monkeypatch.setattr(
        "plugins.life_engine.learning.reflection.create_llm_request",
        lambda *_a, **_k: _Request(),
    )
    monkeypatch.setattr(
        "plugins.life_engine.learning.reflection.get_model_set_by_task",
        lambda _name: object(),
    )
    monkeypatch.setattr(asyncio, "wait_for", recording_wait_for)

    assert await engine._call_llm("prompt") == '{"insights": []}'

    assert len(budgets) == 2
    first, second = budgets
    assert first == 10.0
    assert second is not None
    # The second wait inherits only what the first one left behind.
    assert second < first


def test_audit_timeout_reads_deployment_override(monkeypatch) -> None:
    """The audit ceiling is deployment-tunable and fails loudly when malformed."""

    monkeypatch.delenv("ELYSIUM_AUDIT_TIMEOUT_SECONDS", raising=False)
    assert _resolve_audit_timeout_seconds(None) == 180.0

    monkeypatch.setenv("ELYSIUM_AUDIT_TIMEOUT_SECONDS", "240")
    assert _resolve_audit_timeout_seconds(None) == 240.0

    monkeypatch.setenv("ELYSIUM_AUDIT_TIMEOUT_SECONDS", "0")
    try:
        _resolve_audit_timeout_seconds(None)
    except ValueError as exc:
        assert "positive" in str(exc)
    else:  # pragma: no cover - contract assertion
        raise AssertionError("a non-positive timeout must not be silently accepted")

    monkeypatch.setenv("ELYSIUM_AUDIT_TIMEOUT_SECONDS", "not-a-number")
    try:
        _resolve_audit_timeout_seconds(None)
    except ValueError:
        pass
    else:  # pragma: no cover - contract assertion
        raise AssertionError("a malformed timeout must not fall back silently")


def _stranded_insight(store: InsightStore) -> Insight:
    """Persist one insight and leave it stuck mid-audit."""

    insight = Insight.create(
        category="social",
        claim="a belief that was mid-audit when the process died",
        rationale="recorded so the reclaim contract has something to recover",
    )
    assert store.add_insight(insight)
    store.transition_status(
        insight.insight_id,
        InsightStatus.UNDER_REVIEW,
        reason="审计环调度",
    )
    return insight


async def test_stranded_under_review_insight_is_reclaimed(tmp_path: Path) -> None:
    """An audit cut off mid-flight must not remove the belief from review."""

    store = InsightStore(tmp_path)
    auditor = InsightAuditor(store=store, workspace_path=tmp_path)
    insight = _stranded_insight(store)

    # While stranded it is invisible: can_review admits neither under_review.
    assert store.list_candidates_for_review() == []

    assert await auditor.reclaim_stranded_reviews() == 1

    recovered = store.get_insight(insight.insight_id)
    assert recovered is not None
    assert recovered.status == InsightStatus.CANDIDATE.value
    assert recovered.next_action == InsightNextAction.AWAIT_REVIEW.value
    assert recovered.can_review
    # Recovery is not a verdict: no audit record, no review tally, no rewrite.
    assert recovered.audit_history == []
    assert recovered.review_count == 0
    assert recovered.claim == insight.claim


async def test_cancelled_audit_rolls_back_instead_of_stranding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """CancelledError is not an Exception, so the rollback must still run."""

    store = InsightStore(tmp_path)
    auditor = InsightAuditor(store=store, workspace_path=tmp_path)
    insight = Insight.create(
        category="social",
        claim="a belief audited while the heartbeat was being cancelled",
        rationale="shutdown cancels the task mid-call",
    )
    assert store.add_insight(insight)

    async def cancelled_call(_insight: Insight) -> str:
        raise asyncio.CancelledError

    monkeypatch.setattr(auditor, "_call_llm", cancelled_call)

    try:
        await auditor._audit_single(insight)
    except asyncio.CancelledError:
        pass
    else:  # pragma: no cover - contract assertion
        raise AssertionError("cancellation must keep propagating after rollback")

    recovered = store.get_insight(insight.insight_id)
    assert recovered is not None
    assert recovered.status == InsightStatus.CANDIDATE.value
    assert recovered.can_review


async def test_failed_audit_propagates_after_rollback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A provider failure is a failed phase, not a successful empty verdict."""

    store = InsightStore(tmp_path)
    auditor = InsightAuditor(store=store, workspace_path=tmp_path)
    insight = Insight.create(
        category="social",
        claim="a belief whose independent review timed out",
        rationale="the failure must remain operationally visible",
    )
    assert store.add_insight(insight)

    async def failed_call(_insight: Insight) -> str:
        raise TimeoutError("private provider detail")

    monkeypatch.setattr(auditor, "_call_llm", failed_call)

    try:
        await auditor._audit_single(insight)
    except TimeoutError:
        pass
    else:  # pragma: no cover - contract assertion
        raise AssertionError("ordinary audit failure must propagate")

    recovered = store.get_insight(insight.insight_id)
    assert recovered is not None
    assert recovered.status == InsightStatus.CANDIDATE.value
    assert recovered.can_review


def test_malformed_selection_outputs_are_failures(tmp_path: Path) -> None:
    """Missing decisions cannot masquerade as a healthy explicit rejection."""

    compressor = SelfKnowledgeCompressor(
        store=InsightStore(tmp_path),
        workspace_path=tmp_path,
    )
    invalid_outputs = ("[]", "{}", '{"promote": "no"}')
    for raw in invalid_outputs:
        try:
            compressor._parse_gate_result(raw)
        except ValueError:
            pass
        else:  # pragma: no cover - contract assertion
            raise AssertionError("knowledge gate must reject malformed output")

        try:
            SkillDistiller._parse_gate_result(raw)
        except ValueError:
            pass
        else:  # pragma: no cover - contract assertion
            raise AssertionError("skill gate must reject malformed output")

    for raw in ("[]", "{}", '{"description": "only half"}'):
        try:
            SkillDistiller._parse_distill_result(raw)
        except ValueError:
            pass
        else:  # pragma: no cover - contract assertion
            raise AssertionError("skill distillation must reject malformed output")


async def test_scheduler_reclaims_before_the_audit_gate(tmp_path: Path) -> None:
    """The gate needs candidates, and a stranded insight is not one."""

    journal = _MemoryJournal()
    scheduler = _scheduler(tmp_path, journal)
    insight = _stranded_insight(scheduler.store)

    # The gate is shut precisely because the only work left is stranded, so a
    # reclaim placed after it could never run.
    assert scheduler._should_audit() is False

    cycles = 0

    async def run_audit_cycle() -> list[Any]:
        nonlocal cycles
        cycles += 1
        return []

    scheduler.auditor.run_audit_cycle = run_audit_cycle  # type: ignore[method-assign]

    await scheduler._maybe_run_audit()

    recovered = scheduler.store.get_insight(insight.insight_id)
    assert recovered is not None
    assert recovered.status == InsightStatus.CANDIDATE.value
    assert recovered.can_review
    # Reclaim ran first, so the same pass found the gate open.
    assert cycles == 1


def test_reflection_projects_real_skill_pattern_names(tmp_path: Path) -> None:
    store = InsightStore(tmp_path)
    skill_store = SkillStore(tmp_path)
    pattern = SkillPattern.create(
        name="temperature-before-proof",
        description="recognizable warmth precedes identity proof",
        instructions="respond through the same relationship before listing facts",
    )
    assert skill_store.add_skill(pattern) is True
    reflection = ReflectionEngine(
        store=store,
        workspace_path=tmp_path,
        skill_store=skill_store,
    )

    section = reflection._build_skill_section()

    assert "<your_skills>" in section
    assert "temperature-before-proof" in section
