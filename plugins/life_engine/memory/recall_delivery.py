"""Two-phase exact-delivery traces for ordinary memory search.

Producing a ``nucleus_search_memory`` result is not evidence that the model
actually recalled it.  This module keeps a bounded process-local delivery plan
and appends the durable RecallEpisode/RecallEvent/CoRecall history only after
the LLM kernel proves that the complete ToolResult survived the final
successful request attempt byte-for-byte.
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

from .living import CoRecallEvent, RecallEpisode, RecallEvent

MEMORY_SEARCH_RECALL_DELIVERY_KIND = "memory_search_recall_v1"
MEMORY_SEARCH_RECALL_POLICY = "living-recall-exact-delivery-v2"
MEMORY_SEARCH_RECALL_PENDING_MAX = 256
MEMORY_SEARCH_RECALL_PENDING_TTL_SECONDS = 15 * 60.0


class MemorySearchRecallDeliveryError(RuntimeError):
    """Base error for an invalid or conflicting search-delivery proof."""


class MemorySearchRecallPort(Protocol):
    """Minimal durable living-memory surface used after exact delivery."""

    async def begin_memory_recall(self, **kwargs: Any) -> RecallEpisode: ...

    async def append_memory_recall_events(
        self,
        events: list[RecallEvent] | tuple[RecallEvent, ...],
    ) -> tuple[RecallEvent, ...]: ...

    async def append_memory_corecall(self, event: CoRecallEvent) -> CoRecallEvent: ...


@dataclass(frozen=True, slots=True)
class DeliveredMemorySearchRef:
    """One reference visibly present on the final bounded search page."""

    entity_ref: str
    source: str
    ordinal: int
    metadata: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PendingMemorySearchRecall:
    """Immutable persistence plan waiting for exact ToolResult proof."""

    delivery_id: str
    recall_chain_id: str
    episode_id: str
    consciousness_instance_id: str
    stream_scope: str
    source_occurrence_id: str
    recorded_at: str
    query: str
    retrieval_intent: str
    context_key: str
    random_seed: int
    frontier_sha256: str
    page_offset: int
    delivered_refs: tuple[DeliveredMemorySearchRef, ...]
    search_context: Mapping[str, object]
    recall: MemorySearchRecallPort
    policy_version: str = MEMORY_SEARCH_RECALL_POLICY

    @property
    def delivered_refs_sha256(self) -> str:
        """Return a stable content-free digest of this page's visible refs."""

        raw = json.dumps(
            [item.entity_ref for item in self.delivered_refs],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class MemorySearchRecallDeliveryExpectation:
    """Exact ToolResult bytes registered with the LLM context manager."""

    delivery_id: str
    expected_text: str
    expected_sha256: str
    expected_utf8_bytes: int


@dataclass(slots=True)
class _PendingEntry:
    created_at: float
    plan: PendingMemorySearchRecall
    expected_sha256: str = ""
    expected_utf8_bytes: int = -1


class MemorySearchRecallDeliveryCoordinator:
    """Bounded process-local gate between search projection and recall history."""

    def __init__(
        self,
        *,
        max_pending: int = MEMORY_SEARCH_RECALL_PENDING_MAX,
        ttl_seconds: float = MEMORY_SEARCH_RECALL_PENDING_TTL_SECONDS,
    ) -> None:
        self._max_pending = max(1, int(max_pending))
        self._ttl_seconds = max(0.001, float(ttl_seconds))
        self._lock = threading.RLock()
        self._pending: OrderedDict[str, _PendingEntry] = OrderedDict()
        self._committing: set[str] = set()
        self._registered_total = 0
        self._bound_total = 0
        self._committed_total = 0
        self._discarded_total = 0
        self._rejected_total = 0
        self._expired_total = 0
        self._evicted_total = 0
        self._commit_failures_total = 0

    def _prune_locked(self, now: float) -> None:
        expired = [
            delivery_id
            for delivery_id, entry in self._pending.items()
            if now - entry.created_at >= self._ttl_seconds
            and delivery_id not in self._committing
        ]
        for delivery_id in expired:
            self._pending.pop(delivery_id, None)
            self._expired_total += 1
        while len(self._pending) > self._max_pending:
            delivery_id, _entry = self._pending.popitem(last=False)
            self._committing.discard(delivery_id)
            self._evicted_total += 1

    def register(self, plan: PendingMemorySearchRecall) -> None:
        """Stage one deterministic page without writing durable recall history."""

        if not plan.delivery_id or not plan.recall_chain_id or not plan.episode_id:
            raise MemorySearchRecallDeliveryError(
                "MemorySearchRecallDeliveryIdentityRequired"
            )
        if (
            not plan.consciousness_instance_id
            or not plan.source_occurrence_id
            or not plan.recorded_at
        ):
            raise MemorySearchRecallDeliveryError(
                "MemorySearchRecallActorAndSourceRequired"
            )
        if not plan.delivered_refs:
            raise MemorySearchRecallDeliveryError(
                "MemorySearchRecallDeliveredRefsRequired"
            )

        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            existing = self._pending.get(plan.delivery_id)
            if existing is not None:
                if existing.plan != plan:
                    raise MemorySearchRecallDeliveryError(
                        f"MemorySearchRecallDeliveryConflict:{plan.delivery_id}"
                    )
                self._pending.move_to_end(plan.delivery_id)
                return
            self._pending[plan.delivery_id] = _PendingEntry(now, plan)
            self._registered_total += 1
            self._prune_locked(now)

    def has_pending(self, delivery_id: str) -> bool:
        """Return whether a non-expired page still awaits exact proof."""

        identity = str(delivery_id or "").strip()
        if not identity:
            return False
        with self._lock:
            self._prune_locked(time.monotonic())
            return identity in self._pending

    def register_pending_tool_result(
        self,
        payload: Mapping[str, Any],
        expected_text: str,
    ) -> MemorySearchRecallDeliveryExpectation:
        """Bind a staged plan to the complete serialized ToolResult text."""

        binding = payload.get("recall_delivery_binding")
        if not isinstance(binding, Mapping):
            raise MemorySearchRecallDeliveryError(
                "MemorySearchRecallDeliveryBindingRequired"
            )
        if str(binding.get("kind") or "") != MEMORY_SEARCH_RECALL_DELIVERY_KIND:
            raise MemorySearchRecallDeliveryError(
                "MemorySearchRecallDeliveryKindMismatch"
            )
        delivery_id = str(binding.get("delivery_id") or "").strip()
        if not delivery_id or delivery_id not in str(expected_text):
            raise MemorySearchRecallDeliveryError(
                "MemorySearchRecallDeliveryMarkerMissing"
            )
        encoded = str(expected_text).encode("utf-8")
        expected_sha256 = hashlib.sha256(encoded).hexdigest()
        expected_utf8_bytes = len(encoded)

        with self._lock:
            self._prune_locked(time.monotonic())
            entry = self._pending.get(delivery_id)
            if entry is None:
                raise MemorySearchRecallDeliveryError(
                    f"MemorySearchRecallDeliveryPlanUnavailable:{delivery_id}"
                )
            plan = entry.plan
            valid_binding = all(
                (
                    str(binding.get("recall_chain_id") or "")
                    == plan.recall_chain_id,
                    str(binding.get("episode_id") or "") == plan.episode_id,
                    int(binding.get("page_offset") or 0) == plan.page_offset,
                    str(binding.get("delivered_refs_sha256") or "")
                    == plan.delivered_refs_sha256,
                    int(binding.get("delivered_ref_count") or 0)
                    == len(plan.delivered_refs),
                )
            )
            if not valid_binding:
                raise MemorySearchRecallDeliveryError(
                    f"MemorySearchRecallDeliveryBindingMismatch:{delivery_id}"
                )
            if entry.expected_sha256 and (
                entry.expected_sha256 != expected_sha256
                or entry.expected_utf8_bytes != expected_utf8_bytes
            ):
                raise MemorySearchRecallDeliveryError(
                    f"MemorySearchRecallToolResultConflict:{delivery_id}"
                )
            if not entry.expected_sha256:
                entry.expected_sha256 = expected_sha256
                entry.expected_utf8_bytes = expected_utf8_bytes
                self._bound_total += 1
            self._pending.move_to_end(delivery_id)

        return MemorySearchRecallDeliveryExpectation(
            delivery_id=delivery_id,
            expected_text=str(expected_text),
            expected_sha256=expected_sha256,
            expected_utf8_bytes=expected_utf8_bytes,
        )

    def discard(self, delivery_id: str) -> None:
        """Drop an unverified plan after failure, cancellation, or trimming."""

        identity = str(delivery_id or "").strip()
        with self._lock:
            removed = self._pending.pop(identity, None)
            self._committing.discard(identity)
            if removed is not None:
                self._discarded_total += 1

    async def commit_exact(self, delivery_id: str, receipt: Any) -> bool:
        """Persist one page only when the final attempt proves exact delivery."""

        identity = str(delivery_id or "").strip()
        with self._lock:
            self._prune_locked(time.monotonic())
            entry = self._pending.get(identity)
            if entry is None or identity in self._committing:
                return False
            expected_bytes = getattr(receipt, "expected_utf8_bytes", None)
            effective_bytes = getattr(receipt, "effective_utf8_bytes", None)
            valid_receipt = all(
                (
                    bool(entry.expected_sha256),
                    str(getattr(receipt, "delivery_id", "") or "") == identity,
                    str(getattr(receipt, "part_kind", "") or "")
                    == "tool_result",
                    bool(getattr(receipt, "exact_present", False)),
                    str(getattr(receipt, "expected_sha256", "") or "")
                    == entry.expected_sha256,
                    str(getattr(receipt, "effective_sha256", "") or "")
                    == entry.expected_sha256,
                    isinstance(expected_bytes, int),
                    isinstance(effective_bytes, int),
                    expected_bytes == entry.expected_utf8_bytes,
                    effective_bytes == entry.expected_utf8_bytes,
                )
            )
            if not valid_receipt:
                self._pending.pop(identity, None)
                self._rejected_total += 1
                return False
            self._committing.add(identity)
            plan = entry.plan

        try:
            await self._persist(
                plan,
                effective_sha256=entry.expected_sha256,
                effective_utf8_bytes=entry.expected_utf8_bytes,
            )
        except BaseException:
            with self._lock:
                self._committing.discard(identity)
                self._commit_failures_total += 1
            raise

        with self._lock:
            current = self._pending.get(identity)
            if current is entry:
                self._pending.pop(identity, None)
            self._committing.discard(identity)
            self._committed_total += 1
        return True

    @staticmethod
    async def _persist(
        plan: PendingMemorySearchRecall,
        *,
        effective_sha256: str,
        effective_utf8_bytes: int,
    ) -> None:
        episode = await plan.recall.begin_memory_recall(
            query=plan.query,
            retrieval_intent=plan.retrieval_intent,
            consciousness_instance_id=plan.consciousness_instance_id,
            stream_scope=plan.stream_scope,
            context_key=plan.context_key,
            policy_version=plan.policy_version,
            random_seed=plan.random_seed,
            episode_id=plan.episode_id,
            recorded_at=plan.recorded_at,
            context={
                **dict(plan.search_context),
                "recall_chain_id": plan.recall_chain_id,
                "source_occurrence_id": plan.source_occurrence_id,
                "frontier_sha256": plan.frontier_sha256,
            },
        )
        events = tuple(
            RecallEvent(
                event_id="recall_event_"
                + hashlib.sha256(
                    (
                        plan.delivery_id
                        + "\0"
                        + item.entity_ref
                        + "\0"
                        + str(item.ordinal)
                    ).encode("utf-8")
                ).hexdigest(),
                episode_id=episode.episode_id,
                action="delivered_to_model_context",
                recorded_at=plan.recorded_at,
                entity_ref=item.entity_ref,
                ordinal=item.ordinal,
                source=item.source,
                reason=plan.retrieval_intent,
                metadata={
                    **dict(item.metadata),
                    "projection_version": "memory-search-projection-v2",
                    "exact_tool_result_delivered": True,
                    "delivery_id": plan.delivery_id,
                    "effective_context_sha256": effective_sha256,
                    "effective_context_utf8_bytes": effective_utf8_bytes,
                    "source_occurrence_id": plan.source_occurrence_id,
                    "recall_chain_id": plan.recall_chain_id,
                },
            )
            for item in plan.delivered_refs
        )
        await plan.recall.append_memory_recall_events(events)

        entity_refs = tuple(dict.fromkeys(item.entity_ref for item in plan.delivered_refs))
        if len(entity_refs) < 2:
            return
        corecall_id = "corecall_" + hashlib.sha256(
            (
                plan.delivery_id
                + "\0"
                + "\0".join(entity_refs)
            ).encode("utf-8")
        ).hexdigest()
        await plan.recall.append_memory_corecall(
            CoRecallEvent(
                corecall_id=corecall_id,
                episode_id=episode.episode_id,
                context_key=plan.context_key,
                signal="co_recalled_from_exact_memory_search_delivery",
                entity_refs=entity_refs,
                actor=plan.consciousness_instance_id,
                reason="same exact memory-search ToolResult page",
                recorded_at=plan.recorded_at,
                metadata={
                    "projection_version": "memory-search-projection-v2",
                    "accessibility_only": True,
                    "does_not_change_truth_or_importance": True,
                    "delivery_id": plan.delivery_id,
                    "recall_chain_id": plan.recall_chain_id,
                    "source_occurrence_id": plan.source_occurrence_id,
                    "frontier_sha256": plan.frontier_sha256,
                    "page_offset": plan.page_offset,
                },
            )
        )

    def health_snapshot(self) -> dict[str, object]:
        """Return bounded content-free coordinator health metrics."""

        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            oldest_age = (
                max(0.0, now - next(iter(self._pending.values())).created_at)
                if self._pending
                else 0.0
            )
            degraded = bool(
                len(self._pending) >= self._max_pending
                or oldest_age >= self._ttl_seconds * 0.75
            )
            return {
                "component": "memory_search_recall_delivery",
                "status": "degraded" if degraded else "healthy",
                "pending_count": len(self._pending),
                "committing_count": len(self._committing),
                "oldest_pending_age_seconds": round(oldest_age, 3),
                "max_pending": self._max_pending,
                "ttl_seconds": self._ttl_seconds,
                "registered_total": self._registered_total,
                "bound_total": self._bound_total,
                "committed_total": self._committed_total,
                "discarded_total": self._discarded_total,
                "rejected_total": self._rejected_total,
                "expired_total": self._expired_total,
                "evicted_total": self._evicted_total,
                "commit_failures_total": self._commit_failures_total,
                "authority": "process_local_delivery_proof_only",
            }

    def reset_for_tests(self) -> None:
        """Clear process-local state; intended only for deterministic tests."""

        with self._lock:
            self._pending.clear()
            self._committing.clear()
            self._registered_total = 0
            self._bound_total = 0
            self._committed_total = 0
            self._discarded_total = 0
            self._rejected_total = 0
            self._expired_total = 0
            self._evicted_total = 0
            self._commit_failures_total = 0


_SEARCH_RECALL_COORDINATOR = MemorySearchRecallDeliveryCoordinator()


def get_memory_search_recall_delivery_coordinator(
) -> MemorySearchRecallDeliveryCoordinator:
    """Return the process-local exact-delivery gate for ordinary search."""

    return _SEARCH_RECALL_COORDINATOR


__all__ = [
    "MEMORY_SEARCH_RECALL_DELIVERY_KIND",
    "MEMORY_SEARCH_RECALL_POLICY",
    "DeliveredMemorySearchRef",
    "MemorySearchRecallDeliveryCoordinator",
    "MemorySearchRecallDeliveryError",
    "MemorySearchRecallDeliveryExpectation",
    "PendingMemorySearchRecall",
    "get_memory_search_recall_delivery_coordinator",
]
