"""Content-free health for the current continuity-memory projection.

This module never decides whether a memory is important and never repairs or
rewrites subject content.  It proves that the accepted ``MEMORY.md`` bytes are
one coherent subject-authority version and that every bounded, syntactically
valid Boundary link resolves to the exact immutable artifact it names.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote

from ..storage.memory.contracts import LivingMemoryStore
from ..storage.subject_contracts import (
    SUBJECT_AUTHORITY_PATHS,
    SubjectDocumentStorePort,
    subject_revision_from_contents,
)
from .boundary import MemoryBoundaryRepository, memory_boundary_uri
from .continuity_delivery import (
    ContinuityCandidateDeliveryCoordinator,
    get_memory_continuity_delivery_coordinator,
)
from .continuity_index import (
    CONTINUITY_MEMORY_REVIEW_PRESSURE_BYTES,
    CONTINUITY_MEMORY_SOFT_TARGET_BYTES,
    diagnose_continuity_memory_index,
)

CONTINUITY_HEALTH_MAX_BOUNDARY_CHECKS = 1024
CONTINUITY_HEALTH_MAX_SOURCE_CHECKS = 4096

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BYTE_RANGE_RE = re.compile(r"^(0|[1-9][0-9]*)-(0|[1-9][0-9]*)$")


class ContinuitySourceReferenceError(RuntimeError):
    """Raised when a controlled exact source pin cannot be proven."""


@dataclass(frozen=True, slots=True)
class _SubjectSourceReference:
    logical_path: str
    version_id: str
    source_sha256: str
    byte_start: int
    byte_end: int
    range_sha256: str


def _parse_subject_source_reference(value: str) -> _SubjectSourceReference | None:
    """Parse the server-authored exact source URI without exposing its bytes."""

    if not value.startswith("subject://"):
        return None
    body = value.removeprefix("subject://")
    identity, separator, fragment = body.partition("#")
    if not separator or "@" not in identity:
        raise ContinuitySourceReferenceError("ContinuitySourceReferenceMalformed")
    encoded_path, encoded_version = identity.rsplit("@", 1)
    logical_path = unquote(encoded_path)
    version_id = unquote(encoded_version)
    if not logical_path or not version_id:
        raise ContinuitySourceReferenceError("ContinuitySourceIdentityMissing")
    try:
        fields = parse_qs(
            fragment,
            strict_parsing=True,
            keep_blank_values=True,
        )
    except ValueError as exc:
        raise ContinuitySourceReferenceError(
            "ContinuitySourceReferenceMalformed"
        ) from exc
    if set(fields) != {"sha256", "bytes", "range_sha256"} or any(
        len(items) != 1 for items in fields.values()
    ):
        raise ContinuitySourceReferenceError("ContinuitySourceReferenceSchemaMismatch")
    source_sha256 = fields["sha256"][0]
    range_sha256 = fields["range_sha256"][0]
    if (
        _SHA256_RE.fullmatch(source_sha256) is None
        or _SHA256_RE.fullmatch(range_sha256) is None
    ):
        raise ContinuitySourceReferenceError("ContinuitySourceReferenceHashInvalid")
    match = _BYTE_RANGE_RE.fullmatch(fields["bytes"][0])
    if match is None:
        raise ContinuitySourceReferenceError("ContinuitySourceReferenceRangeInvalid")
    byte_start, byte_end = (int(match.group(1)), int(match.group(2)))
    if byte_end <= byte_start:
        raise ContinuitySourceReferenceError("ContinuitySourceReferenceRangeInvalid")
    return _SubjectSourceReference(
        logical_path=logical_path,
        version_id=version_id,
        source_sha256=source_sha256,
        byte_start=byte_start,
        byte_end=byte_end,
        range_sha256=range_sha256,
    )


async def _verify_subject_source_reference(
    *,
    subject_store: SubjectDocumentStorePort,
    source_ref: str,
    segment_content_sha256: str,
    segment_byte_length: int,
) -> bool:
    """Prove one controlled source version and exact range; ignore opaque legacy refs."""

    reference = _parse_subject_source_reference(source_ref)
    if reference is None:
        return False
    version = await subject_store.get_version(reference.version_id)
    content = bytes(version.content_bytes)
    if not all(
        (
            version.version_id == reference.version_id,
            version.logical_path == reference.logical_path,
            version.content_hash == reference.source_sha256,
            version.byte_length == len(content),
            hashlib.sha256(content).hexdigest() == reference.source_sha256,
            reference.byte_end <= len(content),
        )
    ):
        raise ContinuitySourceReferenceError("ContinuitySourceVersionMismatch")
    selected = content[reference.byte_start : reference.byte_end]
    selected_sha256 = hashlib.sha256(selected).hexdigest()
    if not all(
        (
            selected_sha256 == reference.range_sha256,
            selected_sha256 == segment_content_sha256,
            len(selected) == segment_byte_length,
        )
    ):
        raise ContinuitySourceReferenceError("ContinuitySourceRangeMismatch")
    return True


def _delivery_snapshot(
    coordinator: ContinuityCandidateDeliveryCoordinator,
) -> dict[str, int]:
    snapshot = coordinator.snapshot()
    return {
        "pending_pages": snapshot.pending_pages,
        "committed_pages": snapshot.committed_pages,
        "candidate_coverages": snapshot.candidate_coverages,
        "max_pending": snapshot.max_pending,
        "max_committed_pages": snapshot.max_committed_pages,
    }


async def collect_continuity_memory_health(
    *,
    subject_store: SubjectDocumentStorePort,
    living_store: LivingMemoryStore,
    max_boundary_checks: int = CONTINUITY_HEALTH_MAX_BOUNDARY_CHECKS,
    max_source_checks: int = CONTINUITY_HEALTH_MAX_SOURCE_CHECKS,
    delivery_coordinator: ContinuityCandidateDeliveryCoordinator | None = None,
) -> dict[str, Any]:
    """Return exact, content-free health for current ``MEMORY.md`` and links."""

    bounded_checks = max(1, min(4096, int(max_boundary_checks)))
    bounded_source_checks = max(1, min(16384, int(max_source_checks)))
    coordinator = (
        delivery_coordinator or get_memory_continuity_delivery_coordinator()
    )
    base: dict[str, Any] = {
        "component": "memory_continuity",
        "owner": "active_consciousness_subject_authority",
        "automatic_importance_judgment": False,
        "automatic_deletion": False,
        "boundary_check_limit": bounded_checks,
        "source_check_limit": bounded_source_checks,
        "delivery": _delivery_snapshot(coordinator),
    }
    try:
        snapshot = await subject_store.read_subject_authority()
        if set(snapshot.commits) != set(SUBJECT_AUTHORITY_PATHS):
            raise RuntimeError("ContinuityHealthSubjectSnapshotIncomplete")
        contents = {
            path: bytes(snapshot.commits[path].version.content_bytes)
            for path in SUBJECT_AUTHORITY_PATHS
        }
        expected_revision = subject_revision_from_contents(contents)
        if snapshot.revision != expected_revision:
            raise RuntimeError("ContinuityHealthSubjectRevisionMismatch")
        memory_commit = snapshot.commits["MEMORY.md"]
        memory_version = memory_commit.version
        memory_head = memory_commit.head
        memory = contents["MEMORY.md"]
        if not all(
            (
                memory_head.current_version_id == memory_version.version_id,
                memory_version.byte_length == len(memory),
                memory_version.content_hash == hashlib.sha256(memory).hexdigest(),
            )
        ):
            raise RuntimeError("ContinuityHealthMemoryEvidenceMismatch")
        diagnostics = diagnose_continuity_memory_index(
            memory,
            subject_document_version_id=memory_version.version_id,
            unified_subject_revision=snapshot.revision,
        )
        entries = diagnostics.index.entries
        checked = entries[:bounded_checks]
        repository = MemoryBoundaryRepository(living_store)
        errors: Counter[str] = Counter()
        source_errors: Counter[str] = Counter()
        verified = 0
        verified_sources = 0
        opaque_sources = 0
        unchecked_sources = 0
        source_checks = 0
        for entry in checked:
            try:
                stored = await repository.read_exact(
                    memory_boundary_uri(
                        entry.boundary_id,
                        entry.artifact_id,
                        entry.root_sha256,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - content-free health boundary
                errors[type(exc).__name__] += 1
            else:
                verified += 1
                for segment in stored.manifest.segments:
                    for source_ref in segment.source_refs:
                        if not source_ref.startswith("subject://"):
                            opaque_sources += 1
                            continue
                        if source_checks >= bounded_source_checks:
                            unchecked_sources += 1
                            continue
                        source_checks += 1
                        try:
                            checked_source = await _verify_subject_source_reference(
                                subject_store=subject_store,
                                source_ref=source_ref,
                                segment_content_sha256=segment.content_sha256,
                                segment_byte_length=segment.byte_length,
                            )
                        except Exception as exc:  # noqa: BLE001 - content-free health
                            source_errors[type(exc).__name__] += 1
                        else:
                            verified_sources += int(checked_source)
        unchecked = len(entries) - len(checked)
        broken = sum(errors.values())
        broken_sources = sum(source_errors.values())
        syntax_issues = len(diagnostics.issues)
        status = "healthy"
        if broken or syntax_issues or unchecked or broken_sources or unchecked_sources:
            status = "degraded"
        base.update(
            {
                "status": status,
                "subject_revision": snapshot.revision,
                "memory_version_id": memory_version.version_id,
                "memory_sha256": memory_version.content_hash,
                "memory_bytes": len(memory),
                "soft_target_bytes": CONTINUITY_MEMORY_SOFT_TARGET_BYTES,
                "review_pressure_bytes": CONTINUITY_MEMORY_REVIEW_PRESSURE_BYTES,
                "soft_target_exceeded": (
                    len(memory) > CONTINUITY_MEMORY_SOFT_TARGET_BYTES
                ),
                "review_pressure_reached": (
                    len(memory) >= CONTINUITY_MEMORY_REVIEW_PRESSURE_BYTES
                ),
                "pressure_semantics": "engineering_review_only",
                "index_entry_count": len(entries),
                "syntax_issue_count": syntax_issues,
                "syntax_issues_sha256": diagnostics.issues_sha256,
                "verified_boundary_count": verified,
                "broken_boundary_count": broken,
                "unchecked_boundary_count": unchecked,
                "boundary_error_types": dict(sorted(errors.items())),
                "verified_subject_source_count": verified_sources,
                "broken_subject_source_count": broken_sources,
                "unchecked_subject_source_count": unchecked_sources,
                "opaque_legacy_source_count": opaque_sources,
                "subject_source_error_types": dict(sorted(source_errors.items())),
            }
        )
        return base
    except Exception as exc:  # noqa: BLE001 - health remains content-free
        base.update(
            {
                "status": "failed",
                "error_type": type(exc).__name__,
            }
        )
        return base


__all__ = [
    "CONTINUITY_HEALTH_MAX_BOUNDARY_CHECKS",
    "CONTINUITY_HEALTH_MAX_SOURCE_CHECKS",
    "ContinuitySourceReferenceError",
    "collect_continuity_memory_health",
]
