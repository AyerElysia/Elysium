"""Backend-neutral contracts for exact-byte subject document history."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

SubjectDocumentPath = Literal["SOUL.md", "USER.md", "MEMORY.md"]
SUBJECT_AUTHORITY_PATHS: tuple[SubjectDocumentPath, ...] = (
    "SOUL.md",
    "USER.md",
    "MEMORY.md",
)


class SubjectDocumentConflict(RuntimeError):
    """Raised when occurrence identity or document head CAS conflicts."""


class SubjectDocumentNotFound(LookupError):
    """Raised when a requested immutable document version is unavailable."""


class SubjectAuthorityConflict(RuntimeError):
    """Raised when unified revision, decision identity, or document CAS conflicts."""


class SubjectAuthorityEvidenceError(RuntimeError):
    """Raised when immutable candidate/decision evidence is absent or inconsistent."""


class SubjectAuthorityActorInactive(RuntimeError):
    """Raised when the accepting consciousness instance is not currently active."""


def subject_authority_logical_path(path: SubjectDocumentPath) -> str:
    """Map one public authority name into the selected-storage namespace."""

    if path not in SUBJECT_AUTHORITY_PATHS:
        raise ValueError(f"unsupported subject authority path: {path}")
    return f"life_engine_workspace/{path}"


def subject_revision_from_contents(
    contents: dict[SubjectDocumentPath, bytes],
) -> str:
    """Compute the canonical unified SOUL+USER+MEMORY exact-byte revision."""

    digest = hashlib.sha256()
    for path in SUBJECT_AUTHORITY_PATHS:
        if path not in contents:
            raise ValueError(f"subject authority content is missing: {path}")
        content = bytes(contents[path])
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


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
class AcceptSubjectCandidate:
    """Explicit consciousness decision submitted to subject authority."""

    candidate_id: str
    candidate_revision: int
    candidate_sha256: str
    candidate_occurrence_id: str
    decision_occurrence_id: str
    actor_consciousness_instance_id: str
    expected_subject_revision: str
    target_path: SubjectDocumentPath
    accepted_content_bytes: bytes
    accepted_content_sha256: str
    occurred_at: str


@dataclass(frozen=True, slots=True)
class SubjectAuthorityCommit:
    """Content-free proof of one atomic subject-authority acceptance."""

    authority_occurrence_id: str
    candidate_id: str
    decision_occurrence_id: str
    actor_consciousness_instance_id: str
    previous_subject_revision: str
    new_subject_revision: str
    document_version_id: str
    document_revision: int
    accepted_content_sha256: str
    idempotent_replay: bool


@runtime_checkable
class SubjectAuthorityPort(Protocol):
    """Only formal acceptance boundary for unified subject-owned documents."""

    async def current_subject_revision(self) -> str:
        """Return the exact unified SOUL+USER+MEMORY source digest."""

    async def read_subject_authority(self) -> SubjectAuthoritySnapshot:
        """Read all three authority head/version pairs in one consistent snapshot."""

    async def accept_candidate(
        self,
        command: AcceptSubjectCandidate,
    ) -> SubjectAuthorityCommit:
        """Validate will evidence and atomically CAS one subject document."""


@dataclass(frozen=True, slots=True)
class SubjectAuthoritySnapshot:
    """One coherent single-transaction read of all three subject authorities."""

    commits: dict[SubjectDocumentPath, SubjectDocumentCommit]
    revision: str


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
class SubjectDocumentStorePort(SubjectAuthorityPort, Protocol):
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
    "SUBJECT_AUTHORITY_PATHS",
    "AcceptSubjectCandidate",
    "AppendSubjectDocumentVersion",
    "SubjectAuthorityActorInactive",
    "SubjectAuthorityCommit",
    "SubjectAuthorityConflict",
    "SubjectAuthorityEvidenceError",
    "SubjectAuthorityPort",
    "SubjectAuthoritySnapshot",
    "SubjectDocumentCommit",
    "SubjectDocumentConflict",
    "SubjectDocumentHead",
    "SubjectDocumentNotFound",
    "SubjectDocumentPath",
    "SubjectDocumentStorePort",
    "SubjectDocumentVersion",
    "SubjectProjectionTask",
    "subject_authority_logical_path",
    "subject_revision_from_contents",
]
