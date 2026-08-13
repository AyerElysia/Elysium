"""Backend-neutral contracts for the authoritative Life Event ledger."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..service.event_bus import LifeEvent


class LifeEventOccurrenceConflict(RuntimeError):
    """Raised when one occurrence identity is reused with different evidence."""


class LifeEventConsumerConflict(RuntimeError):
    """Raised when a consumer cursor compare-and-swap cannot be proven."""


@dataclass(frozen=True, slots=True)
class LifeEventConsumerCursor:
    """One monotonic consumer offset and its optimistic-concurrency revision."""

    consumer_id: str
    position: int
    revision: int
    updated_at: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LifeEventDigest:
    """Stable occurrence identity and payload digest for migration verification."""

    occurrence_id: str
    position: int
    payload_hash: str


@dataclass(frozen=True, slots=True)
class LifeEventSnapshotRecord:
    """Exact immutable row imported only by a fenced candidate-copy writer."""

    ingest_position: int
    occurrence_id: str
    source_event_id: str
    source_sequence: int
    occurred_at: str
    recorded_at: str
    payload_json: str
    payload_hash: str


@dataclass(frozen=True, slots=True)
class LifeEventSnapshotCursor:
    """Exact legacy consumer cursor imported into a candidate ledger."""

    consumer_id: str
    ingest_position: int
    revision: int
    updated_at: str
    metadata_json: str


@runtime_checkable
class LifeEventStorePort(Protocol):
    """Append-only event ledger with explicit history and cursor semantics."""

    async def append(self, event: LifeEvent) -> LifeEvent:
        """Append one occurrence or return its byte-equivalent persisted row."""

    async def append_many(self, events: list[LifeEvent]) -> list[LifeEvent]:
        """Atomically append a batch in caller order."""

    async def read_since(
        self,
        position: int,
        *,
        limit: int | None = None,
    ) -> list[LifeEvent]:
        """Read events after an opaque monotonic position."""

    async def read_tail(self, limit: int = 100) -> list[LifeEvent]:
        """Read the latest events in ascending ledger order."""

    async def occurrence_digest(self, occurrence_id: str) -> LifeEventDigest | None:
        """Return one immutable identity/hash pair without exposing mutable SQL."""

    async def occurrence_digests(
        self,
        occurrence_ids: list[str],
    ) -> list[LifeEventDigest]:
        """Batch-read immutable identities in caller order when present."""

    async def consumer_cursor(self, consumer_id: str) -> LifeEventConsumerCursor:
        """Read one consumer cursor without creating it."""

    async def get_consumer_offset(self, consumer_id: str) -> int:
        """Compatibility read for existing at-least-once consumers."""

    async def commit_consumer_cursor(
        self,
        consumer_id: str,
        *,
        expected_position: int,
        expected_revision: int,
        through_position: int,
        metadata: dict[str, Any] | None = None,
    ) -> LifeEventConsumerCursor:
        """CAS-advance one cursor without regression or frontier overrun."""

    async def commit_consumer_offset(
        self,
        consumer_id: str,
        ingest_position: int,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Compatibility monotonic advance implemented through revision CAS."""

    async def health_snapshot(self) -> dict[str, Any]:
        """Return bounded ledger, consumer, and export-outbox diagnostics."""

    async def health(self) -> dict[str, Any]:
        """Compatibility alias for existing asynchronous diagnostics."""


@runtime_checkable
class LifeEventSnapshotImportPort(Protocol):
    """Candidate-only surface for byte-preserving ledger migration."""

    async def import_snapshot_records(
        self,
        records: list[LifeEventSnapshotRecord],
    ) -> list[LifeEventDigest]:
        """Import exact positions and source payload bytes idempotently."""

    async def import_snapshot_cursors(
        self,
        cursors: list[LifeEventSnapshotCursor],
        *,
        source_frontier: int,
    ) -> None:
        """Import exact cursor evidence without advancing active consumers."""

    async def occurrence_digests(
        self,
        occurrence_ids: list[str],
    ) -> list[LifeEventDigest]:
        """Batch-read immutable identities in caller order when present."""


@runtime_checkable
class LifeEventSnapshotSourcePort(Protocol):
    """Read-only exact ledger surface used by audited reverse export."""

    async def snapshot_records_after(
        self,
        position: int,
        *,
        limit: int,
    ) -> list[LifeEventSnapshotRecord]:
        """Read exact immutable rows after a monotonic position."""

    async def snapshot_cursors(self) -> list[LifeEventSnapshotCursor]:
        """Read exact durable consumer cursor evidence."""


__all__ = [
    "LifeEventConsumerConflict",
    "LifeEventConsumerCursor",
    "LifeEventDigest",
    "LifeEventOccurrenceConflict",
    "LifeEventSnapshotCursor",
    "LifeEventSnapshotImportPort",
    "LifeEventSnapshotRecord",
    "LifeEventSnapshotSourcePort",
    "LifeEventStorePort",
]
