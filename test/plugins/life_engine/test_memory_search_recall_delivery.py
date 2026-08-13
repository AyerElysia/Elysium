"""Exact-delivery contracts for ordinary ``nucleus_search_memory`` recall."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.core.chatter import LifeChatter
from plugins.life_engine.memory.experience import EvidenceAwareMemoryResult
from plugins.life_engine.memory.living import CoRecallEvent, RecallEpisode, RecallEvent
from plugins.life_engine.memory.recall_delivery import (
    DeliveredMemorySearchRef,
    MemorySearchRecallDeliveryCoordinator,
    PendingMemorySearchRecall,
    get_memory_search_recall_delivery_coordinator,
)
from plugins.life_engine.memory.tools import (
    MEMORY_SEARCH_CORE_MAX_BYTES,
    LifeEngineSearchMemoryTool,
)
from plugins.life_engine.service.core import LifeEngineService
from src.app.plugin_system.base import BaseTool
from src.kernel.llm.context_delivery import EffectiveContextReceipt
from src.kernel.llm.payload import LLMPayload, ToolResult
from src.kernel.llm.roles import ROLE

pytestmark = pytest.mark.asyncio


class _RecallStore:
    def __init__(self, evidence: list[EvidenceAwareMemoryResult]) -> None:
        self.evidence = evidence
        self.episodes: dict[str, RecallEpisode] = {}
        self.events: dict[str, RecallEvent] = {}
        self.corecalls: dict[str, CoRecallEvent] = {}
        self.begin_calls: list[str] = []

    async def search_memory(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    async def search_evidence_aware(
        self,
        _query: str,
        **_kwargs: Any,
    ) -> list[EvidenceAwareMemoryResult]:
        return list(self.evidence)

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
        existing = self.episodes.get(episode.episode_id)
        if existing is not None:
            assert existing == episode
        self.episodes[episode.episode_id] = episode
        self.begin_calls.append(episode.episode_id)
        return episode

    async def append_memory_recall_events(
        self,
        events: list[RecallEvent] | tuple[RecallEvent, ...],
    ) -> tuple[RecallEvent, ...]:
        for event in events:
            existing = self.events.get(event.event_id)
            if existing is not None:
                assert existing == event
            self.events[event.event_id] = event
        return tuple(events)

    async def append_memory_corecall(self, event: CoRecallEvent) -> CoRecallEvent:
        existing = self.corecalls.get(event.corecall_id)
        if existing is not None:
            assert existing == event
        self.corecalls[event.corecall_id] = event
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


@pytest.fixture(autouse=True)
def _reset_global_coordinator() -> None:
    coordinator = get_memory_search_recall_delivery_coordinator()
    coordinator.reset_for_tests()
    yield
    coordinator.reset_for_tests()


@pytest.fixture
def active_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = SimpleNamespace(instance_id="consciousness-alpha", is_active=True)

    class _Registry:
        @staticmethod
        def get(instance_id: str) -> Any:
            return instance if instance_id == instance.instance_id else None

    runtime = SimpleNamespace(
        consciousness_registry=_Registry(),
        resolve_consciousness_instance=lambda stream: (
            "consciousness-alpha" if stream == "stream-alpha" else ""
        ),
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.registry.get_life_engine_service",
        lambda: runtime,
    )


def _evidence(count: int, *, large: bool = False) -> list[EvidenceAwareMemoryResult]:
    suffix = "爱莉♪" * (1200 if large else 1)
    return [
        EvidenceAwareMemoryResult(
            record_id=f"witness-{index:02d}",
            kind="subjective_witness",
            content=f"第{index}段经历。{suffix}",
            rank_score=1.0 / (index + 1),
            confidence=None,
            source="witness_fts",
            provenance=(f"event-{index}",),
            metadata={"ordinal": index},
        )
        for index in range(count)
    ]


def _tool(
    monkeypatch: pytest.MonkeyPatch,
    store: _RecallStore,
    *,
    occurrence: str = "turn-one",
    recorded_at: str = "2026-08-13T00:00:00+00:00",
) -> LifeEngineSearchMemoryTool:
    tool = LifeEngineSearchMemoryTool(plugin=SimpleNamespace())
    tool._runtime_task_name = "core"
    tool._bind_runtime_context(
        stream_id="stream-alpha",
        tool_call_id=f"call-{occurrence}",
    )
    tool._life_source_occurrence_id = occurrence
    tool._life_source_occurred_at = recorded_at

    async def _service() -> _RecallStore:
        return store

    monkeypatch.setattr(tool, "_get_service", _service)
    return tool


def _exact_receipt(expected_text: str, delivery_id: str) -> EffectiveContextReceipt:
    encoded = expected_text.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return EffectiveContextReceipt(
        delivery_id=delivery_id,
        exact_present=True,
        expected_utf8_bytes=len(encoded),
        expected_sha256=digest,
        effective_utf8_bytes=len(encoded),
        effective_sha256=digest,
        part_kind="tool_result",
    )


async def test_search_writes_only_after_chatter_exact_final_tool_result(
    monkeypatch: pytest.MonkeyPatch,
    active_runtime: None,
) -> None:
    store = _RecallStore(_evidence(3))
    tool = _tool(monkeypatch, store)

    ok, payload = await tool.execute("那段经历")

    assert ok is True
    binding = payload["recall_delivery_binding"]
    assert binding["delivery_id"]
    assert payload["recall_episode"]["trace_state"] == (
        "pending_exact_tool_result_delivery"
    )
    assert payload["recall_episode"]["persisted"] is False
    assert store.episodes == {}
    assert store.events == {}
    assert store.corecalls == {}

    result = ToolResult(
        value=payload,
        call_id="call-search",
        name="nucleus_search_memory",
    )
    response = _Response(result)
    delivery_ids = LifeChatter._register_pending_memory_recall_deliveries(response)
    assert delivery_ids == (binding["delivery_id"],)
    expected, marker, part_kind = response.registrations[binding["delivery_id"]]
    assert expected == result.to_text()
    assert marker == binding["delivery_id"]
    assert part_kind == "tool_result"

    response.receipts[binding["delivery_id"]] = _exact_receipt(
        expected,
        binding["delivery_id"],
    )
    await LifeChatter._commit_memory_recall_deliveries(response, delivery_ids)

    episode = next(iter(store.episodes.values()))
    assert episode.consciousness_instance_id == "consciousness-alpha"
    assert episode.context["source_occurrence_id"] == "turn-one"
    assert {item.action for item in store.events.values()} == {
        "delivered_to_model_context"
    }
    delivered_refs = {
        item["entity_ref"] for item in payload["evidence_results"]
    }
    assert {item.entity_ref for item in store.events.values()} == delivered_refs
    assert set(next(iter(store.corecalls.values())).entity_refs) == delivered_refs
    assert next(iter(store.corecalls.values())).actor == "consciousness-alpha"

    event_ids = set(store.events)
    corecall_ids = set(store.corecalls)
    retry_ok, retry_payload = await tool.execute("那段经历")
    assert retry_ok is True
    assert retry_payload["recall_delivery_binding"] == binding
    retry_response = _Response(
        ToolResult(value=retry_payload, call_id="retry", name="search")
    )
    retry_deliveries = LifeChatter._register_pending_memory_recall_deliveries(
        retry_response
    )
    retry_expected = retry_response.registrations[binding["delivery_id"]][0]
    retry_response.receipts[binding["delivery_id"]] = _exact_receipt(
        retry_expected,
        binding["delivery_id"],
    )
    await LifeChatter._commit_memory_recall_deliveries(
        retry_response,
        retry_deliveries,
    )
    assert set(store.events) == event_ids
    assert set(store.corecalls) == corecall_ids


async def test_missing_trimmed_cancelled_and_restarted_proofs_never_write(
    monkeypatch: pytest.MonkeyPatch,
    active_runtime: None,
) -> None:
    store = _RecallStore(_evidence(2))
    tool = _tool(monkeypatch, store)
    ok, payload = await tool.execute("需要精确证明")
    assert ok is True
    delivery_id = payload["recall_delivery_binding"]["delivery_id"]
    response = _Response(ToolResult(value=payload, call_id="one", name="search"))
    delivery_ids = LifeChatter._register_pending_memory_recall_deliveries(response)

    # A missing receipt is the same as a failed/cancelled final attempt.
    await LifeChatter._commit_memory_recall_deliveries(response, delivery_ids)
    assert store.episodes == {}
    assert not get_memory_search_recall_delivery_coordinator().has_pending(
        delivery_id
    )

    ok, payload = await tool.execute("需要精确证明")
    assert ok is True
    delivery_id = payload["recall_delivery_binding"]["delivery_id"]
    response = _Response(ToolResult(value=payload, call_id="two", name="search"))
    delivery_ids = LifeChatter._register_pending_memory_recall_deliveries(response)
    expected = response.registrations[delivery_id][0]
    exact = _exact_receipt(expected, delivery_id)
    response.receipts[delivery_id] = EffectiveContextReceipt(
        delivery_id=delivery_id,
        exact_present=False,
        expected_utf8_bytes=exact.expected_utf8_bytes,
        expected_sha256=exact.expected_sha256,
        effective_utf8_bytes=None,
        effective_sha256=None,
        part_kind="tool_result",
    )
    await LifeChatter._commit_memory_recall_deliveries(response, delivery_ids)
    assert store.episodes == {}

    ok, payload = await tool.execute("需要精确证明")
    assert ok is True
    delivery_id = payload["recall_delivery_binding"]["delivery_id"]
    LifeChatter._discard_pending_memory_recall_deliveries((delivery_id,))
    assert store.episodes == {}

    # Process restart has no durable proof to replay.
    restarted = MemorySearchRecallDeliveryCoordinator()
    assert await restarted.commit_exact(delivery_id, exact) is False
    assert store.episodes == {}


async def test_paginated_search_shares_episode_and_commits_only_page_refs(
    monkeypatch: pytest.MonkeyPatch,
    active_runtime: None,
) -> None:
    store = _RecallStore(_evidence(12, large=True))
    tool = _tool(monkeypatch, store)
    monkeypatch.setattr(tool, "_result_budget", lambda: 6 * 1024)

    first_ok, first = await tool.execute("分页回忆")
    assert first_ok is True
    assert first["continuation"]
    assert first["delivered_bytes"] == len(
        ToolResult(value=first).to_text().encode("utf-8")
    )
    assert first["delivered_bytes"] <= 6 * 1024
    assert store.episodes == {}

    second_ok, second = await tool.execute(
        "分页回忆",
        continuation=first["continuation"],
    )
    assert second_ok is True
    assert first["recall_episode"]["episode_id"] == second["recall_episode"][
        "episode_id"
    ]
    assert first["recall_episode"]["random_seed"] == second["recall_episode"][
        "random_seed"
    ]
    assert first["recall_episode"]["consciousness_instance_id"] == (
        "consciousness-alpha"
    )
    assert first["recall_episode"]["source_occurrence_id"] == "turn-one"
    assert first["recall_delivery_binding"]["delivery_id"] != second[
        "recall_delivery_binding"
    ]["delivery_id"]

    first_refs = {item["entity_ref"] for item in first["evidence_results"]}
    second_refs = {item["entity_ref"] for item in second["evidence_results"]}
    assert first_refs
    assert second_refs
    assert first_refs.isdisjoint(second_refs)

    for payload in (first, second):
        result = ToolResult(value=payload, call_id="page", name="search")
        response = _Response(result)
        deliveries = LifeChatter._register_pending_memory_recall_deliveries(response)
        delivery_id = deliveries[0]
        response.receipts[delivery_id] = _exact_receipt(
            response.registrations[delivery_id][0],
            delivery_id,
        )
        await LifeChatter._commit_memory_recall_deliveries(response, deliveries)

    assert len(store.episodes) == 1
    assert {item.entity_ref for item in store.events.values()} == (
        first_refs | second_refs
    )
    for corecall in store.corecalls.values():
        refs = set(corecall.entity_refs)
        assert refs <= first_refs or refs <= second_refs


async def test_bad_continuation_and_changed_frontier_leave_no_new_trace(
    monkeypatch: pytest.MonkeyPatch,
    active_runtime: None,
) -> None:
    store = _RecallStore(_evidence(6, large=True))
    tool = _tool(monkeypatch, store)
    monkeypatch.setattr(tool, "_result_budget", lambda: 5 * 1024)
    coordinator = get_memory_search_recall_delivery_coordinator()

    bad_ok, _bad = await tool.execute("稳定前沿", continuation="tampered.token")
    assert bad_ok is False
    assert coordinator.health_snapshot()["pending_count"] == 0
    assert store.episodes == {}

    first_ok, first = await tool.execute("稳定前沿")
    assert first_ok is True
    pending_before = coordinator.health_snapshot()["pending_count"]
    store.evidence.append(_evidence(1)[0])
    changed_ok, changed = await tool.execute(
        "稳定前沿",
        continuation=first["continuation"],
    )
    assert changed_ok is False
    assert "frontier" in changed["error"]
    assert coordinator.health_snapshot()["pending_count"] == pending_before
    assert store.episodes == {}

    tool._life_source_occurrence_id = "different-turn"
    mismatch_ok, mismatch = await tool.execute(
        "稳定前沿",
        continuation=first["continuation"],
    )
    assert mismatch_ok is False
    assert "consciousness/source" in mismatch["error"]
    assert store.episodes == {}


async def test_search_without_active_actor_returns_untraced_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inactive = SimpleNamespace(instance_id="consciousness-alpha", is_active=False)
    runtime = SimpleNamespace(
        consciousness_registry=SimpleNamespace(get=lambda _instance_id: inactive),
        resolve_consciousness_instance=lambda _stream: "consciousness-alpha",
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.registry.get_life_engine_service",
        lambda: runtime,
    )
    store = _RecallStore(_evidence(1))
    tool = _tool(monkeypatch, store)

    ok, payload = await tool.execute("仍然允许只读搜索")

    assert ok is True
    assert payload["recall_delivery_binding"] is None
    assert payload["recall_episode"]["trace_state"] == "unavailable"
    assert payload["recall_episode"]["consciousness_instance_id"] == ""
    assert store.episodes == {}


async def test_search_without_stable_source_time_does_not_fabricate_trace(
    monkeypatch: pytest.MonkeyPatch,
    active_runtime: None,
) -> None:
    store = _RecallStore(_evidence(1))
    tool = _tool(monkeypatch, store)
    del tool._life_source_occurred_at

    ok, payload = await tool.execute("没有稳定发生时间")

    assert ok is True
    assert payload["recall_delivery_binding"] is None
    assert payload["recall_episode"]["trace_state"] == "unavailable"
    assert store.episodes == {}


async def test_heartbeat_registers_and_commits_search_delivery(
    monkeypatch: pytest.MonkeyPatch,
    active_runtime: None,
) -> None:
    store = _RecallStore(_evidence(2))
    tool = _tool(monkeypatch, store, occurrence="heartbeat-run-one")
    ok, payload = await tool.execute("心跳回忆")
    assert ok is True
    result = ToolResult(value=payload, call_id="heartbeat-call", name="search")
    response = _Response(result)

    deliveries = LifeEngineService._register_pending_heartbeat_memory_deliveries(
        response
    )
    delivery_id = payload["recall_delivery_binding"]["delivery_id"]
    assert deliveries == (("search", delivery_id),)
    expected = response.registrations[delivery_id][0]
    response.receipts[delivery_id] = _exact_receipt(expected, delivery_id)

    await LifeEngineService._commit_heartbeat_memory_deliveries(
        response,
        deliveries,
    )
    assert len(store.episodes) == 1
    assert next(iter(store.episodes.values())).consciousness_instance_id == (
        "consciousness-alpha"
    )


async def test_heartbeat_binds_stable_source_time_to_search_tool() -> None:
    captured: dict[str, str] = {}

    class _CaptureTool(BaseTool):
        tool_name = "capture_memory_source"
        tool_description = "capture test-only source metadata"

        async def execute(self) -> tuple[bool, dict[str, object]]:
            captured["occurrence"] = str(self._life_source_occurrence_id)
            captured["occurred_at"] = str(self._life_source_occurred_at)
            return True, {"captured": True}

    registry = SimpleNamespace(get=lambda name: _CaptureTool if name else None)
    service = SimpleNamespace(plugin=SimpleNamespace())
    result, success = await LifeEngineService._run_heartbeat_tool_call_execution(
        service,
        "capture_memory_source",
        {},
        registry,
        tool_call_id="heartbeat-call",
        source_occurrence_id="heartbeat-run-one",
        source_occurred_at="2026-08-13T00:00:00+00:00",
    )
    assert success is True
    assert result == {"captured": True}
    assert captured == {
        "occurrence": "heartbeat-run-one",
        "occurred_at": "2026-08-13T00:00:00+00:00",
    }


async def test_coordinator_ttl_is_bounded_and_content_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    monkeypatch.setattr(
        "plugins.life_engine.memory.recall_delivery.time.monotonic",
        lambda: now,
    )
    store = _RecallStore([])
    coordinator = MemorySearchRecallDeliveryCoordinator(
        max_pending=1,
        ttl_seconds=10.0,
    )

    def plan(delivery_id: str) -> PendingMemorySearchRecall:
        return PendingMemorySearchRecall(
            delivery_id=delivery_id,
            recall_chain_id=f"chain-{delivery_id}",
            episode_id=f"episode-{delivery_id}",
            consciousness_instance_id="consciousness-alpha",
            stream_scope="stream-alpha",
            source_occurrence_id="turn-one",
            recorded_at="2026-08-13T00:00:00+00:00",
            query="private query",
            retrieval_intent="private intent",
            context_key="life_engine/stream-alpha",
            random_seed=1,
            frontier_sha256="f" * 64,
            page_offset=0,
            delivered_refs=(
                DeliveredMemorySearchRef(
                    entity_ref="witness:one",
                    source="witness_fts",
                    ordinal=0,
                    metadata={},
                ),
            ),
            search_context={},
            recall=store,
        )

    coordinator.register(plan("delivery-one"))
    coordinator.register(plan("delivery-two"))
    snapshot = coordinator.health_snapshot()
    assert snapshot["pending_count"] == 1
    assert snapshot["evicted_total"] == 1
    assert "private query" not in str(snapshot)
    assert "witness:one" not in str(snapshot)

    now = 111.0
    snapshot = coordinator.health_snapshot()
    assert snapshot["pending_count"] == 0
    assert snapshot["expired_total"] == 1
    assert store.episodes == {}


async def test_search_tool_result_budget_uses_actual_json_bytes() -> None:
    payload = {
        "unicode": "爱莉♪'\"",
        "budget": MEMORY_SEARCH_CORE_MAX_BYTES,
    }
    result = ToolResult(value=payload)
    assert result.to_text() == json.dumps(payload, ensure_ascii=False)
