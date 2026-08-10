"""Bounded, traceable resolution of exact long-memory boundary references.

The resolver never floats a reference to a newer artifact and never turns
recall frequency into subject truth. It projects only the exact immutable
manifest selected by a ``memory://boundary`` URI, then records what was
actually included in the model-visible tool result.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from ..tools.bounded_projection import (
    project_bounded_items,
    project_bounded_text,
    utf8_prefix,
)
from .boundary import (
    MemoryBoundaryNotFound,
    MemoryBoundaryRepository,
    StoredMemoryBoundary,
)
from .living import CoRecallEvent, RecallEpisode, RecallEvent

MEMORY_BOUNDARY_OVERVIEW_PROJECTION = "memory-boundary-overview-v1"
MEMORY_BOUNDARY_CONTEXT_PROJECTION = "memory-boundary-context-v1"
MEMORY_BOUNDARY_PROVENANCE_PROJECTION = "memory-boundary-provenance-v1"
MEMORY_BOUNDARY_SEGMENT_PROJECTION = "memory-boundary-segment-v1"
MEMORY_BOUNDARY_RECALL_POLICY = "memory-boundary-recall-v1"
MEMORY_BOUNDARY_RECALL_PENDING_MAX = 256
MEMORY_BOUNDARY_RECALL_PENDING_TTL_SECONDS = 15 * 60.0


class MemoryBoundarySegmentNotFound(MemoryBoundaryNotFound):
    """Raised when an exact manifest does not contain the requested segment."""


class MemoryBoundaryRecallPort(Protocol):
    """Minimal living-memory trace surface consumed by the resolver."""

    async def begin_memory_recall(self, **kwargs: Any) -> RecallEpisode: ...

    async def append_memory_recall_events(
        self,
        events: list[RecallEvent] | tuple[RecallEvent, ...],
    ) -> tuple[RecallEvent, ...]: ...

    async def append_memory_corecall(self, event: CoRecallEvent) -> CoRecallEvent: ...


@dataclass(frozen=True, slots=True)
class PendingMemoryBoundaryRecall:
    """One accessibility trace waiting for exact model-context delivery.

    Producing a tool result is not recall.  This immutable plan can only be
    committed after the LLM kernel proves that the complete ``ToolResult`` was
    present in the final successful request attempt.
    """

    delivery_id: str
    recall_chain_id: str
    delivery_occurrence_id: str
    exact_uri: str
    projection: str
    artifact_id: str
    root_sha256: str
    consciousness_instance_id: str
    stream_scope: str
    retrieval_reason: str
    recorded_at: str
    entity_refs: tuple[str, ...]
    association_pairs: tuple[tuple[str, str], ...]
    metadata: Mapping[str, object]
    recall: MemoryBoundaryRecallPort


class MemoryBoundaryRecallCoordinator:
    """Bounded two-phase coordinator for exact-delivery recall evidence."""

    def __init__(
        self,
        *,
        max_pending: int = MEMORY_BOUNDARY_RECALL_PENDING_MAX,
        ttl_seconds: float = MEMORY_BOUNDARY_RECALL_PENDING_TTL_SECONDS,
    ) -> None:
        self._max_pending = max(1, int(max_pending))
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._lock = threading.RLock()
        self._pending: OrderedDict[str, tuple[float, PendingMemoryBoundaryRecall]] = (
            OrderedDict()
        )

    def _prune_locked(self, now: float) -> None:
        expired = [
            delivery_id
            for delivery_id, (created_at, _) in self._pending.items()
            if now - created_at >= self._ttl_seconds
        ]
        for delivery_id in expired:
            self._pending.pop(delivery_id, None)
        while len(self._pending) > self._max_pending:
            self._pending.popitem(last=False)

    def register(self, plan: PendingMemoryBoundaryRecall) -> None:
        """Register one content-bound delivery plan idempotently."""

        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            existing = self._pending.get(plan.delivery_id)
            if existing is not None:
                if existing[1] != plan:
                    raise RuntimeError(
                        f"MemoryBoundaryRecallDeliveryConflict:{plan.delivery_id}"
                    )
                self._pending.move_to_end(plan.delivery_id)
                return
            self._pending[plan.delivery_id] = (now, plan)
            self._prune_locked(now)

    def has_pending(self, delivery_id: str) -> bool:
        """Return whether a non-expired delivery plan is waiting."""

        identity = str(delivery_id or "").strip()
        if not identity:
            return False
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            return identity in self._pending

    def discard(self, delivery_id: str) -> None:
        """Discard a projection that was not delivered exactly."""

        with self._lock:
            self._pending.pop(str(delivery_id or "").strip(), None)

    async def commit_exact(self, delivery_id: str, receipt: Any) -> bool:
        """Persist accessibility evidence only for an exact ToolResult receipt."""

        identity = str(delivery_id or "").strip()
        with self._lock:
            self._prune_locked(time.monotonic())
            entry = self._pending.get(identity)
        if entry is None:
            return False
        plan = entry[1]
        exact = bool(getattr(receipt, "exact_present", False))
        expected_bytes = getattr(receipt, "expected_utf8_bytes", None)
        effective_bytes = getattr(receipt, "effective_utf8_bytes", None)
        valid_receipt = all(
            (
                str(getattr(receipt, "delivery_id", "") or "") == identity,
                str(getattr(receipt, "part_kind", "") or "") == "tool_result",
                exact,
                str(getattr(receipt, "expected_sha256", "") or "")
                == str(getattr(receipt, "effective_sha256", "") or ""),
                isinstance(expected_bytes, int),
                isinstance(effective_bytes, int),
                expected_bytes == effective_bytes,
            )
        )
        if not valid_receipt:
            self.discard(identity)
            return False

        await self._persist(
            plan,
            effective_sha256=str(receipt.effective_sha256),
            effective_utf8_bytes=int(receipt.effective_utf8_bytes),
        )
        with self._lock:
            current = self._pending.get(identity)
            if current is not None and current[1] == plan:
                self._pending.pop(identity, None)
        return True

    @staticmethod
    async def _persist(
        plan: PendingMemoryBoundaryRecall,
        *,
        effective_sha256: str,
        effective_utf8_bytes: int,
    ) -> None:
        context_key = "/".join(
            item for item in ("life_engine", plan.stream_scope) if item
        )
        episode_id = (
            "recall_"
            + hashlib.sha256(
                f"episode\0{plan.recall_chain_id}".encode()
            ).hexdigest()
        )
        seed = int(
            hashlib.sha256(plan.recall_chain_id.encode("utf-8")).hexdigest()[:16],
            16,
        ) & ((1 << 63) - 1)
        episode = await plan.recall.begin_memory_recall(
            query=plan.exact_uri,
            retrieval_intent=plan.retrieval_reason,
            consciousness_instance_id=plan.consciousness_instance_id,
            stream_scope=plan.stream_scope,
            context_key=context_key,
            policy_version=MEMORY_BOUNDARY_RECALL_POLICY,
            random_seed=seed,
            episode_id=episode_id,
            recorded_at=plan.recorded_at,
            context={
                "projection": plan.projection,
                "artifact_id": plan.artifact_id,
                "root_sha256": plan.root_sha256,
                "recall_chain_id": plan.recall_chain_id,
            },
        )
        delivery_metadata = {
            "projection": plan.projection,
            "exact_artifact_pinned": True,
            "exact_tool_result_delivered": True,
            "delivery_id": plan.delivery_id,
            "effective_context_sha256": effective_sha256,
            "effective_context_utf8_bytes": effective_utf8_bytes,
            **dict(plan.metadata),
        }
        events = tuple(
            RecallEvent(
                event_id="recall_event_"
                + hashlib.sha256(
                    f"{plan.delivery_id}\0{entity_ref}".encode()
                ).hexdigest(),
                episode_id=episode.episode_id,
                action="delivered_to_model_context",
                recorded_at=plan.recorded_at,
                entity_ref=entity_ref,
                ordinal=ordinal,
                source="memory_boundary_resolver",
                reason=plan.retrieval_reason,
                metadata=delivery_metadata,
            )
            for ordinal, entity_ref in enumerate(plan.entity_refs)
        )
        await plan.recall.append_memory_recall_events(events)
        for left, right in plan.association_pairs:
            pair = tuple(sorted((left, right)))
            corecall_id = (
                "corecall_"
                + hashlib.sha256(
                    (plan.recall_chain_id + "\0" + pair[0] + "\0" + pair[1]).encode(
                        "utf-8"
                    )
                ).hexdigest()
            )
            await plan.recall.append_memory_corecall(
                CoRecallEvent(
                    corecall_id=corecall_id,
                    episode_id=episode.episode_id,
                    context_key=context_key,
                    signal="co_recalled_from_exact_memory_boundary_delivery",
                    entity_refs=pair,
                    actor=plan.consciousness_instance_id,
                    reason=plan.retrieval_reason,
                    recorded_at=plan.recorded_at,
                    metadata={
                        "projection": plan.projection,
                        "accessibility_only": True,
                        "does_not_change_truth_or_importance": True,
                        "recall_chain_id": plan.recall_chain_id,
                    },
                )
            )


_RECALL_COORDINATOR = MemoryBoundaryRecallCoordinator()


def get_memory_boundary_recall_coordinator() -> MemoryBoundaryRecallCoordinator:
    """Return the process-local bounded exact-delivery coordinator."""

    return _RECALL_COORDINATOR


def _boundary_ref(stored: StoredMemoryBoundary) -> str:
    return f"memory-boundary-artifact:{stored.artifact.artifact_id}"


def _segment_ref(stored: StoredMemoryBoundary, segment_id: str) -> str:
    return f"{_boundary_ref(stored)}#segment={segment_id}"


def _frontier(stored: StoredMemoryBoundary) -> dict[str, object]:
    return {
        "artifact_id": stored.artifact.artifact_id,
        "root_sha256": stored.manifest.root_sha256,
        "manifest_revision": stored.manifest.manifest_revision,
        "segments": [
            {
                "segment_id": item.segment_id,
                "content_sha256": item.content_sha256,
                "byte_length": item.byte_length,
            }
            for item in stored.manifest.segments
        ],
    }


def _field_projection(value: str, *, excerpt_bytes: int = 512) -> dict[str, object]:
    encoded = value.encode("utf-8")
    excerpt = utf8_prefix(value, excerpt_bytes)
    return {
        "excerpt": excerpt,
        "original_bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "complete": excerpt == value,
    }


class MemoryBoundaryResolver:
    """Resolve exact boundary manifests through task-aware bounded pages."""

    def __init__(
        self,
        repository: MemoryBoundaryRepository,
        *,
        recall: MemoryBoundaryRecallPort | None = None,
        coordinator: MemoryBoundaryRecallCoordinator | None = None,
    ) -> None:
        self._repository = repository
        self._recall = recall
        self._coordinator = coordinator or get_memory_boundary_recall_coordinator()

    async def overview(
        self,
        exact_uri: str,
        *,
        task_name: str,
        consciousness_instance_id: str,
        stream_scope: str,
        continuation: str = "",
        max_bytes: int | None = None,
        retrieval_reason: str = "follow explicit continuity-memory boundary",
        recall_chain_id: str = "",
        delivery_occurrence_id: str = "",
        recorded_at: str = "",
    ) -> dict[str, Any]:
        """Return bounded subject context plus the ordered segment directory."""

        stored = await self._repository.read_exact(exact_uri)
        manifest = stored.manifest
        boundary_entity_ref = _boundary_ref(stored)
        items: list[Mapping[str, Any]] = [
            {
                "item_kind": "boundary_context",
                "title": _field_projection(manifest.title),
                "scope": _field_projection(manifest.scope),
                "current_meaning": _field_projection(manifest.current_meaning),
                "non_generalization": _field_projection(manifest.non_generalization),
                "exact_context_mode": "context",
                "visibility": manifest.visibility,
                "manifest_revision": manifest.manifest_revision,
                "subject_revision": manifest.subject_revision,
                "source_occurrence_id": manifest.source_occurrence_id,
                "decision_occurrence_id": manifest.decision_occurrence_id,
            }
        ]
        item_refs = [boundary_entity_ref]
        for ordinal, segment in enumerate(manifest.segments):
            items.append(
                {
                    "item_kind": "segment_descriptor",
                    "ordinal": ordinal,
                    "segment_id": segment.segment_id,
                    "title": segment.title,
                    "byte_length": segment.byte_length,
                    "content_sha256": segment.content_sha256,
                    "source_ref_count": len(segment.source_refs),
                    "source_refs_sha256": hashlib.sha256(
                        json.dumps(
                            segment.source_refs,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                    "source_occurrence_count": len(segment.source_occurrence_ids),
                    "source_occurrences_sha256": hashlib.sha256(
                        json.dumps(
                            segment.source_occurrence_ids,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                    "source_provenance_status": "external_unverified",
                    "scope": segment.scope,
                    "visibility": segment.visibility,
                }
            )
            item_refs.append(_segment_ref(stored, segment.segment_id))

        delivery_id = self._prepare_delivery_id(
            delivery_occurrence_id,
            MEMORY_BOUNDARY_OVERVIEW_PROJECTION,
        )
        payload = project_bounded_items(
            projection_name=MEMORY_BOUNDARY_OVERVIEW_PROJECTION,
            task_name=task_name,
            requested_max_bytes=max_bytes,
            binding={
                "operation": "overview",
                "exact_uri": exact_uri,
                "consciousness_instance_id": consciousness_instance_id,
                "stream_scope": stream_scope,
            },
            frontier=_frontier(stored),
            base_payload={
                "action": "read_memory_boundary_overview",
                "exact_uri": stored.exact_uri,
                "boundary_id": manifest.boundary_id,
                "artifact_id": stored.artifact.artifact_id,
                "root_sha256": manifest.root_sha256,
                "manifest_revision": manifest.manifest_revision,
                "head_revision_at_record": stored.head_revision,
                "memory_recall_delivery_id": delivery_id,
                "recall_trace_state": (
                    "pending_exact_tool_result_delivery"
                    if delivery_id
                    else "unavailable"
                ),
            },
            items_key="boundary_items",
            items=items,
            item_refs=item_refs,
            continuation=continuation,
        )
        delivered_refs: list[str] = []
        for item in payload["boundary_items"]:
            projection = item.get("_projection", {})
            reference = str(projection.get("ref", ""))
            if reference:
                delivered_refs.append(reference)
        self._stage_projection(
            stored,
            delivery_id=delivery_id,
            recall_chain_id=recall_chain_id,
            delivery_occurrence_id=delivery_occurrence_id,
            recorded_at=recorded_at,
            projection=MEMORY_BOUNDARY_OVERVIEW_PROJECTION,
            entity_refs=tuple(dict.fromkeys((_boundary_ref(stored), *delivered_refs))),
            consciousness_instance_id=consciousness_instance_id,
            stream_scope=stream_scope,
            retrieval_reason=retrieval_reason,
            metadata={
                "page_offset": payload["page_offset"],
                "delivered_items": payload["delivered_items"],
                "delivered_bytes": payload["delivered_bytes"],
                "truncated": payload["truncated"],
            },
        )
        return payload

    async def read_context(
        self,
        exact_uri: str,
        *,
        task_name: str,
        consciousness_instance_id: str,
        stream_scope: str,
        continuation: str = "",
        max_bytes: int | None = None,
        retrieval_reason: str = "follow explicit continuity-memory boundary",
        recall_chain_id: str = "",
        delivery_occurrence_id: str = "",
        recorded_at: str = "",
    ) -> dict[str, Any]:
        """Read every subject-authored boundary-context byte without excerpts."""

        stored = await self._repository.read_exact(exact_uri)
        manifest = stored.manifest
        content = json.dumps(
            {
                "title": manifest.title,
                "scope": manifest.scope,
                "current_meaning": manifest.current_meaning,
                "non_generalization": manifest.non_generalization,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        delivery_id = self._prepare_delivery_id(
            delivery_occurrence_id,
            MEMORY_BOUNDARY_CONTEXT_PROJECTION,
        )
        payload = project_bounded_text(
            projection_name=MEMORY_BOUNDARY_CONTEXT_PROJECTION,
            task_name=task_name,
            requested_max_bytes=max_bytes,
            binding={
                "operation": "context",
                "exact_uri": exact_uri,
                "consciousness_instance_id": consciousness_instance_id,
                "stream_scope": stream_scope,
            },
            frontier=_frontier(stored),
            base_payload={
                "action": "read_memory_boundary_context",
                "exact_uri": stored.exact_uri,
                "boundary_id": manifest.boundary_id,
                "artifact_id": stored.artifact.artifact_id,
                "root_sha256": manifest.root_sha256,
                "memory_recall_delivery_id": delivery_id,
                "recall_trace_state": (
                    "pending_exact_tool_result_delivery"
                    if delivery_id
                    else "unavailable"
                ),
            },
            content=content,
            content_ref=f"{_boundary_ref(stored)}#context",
            continuation=continuation,
        )
        self._stage_projection(
            stored,
            delivery_id=delivery_id,
            recall_chain_id=recall_chain_id,
            delivery_occurrence_id=delivery_occurrence_id,
            recorded_at=recorded_at,
            projection=MEMORY_BOUNDARY_CONTEXT_PROJECTION,
            entity_refs=(_boundary_ref(stored),),
            consciousness_instance_id=consciousness_instance_id,
            stream_scope=stream_scope,
            retrieval_reason=retrieval_reason,
            metadata={
                "page_start_byte": payload["page_start_byte"],
                "page_end_byte": payload["page_end_byte"],
                "delivered_content_bytes": payload["delivered_content_bytes"],
                "truncated": payload["truncated"],
            },
        )
        return payload

    async def read_provenance(
        self,
        exact_uri: str,
        *,
        task_name: str,
        consciousness_instance_id: str,
        stream_scope: str,
        continuation: str = "",
        max_bytes: int | None = None,
        retrieval_reason: str = "follow explicit continuity-memory boundary",
        recall_chain_id: str = "",
        delivery_occurrence_id: str = "",
        recorded_at: str = "",
    ) -> dict[str, Any]:
        """Page exact external provenance labels without asserting their truth."""

        stored = await self._repository.read_exact(exact_uri)
        manifest = stored.manifest
        content = json.dumps(
            {
                "provenance_status": "external_unverified",
                "source_occurrence_id": manifest.source_occurrence_id,
                "segments": [
                    {
                        "segment_id": segment.segment_id,
                        "source_refs": list(segment.source_refs),
                        "source_occurrence_ids": list(segment.source_occurrence_ids),
                    }
                    for segment in manifest.segments
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        delivery_id = self._prepare_delivery_id(
            delivery_occurrence_id,
            MEMORY_BOUNDARY_PROVENANCE_PROJECTION,
        )
        payload = project_bounded_text(
            projection_name=MEMORY_BOUNDARY_PROVENANCE_PROJECTION,
            task_name=task_name,
            requested_max_bytes=max_bytes,
            binding={
                "operation": "provenance",
                "exact_uri": exact_uri,
                "consciousness_instance_id": consciousness_instance_id,
                "stream_scope": stream_scope,
            },
            frontier=_frontier(stored),
            base_payload={
                "action": "read_memory_boundary_provenance",
                "exact_uri": stored.exact_uri,
                "boundary_id": manifest.boundary_id,
                "artifact_id": stored.artifact.artifact_id,
                "root_sha256": manifest.root_sha256,
                "provenance_status": "external_unverified",
                "memory_recall_delivery_id": delivery_id,
                "recall_trace_state": (
                    "pending_exact_tool_result_delivery"
                    if delivery_id
                    else "unavailable"
                ),
            },
            content=content,
            content_ref=f"{_boundary_ref(stored)}#provenance",
            continuation=continuation,
        )
        self._stage_projection(
            stored,
            delivery_id=delivery_id,
            recall_chain_id=recall_chain_id,
            delivery_occurrence_id=delivery_occurrence_id,
            recorded_at=recorded_at,
            projection=MEMORY_BOUNDARY_PROVENANCE_PROJECTION,
            entity_refs=(_boundary_ref(stored),),
            consciousness_instance_id=consciousness_instance_id,
            stream_scope=stream_scope,
            retrieval_reason=retrieval_reason,
            metadata={
                "page_start_byte": payload["page_start_byte"],
                "page_end_byte": payload["page_end_byte"],
                "delivered_content_bytes": payload["delivered_content_bytes"],
                "external_provenance_unverified": True,
                "truncated": payload["truncated"],
            },
        )
        return payload

    async def read_segment(
        self,
        exact_uri: str,
        segment_id: str,
        *,
        task_name: str,
        consciousness_instance_id: str,
        stream_scope: str,
        continuation: str = "",
        max_bytes: int | None = None,
        retrieval_reason: str = "follow explicit continuity-memory boundary",
        recall_chain_id: str = "",
        delivery_occurrence_id: str = "",
        recorded_at: str = "",
    ) -> dict[str, Any]:
        """Read an exact segment in UTF-8-safe, source-bound chunks."""

        stored = await self._repository.read_exact(exact_uri)
        normalized_segment_id = str(segment_id or "").strip()
        segment = next(
            (
                item
                for item in stored.manifest.segments
                if item.segment_id == normalized_segment_id
            ),
            None,
        )
        if segment is None:
            raise MemoryBoundarySegmentNotFound(
                "MemoryBoundarySegmentNotFound:"
                f"{stored.manifest.boundary_id}:{normalized_segment_id}"
            )
        segment_entity_ref = _segment_ref(stored, segment.segment_id)
        delivery_id = self._prepare_delivery_id(
            delivery_occurrence_id,
            MEMORY_BOUNDARY_SEGMENT_PROJECTION,
        )
        payload = project_bounded_text(
            projection_name=MEMORY_BOUNDARY_SEGMENT_PROJECTION,
            task_name=task_name,
            requested_max_bytes=max_bytes,
            binding={
                "operation": "segment",
                "exact_uri": exact_uri,
                "segment_id": segment.segment_id,
                "consciousness_instance_id": consciousness_instance_id,
                "stream_scope": stream_scope,
            },
            frontier=_frontier(stored),
            base_payload={
                "action": "read_memory_boundary_segment",
                "exact_uri": stored.exact_uri,
                "boundary_id": stored.manifest.boundary_id,
                "artifact_id": stored.artifact.artifact_id,
                "root_sha256": stored.manifest.root_sha256,
                "manifest_revision": stored.manifest.manifest_revision,
                "segment_id": segment.segment_id,
                "segment_title_sha256": hashlib.sha256(
                    segment.title.encode("utf-8")
                ).hexdigest(),
                "segment_title_bytes": len(segment.title.encode("utf-8")),
                "segment_source_ref_count": len(segment.source_refs),
                "segment_source_refs_sha256": hashlib.sha256(
                    json.dumps(
                        segment.source_refs,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                "segment_source_occurrence_count": len(segment.source_occurrence_ids),
                "segment_source_occurrences_sha256": hashlib.sha256(
                    json.dumps(
                        segment.source_occurrence_ids,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                "memory_recall_delivery_id": delivery_id,
                "recall_trace_state": (
                    "pending_exact_tool_result_delivery"
                    if delivery_id
                    else "unavailable"
                ),
            },
            content=segment.content,
            content_ref=segment_entity_ref,
            continuation=continuation,
        )
        delivered_refs = (
            (_boundary_ref(stored), segment_entity_ref)
            if int(payload["delivered_content_bytes"]) > 0
            else ()
        )
        self._stage_projection(
            stored,
            delivery_id=delivery_id,
            recall_chain_id=recall_chain_id,
            delivery_occurrence_id=delivery_occurrence_id,
            recorded_at=recorded_at,
            projection=MEMORY_BOUNDARY_SEGMENT_PROJECTION,
            entity_refs=delivered_refs,
            consciousness_instance_id=consciousness_instance_id,
            stream_scope=stream_scope,
            retrieval_reason=retrieval_reason,
            metadata={
                "segment_id": segment.segment_id,
                "page_start_byte": payload["page_start_byte"],
                "page_end_byte": payload["page_end_byte"],
                "delivered_content_bytes": payload["delivered_content_bytes"],
                "truncated": payload["truncated"],
            },
        )
        return payload

    def _prepare_delivery_id(
        self,
        delivery_occurrence_id: str,
        projection: str,
    ) -> str:
        if self._recall is None:
            return ""
        occurrence = str(delivery_occurrence_id or "").strip()
        if not occurrence:
            raise RuntimeError("MemoryBoundaryRecallDeliveryIdentityRequired")
        return (
            "memory_recall_delivery_"
            + hashlib.sha256(f"{projection}\0{occurrence}".encode()).hexdigest()
        )

    def _stage_projection(
        self,
        stored: StoredMemoryBoundary,
        *,
        delivery_id: str,
        recall_chain_id: str,
        delivery_occurrence_id: str,
        recorded_at: str,
        projection: str,
        entity_refs: tuple[str, ...],
        consciousness_instance_id: str,
        stream_scope: str,
        retrieval_reason: str,
        metadata: dict[str, object],
    ) -> None:
        if self._recall is None or not entity_refs:
            return
        chain = str(recall_chain_id or "").strip()
        occurrence = str(delivery_occurrence_id or "").strip()
        timestamp = str(recorded_at or "").strip()
        if not chain:
            raise RuntimeError("MemoryBoundaryRecallChainIdentityRequired")
        if not occurrence:
            raise RuntimeError("MemoryBoundaryRecallDeliveryIdentityRequired")
        if not timestamp:
            raise RuntimeError("MemoryBoundaryRecallRecordedAtRequired")
        recalled_refs = tuple(dict.fromkeys(str(item) for item in entity_refs if item))
        association_pairs = tuple(
            (recalled_refs[left], recalled_refs[right])
            for left in range(len(recalled_refs))
            for right in range(left + 1, len(recalled_refs))
        )
        self._coordinator.register(
            PendingMemoryBoundaryRecall(
                delivery_id=delivery_id,
                recall_chain_id=chain,
                delivery_occurrence_id=occurrence,
                exact_uri=stored.exact_uri,
                projection=projection,
                artifact_id=stored.artifact.artifact_id,
                root_sha256=stored.manifest.root_sha256,
                consciousness_instance_id=consciousness_instance_id,
                stream_scope=stream_scope,
                retrieval_reason=retrieval_reason,
                recorded_at=timestamp,
                entity_refs=recalled_refs,
                association_pairs=association_pairs,
                metadata=dict(metadata),
                recall=self._recall,
            )
        )


__all__ = [
    "MEMORY_BOUNDARY_OVERVIEW_PROJECTION",
    "MEMORY_BOUNDARY_RECALL_POLICY",
    "MEMORY_BOUNDARY_SEGMENT_PROJECTION",
    "MemoryBoundaryRecallPort",
    "MemoryBoundaryResolver",
    "MemoryBoundarySegmentNotFound",
]
