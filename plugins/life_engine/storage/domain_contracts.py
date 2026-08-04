"""Backend-neutral contracts for Presence and subjective World projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from plugins.life_engine.service.event_bus import LifeEvent
from plugins.life_engine.service.world_projection import (
    WorldAssertion,
    WorldProjectionChange,
)


class PresenceLeaseConflict(RuntimeError):
    """Raised when renewal or takeover cannot prove the requested lease state."""


@dataclass(frozen=True, slots=True)
class PresenceCommitResult:
    """One committed Presence revision observed at database time."""

    instance: dict[str, Any]
    previous_revision: int
    revision: int
    database_now: str


@dataclass(frozen=True, slots=True)
class PresenceTakeoverResult:
    """Atomic expired-owner replacement and its displaced snapshots."""

    claimant: PresenceCommitResult
    displaced: tuple[PresenceCommitResult, ...]


@runtime_checkable
class PresenceStorePort(Protocol):
    """Durable operational presence, leases, ownership, and lifecycle outbox."""

    async def list_instances(self) -> list[dict[str, Any]]:
        """Return the latest snapshots ordered by stable instance identity."""

    async def commit(
        self,
        instance: dict[str, Any],
        *,
        expected_revision: int | None,
        event_type: str,
        event_payload: dict[str, Any] | None = None,
        refresh_lease: bool = False,
    ) -> PresenceCommitResult:
        """Commit one revision and outbox event with optimistic concurrency."""

    async def renew_lease(
        self,
        instance_id: str,
        *,
        expected_revision: int,
        process_epoch: str,
        lease_seconds: int,
        event_payload: dict[str, Any] | None = None,
    ) -> PresenceCommitResult:
        """Renew an active lease from database time, never application time."""

    async def takeover_expired(
        self,
        instance: dict[str, Any],
        *,
        expected_revision: int | None,
        process_epoch: str,
        lease_seconds: int,
        event_payload: dict[str, Any] | None = None,
    ) -> PresenceTakeoverResult:
        """Atomically displace only expired stream owners and claim their streams."""

    async def pending_events(self, limit: int = 200) -> list[dict[str, Any]]:
        """Read unpublished lifecycle events without advancing the outbox."""

    async def acknowledge_events(self, outbox_ids: list[int]) -> None:
        """Mark lifecycle events published after the authoritative ledger accepts."""

    async def health_snapshot(self) -> dict[str, Any]:
        """Return bounded counts and lease/outbox diagnostics without payload text."""


@runtime_checkable
class WorldProjectionStorePort(Protocol):
    """Rebuildable source-preserving World projection and delivery cursors."""

    async def apply_events(self, events: list[LifeEvent]) -> int:
        """Apply an ordered ledger batch and return its committed frontier."""

    async def begin_rebuild(self) -> None:
        """Clear derived rows and frontier while preserving delivery cursors."""

    async def finish_rebuild(self, *, expected_frontier: int) -> None:
        """Mark a replay complete only at its explicitly verified frontier."""

    async def fail_rebuild(self) -> None:
        """Persist a failed rebuild state so readers fail closed."""

    async def projector_contract(self) -> dict[str, Any]:
        """Return persisted policy, schema version, frontier, and rebuild state."""

    async def list_assertions(
        self,
        *,
        include_retracted: bool = True,
    ) -> list[WorldAssertion]:
        """Return attributed assertions without resolving contradictions."""

    async def changes_since(
        self,
        ingest_position: int,
        *,
        through_position: int | None = None,
    ) -> list[WorldProjectionChange]:
        """Return cursor-visible changes in one stable position window."""

    async def perception_cursor(self, instance_id: str) -> tuple[int, int]:
        """Return one instance's delivery position and cursor revision."""

    async def commit_perception_cursor(
        self,
        instance_id: str,
        *,
        expected_position: int,
        expected_revision: int,
        through_position: int,
    ) -> tuple[int, int]:
        """Position+revision CAS advance, with an idempotent exact no-op."""

    async def health_snapshot(self) -> dict[str, Any]:
        """Return projector contract, counts, and bounded cursor lag diagnostics."""


@dataclass(frozen=True, slots=True)
class PresenceWorldStores:
    """One coherent backend's complete Presence and World adapter bundle."""

    presence: PresenceStorePort
    world: WorldProjectionStorePort


__all__ = [
    "PresenceCommitResult",
    "PresenceLeaseConflict",
    "PresenceStorePort",
    "PresenceTakeoverResult",
    "PresenceWorldStores",
    "WorldProjectionStorePort",
]
