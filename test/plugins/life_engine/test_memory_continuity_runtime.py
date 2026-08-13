"""Public, coherent runtime contracts for continuity review."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.memory.continuity_delivery import (
    get_memory_continuity_delivery_coordinator,
)
from plugins.life_engine.memory.continuity_session import (
    ContinuityReviewRuntimeUnavailable,
)
from plugins.life_engine.memory.continuity_tools import (
    LifeMemoryContinuityReviewSessionTool,
    resolve_continuity_review_tool_runtime,
)
from plugins.life_engine.service.consciousness import ConsciousnessInstance
from plugins.life_engine.service.core import LifeEngineService


class _Registry:
    def __init__(self, instance: ConsciousnessInstance | None) -> None:
        self.instance = instance

    def get_for_stream(self, stream_id: str) -> ConsciousnessInstance | None:
        if self.instance is None or stream_id not in self.instance.stream_ids:
            return None
        return self.instance

    def get(self, instance_id: str) -> ConsciousnessInstance | None:
        if self.instance is None or self.instance.instance_id != instance_id:
            return None
        return self.instance


class _MemoryService:
    def __init__(self, runtime: object, living: object) -> None:
        self.storage_runtime = runtime
        self.bundle = SimpleNamespace(living=living)

    def _require_memory_storage(self) -> Any:
        return self.bundle

    @property
    def living_memory_store(self) -> Any:
        return self.bundle.living


def _coherent_service(
    *,
    active: bool = True,
) -> tuple[LifeEngineService, dict[str, object]]:
    selected_runtime = object()
    living = SimpleNamespace(storage_runtime=selected_runtime)
    memory = _MemoryService(selected_runtime, living)
    subject = SimpleNamespace(storage_runtime=selected_runtime)
    ledger = SimpleNamespace(storage_runtime=selected_runtime)
    review_outcomes: list[dict[str, object]] = []

    async def record_subject_review_outcome(**kwargs: object) -> None:
        review_outcomes.append(dict(kwargs))

    scheduler = SimpleNamespace(
        storage_runtime=selected_runtime,
        decision_ledger=ledger,
        record_subject_review_outcome=record_subject_review_outcome,
    )
    instance = ConsciousnessInstance(
        instance_id="consciousness-continuity",
        stream_ids=["chat:continuity"],
        status="active" if active else "suspended",
    )
    service = LifeEngineService.__new__(LifeEngineService)
    service._selectable_storage_enabled = True
    service._storage_runtime = selected_runtime
    service._memory_service = memory
    service._learning_scheduler = scheduler
    service._subject_document_store = subject
    service._consciousness_registry = _Registry(instance)
    return service, {
        "runtime": selected_runtime,
        "living": living,
        "memory": memory,
        "subject": subject,
        "ledger": ledger,
        "scheduler": scheduler,
        "review_outcomes": review_outcomes,
        "instance": instance,
    }


def _bound_tool(service: LifeEngineService) -> LifeMemoryContinuityReviewSessionTool:
    tool = LifeMemoryContinuityReviewSessionTool(
        plugin=SimpleNamespace(service=service)
    )
    tool._bind_runtime_context(
        stream_id="chat:continuity",
        message=SimpleNamespace(
            stream_id="chat:continuity",
            message_id="message-continuity",
            extra={},
            time="2026-08-12T10:00:00+00:00",
        ),
        tool_call_id="tool-call-continuity",
    )
    tool._life_source_occurrence_id = "source-occurrence-continuity"
    tool._runtime_task_name = "life_chatter"
    return tool


async def test_public_provider_uses_one_coherent_selected_runtime_bundle() -> None:
    service, dependencies = _coherent_service()
    runtime = await resolve_continuity_review_tool_runtime(_bound_tool(service))

    assert dependencies["memory"].storage_runtime is dependencies["runtime"]
    assert dependencies["subject"].storage_runtime is dependencies["runtime"]
    assert dependencies["scheduler"].storage_runtime is dependencies["runtime"]
    assert dependencies["ledger"].storage_runtime is dependencies["runtime"]
    assert runtime.session._subject_authority is dependencies["subject"]
    assert runtime.session._boundary_repository._store is dependencies["living"]
    assert runtime.session._candidate_ledger is dependencies["ledger"]
    assert (
        runtime.session._delivery_verifier
        is get_memory_continuity_delivery_coordinator()
    )
    assert runtime.session._outcome_recorder is not None
    assert runtime.actor.consciousness_instance_id == "consciousness-continuity"
    assert runtime.actor.stream_scope == "chat:continuity"
    assert runtime.actor.source_occurrence_id == "source-occurrence-continuity"
    assert runtime.actor.action_occurrence_id == "tool-call-continuity"
    assert await runtime.session._validate_active_actor(
        runtime.actor.consciousness_instance_id
    )


async def test_public_provider_maps_continuity_outcome_to_scheduler() -> None:
    from plugins.life_engine.memory.continuity_session import ContinuityReviewOutcome

    service, dependencies = _coherent_service()
    runtime = await resolve_continuity_review_tool_runtime(_bound_tool(service))
    recorder = runtime.session._outcome_recorder
    assert recorder is not None
    await recorder(
        ContinuityReviewOutcome(
            outcome_occurrence_id="review-outcome:1",
            outcome_kind="snooze",
            target_path="MEMORY.md",
            candidate_occurrence_id="",
            candidate_id="",
            candidate_revision=0,
            candidate_sha256="",
            subject_revision_before="a" * 64,
            subject_revision_after="a" * 64,
            reason="I will return to this exact version tomorrow.",
            actor_consciousness_instance_id="consciousness-continuity",
            source_occurrence_id="source:1",
            action_occurrence_id="action:1",
            occurred_at="2026-08-12T10:00:00+00:00",
            snooze_hours=24,
        )
    )

    assert dependencies["review_outcomes"] == [
        {
            "target_path": "MEMORY.md",
            "outcome": "snoozed",
            "actor_consciousness_instance_id": "consciousness-continuity",
            "subject_revision": "a" * 64,
            "occurrence_id": "review-outcome:1",
            "reason": "I will return to this exact version tomorrow.",
            "candidate_id": "",
            "candidate_sha256": "",
            "authority_occurrence_id": "",
            "snooze_hours": 24.0,
        }
    ]


async def test_runtime_fails_closed_when_selected_storage_is_disabled() -> None:
    service, _ = _coherent_service()
    service._selectable_storage_enabled = False

    with pytest.raises(
        ContinuityReviewRuntimeUnavailable,
        match="ContinuityReviewSelectedSubjectAuthorityRequired",
    ):
        await resolve_continuity_review_tool_runtime(_bound_tool(service))


@pytest.mark.parametrize(
    "missing",
    ("memory", "scheduler", "subject", "ledger"),
)
async def test_runtime_fails_closed_when_coherent_dependency_is_missing(
    missing: str,
) -> None:
    service, _ = _coherent_service()
    if missing == "memory":
        service._memory_service = None
    elif missing == "scheduler":
        service._learning_scheduler = None
    elif missing == "subject":
        service._subject_document_store = None
    else:
        service._learning_scheduler.decision_ledger = None

    expected = (
        "ContinuityReviewLearningDecisionLedgerUnavailable"
        if missing == "ledger"
        else "ContinuityReviewCoherentRuntimeUnavailable"
    )
    with pytest.raises(ContinuityReviewRuntimeUnavailable, match=expected):
        await resolve_continuity_review_tool_runtime(_bound_tool(service))


@pytest.mark.parametrize(
    "mismatched",
    ("memory", "subject", "scheduler", "ledger"),
)
async def test_runtime_fails_closed_when_dependency_uses_another_runtime(
    mismatched: str,
) -> None:
    service, dependencies = _coherent_service()
    dependencies[mismatched].storage_runtime = object()

    with pytest.raises(
        ContinuityReviewRuntimeUnavailable,
        match="ContinuityReviewCoherentRuntimeMismatch",
    ):
        await resolve_continuity_review_tool_runtime(_bound_tool(service))


async def test_runtime_fails_closed_without_an_active_stream_owner() -> None:
    service, _ = _coherent_service(active=False)

    with pytest.raises(
        ContinuityReviewRuntimeUnavailable,
        match="ContinuityReviewActiveStreamOwnerRequired",
    ):
        await resolve_continuity_review_tool_runtime(_bound_tool(service))


async def test_tool_resolution_never_reaches_private_service_fields() -> None:
    tool = LifeMemoryContinuityReviewSessionTool(plugin=SimpleNamespace())
    tool._bind_runtime_context(
        stream_id="chat:continuity",
        tool_call_id="tool-call-without-provider",
    )

    with pytest.raises(
        ContinuityReviewRuntimeUnavailable,
        match="ContinuityReviewPublicRuntimeProviderUnavailable",
    ):
        await resolve_continuity_review_tool_runtime(tool)
