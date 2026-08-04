"""Resilience and content-free health contracts for learning maintenance."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from plugins.life_engine.learning.maintenance import (
    LearningMaintenanceEvent,
    LearningPhase,
    LearningPhaseOutcome,
    LocalLearningMaintenanceJournal,
)
from plugins.life_engine.learning.reflection import ReflectionEngine
from plugins.life_engine.learning.scheduler import LearningScheduler
from plugins.life_engine.learning.skill_store import SkillPattern, SkillStore
from plugins.life_engine.learning.store import InsightStore


class _MemoryJournal:
    def __init__(self) -> None:
        self.events: list[LearningMaintenanceEvent] = []

    async def initialize(self) -> None:
        return None

    async def append(self, event: LearningMaintenanceEvent) -> None:
        self.events.append(event)

    def health_snapshot(self) -> dict[str, Any]:
        return {"status": "healthy", "event_count": len(self.events)}


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

    pending[0]["next_attempt_at"] = datetime.now(UTC).isoformat()
    scheduler.store.save_state(state)
    result = await scheduler._run_pending_reflection()

    assert result is not None
    assert result[1] == []
    assert scheduler.get_state()["reflection_queue"]["pending_count"] == 0
    assert calls == 2


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
