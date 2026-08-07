"""Backend-neutral contracts for selected runtime state and event storage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


class RuntimeStateConflict(RuntimeError):
    """Raised when a state CAS revision cannot be proven."""


class RuntimeStateCorrupt(RuntimeError):
    """Raised when persisted JSON no longer matches its recorded digest."""


class RuntimeEventConflict(RuntimeError):
    """Raised when one occurrence is reused with different content."""


@dataclass(frozen=True, slots=True)
class RuntimeStateRecord:
    """One current technical runtime state guarded by a monotonic revision."""

    namespace: str
    state_key: str
    revision: int
    schema_version: int
    payload: dict[str, Any]
    payload_sha256: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class RuntimeEventRecord:
    """One positioned immutable technical runtime event."""

    position: int
    namespace: str
    occurrence_id: str
    event_kind: str
    payload: dict[str, Any]
    payload_sha256: str
    occurred_at: str
    recorded_at: str


@runtime_checkable
class RuntimeStateStorePort(Protocol):
    """Fenced selected-backend storage for technical state and event streams."""

    async def get_state(
        self,
        namespace: str,
        state_key: str,
    ) -> RuntimeStateRecord | None:
        """Read one current state with integrity verification."""

    async def put_state(
        self,
        *,
        namespace: str,
        state_key: str,
        expected_revision: int,
        schema_version: int,
        payload: dict[str, Any],
    ) -> RuntimeStateRecord:
        """Create or replace one state through exact revision CAS."""

    async def append_event(
        self,
        *,
        namespace: str,
        occurrence_id: str,
        event_kind: str,
        payload: dict[str, Any],
        occurred_at: str,
    ) -> RuntimeEventRecord:
        """Append one immutable idempotent event."""

    async def read_events(
        self,
        namespace: str,
        *,
        after_position: int = 0,
        limit: int = 100,
    ) -> list[RuntimeEventRecord]:
        """Read one stable ordered event page."""

    async def health_snapshot(self) -> dict[str, Any]:
        """Return content-free state/event diagnostics."""


__all__ = [
    "RuntimeEventConflict",
    "RuntimeEventRecord",
    "RuntimeStateConflict",
    "RuntimeStateCorrupt",
    "RuntimeStateRecord",
    "RuntimeStateStorePort",
]
