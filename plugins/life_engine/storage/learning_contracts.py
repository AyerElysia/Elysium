"""Backend-neutral append-only storage contracts for life learning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


class LearningOccurrenceConflict(RuntimeError):
    """Raised when one immutable occurrence is reused with different bytes."""


class LearningProjectionConflict(RuntimeError):
    """Raised when a projection revision/frontier CAS cannot be proven."""


@dataclass(frozen=True, slots=True)
class LearningEventDraft:
    """One immutable learning occurrence before its storage position is known."""

    occurrence_id: str
    event_kind: str
    occurred_at: str
    source: str
    actor_consciousness_instance_id: str
    subject_revision: str
    provenance: dict[str, Any]
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LearningEventRecord:
    """One positioned immutable learning occurrence."""

    position: int
    occurrence_id: str
    event_kind: str
    occurred_at: str
    recorded_at: str
    source: str
    actor_consciousness_instance_id: str
    subject_revision: str
    provenance: dict[str, Any]
    payload: dict[str, Any]
    event_sha256: str


@dataclass(frozen=True, slots=True)
class LearningProjection:
    """One rebuildable projection guarded by revision and source frontier."""

    projection_name: str
    revision: int
    source_frontier: int
    schema_version: int
    projector_version: str
    rebuild_state: str
    payload: dict[str, Any]
    projection_sha256: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class LearningProjectionWrite:
    """CAS command for one projection in a learning commit."""

    projection_name: str
    expected_revision: int
    expected_source_frontier: int
    schema_version: int
    projector_version: str
    rebuild_state: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LearningCommitResult:
    """Atomic event and projection commit result."""

    events: tuple[LearningEventRecord, ...]
    projections: tuple[LearningProjection, ...]


@runtime_checkable
class LearningStorePort(Protocol):
    """Append-only learning evidence plus rebuildable current projections."""

    async def commit(
        self,
        *,
        events: list[LearningEventDraft],
        projections: list[LearningProjectionWrite],
    ) -> LearningCommitResult:
        """Commit occurrences and their derived projections atomically."""

    async def read_events(
        self,
        after_position: int,
        *,
        limit: int = 100,
        event_kinds: tuple[str, ...] = (),
    ) -> list[LearningEventRecord]:
        """Read a stable ordered page strictly after one position token."""

    async def event_by_occurrence(
        self,
        occurrence_id: str,
    ) -> LearningEventRecord | None:
        """Resolve one immutable occurrence without scanning private payloads."""

    async def get_projection(
        self,
        projection_name: str,
    ) -> LearningProjection | None:
        """Read one current rebuildable projection."""

    async def list_projections(self) -> list[LearningProjection]:
        """List current projections by stable name."""

    async def health_snapshot(self) -> dict[str, Any]:
        """Return content-free event/frontier/rebuild diagnostics."""


@dataclass(frozen=True, slots=True)
class LearningStores:
    """One coherent runtime's learning storage bundle."""

    store: LearningStorePort


__all__ = [
    "LearningCommitResult",
    "LearningEventDraft",
    "LearningEventRecord",
    "LearningOccurrenceConflict",
    "LearningProjection",
    "LearningProjectionConflict",
    "LearningProjectionWrite",
    "LearningStorePort",
    "LearningStores",
]
