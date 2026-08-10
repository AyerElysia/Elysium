"""Life Chatter commits boundary recall only after exact ToolResult delivery."""

from __future__ import annotations

import hashlib
from typing import Any

from plugins.life_engine.core.chatter import LifeChatter
from plugins.life_engine.memory.boundary_resolver import (
    PendingMemoryBoundaryRecall,
    get_memory_boundary_recall_coordinator,
)
from plugins.life_engine.memory.living import CoRecallEvent, RecallEpisode, RecallEvent
from src.kernel.llm.context_delivery import EffectiveContextReceipt
from src.kernel.llm.payload import LLMPayload, ToolResult
from src.kernel.llm.roles import ROLE


class _Recall:
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
    def __init__(self, result: ToolResult) -> None:
        self.payloads = [LLMPayload(ROLE.TOOL_RESULT, result)]
        self.registrations: dict[str, tuple[str, str, str]] = {}
        self.receipts: dict[str, EffectiveContextReceipt] = {}

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


async def test_chatter_binds_and_commits_the_exact_tool_result() -> None:
    coordinator = get_memory_boundary_recall_coordinator()
    delivery_id = "memory_recall_delivery_chatter_contract"
    trace = _Recall()
    coordinator.register(
        PendingMemoryBoundaryRecall(
            delivery_id=delivery_id,
            recall_chain_id="recall-chain-chatter-contract",
            delivery_occurrence_id="delivery-occurrence-chatter-contract",
            exact_uri=("memory://boundary/one@artifact-one#sha256=" + "a" * 64),
            projection="memory-boundary-segment-v1",
            artifact_id="artifact-one",
            root_sha256="a" * 64,
            consciousness_instance_id="consciousness-one",
            stream_scope="chat:one",
            retrieval_reason="explicit recall",
            recorded_at="2026-08-10T00:00:00+00:00",
            entity_refs=("memory-boundary-artifact:artifact-one", "segment:one"),
            association_pairs=(
                ("memory-boundary-artifact:artifact-one", "segment:one"),
            ),
            metadata={"page_start_byte": 0},
            recall=trace,
        )
    )
    result = ToolResult(
        value={
            "memory_recall_delivery_id": delivery_id,
            "content": "完整正文",
        },
        call_id="call-one",
        name="tool-nucleus_read_memory_boundary",
    )
    response = _Response(result)

    delivery_ids = LifeChatter._register_pending_memory_recall_deliveries(response)

    assert delivery_ids == (delivery_id,)
    expected, marker, part_kind = response.registrations[delivery_id]
    assert expected == result.to_text()
    assert marker == delivery_id
    assert part_kind == "tool_result"
    digest = hashlib.sha256(expected.encode()).hexdigest()
    response.receipts[delivery_id] = EffectiveContextReceipt(
        delivery_id=delivery_id,
        exact_present=True,
        expected_utf8_bytes=len(expected.encode()),
        expected_sha256=digest,
        effective_utf8_bytes=len(expected.encode()),
        effective_sha256=digest,
        part_kind="tool_result",
    )

    await LifeChatter._commit_memory_recall_deliveries(response, delivery_ids)

    assert len(trace.episodes) == 1
    assert [item.action for item in trace.events] == [
        "delivered_to_model_context",
        "delivered_to_model_context",
    ]
    assert len(trace.corecalls) == 1
    assert coordinator.has_pending(delivery_id) is False
