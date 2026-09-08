"""Backend-neutral contracts for exact-byte subject document history."""

from __future__ import annotations

import hashlib
from contextlib import AbstractAsyncContextManager
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
    binding_revision: int = 0
    deleted: bool = False


@dataclass(frozen=True, slots=True)
class SubjectDocumentPathBinding:
    """Current path generation; release retains its monotonic revision."""

    logical_path: str
    document_id: str | None
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
    expected_document_id: str | None = None
    expected_binding_revision: int | None = None


@dataclass(frozen=True, slots=True)
class SubjectDocumentCommit:
    """Atomic result containing the immutable version and new head."""

    version: SubjectDocumentVersion
    head: SubjectDocumentHead


@dataclass(frozen=True, slots=True)
class SubjectDocumentMutation:
    """Explicit identity- and path-fenced lifecycle command."""

    operation: Literal["rename", "delete", "copy"]
    logical_path: str
    expected_document_id: str
    expected_revision: int
    expected_head_version_id: str
    expected_binding_revision: int
    occurrence_id: str
    recorded_by: str
    recorded_source: str
    target_logical_path: str = ""
    expected_target_binding_revision: int = 0
    semantic_actor_id: str | None = None
    semantic_source_id: str | None = None
    occurred_at: str | None = None
    change_context: dict[str, Any] | None = None
    content_bytes: bytes | None = None
    encoding: str | None = None
    newline_style: str | None = None


@dataclass(frozen=True, slots=True)
class SubjectDocumentMutationCommit:
    operation: str
    occurrence_id: str
    document_id: str
    head: SubjectDocumentHead
    version: SubjectDocumentVersion
    idempotent_replay: bool = False


@dataclass(frozen=True, slots=True)
class SubjectDocumentOperation:
    """Immutable receipt independent of subsequent path/head changes."""

    occurrence_id: str
    operation: str
    document_id: str
    command_digest: str
    result: dict[str, Any]
    change_context: dict[str, Any]
    recorded_at: str


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

    async def current_subject_change_marker(self) -> str:
        """Return a content-free marker for the three current head pointers."""

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
    change_marker: str = ""


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
    operation: str = "write"
    previous_logical_path: str = ""
    binding_revision: int = 0
    previous_binding_revision: int = 0
    previous_version_id: str = ""
    previous_content_hash: str = ""


@runtime_checkable
class SubjectDocumentStorePort(SubjectAuthorityPort, Protocol):
    """Append-only subject history with a revision-CAS head projection."""

    def workspace_projection_fence(self) -> AbstractAsyncContextManager[None]:
        """LOCAL-only fence against document writes during filesystem projection.

        Read methods remain available. Outbox confirmation/failure and every
        other write must happen after leaving this context.
        """

    def workspace_namespace_fence(self) -> AbstractAsyncContextManager[None]:
        """Serialize LOCAL/MySQL path inspection and authorized publication.

        This does not enable a MySQL workspace projector or authorize file
        writes. Callers own the filesystem action and confirm outside the fence.
        """

    async def get_head(self, logical_path: str) -> SubjectDocumentHead | None:
        """Read one head without creating a document."""

    async def get_path_binding(
        self, logical_path: str,
    ) -> SubjectDocumentPathBinding | None:
        """Read a bound or released path generation."""

    async def list_file_bindings(
        self, *, logical_path_prefix: str = "", after_logical_path: str = "",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Page current and released paths with metadata only, never blobs."""

    async def get_document_head(
        self, document_id: str,
    ) -> SubjectDocumentHead | None:
        """Read a stable identity, including its deletion tombstone."""

    async def get_document_projection_frontier(
        self, *, logical_path_prefix: str = "",
    ) -> int:
        """Read the latest immutable outbox identity, regardless of projection state."""

    async def list_document_projection_changes(
        self, *, after_outbox_id: int, through_outbox_id: int,
        logical_path_prefix: str = "", limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Page changed document identities within a fixed, metadata-only frontier."""

    async def get_document_operation(
        self, occurrence_id: str,
    ) -> SubjectDocumentOperation | None:
        """Read committed receipt before resolving current head on retries."""

    async def list_document_operations(
        self, document_id: str, *, after_recorded_at: str = "",
        after_occurrence_id: str = "", limit: int = 100,
    ) -> list[SubjectDocumentOperation]:
        """Read operation history, including rename/delete lifecycle facts."""

    async def list_document_history(
        self, document_id: str, *, after_recorded_at: str = "",
        after_version_id: str = "", limit: int = 100,
    ) -> list[SubjectDocumentVersion]:
        """Read immutable versions across path changes and deletion."""

    async def list_document_version_descriptors(
        self, document_id: str, *, after_recorded_at: str = "",
        after_version_id: str = "", limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read bounded version metadata without loading any content blobs."""

    async def mutate_document(
        self, command: SubjectDocumentMutation,
    ) -> SubjectDocumentMutationCommit:
        """Atomically append a rename, tombstone, or exact-byte copy."""

    async def apply_document_batch(
        self, commands: list[AppendSubjectDocumentVersion | SubjectDocumentMutation],
    ) -> list[SubjectDocumentCommit | SubjectDocumentMutationCommit]:
        """Commit non-overlapping file commands in a single fenced transaction."""

    async def get_version(self, version_id: str) -> SubjectDocumentVersion:
        """Read one immutable version including exact content bytes."""

    async def get_version_descriptor(self, version_id: str) -> dict[str, Any]:
        """Read exact-version metadata without selecting its content blob."""

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
        *, occurrence_id: str | None = None,
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

    async def retry_projection(
        self,
        task: SubjectProjectionTask,
        *,
        worker_id: str,
    ) -> SubjectProjectionTask:
        """Requeue one exact failed projection without changing subject history."""

    async def heal_projection(
        self,
        task: SubjectProjectionTask,
        *,
        worker_id: str,
    ) -> None:
        """Rebuild a legacy pending/failed projection outbox row as confirmed.

        MySQL-only self-healing for reconstructible projection state; the
        authoritative version bytes are already persisted. LOCAL backend keeps
        ``failed`` as a legitimate terminal state and never calls this.
        """

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
    "SubjectDocumentMutation",
    "SubjectDocumentMutationCommit",
    "SubjectDocumentNotFound",
    "SubjectDocumentOperation",
    "SubjectDocumentPath",
    "SubjectDocumentPathBinding",
    "SubjectDocumentStorePort",
    "SubjectDocumentVersion",
    "SubjectProjectionTask",
    "subject_authority_logical_path",
    "subject_revision_from_contents",
]
