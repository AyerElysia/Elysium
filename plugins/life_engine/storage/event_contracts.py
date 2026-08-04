"""Backend-neutral contracts for the authoritative Life Event ledger."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from plugins.life_engine.service.event_bus import LifeEvent


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


__all__ = [
    "LifeEventConsumerConflict",
    "LifeEventConsumerCursor",
    "LifeEventOccurrenceConflict",
    "LifeEventStorePort",
]
