"""Subject-gated proposals for the bounded ``MEMORY.md`` continuity index.

This module validates a complete candidate against exact immutable boundary
artifacts, then appends it to the existing Learning decision ledger. It never
writes ``MEMORY.md`` and never accepts its own candidate.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from ..learning.decisions import (
    LearningCandidate,
    LearningDecisionReceipt,
)
from .boundary import MemoryBoundaryRepository, memory_boundary_uri
from .continuity_index import (
    ContinuityMemoryIndex,
    ContinuityMemoryLifecycleDiff,
    diagnose_continuity_memory_index,
    diff_continuity_memory_indexes,
    parse_continuity_memory_index,
)

CONTINUITY_MEMORY_CANDIDATE_MAX_BYTES = 240 * 1024
CONTINUITY_MEMORY_STEWARDSHIP_VERSION = "continuity-memory-stewardship-v1"


class ContinuityMemoryCandidateError(RuntimeError):
    """Base class for a continuity-memory proposal that must fail closed."""


class ContinuityMemoryCurrentVersionConflict(ContinuityMemoryCandidateError):
    """Raised when the caller did not review the exact current MEMORY bytes."""


class ContinuityMemoryCandidateTooLarge(ContinuityMemoryCandidateError):
    """Raised instead of truncating a complete subject-document candidate."""


class ContinuityMemoryCandidateLedger(Protocol):
    """Minimal candidate-only surface of the existing Learning ledger."""

    async def append_candidate(
        self,
        candidate: LearningCandidate,
    ) -> LearningDecisionReceipt: ...


@dataclass(frozen=True, slots=True)
class ContinuityMemoryCandidateProposal:
    """A traceable open candidate plus its derived technical lifecycle diff."""

    candidate: LearningCandidate
    receipt: LearningDecisionReceipt
    current_index: ContinuityMemoryIndex
    proposed_index: ContinuityMemoryIndex
    lifecycle: ContinuityMemoryLifecycleDiff
    verified_boundary_count: int
    verified_boundary_refs_sha256: str
    current_index_issue_count: int
    current_index_issues_sha256: str


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _refs_sha256(index: ContinuityMemoryIndex) -> str:
    payload = [
        {
            "entry_id": item.entry_id,
            "artifact_id": item.artifact_version_id,
            "root_sha256": item.boundary_root_sha256,
        }
        for item in index.entries
    ]
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class ContinuityMemoryStewardship:
    """Validate exact boundary links and append a subject-owned open candidate."""

    def __init__(
        self,
        repository: MemoryBoundaryRepository,
        ledger: ContinuityMemoryCandidateLedger,
    ) -> None:
        self._repository = repository
        self._ledger = ledger

    async def propose(
        self,
        *,
        current_memory_bytes: bytes,
        current_memory_version_id: str,
        reviewed_current_memory_sha256: str,
        proposed_memory_bytes: bytes,
        unified_subject_revision: str,
        actor_consciousness_instance_id: str,
        source_occurrence_id: str,
        proposal_occurrence_id: str,
        reason: str,
        stream_scope: str,
    ) -> ContinuityMemoryCandidateProposal:
        """Append a validated candidate without modifying subject authority."""

        current = bytes(current_memory_bytes)
        proposed = bytes(proposed_memory_bytes)
        reviewed_hash = str(reviewed_current_memory_sha256 or "").strip().lower()
        current_hash = _sha256(current)
        if reviewed_hash != current_hash:
            raise ContinuityMemoryCurrentVersionConflict(
                "ContinuityMemoryCurrentVersionConflict"
            )
        if not proposed:
            raise ContinuityMemoryCandidateError(
                "ContinuityMemoryCandidateMustNotBeEmpty"
            )
        if len(proposed) > CONTINUITY_MEMORY_CANDIDATE_MAX_BYTES:
            raise ContinuityMemoryCandidateTooLarge(
                "ContinuityMemoryCandidateTooLarge:"
                f"bytes={len(proposed)}:"
                f"max_bytes={CONTINUITY_MEMORY_CANDIDATE_MAX_BYTES}"
            )
        actor = str(actor_consciousness_instance_id or "").strip()
        source_occurrence = str(source_occurrence_id or "").strip()
        proposal_occurrence = str(proposal_occurrence_id or "").strip()
        review_reason = str(reason or "").strip()
        if not actor or not source_occurrence or not proposal_occurrence:
            raise ContinuityMemoryCandidateError(
                "ContinuityMemoryCandidateIdentityRequired"
            )
        if not review_reason:
            raise ContinuityMemoryCandidateError(
                "ContinuityMemoryCandidateReasonRequired"
            )

        current_diagnostics = diagnose_continuity_memory_index(
            current,
            subject_document_version_id=current_memory_version_id,
            unified_subject_revision=unified_subject_revision,
        )
        current_index = current_diagnostics.index
        proposed_hash = _sha256(proposed)
        candidate_id = (
            "memory_continuity_"
            + hashlib.sha256(
                (
                    proposal_occurrence
                    + "\0"
                    + unified_subject_revision
                    + "\0"
                    + proposed_hash
                ).encode("utf-8")
            ).hexdigest()
        )
        proposed_index = parse_continuity_memory_index(
            proposed,
            subject_document_version_id=f"candidate:{candidate_id}:1",
            unified_subject_revision=unified_subject_revision,
        )
        for entry in proposed_index.entries:
            await self._repository.read_exact(
                memory_boundary_uri(
                    entry.boundary_id,
                    entry.artifact_id,
                    entry.root_sha256,
                )
            )

        lifecycle = diff_continuity_memory_indexes(
            current_index,
            proposed_index,
        )
        refs_sha256 = _refs_sha256(proposed_index)
        candidate = LearningCandidate.create(
            candidate_id=candidate_id,
            candidate_revision=1,
            candidate_occurrence_id=f"{proposal_occurrence}:candidate",
            candidate_kind="memory_continuity_document_revision",
            candidate_content_bytes=proposed,
            source_occurrence_id=source_occurrence,
            source="memory.continuity.active_consciousness",
            actor_consciousness_instance_id=actor,
            subject_revision=unified_subject_revision,
            target_path="MEMORY.md",
            provenance={
                "authority": "candidate_only",
                "stewardship_version": CONTINUITY_MEMORY_STEWARDSHIP_VERSION,
                "stream_scope": stream_scope,
                "review_reason": review_reason,
                "review_reason_sha256": _sha256(review_reason.encode("utf-8")),
                "reviewed_memory_sha256": current_hash,
                "reviewed_memory_version_id": current_memory_version_id,
                "reviewed_memory_bytes": len(current),
                "candidate_memory_bytes": len(proposed),
                "boundary_entry_count": len(proposed_index.entries),
                "boundary_refs_sha256": refs_sha256,
                "current_index_issue_count": len(current_diagnostics.issues),
                "current_index_issues_sha256": (current_diagnostics.issues_sha256),
                "repairs_malformed_current_index": bool(current_diagnostics.issues),
                "malformed_current_entries_are_not_classified_as_deactivated": True,
                "activated_entry_ids": list(lifecycle.activated),
                "deactivated_entry_ids": list(lifecycle.deactivated),
                "rewritten_entry_ids": list(lifecycle.rewritten),
                "retargeted_entry_ids": [
                    item.entry_id for item in lifecycle.retargeted
                ],
                "deactivation_deletes_boundary": False,
                "automatic_importance_judgment": False,
            },
        )
        receipt = await self._ledger.append_candidate(candidate)
        return ContinuityMemoryCandidateProposal(
            candidate=candidate,
            receipt=receipt,
            current_index=current_index,
            proposed_index=proposed_index,
            lifecycle=lifecycle,
            verified_boundary_count=len(proposed_index.entries),
            verified_boundary_refs_sha256=refs_sha256,
            current_index_issue_count=len(current_diagnostics.issues),
            current_index_issues_sha256=current_diagnostics.issues_sha256,
        )


__all__ = [
    "CONTINUITY_MEMORY_CANDIDATE_MAX_BYTES",
    "CONTINUITY_MEMORY_STEWARDSHIP_VERSION",
    "ContinuityMemoryCandidateError",
    "ContinuityMemoryCandidateLedger",
    "ContinuityMemoryCandidateProposal",
    "ContinuityMemoryCandidateTooLarge",
    "ContinuityMemoryCurrentVersionConflict",
    "ContinuityMemoryStewardship",
]
