"""Heartbeat exact-delivery contracts for continuity and boundary reads."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

import pytest

import plugins.life_engine.memory.boundary_resolver as boundary_module
import plugins.life_engine.memory.continuity_delivery as delivery_module
from plugins.life_engine.learning.decisions import LearningCandidate
from plugins.life_engine.memory.boundary_resolver import (
    MemoryBoundaryRecallCoordinator,
    PendingMemoryBoundaryRecall,
)
from plugins.life_engine.memory.continuity_delivery import (
    ContinuityCandidateDeliveryCoordinator,
)
from plugins.life_engine.memory.continuity_session import CandidateDeliveryReceipt
from plugins.life_engine.memory.living import CoRecallEvent, RecallEpisode, RecallEvent
from plugins.life_engine.service.core import LifeEngineService
from src.kernel.llm import ToolRegistry
from src.kernel.llm.context_delivery import EffectiveContextReceipt
from src.kernel.llm.payload import LLMPayload, ToolResult
from src.kernel.llm.roles import ROLE


class _RecallTrace:
    def __init__(self) -> None:
        self.episodes: list[RecallEpisode] = []
        self.events: list[RecallEvent] = []
        self.corecalls: list[CoRecallEvent] = []

    async def begin_memory_recall(self, **kwargs: Any) -> RecallEpisode:
        episode = RecallEpisode(
            episode_id=str(kwargs["episode_id"]),
            query=str(kwargs["query"]),
            retrieval_intent=str(kwargs["retrieval_intent"]),
            consciousness_instance_id=str(kwargs["consciousness_instance_id"]),
            stream_scope=str(kwargs["stream_scope"]),
            context_key=str(kwargs["context_key"]),
            policy_version=str(kwargs["policy_version"]),
            random_seed=int(kwargs["random_seed"]),
            recorded_at=str(kwargs["recorded_at"]),
            context=dict(kwargs["context"]),
        )
        self.episodes.append(episode)
        return episode

    async def append_memory_recall_events(
        self,
        events: list[RecallEvent] | tuple[RecallEvent, ...],
    ) -> tuple[RecallEvent, ...]:
        self.events.extend(events)
        return tuple(events)

    async def append_memory_corecall(self, event: CoRecallEvent) -> CoRecallEvent:
        self.corecalls.append(event)
        return event


class _Response:
    def __init__(self, *results: ToolResult) -> None:
        self.payloads = [LLMPayload(ROLE.TOOL_RESULT, result) for result in results]
        self.registrations: dict[str, tuple[str, str, str]] = {}
        self.receipts: dict[str, EffectiveContextReceipt] = {}

    def add_payload(self, payload: LLMPayload) -> None:
        self.payloads.append(payload)

    def register_context_delivery(
        self,
        delivery_id: str,
        expected_text: str,
        *,
        marker: str,
        part_kind: str,
    ) -> None:
        self.registrations[delivery_id] = (expected_text, marker, part_kind)

    def effective_context_receipt(
        self,
        delivery_id: str,
    ) -> EffectiveContextReceipt | None:
        return self.receipts.get(delivery_id)

    def add_exact_receipts(self) -> None:
        for delivery_id, (expected, _, _) in self.registrations.items():
            encoded = expected.encode("utf-8")
            digest = hashlib.sha256(encoded).hexdigest()
            self.receipts[delivery_id] = EffectiveContextReceipt(
                delivery_id=delivery_id,
                exact_present=True,
                expected_utf8_bytes=len(encoded),
                expected_sha256=digest,
                effective_utf8_bytes=len(encoded),
                effective_sha256=digest,
                part_kind="tool_result",
            )


def _candidate(content: bytes, *, candidate_id: str) -> LearningCandidate:
    return LearningCandidate.create(
        candidate_id=candidate_id,
        candidate_revision=1,
        candidate_kind="continuity_memory_review",
        candidate_occurrence_id=f"candidate-occurrence:{candidate_id}",
        candidate_content_bytes=content,
        source_occurrence_id="source-occurrence-heartbeat",
        source="continuity-review",
        actor_consciousness_instance_id="consciousness-heartbeat",
        subject_revision="c" * 64,
        target_path="MEMORY.md",
        occurred_at="2026-08-12T10:00:00+00:00",
    )


def _candidate_page(
    candidate: LearningCandidate,
    *,
    delivery_id: str,
    offset: int,
    end: int,
) -> dict[str, Any]:
    content = bytes(candidate.candidate_content_bytes)
    page = content[offset:end]
    page_sha256 = hashlib.sha256(page).hexdigest()
    return {
        "action": "candidate_read",
        "session_id": "continuity-review-" + "d" * 32,
        "candidate_id": candidate.candidate_id,
        "candidate_revision": candidate.candidate_revision,
        "candidate_sha256": candidate.candidate_sha256,
        "subject_revision": candidate.subject_revision,
        "page": {
            "offset": offset,
            "next_offset": end if end < len(content) else None,
            "delivered_bytes": len(page),
            "total_bytes": len(content),
            "page_sha256": page_sha256,
            "text": page.decode("utf-8"),
        },
        "delivery_binding": {
            "delivery_id": delivery_id,
            "candidate_id": candidate.candidate_id,
            "candidate_revision": candidate.candidate_revision,
            "candidate_sha256": candidate.candidate_sha256,
            "page_offset": offset,
            "page_sha256": page_sha256,
            "page_bytes": len(page),
            "total_bytes": len(content),
        },
        "delivery_binding_is_not_receipt": True,
        "authority_written": False,
    }


def _boundary_plan(
    coordinator: MemoryBoundaryRecallCoordinator,
    trace: _RecallTrace,
    *,
    delivery_id: str,
) -> dict[str, Any]:
    coordinator.register(
        PendingMemoryBoundaryRecall(
            delivery_id=delivery_id,
            recall_chain_id=f"recall-chain:{delivery_id}",
            delivery_occurrence_id=f"occurrence:{delivery_id}",
            exact_uri="memory://boundary/one@artifact-one#sha256=" + "e" * 64,
            projection="memory-boundary-segment-v1",
            artifact_id="artifact-one",
            root_sha256="e" * 64,
            consciousness_instance_id="consciousness-heartbeat",
            stream_scope="chat_global",
            retrieval_reason="explicit recall",
            recorded_at="2026-08-12T10:00:00+00:00",
            entity_refs=("memory-boundary-artifact:artifact-one", "segment:one"),
            association_pairs=(
                ("memory-boundary-artifact:artifact-one", "segment:one"),
            ),
            metadata={"page_start_byte": 0},
            recall=trace,
        )
    )
    return {
        "memory_recall_delivery_id": delivery_id,
        "projection": "memory-boundary-segment-v1",
        "content": "boundary-page",
    }


def _claim(
    candidate: LearningCandidate,
    delivery_id: str,
) -> CandidateDeliveryReceipt:
    size = len(candidate.candidate_content_bytes)
    return CandidateDeliveryReceipt(
        delivery_id=delivery_id,
        candidate_id=candidate.candidate_id,
        candidate_revision=candidate.candidate_revision,
        candidate_sha256=candidate.candidate_sha256,
        delivered_bytes=size,
        total_bytes=size,
    )


def _install_coordinators(
    monkeypatch: pytest.MonkeyPatch,
    continuity: ContinuityCandidateDeliveryCoordinator,
    boundary: MemoryBoundaryRecallCoordinator,
) -> None:
    monkeypatch.setattr(
        delivery_module,
        "get_memory_continuity_delivery_coordinator",
        lambda: continuity,
    )
    monkeypatch.setattr(
        boundary_module,
        "get_memory_boundary_recall_coordinator",
        lambda: boundary,
    )


async def test_heartbeat_tool_execution_preserves_structured_result() -> None:
    structured = {"action": "candidate_read", "delivery_binding": {"id": "x"}}
    bindings: list[tuple[str, str]] = []

    class _StructuredTool:
        def __init__(self, plugin: object) -> None:
            self.plugin = plugin

        def _bind_runtime_context(
            self,
            *,
            stream_id: str,
            tool_call_id: str,
        ) -> None:
            bindings.append((stream_id, tool_call_id))

        async def execute(self) -> tuple[bool, dict[str, Any]]:
            return True, structured

    registry = ToolRegistry()
    registry.register(_StructuredTool, name="nucleus_memory_continuity_review")
    service = LifeEngineService.__new__(LifeEngineService)
    service.plugin = object()

    result, success = await service._run_heartbeat_tool_call_execution(
        "nucleus_memory_continuity_review",
        {},
        registry,
        tool_call_id="heartbeat-tool-call",
        source_occurrence_id="heartbeat-occurrence",
    )
    response = _Response()
    service._append_heartbeat_tool_result_payload(
        response,
        SimpleNamespace(id="heartbeat-tool-call"),
        "nucleus_memory_continuity_review",
        result,
    )

    assert success is True
    assert result is structured
    assert response.payloads[0].content[0].value is structured
    assert not isinstance(response.payloads[0].content[0].value, str)
    assert bindings == [("chat_global", "heartbeat-tool-call")]


async def test_heartbeat_registers_and_commits_candidate_and_boundary_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    continuity = ContinuityCandidateDeliveryCoordinator()
    boundary = MemoryBoundaryRecallCoordinator()
    trace = _RecallTrace()
    _install_coordinators(monkeypatch, continuity, boundary)
    candidate = _candidate(b"complete-candidate", candidate_id="candidate-heartbeat")
    candidate_delivery = "continuity-delivery-heartbeat"
    boundary_delivery = "boundary-delivery-heartbeat"
    candidate_result = ToolResult(
        value=_candidate_page(
            candidate,
            delivery_id=candidate_delivery,
            offset=0,
            end=len(candidate.candidate_content_bytes),
        ),
        call_id="call-candidate",
        name="tool-nucleus_memory_continuity_review",
    )
    boundary_result = ToolResult(
        value=_boundary_plan(
            boundary,
            trace,
            delivery_id=boundary_delivery,
        ),
        call_id="call-boundary",
        name="tool-nucleus_read_memory_boundary",
    )
    response = _Response(candidate_result, boundary_result)

    deliveries = LifeEngineService._register_pending_heartbeat_memory_deliveries(
        response
    )
    response.add_exact_receipts()
    await LifeEngineService._commit_heartbeat_memory_deliveries(
        response,
        deliveries,
    )

    assert set(deliveries) == {
        ("continuity", candidate_delivery),
        ("boundary", boundary_delivery),
    }
    assert all(value[2] == "tool_result" for value in response.registrations.values())
    assert await continuity.verify_exact_candidate_delivery(
        _claim(candidate, candidate_delivery), candidate
    )
    assert boundary.has_pending(boundary_delivery) is False
    assert len(trace.episodes) == 1
    assert [event.action for event in trace.events] == [
        "delivered_to_model_context",
        "delivered_to_model_context",
    ]


async def test_heartbeat_missing_receipts_discard_all_pending_delivery_proofs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    continuity = ContinuityCandidateDeliveryCoordinator()
    boundary = MemoryBoundaryRecallCoordinator()
    trace = _RecallTrace()
    _install_coordinators(monkeypatch, continuity, boundary)
    candidate = _candidate(b"candidate", candidate_id="candidate-missing-receipt")
    candidate_delivery = "continuity-delivery-missing-receipt"
    boundary_delivery = "boundary-delivery-missing-receipt"
    response = _Response(
        ToolResult(
            value=_candidate_page(
                candidate,
                delivery_id=candidate_delivery,
                offset=0,
                end=len(candidate.candidate_content_bytes),
            )
        ),
        ToolResult(
            value=_boundary_plan(
                boundary,
                trace,
                delivery_id=boundary_delivery,
            )
        ),
    )
    deliveries = LifeEngineService._register_pending_heartbeat_memory_deliveries(
        response
    )

    await LifeEngineService._commit_heartbeat_memory_deliveries(response, deliveries)

    assert continuity.has_pending(candidate_delivery) is False
    assert continuity.snapshot().committed_pages == 0
    assert boundary.has_pending(boundary_delivery) is False
    assert trace.episodes == []
    assert trace.events == []


async def test_heartbeat_candidate_acceptance_requires_complete_page_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    continuity = ContinuityCandidateDeliveryCoordinator()
    boundary = MemoryBoundaryRecallCoordinator()
    _install_coordinators(monkeypatch, continuity, boundary)
    candidate = _candidate(b"abcdef", candidate_id="candidate-paged-heartbeat")
    first_id = "continuity-delivery-heartbeat-page-one"
    second_id = "continuity-delivery-heartbeat-page-two"

    first = _Response(
        ToolResult(
            value=_candidate_page(
                candidate,
                delivery_id=first_id,
                offset=0,
                end=3,
            )
        )
    )
    first_deliveries = LifeEngineService._register_pending_heartbeat_memory_deliveries(
        first
    )
    first.add_exact_receipts()
    await LifeEngineService._commit_heartbeat_memory_deliveries(
        first,
        first_deliveries,
    )

    assert not await continuity.verify_exact_candidate_delivery(
        _claim(candidate, first_id), candidate
    )

    second = _Response(
        ToolResult(
            value=_candidate_page(
                candidate,
                delivery_id=second_id,
                offset=3,
                end=6,
            )
        )
    )
    second_deliveries = LifeEngineService._register_pending_heartbeat_memory_deliveries(
        second
    )
    second.add_exact_receipts()
    await LifeEngineService._commit_heartbeat_memory_deliveries(
        second,
        second_deliveries,
    )

    assert await continuity.verify_exact_candidate_delivery(
        _claim(candidate, second_id), candidate
    )


def test_heartbeat_failed_attempt_discards_registered_proofs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    continuity = ContinuityCandidateDeliveryCoordinator()
    boundary = MemoryBoundaryRecallCoordinator()
    _install_coordinators(monkeypatch, continuity, boundary)
    candidate = _candidate(b"candidate", candidate_id="candidate-discard")
    delivery_id = "continuity-delivery-heartbeat-discard"
    response = _Response(
        ToolResult(
            value=_candidate_page(
                candidate,
                delivery_id=delivery_id,
                offset=0,
                end=len(candidate.candidate_content_bytes),
            )
        )
    )
    deliveries = LifeEngineService._register_pending_heartbeat_memory_deliveries(
        response
    )

    LifeEngineService._discard_pending_heartbeat_memory_deliveries(deliveries)

    assert continuity.has_pending(delivery_id) is False
    assert continuity.snapshot().committed_pages == 0
