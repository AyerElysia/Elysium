"""Life Chatter exact-delivery contracts for continuity candidates."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

import pytest

import plugins.life_engine.memory.continuity_delivery as delivery_module
from plugins.life_engine.core.chatter import (
    LifeChatter,
    _Phase,
    _WorkflowRuntime,
)
from plugins.life_engine.learning.decisions import LearningCandidate
from plugins.life_engine.memory.continuity_delivery import (
    ContinuityCandidateDeliveryCoordinator,
)
from plugins.life_engine.memory.continuity_session import CandidateDeliveryReceipt
from src.core.components.base.chatter import Failure, Success
from src.core.models.message import Message
from src.kernel.llm.context_delivery import EffectiveContextReceipt
from src.kernel.llm.payload import LLMPayload, ToolResult
from src.kernel.llm.roles import ROLE


def _candidate(content: bytes) -> LearningCandidate:
    return LearningCandidate.create(
        candidate_id="candidate-chatter",
        candidate_revision=1,
        candidate_kind="continuity_memory_review",
        candidate_occurrence_id="candidate-occurrence-chatter",
        candidate_content_bytes=content,
        source_occurrence_id="source-occurrence-chatter",
        source="continuity-review",
        actor_consciousness_instance_id="consciousness-chatter",
        subject_revision="a" * 64,
        target_path="MEMORY.md",
        occurred_at="2026-08-12T10:00:00+00:00",
    )


def _candidate_payload(
    candidate: LearningCandidate,
    *,
    delivery_id: str,
) -> dict[str, Any]:
    content = bytes(candidate.candidate_content_bytes)
    page_sha256 = hashlib.sha256(content).hexdigest()
    return {
        "action": "candidate_read",
        "session_id": "continuity-review-" + "b" * 32,
        "candidate_id": candidate.candidate_id,
        "candidate_revision": candidate.candidate_revision,
        "candidate_sha256": candidate.candidate_sha256,
        "subject_revision": candidate.subject_revision,
        "page": {
            "offset": 0,
            "next_offset": None,
            "delivered_bytes": len(content),
            "total_bytes": len(content),
            "page_sha256": page_sha256,
            "text": content.decode("utf-8"),
        },
        "delivery_binding": {
            "delivery_id": delivery_id,
            "candidate_id": candidate.candidate_id,
            "candidate_revision": candidate.candidate_revision,
            "candidate_sha256": candidate.candidate_sha256,
            "page_offset": 0,
            "page_sha256": page_sha256,
            "page_bytes": len(content),
            "total_bytes": len(content),
        },
        "delivery_binding_is_not_receipt": True,
        "authority_written": False,
    }


def _receipt(
    delivery_id: str,
    expected_text: str,
    *,
    tampered: bool = False,
) -> EffectiveContextReceipt:
    encoded = expected_text.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return EffectiveContextReceipt(
        delivery_id=delivery_id,
        exact_present=True,
        expected_utf8_bytes=len(encoded),
        expected_sha256=digest,
        effective_utf8_bytes=len(encoded),
        effective_sha256=("f" * 64 if tampered else digest),
        part_kind="tool_result",
    )


class _Response:
    def __init__(self, result: ToolResult, *, send_mode: str = "exact") -> None:
        self.payloads = [LLMPayload(ROLE.TOOL_RESULT, result)]
        self.registrations: dict[str, tuple[str, str, str]] = {}
        self.receipts: dict[str, EffectiveContextReceipt] = {}
        self.send_mode = send_mode
        self.send_calls = 0

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

    async def send(self, *, stream: bool = False) -> _Response:
        assert stream is False
        self.send_calls += 1
        if self.send_mode == "failed":
            raise RuntimeError("simulated provider failure")
        if self.send_mode != "missing":
            for delivery_id, (expected, _, _) in self.registrations.items():
                self.receipts[delivery_id] = _receipt(
                    delivery_id,
                    expected,
                    tampered=self.send_mode == "tampered",
                )
        return self

    def __await__(self):
        async def _done() -> _Response:
            return self

        return _done().__await__()


async def _skip_snapshot_save(_response: object) -> None:
    return None


def test_chatter_registers_the_exact_serialized_candidate_tool_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = ContinuityCandidateDeliveryCoordinator()
    monkeypatch.setattr(
        delivery_module,
        "get_memory_continuity_delivery_coordinator",
        lambda: coordinator,
    )
    candidate = _candidate(b"continuity-candidate")
    delivery_id = "continuity-delivery-chatter-registration"
    result = ToolResult(
        value=_candidate_payload(candidate, delivery_id=delivery_id),
        call_id="call-chatter-registration",
        name="tool-nucleus_memory_continuity_review",
    )
    response = _Response(result)

    delivery_ids = LifeChatter._register_pending_continuity_candidate_deliveries(
        response
    )

    assert delivery_ids == (delivery_id,)
    expected, marker, part_kind = response.registrations[delivery_id]
    assert expected == result.to_text()
    assert marker == delivery_id
    assert part_kind == "tool_result"
    assert coordinator.has_pending(delivery_id)
    coordinator.discard_pending(delivery_id)


@pytest.mark.parametrize(
    ("send_mode", "result_type", "committed"),
    (
        ("exact", Success, True),
        ("missing", Success, False),
        ("tampered", Success, False),
        ("failed", Failure, False),
    ),
)
async def test_chatter_commits_only_the_successful_final_attempt_receipt(
    monkeypatch: pytest.MonkeyPatch,
    send_mode: str,
    result_type: type[Success | Failure],
    committed: bool,
) -> None:
    coordinator = ContinuityCandidateDeliveryCoordinator()
    monkeypatch.setattr(
        delivery_module,
        "get_memory_continuity_delivery_coordinator",
        lambda: coordinator,
    )
    candidate = _candidate(b"candidate-for-final-attempt")
    delivery_id = f"continuity-delivery-chatter-{send_mode}"
    result = ToolResult(
        value=_candidate_payload(candidate, delivery_id=delivery_id),
        call_id=f"call-chatter-{send_mode}",
        name="tool-nucleus_memory_continuity_review",
    )
    response = _Response(result, send_mode=send_mode)
    unread = Message(
        message_id=f"message-{send_mode}",
        content="turn",
        stream_id="chat:continuity",
    )
    runtime = _WorkflowRuntime(
        response=response,
        phase=_Phase.FOLLOW_UP,
        history_merged=True,
        unreads=[unread],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[unread],
        active_stream_id="chat:continuity",
        active_unread_turn_key="turn-continuity",
    )
    LifeChatter.reset_global_runtime()
    LifeChatter._GLOBAL_RUNTIME = runtime
    LifeChatter._GLOBAL_USABLE_MAP = {}
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=None)
    chatter.stream_id = "chat:continuity"

    async def fetch_unreads() -> tuple[list[Message], list[Message]]:
        return [], [unread]

    async def flush_forbidden(_messages: list[Message]) -> None:
        raise AssertionError("FOLLOW_UP must not flush the accepted user turn")

    async def immediate_model_turn(awaitable: Any) -> Any:
        return await awaitable

    from src.kernel import concurrency

    monkeypatch.setattr(
        concurrency,
        "get_watchdog",
        lambda: SimpleNamespace(feed_dog=lambda _stream_id: None),
    )
    monkeypatch.setattr(chatter, "fetch_unreads", fetch_unreads)
    monkeypatch.setattr(chatter, "flush_unreads", flush_forbidden)
    monkeypatch.setattr(chatter, "_await_model_turn", immediate_model_turn)
    monkeypatch.setattr(chatter, "_save_rolling_context_snapshot", _skip_snapshot_save)

    try:
        outcome = await chatter._drive_global_runtime_until_yield(
            SimpleNamespace(stream_id="chat:continuity"),
            service=None,
        )
    finally:
        LifeChatter.reset_global_runtime()

    assert isinstance(outcome, result_type)
    assert response.send_calls == 1
    assert coordinator.has_pending(delivery_id) is False
    claim = CandidateDeliveryReceipt(
        delivery_id=delivery_id,
        candidate_id=candidate.candidate_id,
        candidate_revision=candidate.candidate_revision,
        candidate_sha256=candidate.candidate_sha256,
        delivered_bytes=len(candidate.candidate_content_bytes),
        total_bytes=len(candidate.candidate_content_bytes),
    )
    assert (
        await coordinator.verify_exact_candidate_delivery(claim, candidate) is committed
    )
    assert coordinator.snapshot().committed_pages == int(committed)
