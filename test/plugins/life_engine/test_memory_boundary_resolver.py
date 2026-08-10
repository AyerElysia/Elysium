"""Bounded delivery and living recall traces for long-memory boundaries."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

import pytest

from plugins.life_engine.memory.boundary import (
    MEMORY_BOUNDARY_ARTIFACT_KIND,
    MemoryBoundaryManifest,
    MemoryBoundarySegment,
    StoredMemoryBoundary,
    memory_boundary_uri,
)
from plugins.life_engine.memory.boundary_resolver import (
    MemoryBoundaryRecallCoordinator,
    MemoryBoundaryResolver,
    MemoryBoundarySegmentNotFound,
)
from plugins.life_engine.memory.living import (
    CoRecallEvent,
    RecallEpisode,
    RecallEvent,
    new_artifact_version,
)
from plugins.life_engine.tools.bounded_projection import BoundedContinuationError
from src.kernel.llm.context_delivery import EffectiveContextReceipt
from src.kernel.llm.payload import ToolResult


def _stored(
    *,
    boundary_id: str = "shared-morning",
    content: str = "她记得那天早晨的光。" * 800,
) -> StoredMemoryBoundary:
    segment = MemoryBoundarySegment.create(
        segment_id="whole-scene",
        title="那天早晨",
        content=content,
        source_refs=("experience:morning",),
        source_occurrence_ids=("event:morning",),
        scope="那次真实发生的交谈",
        visibility="private",
    )
    manifest = MemoryBoundaryManifest(
        boundary_id=boundary_id,
        manifest_revision=1,
        operation_occurrence_id=f"boundary:create:{boundary_id}",
        title="一段很长但仍有边界的记忆",
        scope="这次交谈及当时的感受",
        current_meaning="它仍然影响我理解陪伴的方式。" * 300,
        non_generalization="这不是对所有关系的普遍结论。",
        actor_id="elysia",
        consciousness_instance_id="chat-main",
        stream_scope="chat:one",
        decision_occurrence_id=f"boundary:decision:{boundary_id}",
        source_occurrence_id="message:source",
        subject_revision=hashlib.sha256(b"subject-revision").hexdigest(),
        segments=(segment,),
    )
    artifact = new_artifact_version(
        logical_key=manifest.logical_key,
        artifact_kind=MEMORY_BOUNDARY_ARTIFACT_KIND,
        content=manifest.canonical_json,
        authored_by=manifest.actor_id,
        consciousness_instance_id=manifest.consciousness_instance_id,
        stream_scope=manifest.stream_scope,
        visibility=manifest.visibility,
    )
    return StoredMemoryBoundary(
        manifest=manifest,
        artifact=artifact,
        head_revision=1,
        exact_uri=memory_boundary_uri(
            manifest.boundary_id,
            artifact.artifact_id,
            manifest.root_sha256,
        ),
    )


class _Repository:
    def __init__(self, *records: StoredMemoryBoundary) -> None:
        self.records = {item.exact_uri: item for item in records}

    async def read_exact(self, uri: str) -> StoredMemoryBoundary:
        return self.records[uri]


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
            random_seed=7,
            recorded_at="2026-08-10T00:00:00+00:00",
            context=dict(kwargs["context"]),
        )
        existing = next(
            (item for item in self.episodes if item.episode_id == episode.episode_id),
            None,
        )
        if existing is not None:
            assert existing == episode
            return existing
        self.episodes.append(episode)
        return episode

    async def append_memory_recall_events(
        self,
        events: list[RecallEvent] | tuple[RecallEvent, ...],
    ) -> tuple[RecallEvent, ...]:
        for event in events:
            existing = next(
                (item for item in self.events if item.event_id == event.event_id),
                None,
            )
            if existing is not None:
                assert existing == event
                continue
            self.events.append(event)
        return tuple(events)

    async def append_memory_corecall(self, event: CoRecallEvent) -> CoRecallEvent:
        existing = next(
            (item for item in self.corecalls if item.corecall_id == event.corecall_id),
            None,
        )
        if existing is not None:
            assert existing == event
            return existing
        self.corecalls.append(event)
        return event


def _receipt(payload: dict[str, Any], *, exact: bool = True) -> EffectiveContextReceipt:
    delivery_id = str(payload["memory_recall_delivery_id"])
    text = ToolResult(value=payload).to_text()
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    size = len(text.encode("utf-8"))
    return EffectiveContextReceipt(
        delivery_id=delivery_id,
        exact_present=exact,
        expected_utf8_bytes=size,
        expected_sha256=digest,
        effective_utf8_bytes=size if exact else None,
        effective_sha256=digest if exact else None,
        part_kind="tool_result",
    )


async def _deliver(
    coordinator: MemoryBoundaryRecallCoordinator,
    payload: dict[str, Any],
    *,
    exact: bool = True,
) -> bool:
    return await coordinator.commit_exact(
        str(payload["memory_recall_delivery_id"]),
        _receipt(payload, exact=exact),
    )


async def test_overview_is_hard_bounded_and_traced_without_semantic_ranking() -> None:
    stored = _stored()
    trace = _Recall()
    coordinator = MemoryBoundaryRecallCoordinator()
    resolver = MemoryBoundaryResolver(  # type: ignore[arg-type]
        _Repository(stored), recall=trace, coordinator=coordinator
    )

    payload = await resolver.overview(
        stored.exact_uri,
        task_name="core",
        consciousness_instance_id="chat-main",
        stream_scope="chat:one",
        max_bytes=2048,
        recall_chain_id="recall-chain-overview",
        delivery_occurrence_id="delivery-overview-1",
        recorded_at="2026-08-10T00:00:00+00:00",
    )

    assert len(str(payload).encode("utf-8")) <= 2048
    assert payload["root_sha256"] == stored.manifest.root_sha256
    assert payload["truncated"] is True
    descriptive = await MemoryBoundaryResolver(  # type: ignore[arg-type]
        _Repository(stored)
    ).overview(
        stored.exact_uri,
        task_name="core",
        consciousness_instance_id="chat-main",
        stream_scope="chat:one",
        max_bytes=8192,
    )
    context_item = descriptive["boundary_items"][0]
    assert context_item["exact_context_mode"] == "context"
    assert context_item["current_meaning"]["complete"] is False
    assert "importance" not in str(payload)
    assert "score" not in str(payload)
    assert trace.episodes == []
    assert payload["recall_trace_state"] == "pending_exact_tool_result_delivery"
    assert await _deliver(coordinator, payload) is True
    assert len(trace.episodes) == 1
    assert trace.events
    assert all(item.action == "delivered_to_model_context" for item in trace.events)
    assert all(item.metadata["exact_artifact_pinned"] for item in trace.events)


async def test_full_overview_descriptor_strengthens_only_delivered_source_links() -> (
    None
):
    stored = _stored()
    trace = _Recall()
    coordinator = MemoryBoundaryRecallCoordinator()
    resolver = MemoryBoundaryResolver(  # type: ignore[arg-type]
        _Repository(stored), recall=trace, coordinator=coordinator
    )

    first = await resolver.overview(
        stored.exact_uri,
        task_name="core",
        consciousness_instance_id="chat-main",
        stream_scope="chat:one",
        max_bytes=2048,
        recall_chain_id="recall-chain-overview-pages",
        delivery_occurrence_id="delivery-overview-page-1",
        recorded_at="2026-08-10T00:00:00+00:00",
    )
    assert first["continuation"]
    assert await _deliver(coordinator, first) is True
    second = await resolver.overview(
        stored.exact_uri,
        task_name="core",
        consciousness_instance_id="chat-main",
        stream_scope="chat:one",
        continuation=first["continuation"],
        max_bytes=2048,
        recall_chain_id="recall-chain-overview-pages",
        delivery_occurrence_id="delivery-overview-page-2",
        recorded_at="2026-08-10T00:00:00+00:00",
    )
    assert await _deliver(coordinator, second) is True

    assert trace.corecalls
    recalled = {
        entity_ref
        for corecall in trace.corecalls
        for entity_ref in corecall.entity_refs
    }
    assert "experience:morning" not in recalled
    assert any("#segment=whole-scene" in item for item in recalled)
    assert all(item.metadata["accessibility_only"] for item in trace.corecalls)


async def test_segment_pages_reconstruct_exact_utf8_content_and_form_corecall() -> None:
    content = "花瓣落下。🌸\n" * 700
    stored = _stored(content=content)
    trace = _Recall()
    coordinator = MemoryBoundaryRecallCoordinator()
    resolver = MemoryBoundaryResolver(  # type: ignore[arg-type]
        _Repository(stored), recall=trace, coordinator=coordinator
    )
    continuation = ""
    chunks: list[str] = []

    while True:
        payload = await resolver.read_segment(
            stored.exact_uri,
            "whole-scene",
            task_name="core",
            consciousness_instance_id="chat-main",
            stream_scope="chat:one",
            continuation=continuation,
            max_bytes=2048,
            recall_chain_id="recall-chain-segment",
            delivery_occurrence_id=f"delivery-segment-{len(chunks)}",
            recorded_at="2026-08-10T00:00:00+00:00",
        )
        assert len(str(payload).encode("utf-8")) <= 2048
        chunks.append(payload["content"])
        assert await _deliver(coordinator, payload) is True
        continuation = payload["continuation"]
        if not continuation:
            break

    assert "".join(chunks) == content
    assert len(trace.episodes) == 1
    assert len(trace.corecalls) == 1
    assert all(item.metadata["accessibility_only"] for item in trace.corecalls)
    assert all(len(item.entity_refs) == 2 for item in trace.corecalls)


async def test_context_and_provenance_pages_reconstruct_every_exact_byte() -> None:
    stored = _stored()
    resolver = MemoryBoundaryResolver(_Repository(stored))  # type: ignore[arg-type]

    async def collect(mode: str) -> str:
        continuation = ""
        chunks: list[str] = []
        page = 0
        while True:
            if mode == "context":
                payload = await resolver.read_context(
                    stored.exact_uri,
                    task_name="core",
                    consciousness_instance_id="chat-main",
                    stream_scope="chat:one",
                    continuation=continuation,
                    max_bytes=2048,
                )
            else:
                payload = await resolver.read_provenance(
                    stored.exact_uri,
                    task_name="core",
                    consciousness_instance_id="chat-main",
                    stream_scope="chat:one",
                    continuation=continuation,
                    max_bytes=2048,
                )
            page += 1
            assert len(str(payload).encode("utf-8")) <= 2048
            chunks.append(payload["content"])
            continuation = payload["continuation"]
            if not continuation:
                return "".join(chunks)

    context = json.loads(await collect("context"))
    assert context == {
        "title": stored.manifest.title,
        "scope": stored.manifest.scope,
        "current_meaning": stored.manifest.current_meaning,
        "non_generalization": stored.manifest.non_generalization,
    }
    provenance = json.loads(await collect("provenance"))
    assert provenance["provenance_status"] == "external_unverified"
    assert provenance["segments"][0]["source_refs"] == ["experience:morning"]
    assert provenance["segments"][0]["source_occurrence_ids"] == ["event:morning"]


async def test_segment_continuation_is_bound_to_exact_artifact_frontier() -> None:
    first = _stored(boundary_id="first-boundary")
    second = _stored(boundary_id="second-boundary")
    resolver = MemoryBoundaryResolver(_Repository(first, second))  # type: ignore[arg-type]
    page = await resolver.read_segment(
        first.exact_uri,
        "whole-scene",
        task_name="core",
        consciousness_instance_id="chat-main",
        stream_scope="chat:one",
        max_bytes=2048,
    )
    assert page["continuation"]

    with pytest.raises(BoundedContinuationError):
        await resolver.read_segment(
            second.exact_uri,
            "whole-scene",
            task_name="core",
            consciousness_instance_id="chat-main",
            stream_scope="chat:one",
            continuation=page["continuation"],
            max_bytes=2048,
        )


async def test_missing_segment_fails_without_trace() -> None:
    stored = _stored()
    trace = _Recall()
    coordinator = MemoryBoundaryRecallCoordinator()
    resolver = MemoryBoundaryResolver(  # type: ignore[arg-type]
        _Repository(stored), recall=trace, coordinator=coordinator
    )

    with pytest.raises(MemoryBoundarySegmentNotFound):
        await resolver.read_segment(
            stored.exact_uri,
            "missing",
            task_name="core",
            consciousness_instance_id="chat-main",
            stream_scope="chat:one",
            recall_chain_id="recall-chain-missing",
            delivery_occurrence_id="delivery-missing",
            recorded_at="2026-08-10T00:00:00+00:00",
        )

    assert trace.episodes == []


async def test_non_exact_tool_result_never_records_recall() -> None:
    stored = _stored()
    trace = _Recall()
    coordinator = MemoryBoundaryRecallCoordinator()
    resolver = MemoryBoundaryResolver(  # type: ignore[arg-type]
        _Repository(stored), recall=trace, coordinator=coordinator
    )
    payload = await resolver.read_segment(
        stored.exact_uri,
        "whole-scene",
        task_name="core",
        consciousness_instance_id="chat-main",
        stream_scope="chat:one",
        max_bytes=2048,
        recall_chain_id="recall-chain-not-delivered",
        delivery_occurrence_id="delivery-not-delivered",
        recorded_at="2026-08-10T00:00:00+00:00",
    )

    assert await _deliver(coordinator, payload, exact=False) is False
    assert trace.episodes == []
    assert trace.events == []
    assert trace.corecalls == []


def test_stored_fixture_is_pinned_to_manifest_bytes() -> None:
    stored = _stored()
    tampered = replace(stored.artifact, content=stored.artifact.content + " ")
    assert tampered.content_hash == stored.artifact.content_hash
    assert tampered.content != stored.manifest.canonical_json
