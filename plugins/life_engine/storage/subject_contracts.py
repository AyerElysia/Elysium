"""Backend-neutral contracts for exact-byte subject document history."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


class SubjectDocumentConflict(RuntimeError):
    """Raised when occurrence identity or document head CAS conflicts."""


class SubjectDocumentNotFound(LookupError):
    """Raised when a requested immutable document version is unavailable."""


@dataclass(frozen=True, slots=True)
class SubjectDocumentHead:
    """Rebuildable head pointer for one declared logical path."""

    document_id: str
    logical_path: str
    declared_owner: str | None
    current_version_id: str
    revision: int


@dataclass(frozen=True, slots=True)
class SubjectDocumentVersion:
    """One immutable exact-byte or explicitly legacy-derived version."""

    version_id: str
    document_id: str
    logical_path: str
    parent_version_id: str
    occurrence_id: str
    semantic_actor_id: str | None
    semantic_source_id: str | None
    occurred_at: str | None
    recorded_by: str
    recorded_source: str
    recorded_at: str
    provenance_status: str
    content_bytes: bytes
    content_hash: str
    byte_length: int
    byte_fidelity: str
    encoding: str | None
    newline_style: str | None
    change_context: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AppendSubjectDocumentVersion:
    """Explicit command to append history and CAS-advance its head."""

    logical_path: str
    expected_revision: int
    expected_head_version_id: str
    content_bytes: bytes
    occurrence_id: str
    recorded_by: str
    recorded_source: str
    declared_owner: str | None = None
    semantic_actor_id: str | None = None
    semantic_source_id: str | None = None
    occurred_at: str | None = None
    provenance_status: str = "complete"
    byte_fidelity: str = "exact_bytes"
    encoding: str | None = None
    newline_style: str | None = None
    change_context: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SubjectDocumentCommit:
    """Atomic result containing the immutable version and new head."""

    version: SubjectDocumentVersion
    head: SubjectDocumentHead


@dataclass(frozen=True, slots=True)
class SubjectProjectionTask:
    """One leased workspace projection request."""

    outbox_id: int
    head_event_id: str
    document_id: str
    logical_path: str
    version_id: str
    content_hash: str
    state: str
    attempt_count: int
    lease_owner: str
    lease_until: str
    revision: int


@runtime_checkable
class SubjectDocumentStorePort(Protocol):
    """Append-only subject history with a revision-CAS head projection."""

    async def get_head(self, logical_path: str) -> SubjectDocumentHead | None:
        """Read one head without creating a document."""

    async def get_version(self, version_id: str) -> SubjectDocumentVersion:
        """Read one immutable version including exact content bytes."""

    async def list_heads(
        self,
        *,
        after_logical_path: str = "",
        limit: int = 100,
    ) -> list[SubjectDocumentHead]:
        """Read stable logical-path ordered heads for verification/export."""

    async def list_current_versions(
        self,
        *,
        after_logical_path: str = "",
        limit: int = 100,
    ) -> list[SubjectDocumentCommit]:
        """Read stable head/current-version pairs without per-document queries."""

    async def list_history(
        self,
        logical_path: str,
        *,
        after_recorded_at: str = "",
        after_version_id: str = "",
        limit: int = 100,
    ) -> list[SubjectDocumentVersion]:
        """Read stable chronological history using a composite cursor."""

    async def append_version(
        self,
        command: AppendSubjectDocumentVersion,
    ) -> SubjectDocumentCommit:
        """Append a version/head-event/outbox and CAS-advance the head."""

    async def claim_projection(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        logical_path: str | None = None,
    ) -> SubjectProjectionTask | None:
        """Lease one pending projection, optionally for one declared path."""

    async def get_projection_task(
        self,
        logical_path: str,
        version_id: str,
    ) -> SubjectProjectionTask | None:
        """Return the durable projection state for one exact version."""

    async def confirm_projection(
        self,
        task: SubjectProjectionTask,
        *,
        worker_id: str,
    ) -> None:
        """Confirm a leased projection after exact file verification."""

    async def fail_projection(
        self,
        task: SubjectProjectionTask,
        *,
        worker_id: str,
        error: str,
    ) -> None:
        """Persist a bounded projection failure without changing history."""

    async def health_snapshot(self) -> dict[str, Any]:
        """Return bounded counts and projection backlog diagnostics."""


__all__ = [
    "AppendSubjectDocumentVersion",
    "SubjectDocumentCommit",
    "SubjectDocumentConflict",
    "SubjectDocumentHead",
    "SubjectDocumentNotFound",
    "SubjectDocumentStorePort",
    "SubjectDocumentVersion",
    "SubjectProjectionTask",
]
