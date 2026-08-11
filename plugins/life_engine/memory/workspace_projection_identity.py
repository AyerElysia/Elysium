"""Content-free ownership fencing for workspace-derived memory projections.

The document index is rebuildable, but rebuilding it from two different
workspace copies into one shared backend is destructive: absence in one copy
must never be interpreted as deletion from the other. This module defines the
backend-neutral identity and transition contract that prevents that failure.

It never inspects document bodies or assigns memory meaning. A workspace
identity comes from its canonical source root and a metadata-only inventory of
eligible paths and byte sizes. Raw roots and paths stay local; persisted and
logged values are SHA-256 digests, counts, and byte totals.

Storage adapters integrate the contract by persisting one binding head plus an
append-only event ledger. Every document mutation and index outbox job must be
preceded by a successfully established
:class:`WorkspaceProjectionWritePermit`; service implementations may retain
that permit inside a projection-wide mutation boundary instead of serializing
it into rebuildable jobs. A workspace scan may upsert present documents, but
it can never authorize tombstoning documents that are merely absent. Deletion
requires a separate occurrence-bound permit.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .eligibility import (
    WorkspaceDocumentScan,
    assess_indexed_document_path,
    scan_workspace_documents,
)

WORKSPACE_PROJECTION_IDENTITY_VERSION = 1

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,190}$")


class WorkspaceProjectionError(RuntimeError):
    """Base class for workspace projection ownership failures."""


class WorkspaceProjectionIdentityError(ValueError):
    """Raised when a workspace or persisted identity is malformed."""


class WorkspaceProjectionBindingConflict(WorkspaceProjectionError):
    """Raised when a writer does not match the bound generation or owner."""


class WorkspaceProjectionRevisionConflict(WorkspaceProjectionError):
    """Raised when an ownership transition loses its compare-and-swap race."""


class WorkspaceProjectionRebuildRequired(WorkspaceProjectionBindingConflict):
    """Raised when a different source root needs a new projection generation."""


class WorkspaceProjectionBulkRetirementForbidden(WorkspaceProjectionError):
    """Raised when scan absence is about to become a destructive tombstone."""


class WorkspaceProjectionDeleteEvidenceError(WorkspaceProjectionError):
    """Raised when an explicit document deletion lacks immutable evidence."""


class WorkspaceProjectionEventKind(StrEnum):
    """Technical, non-semantic ownership transition kinds."""

    OWNER_BOUND = "owner_bound"
    OWNER_HANDOFF = "owner_handoff"
    GENERATION_REBUILT = "generation_rebuilt"
    INVENTORY_COMMITTED = "inventory_committed"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_digest(value: str, *, field_name: str, allow_empty: bool = False) -> str:
    candidate = str(value or "")
    if allow_empty and not candidate:
        return ""
    if not _DIGEST.fullmatch(candidate):
        raise WorkspaceProjectionIdentityError(
            f"{field_name} must be a lowercase SHA-256"
        )
    return candidate


def _require_identifier(value: str, *, field_name: str) -> str:
    candidate = str(value or "").strip()
    if not _IDENTIFIER.fullmatch(candidate):
        raise WorkspaceProjectionIdentityError(
            f"{field_name} must be a stable 1..191 character identifier"
        )
    return candidate


def _require_timestamp(value: str) -> str:
    candidate = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise WorkspaceProjectionIdentityError(
            "occurred_at must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise WorkspaceProjectionIdentityError("occurred_at must include a timezone")
    return candidate


@dataclass(frozen=True, slots=True)
class WorkspaceProjectionIdentity:
    """Metadata-only identity of one eligible workspace inventory.

    ``canonical_root`` is needed for local path checks, but is excluded from
    repr/equality and :meth:`safe_dict`. Cross-process comparison uses its
    digest. The inventory digest contains only canonical relative-path digests
    and byte sizes; deriving it never reads document content.
    """

    canonical_root: Path = field(repr=False, compare=False)
    canonical_root_sha256: str
    eligible_inventory_sha256: str
    source_root_sha256: str
    eligible_document_count: int
    eligible_total_bytes: int
    identity_version: int = WORKSPACE_PROJECTION_IDENTITY_VERSION

    def __post_init__(self) -> None:
        if int(self.identity_version) != WORKSPACE_PROJECTION_IDENTITY_VERSION:
            raise WorkspaceProjectionIdentityError(
                "unsupported workspace projection identity version"
            )
        if not Path(self.canonical_root).is_absolute():
            raise WorkspaceProjectionIdentityError("canonical_root must be absolute")
        _require_digest(
            self.canonical_root_sha256,
            field_name="canonical_root_sha256",
        )
        _require_digest(
            self.eligible_inventory_sha256,
            field_name="eligible_inventory_sha256",
        )
        _require_digest(self.source_root_sha256, field_name="source_root_sha256")
        if int(self.eligible_document_count) < 0:
            raise WorkspaceProjectionIdentityError(
                "eligible_document_count must not be negative"
            )
        if int(self.eligible_total_bytes) < 0:
            raise WorkspaceProjectionIdentityError(
                "eligible_total_bytes must not be negative"
            )

    def safe_dict(self) -> dict[str, Any]:
        """Return persistable/loggable identity without roots or document paths."""

        return {
            "identity_version": int(self.identity_version),
            "canonical_root_sha256": self.canonical_root_sha256,
            "eligible_inventory_sha256": self.eligible_inventory_sha256,
            "source_root_sha256": self.source_root_sha256,
            "eligible_document_count": int(self.eligible_document_count),
            "eligible_total_bytes": int(self.eligible_total_bytes),
        }


def build_workspace_projection_identity(
    workspace: str | Path,
    *,
    scan: WorkspaceDocumentScan | None = None,
) -> WorkspaceProjectionIdentity:
    """Derive one stable, content-free identity from an eligible workspace scan."""

    root = Path(workspace).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise WorkspaceProjectionIdentityError("workspace root must be a directory")
    observed = scan if scan is not None else scan_workspace_documents(root)

    inventory: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    total_bytes = 0
    for document in sorted(observed.documents, key=lambda item: item.path):
        decision = assess_indexed_document_path(document.path)
        if not decision.eligible:
            raise WorkspaceProjectionIdentityError(
                "workspace scan contains a noncanonical or ineligible document"
            )
        try:
            resolved = Path(document.absolute_path).resolve(strict=True)
            relative = resolved.relative_to(root).as_posix()
        except (OSError, ValueError) as exc:
            raise WorkspaceProjectionIdentityError(
                "workspace scan contains a document outside the canonical root"
            ) from exc
        if relative != decision.path:
            raise WorkspaceProjectionIdentityError(
                "workspace scan path does not match its canonical source"
            )
        if decision.path in seen_paths:
            raise WorkspaceProjectionIdentityError(
                "workspace scan contains a duplicate eligible path"
            )
        seen_paths.add(decision.path)
        size_bytes = int(document.size_bytes)
        if size_bytes < 0:
            raise WorkspaceProjectionIdentityError(
                "workspace document size must not be negative"
            )
        total_bytes += size_bytes
        inventory.append(
            {
                "path_sha256": _sha256(decision.path),
                "size_bytes": size_bytes,
            }
        )

    path_flavour = "windows" if os.name == "nt" else "posix"
    root_text = os.path.normcase(str(root)).replace("\\", "/")
    root_digest = _sha256(
        _canonical_json(
            {
                "identity_version": WORKSPACE_PROJECTION_IDENTITY_VERSION,
                "path_flavour": path_flavour,
                "canonical_root": root_text,
            }
        )
    )
    inventory_digest = _sha256(
        _canonical_json(
            {
                "identity_version": WORKSPACE_PROJECTION_IDENTITY_VERSION,
                "documents": inventory,
            }
        )
    )
    source_root_digest = _sha256(
        _canonical_json(
            {
                "identity_version": WORKSPACE_PROJECTION_IDENTITY_VERSION,
                "canonical_root_sha256": root_digest,
                "eligible_inventory_sha256": inventory_digest,
                "eligible_document_count": len(inventory),
                "eligible_total_bytes": total_bytes,
            }
        )
    )
    return WorkspaceProjectionIdentity(
        canonical_root=root,
        canonical_root_sha256=root_digest,
        eligible_inventory_sha256=inventory_digest,
        source_root_sha256=source_root_digest,
        eligible_document_count=len(inventory),
        eligible_total_bytes=total_bytes,
    )


@dataclass(frozen=True, slots=True)
class WorkspaceProjectionBinding:
    """Current generation-scoped owner head for one document projection."""

    storage_generation_id: str
    projection_generation_id: str
    owner_id: str
    workspace_root_sha256: str
    source_root_sha256: str
    eligible_inventory_sha256: str
    revision: int
    last_event_sha256: str
    updated_at: str

    def __post_init__(self) -> None:
        _require_identifier(
            self.storage_generation_id,
            field_name="storage_generation_id",
        )
        _require_identifier(
            self.projection_generation_id,
            field_name="projection_generation_id",
        )
        _require_identifier(self.owner_id, field_name="owner_id")
        _require_digest(self.workspace_root_sha256, field_name="workspace_root_sha256")
        _require_digest(self.source_root_sha256, field_name="source_root_sha256")
        _require_digest(
            self.eligible_inventory_sha256,
            field_name="eligible_inventory_sha256",
        )
        if int(self.revision) <= 0:
            raise WorkspaceProjectionIdentityError("binding revision must be positive")
        _require_digest(self.last_event_sha256, field_name="last_event_sha256")
        _require_timestamp(self.updated_at)

    def safe_dict(self) -> dict[str, Any]:
        return {
            "storage_generation_id": self.storage_generation_id,
            "projection_generation_id": self.projection_generation_id,
            "owner_id": self.owner_id,
            "workspace_root_sha256": self.workspace_root_sha256,
            "source_root_sha256": self.source_root_sha256,
            "eligible_inventory_sha256": self.eligible_inventory_sha256,
            "revision": int(self.revision),
            "last_event_sha256": self.last_event_sha256,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceProjectionOwnershipEvent:
    """One immutable, content-free owner/generation transition event."""

    event_kind: WorkspaceProjectionEventKind
    storage_generation_id: str
    projection_generation_id: str
    previous_projection_generation_id: str
    owner_id: str
    previous_owner_id: str
    workspace_root_sha256: str
    previous_workspace_root_sha256: str
    source_root_sha256: str
    eligible_inventory_sha256: str
    revision: int
    expected_revision: int
    actor_id: str
    audit_occurrence_id: str
    reason_code: str
    occurred_at: str
    previous_event_sha256: str = ""

    def __post_init__(self) -> None:
        _require_identifier(
            self.storage_generation_id,
            field_name="storage_generation_id",
        )
        _require_identifier(
            self.projection_generation_id,
            field_name="projection_generation_id",
        )
        if self.previous_projection_generation_id:
            _require_identifier(
                self.previous_projection_generation_id,
                field_name="previous_projection_generation_id",
            )
        _require_identifier(self.owner_id, field_name="owner_id")
        if self.previous_owner_id:
            _require_identifier(self.previous_owner_id, field_name="previous_owner_id")
        _require_identifier(self.actor_id, field_name="actor_id")
        _require_identifier(
            self.audit_occurrence_id,
            field_name="audit_occurrence_id",
        )
        _require_identifier(self.reason_code, field_name="reason_code")
        _require_digest(self.workspace_root_sha256, field_name="workspace_root_sha256")
        _require_digest(
            self.previous_workspace_root_sha256,
            field_name="previous_workspace_root_sha256",
            allow_empty=True,
        )
        _require_digest(self.source_root_sha256, field_name="source_root_sha256")
        _require_digest(
            self.eligible_inventory_sha256,
            field_name="eligible_inventory_sha256",
        )
        _require_digest(
            self.previous_event_sha256,
            field_name="previous_event_sha256",
            allow_empty=True,
        )
        if int(self.expected_revision) < 0:
            raise WorkspaceProjectionIdentityError(
                "expected_revision must not be negative"
            )
        if int(self.revision) != int(self.expected_revision) + 1:
            raise WorkspaceProjectionIdentityError(
                "event revision must immediately follow expected_revision"
            )
        _require_timestamp(self.occurred_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_kind": self.event_kind.value,
            "storage_generation_id": self.storage_generation_id,
            "projection_generation_id": self.projection_generation_id,
            "previous_projection_generation_id": (
                self.previous_projection_generation_id
            ),
            "owner_id": self.owner_id,
            "previous_owner_id": self.previous_owner_id,
            "workspace_root_sha256": self.workspace_root_sha256,
            "previous_workspace_root_sha256": (self.previous_workspace_root_sha256),
            "source_root_sha256": self.source_root_sha256,
            "eligible_inventory_sha256": self.eligible_inventory_sha256,
            "revision": int(self.revision),
            "expected_revision": int(self.expected_revision),
            "actor_id": self.actor_id,
            "audit_occurrence_id": self.audit_occurrence_id,
            "reason_code": self.reason_code,
            "occurred_at": self.occurred_at,
            "previous_event_sha256": self.previous_event_sha256,
        }

    @property
    def event_sha256(self) -> str:
        return _sha256(_canonical_json(self.to_dict()))


@dataclass(frozen=True, slots=True)
class WorkspaceProjectionTransition:
    """A head update and its immutable event, committed atomically by a store."""

    binding: WorkspaceProjectionBinding
    event: WorkspaceProjectionOwnershipEvent

    def __post_init__(self) -> None:
        expected_fields = (
            (self.binding.storage_generation_id, self.event.storage_generation_id),
            (
                self.binding.projection_generation_id,
                self.event.projection_generation_id,
            ),
            (self.binding.owner_id, self.event.owner_id),
            (self.binding.workspace_root_sha256, self.event.workspace_root_sha256),
            (self.binding.source_root_sha256, self.event.source_root_sha256),
            (
                self.binding.eligible_inventory_sha256,
                self.event.eligible_inventory_sha256,
            ),
            (self.binding.updated_at, self.event.occurred_at),
        )
        if any(
            binding_value != event_value
            for binding_value, event_value in expected_fields
        ):
            raise WorkspaceProjectionIdentityError(
                "binding head fields do not match the transition event"
            )
        if self.binding.last_event_sha256 != self.event.event_sha256:
            raise WorkspaceProjectionIdentityError(
                "binding head does not reference the transition event"
            )
        if self.binding.revision != self.event.revision:
            raise WorkspaceProjectionIdentityError(
                "binding and transition event revisions differ"
            )


def _event_to_binding(
    event: WorkspaceProjectionOwnershipEvent,
) -> WorkspaceProjectionTransition:
    binding = WorkspaceProjectionBinding(
        storage_generation_id=event.storage_generation_id,
        projection_generation_id=event.projection_generation_id,
        owner_id=event.owner_id,
        workspace_root_sha256=event.workspace_root_sha256,
        source_root_sha256=event.source_root_sha256,
        eligible_inventory_sha256=event.eligible_inventory_sha256,
        revision=event.revision,
        last_event_sha256=event.event_sha256,
        updated_at=event.occurred_at,
    )
    return WorkspaceProjectionTransition(binding=binding, event=event)


def bind_workspace_projection(
    identity: WorkspaceProjectionIdentity,
    *,
    storage_generation_id: str,
    projection_generation_id: str,
    owner_id: str,
    actor_id: str,
    audit_occurrence_id: str,
    reason_code: str,
    occurred_at: str,
) -> WorkspaceProjectionTransition:
    """Create the first binding; persistence must CAS against an absent head."""

    event = WorkspaceProjectionOwnershipEvent(
        event_kind=WorkspaceProjectionEventKind.OWNER_BOUND,
        storage_generation_id=storage_generation_id,
        projection_generation_id=projection_generation_id,
        previous_projection_generation_id="",
        owner_id=owner_id,
        previous_owner_id="",
        workspace_root_sha256=identity.canonical_root_sha256,
        previous_workspace_root_sha256="",
        source_root_sha256=identity.source_root_sha256,
        eligible_inventory_sha256=identity.eligible_inventory_sha256,
        revision=1,
        expected_revision=0,
        actor_id=actor_id,
        audit_occurrence_id=audit_occurrence_id,
        reason_code=reason_code,
        occurred_at=occurred_at,
    )
    return _event_to_binding(event)


def _require_expected_revision(
    binding: WorkspaceProjectionBinding,
    expected_revision: int,
) -> None:
    if int(expected_revision) != int(binding.revision):
        raise WorkspaceProjectionRevisionConflict(
            "workspace projection binding revision changed concurrently"
        )


def handoff_workspace_projection_owner(
    binding: WorkspaceProjectionBinding,
    identity: WorkspaceProjectionIdentity,
    *,
    expected_revision: int,
    new_owner_id: str,
    actor_id: str,
    audit_occurrence_id: str,
    reason_code: str,
    occurred_at: str,
) -> WorkspaceProjectionTransition:
    """Explicitly hand one physical workspace to another stable owner.

    A different root cannot reuse the current projection generation. It must
    use :func:`rebuild_workspace_projection_generation`, keeping stale rows and
    vectors isolated from the new rebuild.
    """

    _require_expected_revision(binding, expected_revision)
    new_owner = _require_identifier(new_owner_id, field_name="new_owner_id")
    if new_owner == binding.owner_id:
        raise WorkspaceProjectionBindingConflict("new owner already owns projection")
    if identity.canonical_root_sha256 != binding.workspace_root_sha256:
        raise WorkspaceProjectionRebuildRequired(
            "owner handoff across workspace roots requires a new projection generation"
        )
    event = WorkspaceProjectionOwnershipEvent(
        event_kind=WorkspaceProjectionEventKind.OWNER_HANDOFF,
        storage_generation_id=binding.storage_generation_id,
        projection_generation_id=binding.projection_generation_id,
        previous_projection_generation_id=binding.projection_generation_id,
        owner_id=new_owner,
        previous_owner_id=binding.owner_id,
        workspace_root_sha256=identity.canonical_root_sha256,
        previous_workspace_root_sha256=binding.workspace_root_sha256,
        source_root_sha256=identity.source_root_sha256,
        eligible_inventory_sha256=identity.eligible_inventory_sha256,
        revision=binding.revision + 1,
        expected_revision=binding.revision,
        actor_id=actor_id,
        audit_occurrence_id=audit_occurrence_id,
        reason_code=reason_code,
        occurred_at=occurred_at,
        previous_event_sha256=binding.last_event_sha256,
    )
    return _event_to_binding(event)


def rebuild_workspace_projection_generation(
    binding: WorkspaceProjectionBinding,
    identity: WorkspaceProjectionIdentity,
    *,
    expected_revision: int,
    new_projection_generation_id: str,
    new_owner_id: str,
    actor_id: str,
    audit_occurrence_id: str,
    reason_code: str,
    occurred_at: str,
) -> WorkspaceProjectionTransition:
    """Explicitly bind a fresh, physically isolated projection generation."""

    _require_expected_revision(binding, expected_revision)
    new_generation = _require_identifier(
        new_projection_generation_id,
        field_name="new_projection_generation_id",
    )
    if new_generation == binding.projection_generation_id:
        raise WorkspaceProjectionBindingConflict(
            "rebuild requires a new projection generation id"
        )
    event = WorkspaceProjectionOwnershipEvent(
        event_kind=WorkspaceProjectionEventKind.GENERATION_REBUILT,
        storage_generation_id=binding.storage_generation_id,
        projection_generation_id=new_generation,
        previous_projection_generation_id=binding.projection_generation_id,
        owner_id=new_owner_id,
        previous_owner_id=binding.owner_id,
        workspace_root_sha256=identity.canonical_root_sha256,
        previous_workspace_root_sha256=binding.workspace_root_sha256,
        source_root_sha256=identity.source_root_sha256,
        eligible_inventory_sha256=identity.eligible_inventory_sha256,
        revision=binding.revision + 1,
        expected_revision=binding.revision,
        actor_id=actor_id,
        audit_occurrence_id=audit_occurrence_id,
        reason_code=reason_code,
        occurred_at=occurred_at,
        previous_event_sha256=binding.last_event_sha256,
    )
    return _event_to_binding(event)


def _assert_binding_matches(
    binding: WorkspaceProjectionBinding,
    identity: WorkspaceProjectionIdentity,
    *,
    storage_generation_id: str,
    projection_generation_id: str,
    owner_id: str,
) -> None:
    if storage_generation_id != binding.storage_generation_id:
        raise WorkspaceProjectionBindingConflict(
            "storage generation does not own this document projection"
        )
    if projection_generation_id != binding.projection_generation_id:
        raise WorkspaceProjectionBindingConflict(
            "projection generation is not the bound generation"
        )
    if owner_id != binding.owner_id:
        raise WorkspaceProjectionBindingConflict(
            "workspace projection is bound to another owner"
        )
    if identity.canonical_root_sha256 != binding.workspace_root_sha256:
        raise WorkspaceProjectionRebuildRequired(
            "workspace root differs from the bound projection source"
        )


def commit_workspace_projection_inventory(
    binding: WorkspaceProjectionBinding,
    identity: WorkspaceProjectionIdentity,
    *,
    expected_revision: int,
    owner_id: str,
    actor_id: str,
    audit_occurrence_id: str,
    reason_code: str,
    occurred_at: str,
) -> WorkspaceProjectionTransition:
    """Audit one completed present-document reconciliation.

    The transition records the observed inventory only after all permitted
    upserts/outbox writes succeed. It does not authorize deletion by absence.
    """

    _require_expected_revision(binding, expected_revision)
    _assert_binding_matches(
        binding,
        identity,
        storage_generation_id=binding.storage_generation_id,
        projection_generation_id=binding.projection_generation_id,
        owner_id=owner_id,
    )
    event = WorkspaceProjectionOwnershipEvent(
        event_kind=WorkspaceProjectionEventKind.INVENTORY_COMMITTED,
        storage_generation_id=binding.storage_generation_id,
        projection_generation_id=binding.projection_generation_id,
        previous_projection_generation_id=binding.projection_generation_id,
        owner_id=binding.owner_id,
        previous_owner_id=binding.owner_id,
        workspace_root_sha256=binding.workspace_root_sha256,
        previous_workspace_root_sha256=binding.workspace_root_sha256,
        source_root_sha256=identity.source_root_sha256,
        eligible_inventory_sha256=identity.eligible_inventory_sha256,
        revision=binding.revision + 1,
        expected_revision=binding.revision,
        actor_id=actor_id,
        audit_occurrence_id=audit_occurrence_id,
        reason_code=reason_code,
        occurred_at=occurred_at,
        previous_event_sha256=binding.last_event_sha256,
    )
    return _event_to_binding(event)


@dataclass(frozen=True, slots=True)
class WorkspaceProjectionWritePermit:
    """Exact write/outbox fence for one scan and one binding revision."""

    storage_generation_id: str
    projection_generation_id: str
    owner_id: str
    workspace_root_sha256: str
    source_root_sha256: str
    eligible_inventory_sha256: str
    binding_revision: int
    binding_event_sha256: str
    write_fence_sha256: str

    def __post_init__(self) -> None:
        _require_identifier(
            self.storage_generation_id,
            field_name="storage_generation_id",
        )
        _require_identifier(
            self.projection_generation_id,
            field_name="projection_generation_id",
        )
        _require_identifier(self.owner_id, field_name="owner_id")
        _require_digest(self.workspace_root_sha256, field_name="workspace_root_sha256")
        _require_digest(self.source_root_sha256, field_name="source_root_sha256")
        _require_digest(
            self.eligible_inventory_sha256,
            field_name="eligible_inventory_sha256",
        )
        if int(self.binding_revision) <= 0:
            raise WorkspaceProjectionIdentityError(
                "write permit binding revision must be positive"
            )
        _require_digest(
            self.binding_event_sha256,
            field_name="binding_event_sha256",
        )
        _require_digest(self.write_fence_sha256, field_name="write_fence_sha256")

    def reject_scan_derived_retirements(
        self,
        missing_node_ids: Sequence[str],
    ) -> None:
        """Fail closed if a reconciler tries to tombstone scan absences."""

        if any(str(node_id or "").strip() for node_id in missing_node_ids):
            raise WorkspaceProjectionBulkRetirementForbidden(
                "workspace scan absence cannot authorize document tombstones"
            )

    def safe_dict(self) -> dict[str, Any]:
        return {
            "storage_generation_id": self.storage_generation_id,
            "projection_generation_id": self.projection_generation_id,
            "owner_id": self.owner_id,
            "workspace_root_sha256": self.workspace_root_sha256,
            "source_root_sha256": self.source_root_sha256,
            "eligible_inventory_sha256": self.eligible_inventory_sha256,
            "binding_revision": int(self.binding_revision),
            "binding_event_sha256": self.binding_event_sha256,
            "write_fence_sha256": self.write_fence_sha256,
            "scan_derived_retirement_allowed": False,
        }


def authorize_workspace_projection_write(
    binding: WorkspaceProjectionBinding,
    identity: WorkspaceProjectionIdentity,
    *,
    storage_generation_id: str,
    projection_generation_id: str,
    owner_id: str,
) -> WorkspaceProjectionWritePermit:
    """Authorize present-document upserts and generation-bound outbox writes."""

    _assert_binding_matches(
        binding,
        identity,
        storage_generation_id=storage_generation_id,
        projection_generation_id=projection_generation_id,
        owner_id=owner_id,
    )
    fence = _sha256(
        _canonical_json(
            {
                "storage_generation_id": binding.storage_generation_id,
                "projection_generation_id": binding.projection_generation_id,
                "owner_id": binding.owner_id,
                "workspace_root_sha256": binding.workspace_root_sha256,
                "source_root_sha256": identity.source_root_sha256,
                "eligible_inventory_sha256": identity.eligible_inventory_sha256,
                "binding_revision": binding.revision,
                "binding_event_sha256": binding.last_event_sha256,
            }
        )
    )
    return WorkspaceProjectionWritePermit(
        storage_generation_id=binding.storage_generation_id,
        projection_generation_id=binding.projection_generation_id,
        owner_id=binding.owner_id,
        workspace_root_sha256=binding.workspace_root_sha256,
        source_root_sha256=identity.source_root_sha256,
        eligible_inventory_sha256=identity.eligible_inventory_sha256,
        binding_revision=binding.revision,
        binding_event_sha256=binding.last_event_sha256,
        write_fence_sha256=fence,
    )


@dataclass(frozen=True, slots=True)
class ExplicitDocumentDeletionPermit:
    """Occurrence-bound evidence for deleting one exact indexed revision."""

    file_path: str = field(repr=False)
    file_path_sha256: str
    expected_content_hash: str
    expected_index_revision: int
    source_occurrence_id: str
    actor_id: str
    reason_code: str
    projection_write_fence_sha256: str
    permit_sha256: str

    def safe_dict(self) -> dict[str, Any]:
        return {
            "file_path_sha256": self.file_path_sha256,
            "expected_content_hash": self.expected_content_hash,
            "expected_index_revision": int(self.expected_index_revision),
            "source_occurrence_id": self.source_occurrence_id,
            "actor_id": self.actor_id,
            "reason_code": self.reason_code,
            "projection_write_fence_sha256": self.projection_write_fence_sha256,
            "permit_sha256": self.permit_sha256,
        }


def authorize_explicit_document_deletion(
    permit: WorkspaceProjectionWritePermit,
    *,
    file_path: str,
    expected_content_hash: str,
    expected_index_revision: int,
    source_occurrence_id: str,
    actor_id: str,
    reason_code: str,
) -> ExplicitDocumentDeletionPermit:
    """Authorize one deletion proven by a real mutation occurrence, not absence."""

    decision = assess_indexed_document_path(file_path)
    if not decision.eligible:
        raise WorkspaceProjectionDeleteEvidenceError(
            "explicit deletion path is not canonical and eligible"
        )
    try:
        content_hash = _require_digest(
            expected_content_hash,
            field_name="expected_content_hash",
        )
        revision = int(expected_index_revision)
        if revision <= 0:
            raise WorkspaceProjectionIdentityError(
                "explicit deletion requires a positive expected index revision"
            )
        occurrence = _require_identifier(
            source_occurrence_id,
            field_name="source_occurrence_id",
        )
        actor = _require_identifier(actor_id, field_name="actor_id")
        reason = _require_identifier(reason_code, field_name="reason_code")
    except WorkspaceProjectionIdentityError as exc:
        raise WorkspaceProjectionDeleteEvidenceError(str(exc)) from exc

    body = {
        "file_path_sha256": _sha256(decision.path),
        "expected_content_hash": content_hash,
        "expected_index_revision": revision,
        "source_occurrence_id": occurrence,
        "actor_id": actor,
        "reason_code": reason,
        "projection_write_fence_sha256": permit.write_fence_sha256,
    }
    return ExplicitDocumentDeletionPermit(
        file_path=decision.path,
        permit_sha256=_sha256(_canonical_json(body)),
        **body,
    )


@runtime_checkable
class WorkspaceProjectionBindingStore(Protocol):
    """Durable CAS/head plus append-only event contract for storage adapters.

    ``commit_transition`` must lock the generation head, verify
    ``transition.event.expected_revision`` (zero means absent), verify
    ``previous_event_sha256`` against the locked head, append the event, and
    update the head in one transaction. Event UPDATE/DELETE must be blocked by
    database constraints. A rebuild generation must use independent index rows,
    outbox jobs, tombstones, and vector collection names.
    """

    async def load_binding(
        self,
        storage_generation_id: str,
    ) -> WorkspaceProjectionBinding | None: ...

    async def commit_transition(
        self,
        transition: WorkspaceProjectionTransition,
    ) -> WorkspaceProjectionBinding: ...

    async def list_events(
        self,
        storage_generation_id: str,
        *,
        after_revision: int = 0,
        limit: int = 100,
    ) -> Sequence[WorkspaceProjectionOwnershipEvent]: ...


__all__ = [
    "WORKSPACE_PROJECTION_IDENTITY_VERSION",
    "ExplicitDocumentDeletionPermit",
    "WorkspaceProjectionBinding",
    "WorkspaceProjectionBindingConflict",
    "WorkspaceProjectionBindingStore",
    "WorkspaceProjectionBulkRetirementForbidden",
    "WorkspaceProjectionDeleteEvidenceError",
    "WorkspaceProjectionError",
    "WorkspaceProjectionEventKind",
    "WorkspaceProjectionIdentity",
    "WorkspaceProjectionIdentityError",
    "WorkspaceProjectionOwnershipEvent",
    "WorkspaceProjectionRebuildRequired",
    "WorkspaceProjectionRevisionConflict",
    "WorkspaceProjectionTransition",
    "WorkspaceProjectionWritePermit",
    "authorize_explicit_document_deletion",
    "authorize_workspace_projection_write",
    "bind_workspace_projection",
    "build_workspace_projection_identity",
    "commit_workspace_projection_inventory",
    "handoff_workspace_projection_owner",
    "rebuild_workspace_projection_generation",
]
