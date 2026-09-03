"""Named Life Event consumer for the subconscious heartbeat work set.

Pending is a derived buffer. The authoritative cursor lives on the ledger.
This module classifies ledger rows into the heartbeat work set; it does not
decide what those events mean.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from .event_builder import EventType, LifeEngineEvent
from .event_bus import (
    LifeEvent,
    RawEventGapError,
    legacy_event_from_life_event,
)

SUBCONSCIOUS_INGEST_CONSUMER_ID = "life_engine_subconscious_ingest:v1"
SUBCONSCIOUS_INGEST_BATCH_LIMIT = 200
_DELIVERY_WORKSET_TYPES = frozenset(
    {
        "chat.message.delivery_failed",
        "chat.message.delivery_unknown",
    }
)


class SubconsciousLedgerGap(RuntimeError):
    """Raised when the subconscious consumer refuses to skip missing history."""


class SubconsciousIngestStoreUnavailable(RuntimeError):
    """Raised when the heartbeat cannot prove a readable Life Event store."""


class _SequenceAllocator(Protocol):
    def __call__(self) -> int: ...


@dataclass(frozen=True, slots=True)
class SubconsciousIngestReport:
    """Content-free catch-up diagnostics for one bounded ingest pass."""

    consumer_id: str
    from_position: int
    through_position: int
    frontier: int
    scanned: int
    queued: int
    classified_out: int
    bootstrapped: bool
    backlog: int
    gap: bool
    error_type: str = ""
    bootstrap_reason: str = ""
    revision: int = 0

    def health(self) -> dict[str, Any]:
        status = "failed" if self.gap or self.error_type else "healthy"
        if self.bootstrapped and status == "healthy":
            status = "healthy"
        return {
            "status": status,
            "component": "subconscious_ingest",
            "consumer_id": self.consumer_id,
            "position": self.through_position,
            "revision": self.revision,
            "frontier": self.frontier,
            "backlog": self.backlog,
            "scanned": self.scanned,
            "queued": self.queued,
            "classified_out": self.classified_out,
            "bootstrapped": self.bootstrapped,
            "bootstrap_reason": self.bootstrap_reason,
            "gap": self.gap,
            "error_type": self.error_type,
        }


def workset_identities(event: Any) -> set[str]:
    """Return occurrence and event identities used for work-set idempotency."""

    identities: set[str] = set()
    for attr in ("occurrence_id", "event_id"):
        value = str(getattr(event, attr, "") or "").strip()
        if value:
            identities.add(value)
    return identities


def classify_life_event_for_workset(event: LifeEvent) -> str:
    """Return ``workset``, ``synthetic_delivery``, or ``advance``.

    ``advance`` still consumes the ingest cursor. The row remains on the
    ledger and is searchable; it is not a semantic discard.
    """

    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    if str(metadata.get("legacy_event_type") or "").strip():
        return "workset"
    event_type = str(event.event_type or "").strip().lower()
    if event_type in _DELIVERY_WORKSET_TYPES:
        return "synthetic_delivery"
    return "advance"


def synthetic_delivery_event(event: LifeEvent, *, sequence: int) -> LifeEngineEvent:
    """Materialize a delivery failure/unknown fact as a MESSAGE work-set event."""

    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    chat = metadata.get("chat") if isinstance(metadata.get("chat"), dict) else {}
    sender = str(
        metadata.get("actor_display_name")
        or metadata.get("sender")
        or chat.get("sender_name")
        or ""
    ).strip()
    return LifeEngineEvent(
        event_id=str(event.event_id or event.occurrence_id or f"delivery_{sequence}"),
        event_type=EventType.MESSAGE,
        timestamp=str(event.timestamp or ""),
        sequence=int(sequence),
        source=str(event.source or ""),
        source_detail=str(event.event_type or "chat.message.delivery"),
        content=str(event.content or ""),
        content_type=str(event.event_type or "chat.message.delivery_failed"),
        sender=sender or None,
        sender_id=str(metadata.get("actor_id") or chat.get("sender_id") or "") or None,
        chat_type=str(chat.get("chat_type") or "") or None,
        stream_id=str(event.stream_id or "") or None,
        occurrence_id=str(event.occurrence_id or event.event_id or "") or None,
        causation_id=str(event.causation_id or "") or None,
        correlation_id=str(event.correlation_id or "") or None,
        source_instance_id=str(event.source_instance_id or "") or None,
        content_ref=str(event.content_ref or "") or None,
        raw_content=str(event.content or ""),
    )


def reconstruct_workset_event(
    event: LifeEvent,
    *,
    next_sequence: _SequenceAllocator,
) -> LifeEngineEvent | None:
    """Turn one ledger row into a heartbeat work-set event, or skip it."""

    decision = classify_life_event_for_workset(event)
    if decision == "advance":
        return None
    if decision == "synthetic_delivery":
        return synthetic_delivery_event(event, sequence=next_sequence())
    reconstructed = legacy_event_from_life_event(event)
    if reconstructed is None:
        return None
    if int(reconstructed.sequence or 0) <= 0:
        reconstructed = replace(reconstructed, sequence=next_sequence())
    identity = str(reconstructed.occurrence_id or reconstructed.event_id or "").strip()
    if not identity:
        reconstructed = replace(
            reconstructed,
            occurrence_id=str(event.occurrence_id or event.event_id or ""),
        )
    return reconstructed


async def _consumer_state(store: Any, consumer_id: str) -> Any:
    cursor_fn = getattr(store, "consumer_cursor", None)
    if callable(cursor_fn):
        return await cursor_fn(consumer_id)
    get_offset = getattr(store, "get_consumer_offset", None)
    if not callable(get_offset):
        raise SubconsciousIngestStoreUnavailable(
            "Life Event store cannot read a consumer cursor"
        )
    position = int(await get_offset(consumer_id))
    return _OffsetCursor(consumer_id=consumer_id, position=position)


@dataclass(frozen=True, slots=True)
class _OffsetCursor:
    consumer_id: str
    position: int
    revision: int = 0
    updated_at: str = ""
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.metadata is None:
            object.__setattr__(self, "metadata", {})


async def _ledger_frontier(store: Any) -> int:
    snapshot_fn = getattr(store, "health_snapshot", None)
    if callable(snapshot_fn):
        snapshot = snapshot_fn()
        if hasattr(snapshot, "__await__"):
            snapshot = await snapshot
        if isinstance(snapshot, dict):
            return max(0, int(snapshot.get("latest_position") or 0))
    health_fn = getattr(store, "health", None)
    if callable(health_fn):
        snapshot = health_fn()
        if hasattr(snapshot, "__await__"):
            snapshot = await snapshot
        if isinstance(snapshot, dict):
            return max(0, int(snapshot.get("latest_position") or 0))
    tail_fn = getattr(store, "read_tail", None)
    if callable(tail_fn):
        tail = await tail_fn(1)
        if tail:
            return max(0, int(tail[-1].sequence or 0))
    return 0


def _consumer_never_created(cursor: Any) -> bool:
    updated_at = str(getattr(cursor, "updated_at", "") or "").strip()
    revision = int(getattr(cursor, "revision", 0) or 0)
    metadata = getattr(cursor, "metadata", None)
    has_metadata = bool(metadata)
    return not updated_at and revision == 0 and not has_metadata


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat()


async def catch_up_subconscious_ingest(
    *,
    store: Any,
    next_sequence: _SequenceAllocator,
    known_occurrences: set[str],
    heartbeat_context_cursor: int,
    batch_limit: int = SUBCONSCIOUS_INGEST_BATCH_LIMIT,
) -> tuple[SubconsciousIngestReport, list[LifeEngineEvent]]:
    """Read ledger rows after the ingest cursor into one bounded work-set batch.

    The caller must persist the returned events into pending/history before
    committing the ingest cursor. Classified-out rows still advance the cursor.
    """

    consumer_id = SUBCONSCIOUS_INGEST_CONSUMER_ID
    read_since = getattr(store, "read_since", None)
    commit_offset = getattr(store, "commit_consumer_offset", None)
    if not callable(read_since) or not callable(commit_offset):
        raise SubconsciousIngestStoreUnavailable(
            "Life Event store cannot catch up the subconscious consumer"
        )
    cursor = await _consumer_state(store, consumer_id)
    from_position = int(getattr(cursor, "position", 0) or 0)
    revision = int(getattr(cursor, "revision", 0) or 0)
    frontier = await _ledger_frontier(store)
    if (
        _consumer_never_created(cursor)
        and frontier > 0
        and int(heartbeat_context_cursor or 0) > 0
    ):
        metadata = {
            "bootstrap": "high_water",
            "reason": "do_not_replay_history_as_new_life",
            "seeded_at": _now_iso(),
            "heartbeat_context_cursor": int(heartbeat_context_cursor),
        }
        committed = int(
            await commit_offset(consumer_id, frontier, metadata=metadata) or frontier
        )
        return (
            SubconsciousIngestReport(
                consumer_id=consumer_id,
                from_position=from_position,
                through_position=committed,
                frontier=frontier,
                scanned=0,
                queued=0,
                classified_out=0,
                bootstrapped=True,
                backlog=max(0, frontier - committed),
                gap=False,
                bootstrap_reason="high_water",
                revision=revision,
            ),
            [],
        )

    try:
        rows = await read_since(from_position, limit=max(1, int(batch_limit)))
    except RawEventGapError as gap:
        raise SubconsciousLedgerGap(
            "SubconsciousLedgerGap: refusing to skip missing life history "
            f"after={gap.requested_sequence} earliest={gap.earliest_available}"
        ) from gap

    queued: list[LifeEngineEvent] = []
    classified_out = 0
    through = from_position
    seen = set(known_occurrences)
    for row in rows:
        through = max(through, int(row.sequence or 0))
        reconstructed = reconstruct_workset_event(row, next_sequence=next_sequence)
        if reconstructed is None:
            classified_out += 1
            continue
        identities = workset_identities(reconstructed)
        if identities and identities & seen:
            classified_out += 1
            continue
        queued.append(reconstructed)
        seen.update(identities)

    backlog = max(0, frontier - through)
    report = SubconsciousIngestReport(
        consumer_id=consumer_id,
        from_position=from_position,
        through_position=through,
        frontier=frontier,
        scanned=len(rows),
        queued=len(queued),
        classified_out=classified_out,
        bootstrapped=False,
        backlog=backlog,
        gap=False,
        revision=revision,
    )
    return report, queued


async def commit_subconscious_ingest_cursor(
    store: Any,
    *,
    through_position: int,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Advance the ingest cursor after the work set is durably checkpointed."""

    commit_offset = getattr(store, "commit_consumer_offset", None)
    if not callable(commit_offset):
        raise SubconsciousIngestStoreUnavailable(
            "Life Event store cannot commit the subconscious consumer"
        )
    payload = dict(metadata or {})
    payload.setdefault("stage", "subconscious_ingest")
    payload.setdefault("committed_at", _now_iso())
    committed = await commit_offset(
        SUBCONSCIOUS_INGEST_CONSUMER_ID,
        int(through_position),
        metadata=payload,
    )
    return int(committed if committed is not None else through_position)


__all__ = [
    "SUBCONSCIOUS_INGEST_BATCH_LIMIT",
    "SUBCONSCIOUS_INGEST_CONSUMER_ID",
    "SubconsciousIngestReport",
    "SubconsciousIngestStoreUnavailable",
    "SubconsciousLedgerGap",
    "catch_up_subconscious_ingest",
    "classify_life_event_for_workset",
    "commit_subconscious_ingest_cursor",
    "reconstruct_workset_event",
    "synthetic_delivery_event",
    "workset_identities",
]
