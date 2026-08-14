"""Subject-gated, recoverable continuity-review sessions.

The session is deliberately stateless.  Immutable Memory Boundaries and the
existing Learning candidate ledger are the only durable records.  This module
never writes subject authority and never manufactures subject prose.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol, cast
from urllib.parse import quote

from ..learning.decisions import (
    LearningCandidate,
    LearningDecision,
    LearningDecisionKind,
    LearningDecisionReceipt,
    SubjectAuthorityUnavailable,
)
from ..storage.subject_contracts import (
    SUBJECT_AUTHORITY_PATHS,
    SubjectAuthorityPort,
    SubjectAuthoritySnapshot,
    SubjectDocumentHead,
    SubjectDocumentNotFound,
    SubjectDocumentVersion,
    subject_revision_from_contents,
)
from .boundary import (
    MemoryBoundaryManifest,
    MemoryBoundarySegment,
    StoredMemoryBoundary,
)
from .continuity_stewardship import (
    ContinuityMemoryCandidateProposal,
    ContinuityMemoryStewardship,
)

CONTINUITY_REVIEW_SESSION_VERSION = "continuity-review-session-v1"
CONTINUITY_REVIEW_MAX_PAGE_BYTES = 32 * 1024
CONTINUITY_REVIEW_MAX_BOUNDARIES = 16
CONTINUITY_REVIEW_MAX_TEXT_EDITS = 32
CONTINUITY_REVIEW_MAX_TEXT_EDIT_BYTES = 8 * 1024
CONTINUITY_REVIEW_MAX_TEXT_REPLACEMENT_BYTES = 32 * 1024

_AUXILIARY_SUBJECT_PREFIXES = (
    "life_engine_workspace/diaries/witness/",
    "diaries/witness/",
    "life_engine_workspace/diaries/",
    "diaries/",
    "life_engine_workspace/notes/",
    "notes/",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SESSION_RE = re.compile(r"^continuity-review-[0-9a-f]{32}$")
_CANDIDATE_OCCURRENCE_RE = re.compile(
    r"^continuity-review:(?P<session>[0-9a-f]{32}):"
    r"prepare:(?P<action>[0-9a-f]{16}):candidate$"
)


class ContinuityReviewError(RuntimeError):
    """Base fail-closed continuity-review error."""


class ContinuityReviewRuntimeUnavailable(ContinuityReviewError):
    """Raised when no public dependency bundle is available."""


class ContinuityReviewActorInactive(ContinuityReviewError):
    """Raised when the semantic actor is not an active consciousness instance."""


class ContinuityReviewInputError(ContinuityReviewError):
    """Raised for an unsafe or structurally ambiguous request."""


class ContinuityReviewStale(ContinuityReviewError):
    """Raised when the reviewed subject snapshot is no longer current."""


class ContinuityReviewCandidateNotFound(ContinuityReviewError):
    """Raised when immutable candidate evidence cannot be recovered."""


class ContinuityReviewAuxiliarySourceNotFound(ContinuityReviewError):
    """Raised when an allowed auxiliary Subject version cannot be recovered."""


class ContinuityReviewSessionMismatch(ContinuityReviewError):
    """Raised when a candidate or source pin belongs to another session."""


class ContinuityReviewIndependentDecisionRequired(ContinuityReviewError):
    """Raised when preparation and decision reuse the same action occurrence."""


class ContinuityReviewDecisionPersistenceUnverified(ContinuityReviewError):
    """Raised when the existing ledger cannot prove the requested decision."""


class ContinuityReviewDeliveryProofUnavailable(ContinuityReviewError):
    """Raised when acceptance has no trusted exact-delivery verifier."""


class ContinuityReviewDeliveryUnverified(ContinuityReviewError):
    """Raised when candidate delivery cannot be proven exact and complete."""


class MemoryBoundarySessionPort(Protocol):
    """Existing immutable Boundary repository surface consumed by a session."""

    async def append(
        self,
        manifest: MemoryBoundaryManifest,
        *,
        expected_head_revision: int,
    ) -> StoredMemoryBoundary: ...

    async def read_exact(self, uri: str) -> StoredMemoryBoundary: ...


class AuxiliarySubjectDocumentReadPort(Protocol):
    """Existing cross-backend exact-byte reads used for auxiliary sources.

    Both local and MySQL ``SubjectDocumentStorePort`` implementations already
    expose this surface.  The continuity session declares only the minimum it
    consumes and never reaches into either adapter or its raw database session.
    """

    async def get_head(self, logical_path: str) -> SubjectDocumentHead | None: ...

    async def get_version(self, version_id: str) -> SubjectDocumentVersion: ...


class ContinuityReviewCandidateLedger(Protocol):
    """Existing Learning ledger surface; no session-owned persistence."""

    async def append_candidate(
        self,
        candidate: LearningCandidate,
    ) -> LearningDecisionReceipt: ...

    async def record_decision(
        self,
        decision: LearningDecision,
    ) -> LearningDecisionReceipt: ...

    async def accept_subject_candidate(
        self,
        decision: LearningDecision,
    ) -> LearningDecisionReceipt: ...

    async def read_candidate(self, candidate_id: str) -> LearningCandidate | None: ...

    async def list_candidates(
        self,
        *,
        status: str = "open",
        limit: int = 20,
    ) -> list[dict[str, object]]: ...


class _TraceableCandidateLedger:
    """Bind one stewardship append to stable action identity and edit evidence."""

    def __init__(
        self,
        ledger: ContinuityReviewCandidateLedger,
        *,
        occurred_at: str,
        provenance: Mapping[str, object],
    ) -> None:
        self._ledger = ledger
        self._occurred_at = occurred_at
        self._provenance = dict(provenance)
        self.persisted_candidate: LearningCandidate | None = None

    async def append_candidate(
        self,
        candidate: LearningCandidate,
    ) -> LearningDecisionReceipt:
        persisted = replace(
            candidate,
            occurred_at=self._occurred_at,
            provenance={**candidate.provenance, **self._provenance},
        )
        self.persisted_candidate = persisted
        return await self._ledger.append_candidate(persisted)


@dataclass(frozen=True, slots=True)
class CandidateDeliveryReceipt:
    """Content-free claim that a trusted layer must verify independently."""

    delivery_id: str
    candidate_id: str
    candidate_revision: int
    candidate_sha256: str
    delivered_bytes: int
    total_bytes: int

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> CandidateDeliveryReceipt:
        expected = {
            "delivery_id",
            "candidate_id",
            "candidate_revision",
            "candidate_sha256",
            "delivered_bytes",
            "total_bytes",
        }
        _require_exact_keys(payload, expected, "delivery_receipt")
        receipt = cls(
            delivery_id=_identity(payload["delivery_id"], "delivery_id", 512),
            candidate_id=_identity(payload["candidate_id"], "candidate_id", 512),
            candidate_revision=_positive_int(
                payload["candidate_revision"], "candidate_revision"
            ),
            candidate_sha256=_sha256_text(
                payload["candidate_sha256"], "candidate_sha256"
            ),
            delivered_bytes=_nonnegative_int(
                payload["delivered_bytes"], "delivered_bytes"
            ),
            total_bytes=_nonnegative_int(payload["total_bytes"], "total_bytes"),
        )
        if receipt.delivered_bytes != receipt.total_bytes:
            raise ContinuityReviewDeliveryUnverified(
                "ContinuityReviewCandidateDeliveryIncomplete"
            )
        return receipt


class ExactCandidateDeliveryVerifier(Protocol):
    """Trusted receipt ledger owned by the eventual delivery integration."""

    async def verify_exact_candidate_delivery(
        self,
        receipt: CandidateDeliveryReceipt,
        candidate: LearningCandidate,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class ContinuityReviewOutcome:
    """Replayable subject-review outcome offered to a derived scheduler view."""

    outcome_occurrence_id: str
    outcome_kind: str
    target_path: str
    candidate_occurrence_id: str
    candidate_id: str
    candidate_revision: int
    candidate_sha256: str
    subject_revision_before: str
    subject_revision_after: str
    reason: str
    actor_consciousness_instance_id: str
    source_occurrence_id: str
    action_occurrence_id: str
    occurred_at: str
    decision_occurrence_id: str = ""
    authority_occurrence_id: str = ""
    snooze_hours: int = 0


class ContinuityReviewOutcomeRecorder(Protocol):
    """Idempotent projection callback keyed by ``outcome_occurrence_id``."""

    async def __call__(self, outcome: ContinuityReviewOutcome) -> None: ...


@dataclass(frozen=True, slots=True)
class ContinuityReviewOutcomeRecording:
    """Content-free result that never obscures the durable domain outcome."""

    status: str
    outcome_kind: str
    outcome_occurrence_id: str
    error_type: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "outcome_kind": self.outcome_kind,
            "outcome_occurrence_id": self.outcome_occurrence_id,
            "error_type": self.error_type,
            "durable_domain_outcome_preserved": True,
            "retry_reuses_outcome_occurrence": True,
        }


ActiveActorValidator = Callable[[str], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class ContinuityReviewActorContext:
    """Trusted runtime identity; never accepted from model tool arguments."""

    consciousness_instance_id: str
    stream_scope: str
    source_occurrence_id: str
    action_occurrence_id: str
    occurred_at: str

    def __post_init__(self) -> None:
        _identity(
            self.consciousness_instance_id,
            "consciousness_instance_id",
            255,
        )
        _identity(self.stream_scope, "stream_scope", 512)
        _identity(self.source_occurrence_id, "source_occurrence_id", 512)
        _identity(self.action_occurrence_id, "action_occurrence_id", 512)
        try:
            parsed = datetime.fromisoformat(self.occurred_at)
        except (TypeError, ValueError) as exc:
            raise ContinuityReviewInputError("occurred_at must be ISO-8601") from exc
        if parsed.tzinfo is None:
            raise ContinuityReviewInputError("occurred_at must include timezone")


@dataclass(frozen=True, slots=True)
class ReviewedMemorySegmentPlan:
    """Subject-selected exact source range; content is always server-derived."""

    segment_id: str
    title: str
    byte_start: int
    byte_end: int
    scope: str
    visibility: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ReviewedMemorySegmentPlan:
        expected = {
            "segment_id",
            "title",
            "byte_start",
            "byte_end",
            "scope",
            "visibility",
        }
        _require_exact_keys(payload, expected, "segment")
        start = _nonnegative_int(payload["byte_start"], "segment.byte_start")
        end = _positive_int(payload["byte_end"], "segment.byte_end")
        if end <= start:
            raise ContinuityReviewInputError("segment byte range must be non-empty")
        return cls(
            segment_id=_identity(payload["segment_id"], "segment.segment_id", 128),
            title=_prose(payload["title"], "segment.title"),
            byte_start=start,
            byte_end=end,
            scope=_prose(payload["scope"], "segment.scope"),
            visibility=_identity(payload["visibility"], "segment.visibility", 128),
        )


@dataclass(frozen=True, slots=True)
class AuxiliarySubjectSegmentPlan:
    """One exact range from a pinned immutable auxiliary Subject document.

    The caller may select only identity pins and byte ranges.  Segment content,
    source references and occurrence identities are always recovered from the
    Subject Document Store by the server.
    """

    segment_id: str
    title: str
    logical_path: str
    version_id: str
    content_hash: str
    byte_start: int
    byte_end: int
    scope: str
    visibility: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> AuxiliarySubjectSegmentPlan:
        expected = {
            "segment_id",
            "title",
            "logical_path",
            "version_id",
            "content_hash",
            "byte_start",
            "byte_end",
            "scope",
            "visibility",
        }
        _require_exact_keys(payload, expected, "auxiliary_subject_segment")
        start = _nonnegative_int(payload["byte_start"], "segment.byte_start")
        end = _positive_int(payload["byte_end"], "segment.byte_end")
        if end <= start:
            raise ContinuityReviewInputError("segment byte range must be non-empty")
        return cls(
            segment_id=_identity(payload["segment_id"], "segment.segment_id", 128),
            title=_prose(payload["title"], "segment.title"),
            logical_path=_auxiliary_subject_path(payload["logical_path"]),
            version_id=_identity(payload["version_id"], "segment.version_id", 512),
            content_hash=_sha256_text(payload["content_hash"], "segment.content_hash"),
            byte_start=start,
            byte_end=end,
            scope=_prose(payload["scope"], "segment.scope"),
            visibility=_identity(payload["visibility"], "segment.visibility", 128),
        )


@dataclass(frozen=True, slots=True)
class ContinuityBoundaryPlan:
    """Subject-authored Boundary semantics plus exact mechanical ranges."""

    boundary_id: str
    title: str
    scope: str
    current_meaning: str
    non_generalization: str
    expected_head_revision: int
    visibility: str
    segments: tuple[ReviewedMemorySegmentPlan | AuxiliarySubjectSegmentPlan, ...]

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ContinuityBoundaryPlan:
        expected = {
            "boundary_id",
            "title",
            "scope",
            "current_meaning",
            "non_generalization",
            "expected_head_revision",
            "visibility",
            "segments",
        }
        _require_exact_keys(payload, expected, "boundary")
        raw_segments = payload["segments"]
        if not isinstance(raw_segments, list) or not raw_segments:
            raise ContinuityReviewInputError(
                "boundary segments must be a non-empty list"
            )
        if len(raw_segments) > CONTINUITY_REVIEW_MAX_BOUNDARIES:
            raise ContinuityReviewInputError("boundary has too many segments")
        segments: tuple[ReviewedMemorySegmentPlan | AuxiliarySubjectSegmentPlan, ...]
        parsed_segments: list[
            ReviewedMemorySegmentPlan | AuxiliarySubjectSegmentPlan
        ] = []
        memory_fields = {
            "segment_id",
            "title",
            "byte_start",
            "byte_end",
            "scope",
            "visibility",
        }
        auxiliary_fields = memory_fields | {
            "logical_path",
            "version_id",
            "content_hash",
        }
        for item in raw_segments:
            segment_payload = _mapping(item, "segment")
            if set(segment_payload) == memory_fields:
                parsed_segments.append(
                    ReviewedMemorySegmentPlan.from_payload(segment_payload)
                )
            elif set(segment_payload) == auxiliary_fields:
                parsed_segments.append(
                    AuxiliarySubjectSegmentPlan.from_payload(segment_payload)
                )
            else:
                expected_fields = (
                    auxiliary_fields
                    if set(segment_payload)
                    & {"logical_path", "version_id", "content_hash"}
                    else memory_fields
                )
                _require_exact_keys(segment_payload, expected_fields, "segment")
        segments = tuple(parsed_segments)
        if any(
            isinstance(item, AuxiliarySubjectSegmentPlan) for item in segments
        ) and any(isinstance(item, ReviewedMemorySegmentPlan) for item in segments):
            raise ContinuityReviewInputError(
                "one Boundary must not mix MEMORY and auxiliary source segments"
            )
        if len({item.segment_id for item in segments}) != len(segments):
            raise ContinuityReviewInputError("boundary segment_id must be unique")
        return cls(
            boundary_id=_identity(payload["boundary_id"], "boundary_id", 128),
            title=_prose(payload["title"], "boundary.title"),
            scope=_prose(payload["scope"], "boundary.scope"),
            current_meaning=_prose(
                payload["current_meaning"], "boundary.current_meaning"
            ),
            non_generalization=_prose(
                payload["non_generalization"], "boundary.non_generalization"
            ),
            expected_head_revision=_nonnegative_int(
                payload["expected_head_revision"], "expected_head_revision"
            ),
            visibility=_identity(payload["visibility"], "boundary.visibility", 128),
            segments=segments,
        )


@dataclass(frozen=True, slots=True)
class BoundaryAnchorEdit:
    """Mechanical replacement of one exact source range by one exact URI."""

    boundary_slot: int
    byte_start: int
    byte_end: int
    anchor_text: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> BoundaryAnchorEdit:
        expected = {"boundary_slot", "byte_start", "byte_end", "anchor_text"}
        _require_exact_keys(payload, expected, "anchor_edit")
        slot = _nonnegative_int(payload["boundary_slot"], "boundary_slot")
        start = _nonnegative_int(payload["byte_start"], "byte_start")
        end = _nonnegative_int(payload["byte_end"], "byte_end")
        if end < start:
            raise ContinuityReviewInputError(
                "anchor edit byte_end must not precede byte_start"
            )
        anchor = _prose(payload["anchor_text"], "anchor_text")
        if any(character in anchor for character in "[]\r\n"):
            raise ContinuityReviewInputError("anchor_text is not Markdown-safe")
        return cls(slot, start, end, anchor)


@dataclass(frozen=True, slots=True)
class SubjectTextEdit:
    """One exact subject-authored delta over the pinned ``MEMORY.md`` bytes.

    ``replacement`` is accepted only as text supplied in the active
    consciousness instance's tool action.  Infrastructure applies it
    mechanically and never accepts a caller-supplied complete candidate.
    """

    byte_start: int
    byte_end: int
    replacement: str

    def __post_init__(self) -> None:
        start = _nonnegative_int(self.byte_start, "text_edit.byte_start")
        end = _nonnegative_int(self.byte_end, "text_edit.byte_end")
        if end < start:
            raise ContinuityReviewInputError(
                "text edit byte_end must not precede byte_start"
            )
        if not isinstance(self.replacement, str):
            raise ContinuityReviewInputError("text_edit.replacement must be exact text")
        try:
            replacement_bytes = self.replacement.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ContinuityReviewInputError(
                "text_edit.replacement must be valid UTF-8 text"
            ) from exc
        if b"\x00" in replacement_bytes:
            raise ContinuityReviewInputError(
                "text_edit.replacement contains a NUL byte"
            )
        if len(replacement_bytes) > CONTINUITY_REVIEW_MAX_TEXT_EDIT_BYTES:
            raise ContinuityReviewInputError(
                "text_edit.replacement exceeds the per-edit UTF-8 byte limit"
            )
        if start == end and not replacement_bytes:
            raise ContinuityReviewInputError(
                "zero-length text edit must insert non-empty text"
            )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> SubjectTextEdit:
        expected = {"byte_start", "byte_end", "replacement"}
        _require_exact_keys(payload, expected, "text_edit")
        replacement = payload["replacement"]
        if not isinstance(replacement, str):
            raise ContinuityReviewInputError("text_edit.replacement must be exact text")
        return cls(
            byte_start=_nonnegative_int(payload["byte_start"], "text_edit.byte_start"),
            byte_end=_nonnegative_int(payload["byte_end"], "text_edit.byte_end"),
            replacement=replacement,
        )


@dataclass(frozen=True, slots=True)
class ExactTextPage:
    offset: int
    next_offset: int | None
    delivered_bytes: int
    total_bytes: int
    page_sha256: str
    text: str

    def as_dict(self) -> dict[str, object]:
        return {
            "offset": self.offset,
            "next_offset": self.next_offset,
            "delivered_bytes": self.delivered_bytes,
            "total_bytes": self.total_bytes,
            "page_sha256": self.page_sha256,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class AuxiliarySubjectSourceOpened:
    """Exact current-version pins for one controlled auxiliary document."""

    session_id: str
    logical_path: str
    version_id: str
    content_hash: str
    byte_length: int
    head_revision: int
    source_occurrence_id: str
    source_kind: str
    page: ExactTextPage

    def as_dict(self) -> dict[str, object]:
        return {
            "action": "auxiliary_source_opened",
            "session_id": self.session_id,
            "logical_path": self.logical_path,
            "version_id": self.version_id,
            "content_hash": self.content_hash,
            "byte_length": self.byte_length,
            "head_revision": self.head_revision,
            "source_occurrence_id": self.source_occurrence_id,
            "source_kind": self.source_kind,
            "page": self.page.as_dict(),
            "authority_written": False,
        }


@dataclass(frozen=True, slots=True)
class AuxiliarySubjectSourceRead:
    """One exact page tied to immutable auxiliary source pins."""

    session_id: str
    logical_path: str
    version_id: str
    content_hash: str
    source_occurrence_id: str
    page: ExactTextPage

    def as_dict(self) -> dict[str, object]:
        return {
            "action": "auxiliary_source_read",
            "session_id": self.session_id,
            "logical_path": self.logical_path,
            "version_id": self.version_id,
            "content_hash": self.content_hash,
            "source_occurrence_id": self.source_occurrence_id,
            "page": self.page.as_dict(),
            "authority_written": False,
        }


@dataclass(frozen=True, slots=True)
class ContinuityReviewOpened:
    session_id: str
    actor_consciousness_instance_id: str
    subject_revision: str
    memory_version_id: str
    memory_sha256: str
    memory_bytes: int
    current_index_entries: int
    current_index_issues: int
    page: ExactTextPage

    def as_dict(self) -> dict[str, object]:
        return {
            "action": "opened",
            "session_id": self.session_id,
            "actor_consciousness_instance_id": self.actor_consciousness_instance_id,
            "subject_revision": self.subject_revision,
            "memory_version_id": self.memory_version_id,
            "memory_sha256": self.memory_sha256,
            "memory_bytes": self.memory_bytes,
            "current_index_entries": self.current_index_entries,
            "current_index_issues": self.current_index_issues,
            "page": self.page.as_dict(),
            "authority_written": False,
        }


@dataclass(frozen=True, slots=True)
class BoundaryPreparationReceipt:
    boundary_id: str
    exact_uri: str
    root_sha256: str
    artifact_id: str
    manifest_revision: int
    idempotency_occurrence_id: str

    def as_dict(self) -> dict[str, object]:
        return {
            "boundary_id": self.boundary_id,
            "exact_uri": self.exact_uri,
            "root_sha256": self.root_sha256,
            "artifact_id": self.artifact_id,
            "manifest_revision": self.manifest_revision,
            "idempotency_occurrence_id": self.idempotency_occurrence_id,
        }


@dataclass(frozen=True, slots=True)
class ContinuityCandidatePrepared:
    session_id: str
    candidate_id: str
    candidate_revision: int
    candidate_sha256: str
    candidate_occurrence_id: str
    status: str
    subject_revision: str
    candidate_bytes: int
    boundaries: tuple[BoundaryPreparationReceipt, ...]
    boundary_anchor_edit_count: int
    subject_text_edit_count: int
    subject_text_replacement_bytes: int
    edit_plan_sha256: str
    boundary_source_plan_sha256: str
    auxiliary_source_count: int
    auxiliary_segment_count: int
    outcome_recording: ContinuityReviewOutcomeRecording

    def as_dict(self) -> dict[str, object]:
        return {
            "action": "candidate_prepared",
            "session_id": self.session_id,
            "candidate_id": self.candidate_id,
            "candidate_revision": self.candidate_revision,
            "candidate_sha256": self.candidate_sha256,
            "candidate_occurrence_id": self.candidate_occurrence_id,
            "status": self.status,
            "subject_revision": self.subject_revision,
            "candidate_bytes": self.candidate_bytes,
            "boundaries": [item.as_dict() for item in self.boundaries],
            "boundary_anchor_edit_count": self.boundary_anchor_edit_count,
            "subject_text_edit_count": self.subject_text_edit_count,
            "subject_text_replacement_bytes": self.subject_text_replacement_bytes,
            "edit_plan_sha256": self.edit_plan_sha256,
            "boundary_source_plan_sha256": self.boundary_source_plan_sha256,
            "auxiliary_source_count": self.auxiliary_source_count,
            "auxiliary_segment_count": self.auxiliary_segment_count,
            "candidate_generated_mechanically": True,
            "outcome_recording": self.outcome_recording.as_dict(),
            "authority_written": False,
        }


@dataclass(frozen=True, slots=True)
class ContinuityCandidateRead:
    session_id: str
    candidate_id: str
    candidate_revision: int
    candidate_sha256: str
    subject_revision: str
    page: ExactTextPage
    delivery_binding: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "action": "candidate_read",
            "session_id": self.session_id,
            "candidate_id": self.candidate_id,
            "candidate_revision": self.candidate_revision,
            "candidate_sha256": self.candidate_sha256,
            "subject_revision": self.subject_revision,
            "page": self.page.as_dict(),
            "delivery_binding": dict(self.delivery_binding),
            "delivery_binding_is_not_receipt": True,
            "authority_written": False,
        }


@dataclass(frozen=True, slots=True)
class ContinuityReviewStatus:
    session_id: str
    current_subject_revision: str
    candidates: tuple[dict[str, object], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "action": "status",
            "session_id": self.session_id,
            "current_subject_revision": self.current_subject_revision,
            "candidates": [dict(item) for item in self.candidates],
            "authority_written": False,
        }


@dataclass(frozen=True, slots=True)
class ContinuityDecisionPersisted:
    """Content-free proof that the existing Learning ledger handled a decision."""

    session_id: str
    decision_kind: LearningDecisionKind
    receipt: LearningDecisionReceipt
    exact_delivery_verified: bool
    authority_error_type: str = ""
    outcome_recording: ContinuityReviewOutcomeRecording | None = None

    def as_dict(self) -> dict[str, object]:
        receipt = self.receipt
        committed = receipt.status == "committed"
        outcome_recording = self.outcome_recording or ContinuityReviewOutcomeRecording(
            status="not_configured",
            outcome_kind="",
            outcome_occurrence_id="",
        )
        return {
            "action": "decision_persisted",
            "session_id": self.session_id,
            "decision_kind": self.decision_kind,
            "persisted_receipt": {
                "candidate_id": receipt.candidate_id,
                "candidate_revision": receipt.candidate_revision,
                "candidate_sha256": receipt.candidate_sha256,
                "status": receipt.status,
                "decision_occurrence_id": receipt.decision_occurrence_id,
                "authority_occurrence_id": receipt.authority_occurrence_id,
            },
            "status": receipt.status,
            "decision_occurrence_id": receipt.decision_occurrence_id,
            "authority_occurrence_id": receipt.authority_occurrence_id,
            "exact_delivery_verified": self.exact_delivery_verified,
            "decision_recorded": True,
            "subject_authority_committed": committed,
            "authority_pending": (
                self.decision_kind == "accept_requested" and not committed
            ),
            "authority_error_type": self.authority_error_type,
            "outcome_recording": outcome_recording.as_dict(),
            "subject_authority_called_only_by_learning_ledger": True,
        }


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContinuityReviewInputError(f"{field} must be an object")
    return value


def _require_exact_keys(
    payload: Mapping[str, Any], expected: set[str], object_name: str
) -> None:
    actual = set(payload)
    if actual != expected:
        raise ContinuityReviewInputError(
            f"{object_name} fields mismatch: "
            f"missing={sorted(expected - actual)!r}:"
            f"unexpected={sorted(actual - expected)!r}"
        )


def _identity(value: object, field: str, max_chars: int) -> str:
    text = str(value or "")
    if not text or text != text.strip() or len(text) > max_chars:
        raise ContinuityReviewInputError(f"{field} must be a canonical identity")
    if any(ord(character) < 32 for character in text):
        raise ContinuityReviewInputError(f"{field} contains control characters")
    return text


def _auxiliary_subject_path(value: object) -> str:
    """Validate one canonical logical path inside the declared diaries roots."""

    path = _identity(value, "auxiliary logical_path", 1024)
    if (
        "\\" in path
        or path.startswith("/")
        or "//" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or re.match(r"^[A-Za-z]:", path) is not None
    ):
        raise ContinuityReviewInputError(
            "auxiliary logical_path must be a canonical relative Subject path"
        )
    if path in SUBJECT_AUTHORITY_PATHS or not any(
        path.startswith(prefix) for prefix in _AUXILIARY_SUBJECT_PREFIXES
    ):
        raise ContinuityReviewInputError(
            "auxiliary logical_path is outside the controlled diaries roots"
        )
    return path


def _auxiliary_source_kind(logical_path: str) -> str:
    relative = logical_path.removeprefix("life_engine_workspace/")
    if relative.startswith("diaries/witness/"):
        return "witness_projection"
    if relative.startswith("notes/"):
        return "note_document"
    return "diary_document"


def _prose(value: object, field: str) -> str:
    text = str(value or "")
    if not text.strip() or len(text.encode("utf-8")) > 64 * 1024:
        raise ContinuityReviewInputError(f"{field} must be non-empty bounded text")
    return text


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContinuityReviewInputError(f"{field} must be a non-negative integer")
    return value


def _positive_int(value: object, field: str) -> int:
    result = _nonnegative_int(value, field)
    if result <= 0:
        raise ContinuityReviewInputError(f"{field} must be positive")
    return result


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: object, field: str) -> str:
    text = str(value or "").strip().lower()
    if _SHA256_RE.fullmatch(text) is None:
        raise ContinuityReviewInputError(f"{field} must be a lowercase SHA-256")
    return text


def _assert_utf8_source_range(
    content: bytes,
    *,
    byte_start: int,
    byte_end: int,
    field: str,
) -> None:
    if byte_end > len(content):
        raise ContinuityReviewInputError(f"{field} exceeds pinned source bytes")
    for position in (byte_start, byte_end):
        try:
            content[:position].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContinuityReviewInputError(
                f"{field} is not on UTF-8 byte boundaries"
            ) from exc


def _edit_plan_sha256(
    boundary_edits: Sequence[BoundaryAnchorEdit],
    text_edits: Sequence[SubjectTextEdit],
) -> str:
    plan = [
        {
            "kind": "boundary_anchor",
            "boundary_slot": item.boundary_slot,
            "byte_start": item.byte_start,
            "byte_end": item.byte_end,
            "anchor_text_sha256": _sha256_bytes(item.anchor_text.encode("utf-8")),
        }
        for item in boundary_edits
    ]
    plan.extend(
        {
            "kind": "subject_text",
            "byte_start": item.byte_start,
            "byte_end": item.byte_end,
            "replacement_bytes": len(item.replacement.encode("utf-8")),
            "replacement_sha256": _sha256_bytes(item.replacement.encode("utf-8")),
        }
        for item in text_edits
    )
    plan.sort(
        key=lambda item: (
            int(item["byte_start"]),
            int(item["byte_end"]),
            str(item["kind"]),
        )
    )
    return _sha256_bytes(
        json.dumps(
            plan,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _boundary_source_plan_sha256(
    boundaries: Sequence[ContinuityBoundaryPlan],
) -> str:
    plan: list[dict[str, object]] = []
    for boundary_slot, boundary in enumerate(boundaries):
        for segment in boundary.segments:
            item: dict[str, object] = {
                "boundary_slot": boundary_slot,
                "boundary_id": boundary.boundary_id,
                "segment_id": segment.segment_id,
                "byte_start": segment.byte_start,
                "byte_end": segment.byte_end,
                "source_kind": (
                    "auxiliary_subject"
                    if isinstance(segment, AuxiliarySubjectSegmentPlan)
                    else "memory"
                ),
            }
            if isinstance(segment, AuxiliarySubjectSegmentPlan):
                item.update(
                    {
                        "logical_path": segment.logical_path,
                        "version_id": segment.version_id,
                        "content_hash": segment.content_hash,
                    }
                )
            plan.append(item)
    return _sha256_bytes(
        json.dumps(
            plan,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _short_occurrence(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))[:16]


def _session_digest(session_id: str) -> str:
    if _SESSION_RE.fullmatch(session_id) is None:
        raise ContinuityReviewInputError("session_id is invalid")
    return session_id.removeprefix("continuity-review-")


def _make_session_id(
    *, actor_id: str, subject_revision: str, memory_version_id: str, memory_sha256: str
) -> str:
    payload = (
        f"{CONTINUITY_REVIEW_SESSION_VERSION}\0{actor_id}\0{subject_revision}\0"
        f"{memory_version_id}\0{memory_sha256}"
    ).encode()
    return f"continuity-review-{_sha256_bytes(payload)[:32]}"


def _exact_page(content: bytes, *, offset: int, max_bytes: int) -> ExactTextPage:
    start = _nonnegative_int(offset, "offset")
    requested = _positive_int(max_bytes, "max_bytes")
    bounded = min(requested, CONTINUITY_REVIEW_MAX_PAGE_BYTES)
    if start > len(content):
        raise ContinuityReviewInputError("offset exceeds exact content")
    try:
        content[:start].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContinuityReviewInputError("offset is not a UTF-8 boundary") from exc
    end = min(len(content), start + bounded)
    while end > start:
        try:
            text = content[start:end].decode("utf-8")
            break
        except UnicodeDecodeError as exc:
            if exc.reason != "unexpected end of data":
                raise ContinuityReviewInputError(
                    "source content is not valid UTF-8"
                ) from exc
            end -= 1
    else:
        text = ""
        if start < len(content):
            raise ContinuityReviewInputError("max_bytes cannot fit one UTF-8 codepoint")
    page_bytes = content[start:end]
    return ExactTextPage(
        offset=start,
        next_offset=end if end < len(content) else None,
        delivered_bytes=len(page_bytes),
        total_bytes=len(content),
        page_sha256=_sha256_bytes(page_bytes),
        text=text,
    )


class ContinuityReviewSession:
    """Stateless state machine over existing immutable authorities."""

    def __init__(
        self,
        *,
        subject_authority: SubjectAuthorityPort,
        boundary_repository: MemoryBoundarySessionPort,
        candidate_ledger: ContinuityReviewCandidateLedger,
        validate_active_actor: ActiveActorValidator,
        delivery_verifier: ExactCandidateDeliveryVerifier | None = None,
        outcome_recorder: ContinuityReviewOutcomeRecorder | None = None,
        subject_documents: AuxiliarySubjectDocumentReadPort | None = None,
    ) -> None:
        self._subject_authority = subject_authority
        # Production passes one coherent SubjectDocumentStorePort as authority;
        # tests/integrations may inject the same public read surface explicitly.
        self._subject_documents: object = (
            subject_documents if subject_documents is not None else subject_authority
        )
        self._boundary_repository = boundary_repository
        self._candidate_ledger = candidate_ledger
        self._validate_active_actor = validate_active_actor
        self._delivery_verifier = delivery_verifier
        self._outcome_recorder = outcome_recorder

    def _subject_document_reader(self) -> AuxiliarySubjectDocumentReadPort:
        reader = self._subject_documents
        if not callable(getattr(reader, "get_head", None)) or not callable(
            getattr(reader, "get_version", None)
        ):
            raise ContinuityReviewRuntimeUnavailable(
                "ContinuityReviewAuxiliarySubjectDocumentReaderUnavailable"
            )
        return cast(AuxiliarySubjectDocumentReadPort, reader)

    async def _ensure_active(self, actor: ContinuityReviewActorContext) -> None:
        if not await self._validate_active_actor(actor.consciousness_instance_id):
            raise ContinuityReviewActorInactive(
                "ContinuityReviewActorConsciousnessInstanceInactive"
            )

    async def _record_outcome(
        self,
        outcome: ContinuityReviewOutcome,
    ) -> ContinuityReviewOutcomeRecording:
        recorder = self._outcome_recorder
        if recorder is None:
            return ContinuityReviewOutcomeRecording(
                status="not_configured",
                outcome_kind=outcome.outcome_kind,
                outcome_occurrence_id=outcome.outcome_occurrence_id,
            )
        try:
            await recorder(outcome)
        except Exception as exc:  # noqa: BLE001 - derived projection is isolated
            return ContinuityReviewOutcomeRecording(
                status="failed",
                outcome_kind=outcome.outcome_kind,
                outcome_occurrence_id=outcome.outcome_occurrence_id,
                error_type=type(exc).__name__,
            )
        return ContinuityReviewOutcomeRecording(
            status="recorded",
            outcome_kind=outcome.outcome_kind,
            outcome_occurrence_id=outcome.outcome_occurrence_id,
        )

    async def _snapshot(self) -> SubjectAuthoritySnapshot:
        snapshot = await self._subject_authority.read_subject_authority()
        if set(snapshot.commits) != set(SUBJECT_AUTHORITY_PATHS):
            raise ContinuityReviewStale("SubjectAuthoritySnapshotIncomplete")
        contents: dict[Any, bytes] = {}
        for path in SUBJECT_AUTHORITY_PATHS:
            commit = snapshot.commits[path]
            version = commit.version
            head = commit.head
            content = bytes(version.content_bytes)
            if not all(
                (
                    version.logical_path == path,
                    head.logical_path == path,
                    head.current_version_id == version.version_id,
                    version.byte_length == len(content),
                    version.content_hash == _sha256_bytes(content),
                )
            ):
                raise ContinuityReviewStale(
                    f"SubjectAuthoritySnapshotEvidenceMismatch:{path}"
                )
            contents[path] = content
        revision = subject_revision_from_contents(contents)
        if snapshot.revision != revision:
            raise ContinuityReviewStale("SubjectAuthorityUnifiedRevisionMismatch")
        return snapshot

    @staticmethod
    def _memory(snapshot: SubjectAuthoritySnapshot) -> tuple[bytes, str, str]:
        version = snapshot.commits["MEMORY.md"].version
        content = bytes(version.content_bytes)
        return content, version.version_id, version.content_hash

    @staticmethod
    def _revision_with_memory(
        snapshot: SubjectAuthoritySnapshot,
        memory: bytes,
    ) -> str:
        contents = {
            path: (
                bytes(memory)
                if path == "MEMORY.md"
                else bytes(snapshot.commits[path].version.content_bytes)
            )
            for path in SUBJECT_AUTHORITY_PATHS
        }
        return subject_revision_from_contents(contents)

    @staticmethod
    def _assert_pins(
        snapshot: SubjectAuthoritySnapshot,
        *,
        actor: ContinuityReviewActorContext,
        session_id: str,
        expected_subject_revision: str,
        memory_version_id: str,
        memory_sha256: str,
    ) -> tuple[bytes, str, str]:
        content, actual_version, actual_hash = ContinuityReviewSession._memory(snapshot)
        expected_revision = _sha256_text(
            expected_subject_revision, "expected_subject_revision"
        )
        expected_memory_hash = _sha256_text(memory_sha256, "memory_sha256")
        expected_version = _identity(memory_version_id, "memory_version_id", 512)
        if (
            snapshot.revision != expected_revision
            or actual_version != expected_version
            or actual_hash != expected_memory_hash
        ):
            raise ContinuityReviewStale("ContinuityReviewSourcePinsStale")
        canonical_session = _make_session_id(
            actor_id=actor.consciousness_instance_id,
            subject_revision=snapshot.revision,
            memory_version_id=actual_version,
            memory_sha256=actual_hash,
        )
        if session_id != canonical_session:
            raise ContinuityReviewSessionMismatch("ContinuityReviewSessionMismatch")
        return content, actual_version, actual_hash

    async def open(
        self,
        actor: ContinuityReviewActorContext,
        *,
        offset: int = 0,
        max_bytes: int = CONTINUITY_REVIEW_MAX_PAGE_BYTES,
    ) -> ContinuityReviewOpened:
        await self._ensure_active(actor)
        snapshot = await self._snapshot()
        memory, version_id, memory_hash = self._memory(snapshot)
        session_id = _make_session_id(
            actor_id=actor.consciousness_instance_id,
            subject_revision=snapshot.revision,
            memory_version_id=version_id,
            memory_sha256=memory_hash,
        )
        from .continuity_index import diagnose_continuity_memory_index

        diagnostics = diagnose_continuity_memory_index(
            memory,
            subject_document_version_id=version_id,
            unified_subject_revision=snapshot.revision,
        )
        return ContinuityReviewOpened(
            session_id=session_id,
            actor_consciousness_instance_id=actor.consciousness_instance_id,
            subject_revision=snapshot.revision,
            memory_version_id=version_id,
            memory_sha256=memory_hash,
            memory_bytes=len(memory),
            current_index_entries=len(diagnostics.index.entries),
            current_index_issues=len(diagnostics.issues),
            page=_exact_page(memory, offset=offset, max_bytes=max_bytes),
        )

    async def read_source(
        self,
        actor: ContinuityReviewActorContext,
        *,
        session_id: str,
        expected_subject_revision: str,
        memory_version_id: str,
        memory_sha256: str,
        offset: int,
        max_bytes: int,
    ) -> ExactTextPage:
        await self._ensure_active(actor)
        snapshot = await self._snapshot()
        memory, _, _ = self._assert_pins(
            snapshot,
            actor=actor,
            session_id=session_id,
            expected_subject_revision=expected_subject_revision,
            memory_version_id=memory_version_id,
            memory_sha256=memory_sha256,
        )
        return _exact_page(memory, offset=offset, max_bytes=max_bytes)

    async def _read_auxiliary_version(
        self,
        *,
        logical_path: str,
        version_id: str,
        content_hash: str | None,
    ) -> SubjectDocumentVersion:
        path = _auxiliary_subject_path(logical_path)
        identity = _identity(version_id, "auxiliary version_id", 512)
        expected_hash = (
            _sha256_text(content_hash, "auxiliary content_hash")
            if content_hash is not None
            else None
        )
        try:
            version = await self._subject_document_reader().get_version(identity)
        except (SubjectDocumentNotFound, KeyError) as exc:
            raise ContinuityReviewAuxiliarySourceNotFound(
                "ContinuityReviewAuxiliarySubjectVersionMissing"
            ) from exc
        content = bytes(version.content_bytes)
        declared_hash = _sha256_text(
            version.content_hash,
            "stored auxiliary content_hash",
        )
        if expected_hash is None:
            expected_hash = declared_hash
        if not all(
            (
                version.logical_path == path,
                version.version_id == identity,
                declared_hash == expected_hash,
                version.byte_length == len(content),
                _sha256_bytes(content) == expected_hash,
            )
        ):
            raise ContinuityReviewStale(
                "ContinuityReviewAuxiliarySubjectVersionPinsStale"
            )
        _identity(
            version.occurrence_id,
            "auxiliary source occurrence_id",
            512,
        )
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContinuityReviewInputError(
                "auxiliary Subject version is not valid UTF-8"
            ) from exc
        return version

    async def open_auxiliary_source(
        self,
        actor: ContinuityReviewActorContext,
        *,
        session_id: str,
        expected_subject_revision: str,
        memory_version_id: str,
        memory_sha256: str,
        logical_path: str,
        offset: int = 0,
        max_bytes: int = CONTINUITY_REVIEW_MAX_PAGE_BYTES,
    ) -> AuxiliarySubjectSourceOpened:
        """Open the current immutable version of one controlled diary source."""

        await self._ensure_active(actor)
        snapshot = await self._snapshot()
        self._assert_pins(
            snapshot,
            actor=actor,
            session_id=session_id,
            expected_subject_revision=expected_subject_revision,
            memory_version_id=memory_version_id,
            memory_sha256=memory_sha256,
        )
        path = _auxiliary_subject_path(logical_path)
        head = await self._subject_document_reader().get_head(path)
        if head is None or not head.current_version_id:
            raise ContinuityReviewAuxiliarySourceNotFound(
                "ContinuityReviewAuxiliarySubjectHeadMissing"
            )
        version = await self._read_auxiliary_version(
            logical_path=path,
            version_id=head.current_version_id,
            content_hash=None,
        )
        if not all(
            (
                head.logical_path == path,
                head.document_id == version.document_id,
                head.current_version_id == version.version_id,
                head.revision > 0,
            )
        ):
            raise ContinuityReviewStale(
                "ContinuityReviewAuxiliarySubjectHeadEvidenceMismatch"
            )
        return AuxiliarySubjectSourceOpened(
            session_id=session_id,
            logical_path=path,
            version_id=version.version_id,
            content_hash=version.content_hash,
            byte_length=version.byte_length,
            head_revision=head.revision,
            source_occurrence_id=version.occurrence_id,
            source_kind=_auxiliary_source_kind(path),
            page=_exact_page(
                bytes(version.content_bytes),
                offset=offset,
                max_bytes=max_bytes,
            ),
        )

    async def read_auxiliary_source(
        self,
        actor: ContinuityReviewActorContext,
        *,
        session_id: str,
        expected_subject_revision: str,
        memory_version_id: str,
        memory_sha256: str,
        logical_path: str,
        version_id: str,
        content_hash: str,
        offset: int,
        max_bytes: int,
    ) -> AuxiliarySubjectSourceRead:
        """Read one exact page from a pinned immutable auxiliary version."""

        await self._ensure_active(actor)
        snapshot = await self._snapshot()
        self._assert_pins(
            snapshot,
            actor=actor,
            session_id=session_id,
            expected_subject_revision=expected_subject_revision,
            memory_version_id=memory_version_id,
            memory_sha256=memory_sha256,
        )
        version = await self._read_auxiliary_version(
            logical_path=logical_path,
            version_id=version_id,
            content_hash=content_hash,
        )
        return AuxiliarySubjectSourceRead(
            session_id=session_id,
            logical_path=version.logical_path,
            version_id=version.version_id,
            content_hash=version.content_hash,
            source_occurrence_id=version.occurrence_id,
            page=_exact_page(
                bytes(version.content_bytes),
                offset=offset,
                max_bytes=max_bytes,
            ),
        )

    async def _record_review_without_candidate(
        self,
        actor: ContinuityReviewActorContext,
        *,
        outcome_kind: str,
        session_id: str,
        expected_subject_revision: str,
        memory_version_id: str,
        memory_sha256: str,
        reason: str,
        snooze_hours: int = 0,
    ) -> ContinuityReviewOutcomeRecording:
        await self._ensure_active(actor)
        snapshot = await self._snapshot()
        self._assert_pins(
            snapshot,
            actor=actor,
            session_id=session_id,
            expected_subject_revision=expected_subject_revision,
            memory_version_id=memory_version_id,
            memory_sha256=memory_sha256,
        )
        validated_snooze_hours = 0
        if outcome_kind == "snooze":
            validated_snooze_hours = _positive_int(snooze_hours, "snooze_hours")
            if validated_snooze_hours > 720:
                raise ContinuityReviewInputError(
                    "snooze_hours must be between 1 and 720"
                )
        review_reason = _prose(reason, "reason")
        action_digest = _short_occurrence(actor.action_occurrence_id)
        outcome_occurrence_id = (
            f"continuity-review:{_session_digest(session_id)}:review:{action_digest}"
        )
        return await self._record_outcome(
            ContinuityReviewOutcome(
                outcome_occurrence_id=outcome_occurrence_id,
                outcome_kind=outcome_kind,
                target_path="MEMORY.md",
                candidate_occurrence_id="",
                candidate_id="",
                candidate_revision=0,
                candidate_sha256="",
                subject_revision_before=snapshot.revision,
                subject_revision_after=snapshot.revision,
                reason=review_reason,
                actor_consciousness_instance_id=(actor.consciousness_instance_id),
                source_occurrence_id=actor.source_occurrence_id,
                action_occurrence_id=actor.action_occurrence_id,
                occurred_at=actor.occurred_at,
                snooze_hours=validated_snooze_hours,
            )
        )

    async def unchanged(
        self,
        actor: ContinuityReviewActorContext,
        *,
        session_id: str,
        expected_subject_revision: str,
        memory_version_id: str,
        memory_sha256: str,
        reason: str,
    ) -> ContinuityReviewOutcomeRecording:
        """Record an explicit subject choice to leave pinned MEMORY unchanged."""

        return await self._record_review_without_candidate(
            actor,
            outcome_kind="unchanged",
            session_id=session_id,
            expected_subject_revision=expected_subject_revision,
            memory_version_id=memory_version_id,
            memory_sha256=memory_sha256,
            reason=reason,
        )

    async def snooze(
        self,
        actor: ContinuityReviewActorContext,
        *,
        session_id: str,
        expected_subject_revision: str,
        memory_version_id: str,
        memory_sha256: str,
        reason: str,
        snooze_hours: int,
    ) -> ContinuityReviewOutcomeRecording:
        """Record an explicit bounded postponement without creating a candidate."""

        return await self._record_review_without_candidate(
            actor,
            outcome_kind="snooze",
            session_id=session_id,
            expected_subject_revision=expected_subject_revision,
            memory_version_id=memory_version_id,
            memory_sha256=memory_sha256,
            reason=reason,
            snooze_hours=snooze_hours,
        )

    @staticmethod
    def _validate_plans(
        memory: bytes,
        boundaries: Sequence[ContinuityBoundaryPlan],
        edits: Sequence[BoundaryAnchorEdit],
        text_edits: Sequence[SubjectTextEdit],
    ) -> tuple[tuple[BoundaryAnchorEdit, ...], tuple[SubjectTextEdit, ...]]:
        if len(boundaries) > CONTINUITY_REVIEW_MAX_BOUNDARIES:
            raise ContinuityReviewInputError("boundary plan count is outside limits")
        if len(text_edits) > CONTINUITY_REVIEW_MAX_TEXT_EDITS:
            raise ContinuityReviewInputError("text edit count is outside limits")
        if not boundaries and not text_edits:
            raise ContinuityReviewInputError(
                "prepare_candidate requires at least one explicit edit"
            )
        if len(boundaries) != len(edits):
            raise ContinuityReviewInputError(
                "each Boundary requires exactly one mechanical anchor edit"
            )
        if len({item.boundary_id for item in boundaries}) != len(boundaries):
            raise ContinuityReviewInputError("boundary_id must be unique in one plan")
        if {item.boundary_slot for item in edits} != set(range(len(boundaries))):
            raise ContinuityReviewInputError("boundary_slot must cover every Boundary")

        ordered_boundary_edits = tuple(
            sorted(
                edits,
                key=lambda item: (
                    item.byte_start,
                    item.byte_end,
                    item.boundary_slot,
                ),
            )
        )
        for edit in ordered_boundary_edits:
            _assert_utf8_source_range(
                memory,
                byte_start=edit.byte_start,
                byte_end=edit.byte_end,
                field="Boundary anchor edit",
            )
            boundary = boundaries[edit.boundary_slot]
            if not boundary.segments:
                raise ContinuityReviewInputError("Boundary segments must be non-empty")
            has_auxiliary = any(
                isinstance(segment, AuxiliarySubjectSegmentPlan)
                for segment in boundary.segments
            )
            has_memory = any(
                isinstance(segment, ReviewedMemorySegmentPlan)
                for segment in boundary.segments
            )
            if has_auxiliary and has_memory:
                raise ContinuityReviewInputError(
                    "one Boundary must not mix MEMORY and auxiliary source segments"
                )
            if has_auxiliary:
                # Auxiliary content is independent from the MEMORY anchor.  A
                # zero-length anchor inserts a new index; a non-empty anchor
                # mechanically replaces an existing index or selected text.
                continue
            if edit.byte_start == edit.byte_end:
                raise ContinuityReviewInputError(
                    "zero-length Boundary anchors require auxiliary Subject segments"
                )
            segments = tuple(
                sorted(boundary.segments, key=lambda item: item.byte_start)
            )
            cursor = edit.byte_start
            for segment in segments:
                if segment.byte_start != cursor or segment.byte_end > edit.byte_end:
                    raise ContinuityReviewInputError(
                        "Boundary segments must exactly and contiguously cover its edit"
                    )
                _assert_utf8_source_range(
                    memory,
                    byte_start=segment.byte_start,
                    byte_end=segment.byte_end,
                    field="Boundary segment range",
                )
                cursor = segment.byte_end
            if cursor != edit.byte_end:
                raise ContinuityReviewInputError(
                    "Boundary segments must not omit bytes from the replaced source"
                )

        ordered_text_edits = tuple(
            sorted(text_edits, key=lambda item: (item.byte_start, item.byte_end))
        )
        total_replacement_bytes = 0
        for edit in ordered_text_edits:
            _assert_utf8_source_range(
                memory,
                byte_start=edit.byte_start,
                byte_end=edit.byte_end,
                field="subject text edit",
            )
            replacement_bytes = edit.replacement.encode("utf-8")
            if len(replacement_bytes) > CONTINUITY_REVIEW_MAX_TEXT_EDIT_BYTES:
                raise ContinuityReviewInputError(
                    "text_edit.replacement exceeds the per-edit UTF-8 byte limit"
                )
            total_replacement_bytes += len(replacement_bytes)
        if total_replacement_bytes > CONTINUITY_REVIEW_MAX_TEXT_REPLACEMENT_BYTES:
            raise ContinuityReviewInputError(
                "text edit replacements exceed the total UTF-8 byte limit"
            )

        combined = [
            (item.byte_start, item.byte_end, "boundary_anchor")
            for item in ordered_boundary_edits
        ]
        combined.extend(
            (item.byte_start, item.byte_end, "subject_text")
            for item in ordered_text_edits
        )
        combined.sort(key=lambda item: (item[0], item[1], item[2]))
        for index, current in enumerate(combined):
            current_start, current_end, _ = current
            for previous_start, previous_end, _ in combined[:index]:
                ranges_overlap = (
                    current_start < previous_end and previous_start < current_end
                )
                if ranges_overlap:
                    raise ContinuityReviewInputError("candidate edits must not overlap")
                previous_is_insertion = previous_start == previous_end
                current_is_insertion = current_start == current_end
                ambiguous_point = (
                    previous_is_insertion
                    and current_start <= previous_start <= current_end
                ) or (
                    current_is_insertion
                    and previous_start <= current_start <= previous_end
                )
                if ambiguous_point:
                    raise ContinuityReviewInputError(
                        "candidate edits have an ambiguous shared point"
                    )
        return ordered_boundary_edits, ordered_text_edits

    @staticmethod
    def _source_ref(
        *,
        logical_path: str,
        version_id: str,
        source_sha256: str,
        byte_start: int,
        byte_end: int,
        range_sha256: str,
    ) -> str:
        return (
            "subject://"
            + quote(logical_path, safe="/._-")
            + "@"
            + quote(version_id, safe="._:-")
            + f"#sha256={source_sha256}"
            + f"&bytes={byte_start}-{byte_end}"
            + f"&range_sha256={range_sha256}"
        )

    async def prepare_candidate(
        self,
        actor: ContinuityReviewActorContext,
        *,
        session_id: str,
        expected_subject_revision: str,
        memory_version_id: str,
        memory_sha256: str,
        boundaries: Sequence[ContinuityBoundaryPlan],
        edits: Sequence[BoundaryAnchorEdit],
        reason: str,
        text_edits: Sequence[SubjectTextEdit] = (),
    ) -> ContinuityCandidatePrepared:
        await self._ensure_active(actor)
        snapshot = await self._snapshot()
        memory, version_id, memory_hash = self._assert_pins(
            snapshot,
            actor=actor,
            session_id=session_id,
            expected_subject_revision=expected_subject_revision,
            memory_version_id=memory_version_id,
            memory_sha256=memory_sha256,
        )
        review_reason = _prose(reason, "reason")
        ordered_boundary_edits, ordered_text_edits = self._validate_plans(
            memory,
            boundaries,
            edits,
            text_edits,
        )
        edit_plan_sha256 = _edit_plan_sha256(
            ordered_boundary_edits,
            ordered_text_edits,
        )
        boundary_source_plan_sha256 = _boundary_source_plan_sha256(boundaries)
        auxiliary_versions: dict[tuple[str, str, str], SubjectDocumentVersion] = {}
        auxiliary_segment_count = 0
        for plan in boundaries:
            for segment in plan.segments:
                if not isinstance(segment, AuxiliarySubjectSegmentPlan):
                    continue
                auxiliary_segment_count += 1
                source_key = (
                    segment.logical_path,
                    segment.version_id,
                    segment.content_hash,
                )
                version = auxiliary_versions.get(source_key)
                if version is None:
                    version = await self._read_auxiliary_version(
                        logical_path=segment.logical_path,
                        version_id=segment.version_id,
                        content_hash=segment.content_hash,
                    )
                    auxiliary_versions[source_key] = version
                _assert_utf8_source_range(
                    bytes(version.content_bytes),
                    byte_start=segment.byte_start,
                    byte_end=segment.byte_end,
                    field="auxiliary Boundary segment range",
                )
        session_digest = _session_digest(session_id)
        action_digest = _short_occurrence(actor.action_occurrence_id)
        proposal_occurrence = (
            f"continuity-review:{session_digest}:prepare:{action_digest}"
        )
        stored_by_slot: dict[int, StoredMemoryBoundary] = {}
        receipts: list[BoundaryPreparationReceipt] = []
        for slot, plan in enumerate(boundaries):
            operation = f"{proposal_occurrence}:boundary:{slot}"
            segments: list[MemoryBoundarySegment] = []
            for segment_plan in plan.segments:
                if isinstance(segment_plan, AuxiliarySubjectSegmentPlan):
                    source_version = auxiliary_versions[
                        (
                            segment_plan.logical_path,
                            segment_plan.version_id,
                            segment_plan.content_hash,
                        )
                    ]
                    source_content = bytes(source_version.content_bytes)
                    source_logical_path = source_version.logical_path
                    source_version_id = source_version.version_id
                    source_hash = source_version.content_hash
                    source_occurrence_ids = (source_version.occurrence_id,)
                else:
                    source_content = memory
                    source_logical_path = "life_engine_workspace/MEMORY.md"
                    source_version_id = version_id
                    source_hash = memory_hash
                    source_occurrence_ids = (actor.source_occurrence_id,)
                content_bytes = source_content[
                    segment_plan.byte_start : segment_plan.byte_end
                ]
                content = content_bytes.decode("utf-8")
                content_hash = _sha256_bytes(content_bytes)
                segments.append(
                    MemoryBoundarySegment.create(
                        segment_id=segment_plan.segment_id,
                        title=segment_plan.title,
                        content=content,
                        source_refs=(
                            self._source_ref(
                                logical_path=source_logical_path,
                                version_id=source_version_id,
                                source_sha256=source_hash,
                                byte_start=segment_plan.byte_start,
                                byte_end=segment_plan.byte_end,
                                range_sha256=content_hash,
                            ),
                        ),
                        source_occurrence_ids=source_occurrence_ids,
                        scope=segment_plan.scope,
                        visibility=segment_plan.visibility,
                    )
                )
            manifest = MemoryBoundaryManifest(
                boundary_id=plan.boundary_id,
                manifest_revision=plan.expected_head_revision + 1,
                operation_occurrence_id=operation,
                title=plan.title,
                scope=plan.scope,
                current_meaning=plan.current_meaning,
                non_generalization=plan.non_generalization,
                actor_id=actor.consciousness_instance_id,
                consciousness_instance_id=actor.consciousness_instance_id,
                stream_scope=actor.stream_scope,
                decision_occurrence_id=f"{operation}:subject_action",
                source_occurrence_id=actor.source_occurrence_id,
                subject_revision=snapshot.revision,
                segments=tuple(segments),
                visibility=plan.visibility,
            )
            stored = await self._boundary_repository.append(
                manifest,
                expected_head_revision=plan.expected_head_revision,
            )
            stored_by_slot[slot] = stored
            receipts.append(
                BoundaryPreparationReceipt(
                    boundary_id=manifest.boundary_id,
                    exact_uri=stored.exact_uri,
                    root_sha256=manifest.root_sha256,
                    artifact_id=stored.artifact.artifact_id,
                    manifest_revision=manifest.manifest_revision,
                    idempotency_occurrence_id=operation,
                )
            )

        mechanical_edits: list[tuple[int, int, bytes]] = []
        for edit in ordered_boundary_edits:
            exact_uri = stored_by_slot[edit.boundary_slot].exact_uri
            mechanical_edits.append(
                (
                    edit.byte_start,
                    edit.byte_end,
                    f"[{edit.anchor_text}]({exact_uri})".encode(),
                )
            )
        mechanical_edits.extend(
            (
                edit.byte_start,
                edit.byte_end,
                edit.replacement.encode("utf-8"),
            )
            for edit in ordered_text_edits
        )
        mechanical_edits.sort(key=lambda item: (item[0], item[1]))
        proposed = bytearray()
        cursor = 0
        for byte_start, byte_end, replacement in mechanical_edits:
            proposed.extend(memory[cursor:byte_start])
            proposed.extend(replacement)
            cursor = byte_end
        proposed.extend(memory[cursor:])
        expected_subject_revision_after_accept = self._revision_with_memory(
            snapshot,
            bytes(proposed),
        )
        traceable_ledger = _TraceableCandidateLedger(
            self._candidate_ledger,
            occurred_at=actor.occurred_at,
            provenance={
                "continuity_review_session": session_id,
                "continuity_review_action_occurrence_id": (actor.action_occurrence_id),
                "continuity_review_source_occurrence_id": (actor.source_occurrence_id),
                "continuity_review_edit_plan_sha256": edit_plan_sha256,
                "continuity_review_boundary_source_plan_sha256": (
                    boundary_source_plan_sha256
                ),
                "expected_subject_revision_after_accept": (
                    expected_subject_revision_after_accept
                ),
                "boundary_anchor_edit_count": len(ordered_boundary_edits),
                "subject_text_edit_count": len(ordered_text_edits),
                "subject_text_replacement_bytes": sum(
                    len(item.replacement.encode("utf-8")) for item in ordered_text_edits
                ),
                "subject_text_from_active_consciousness": bool(ordered_text_edits),
                "auxiliary_subject_source_count": len(auxiliary_versions),
                "auxiliary_subject_segment_count": auxiliary_segment_count,
                "auxiliary_subject_content_server_derived": bool(auxiliary_versions),
                "candidate_generated_mechanically": True,
                "caller_supplied_complete_candidate": False,
            },
        )
        stewardship = ContinuityMemoryStewardship(
            self._boundary_repository,  # type: ignore[arg-type]
            traceable_ledger,
        )
        proposal: ContinuityMemoryCandidateProposal = await stewardship.propose(
            current_memory_bytes=memory,
            current_memory_version_id=version_id,
            reviewed_current_memory_sha256=memory_hash,
            proposed_memory_bytes=bytes(proposed),
            unified_subject_revision=snapshot.revision,
            actor_consciousness_instance_id=actor.consciousness_instance_id,
            source_occurrence_id=actor.source_occurrence_id,
            proposal_occurrence_id=proposal_occurrence,
            reason=review_reason,
            stream_scope=actor.stream_scope,
        )
        candidate = traceable_ledger.persisted_candidate
        if candidate is None:
            raise ContinuityReviewCandidateNotFound(
                "ContinuityReviewCandidateAppendEvidenceMissing"
            )
        outcome_recording = await self._record_outcome(
            ContinuityReviewOutcome(
                outcome_occurrence_id=(
                    f"{candidate.candidate_occurrence_id}:subject-review-outcome"
                ),
                outcome_kind="candidate_proposed",
                target_path="MEMORY.md",
                candidate_occurrence_id=candidate.candidate_occurrence_id,
                candidate_id=candidate.candidate_id,
                candidate_revision=candidate.candidate_revision,
                candidate_sha256=candidate.candidate_sha256,
                subject_revision_before=candidate.subject_revision,
                subject_revision_after=candidate.subject_revision,
                reason=review_reason,
                actor_consciousness_instance_id=(actor.consciousness_instance_id),
                source_occurrence_id=actor.source_occurrence_id,
                action_occurrence_id=actor.action_occurrence_id,
                occurred_at=actor.occurred_at,
            )
        )
        return ContinuityCandidatePrepared(
            session_id=session_id,
            candidate_id=candidate.candidate_id,
            candidate_revision=candidate.candidate_revision,
            candidate_sha256=candidate.candidate_sha256,
            candidate_occurrence_id=candidate.candidate_occurrence_id,
            status=proposal.receipt.status,
            subject_revision=candidate.subject_revision,
            candidate_bytes=len(candidate.candidate_content_bytes),
            boundaries=tuple(receipts),
            boundary_anchor_edit_count=len(ordered_boundary_edits),
            subject_text_edit_count=len(ordered_text_edits),
            subject_text_replacement_bytes=sum(
                len(item.replacement.encode("utf-8")) for item in ordered_text_edits
            ),
            edit_plan_sha256=edit_plan_sha256,
            boundary_source_plan_sha256=boundary_source_plan_sha256,
            auxiliary_source_count=len(auxiliary_versions),
            auxiliary_segment_count=auxiliary_segment_count,
            outcome_recording=outcome_recording,
        )

    @staticmethod
    def _candidate_session(
        candidate: LearningCandidate,
        *,
        session_id: str,
        actor_id: str,
    ) -> str:
        match = _CANDIDATE_OCCURRENCE_RE.fullmatch(candidate.candidate_occurrence_id)
        if match is None or match.group("session") != _session_digest(session_id):
            raise ContinuityReviewSessionMismatch(
                "ContinuityReviewCandidateSessionMismatch"
            )
        if candidate.candidate_kind != "memory_continuity_document_revision":
            raise ContinuityReviewSessionMismatch(
                "ContinuityReviewCandidateKindInvalid"
            )
        if candidate.target_path != "MEMORY.md":
            raise ContinuityReviewSessionMismatch(
                "ContinuityReviewCandidateTargetInvalid"
            )
        if candidate.actor_consciousness_instance_id != actor_id:
            raise ContinuityReviewSessionMismatch(
                "ContinuityReviewCandidateActorMismatch"
            )
        return match.group("action")

    async def _candidate(
        self,
        actor: ContinuityReviewActorContext,
        *,
        session_id: str,
        candidate_id: str,
        candidate_revision: int,
        candidate_sha256: str,
        expected_subject_revision: str,
    ) -> tuple[LearningCandidate, str]:
        candidate = await self._candidate_ledger.read_candidate(
            _identity(candidate_id, "candidate_id", 512)
        )
        if candidate is None:
            raise ContinuityReviewCandidateNotFound(
                "ContinuityReviewCandidateEvidenceMissing"
            )
        prepare_action = self._candidate_session(
            candidate,
            session_id=session_id,
            actor_id=actor.consciousness_instance_id,
        )
        if not all(
            (
                candidate.candidate_revision
                == _positive_int(candidate_revision, "candidate_revision"),
                candidate.candidate_sha256
                == _sha256_text(candidate_sha256, "candidate_sha256"),
                candidate.subject_revision
                == _sha256_text(expected_subject_revision, "expected_subject_revision"),
                _sha256_bytes(candidate.candidate_content_bytes)
                == candidate.candidate_sha256,
            )
        ):
            raise ContinuityReviewSessionMismatch(
                "ContinuityReviewCandidateIdentityMismatch"
            )
        return candidate, prepare_action

    async def read_candidate(
        self,
        actor: ContinuityReviewActorContext,
        *,
        session_id: str,
        candidate_id: str,
        candidate_revision: int,
        candidate_sha256: str,
        expected_subject_revision: str,
        offset: int,
        max_bytes: int,
    ) -> ContinuityCandidateRead:
        await self._ensure_active(actor)
        candidate, _ = await self._candidate(
            actor,
            session_id=session_id,
            candidate_id=candidate_id,
            candidate_revision=candidate_revision,
            candidate_sha256=candidate_sha256,
            expected_subject_revision=expected_subject_revision,
        )
        page = _exact_page(
            candidate.candidate_content_bytes,
            offset=offset,
            max_bytes=max_bytes,
        )
        delivery_id = "continuity-delivery-" + _sha256_bytes(
            json.dumps(
                {
                    "session_id": session_id,
                    "candidate_id": candidate.candidate_id,
                    "candidate_revision": candidate.candidate_revision,
                    "candidate_sha256": candidate.candidate_sha256,
                    "offset": page.offset,
                    "delivered_bytes": page.delivered_bytes,
                    "page_sha256": page.page_sha256,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        return ContinuityCandidateRead(
            session_id=session_id,
            candidate_id=candidate.candidate_id,
            candidate_revision=candidate.candidate_revision,
            candidate_sha256=candidate.candidate_sha256,
            subject_revision=candidate.subject_revision,
            page=page,
            delivery_binding={
                "delivery_id": delivery_id,
                "candidate_id": candidate.candidate_id,
                "candidate_revision": candidate.candidate_revision,
                "candidate_sha256": candidate.candidate_sha256,
                "page_offset": page.offset,
                "page_sha256": page.page_sha256,
                "page_bytes": page.delivered_bytes,
                "total_bytes": page.total_bytes,
            },
        )

    async def status(
        self,
        actor: ContinuityReviewActorContext,
        *,
        session_id: str,
        candidate_id: str = "",
    ) -> ContinuityReviewStatus:
        await self._ensure_active(actor)
        session_digest = _session_digest(session_id)
        current_revision = (await self._snapshot()).revision
        rows: list[dict[str, object]] = []
        if candidate_id:
            candidate = await self._candidate_ledger.read_candidate(candidate_id)
            candidates = [candidate] if candidate is not None else []
        else:
            summaries = await self._candidate_ledger.list_candidates(
                status="all", limit=100
            )
            candidates = []
            for summary in summaries:
                occurrence = str(summary.get("candidate_occurrence_id", ""))
                match = _CANDIDATE_OCCURRENCE_RE.fullmatch(occurrence)
                if match is None or match.group("session") != session_digest:
                    continue
                candidate = await self._candidate_ledger.read_candidate(
                    str(summary.get("candidate_id", ""))
                )
                if candidate is not None:
                    candidates.append(candidate)
        for candidate in candidates:
            self._candidate_session(
                candidate,
                session_id=session_id,
                actor_id=actor.consciousness_instance_id,
            )
            matching = await self._candidate_ledger.list_candidates(
                status="all", limit=100
            )
            summary = next(
                (
                    item
                    for item in matching
                    if str(item.get("candidate_id", "")) == candidate.candidate_id
                ),
                {},
            )
            rows.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "candidate_revision": candidate.candidate_revision,
                    "candidate_sha256": candidate.candidate_sha256,
                    "candidate_occurrence_id": candidate.candidate_occurrence_id,
                    "subject_revision": candidate.subject_revision,
                    "status": str(summary.get("status", "open")),
                    "decision_occurrence_id": str(
                        summary.get("decision_occurrence_id", "")
                    ),
                    "authority_occurrence_id": str(
                        summary.get("authority_occurrence_id", "")
                    ),
                    "stale": candidate.subject_revision != current_revision,
                }
            )
        rows.sort(key=lambda item: str(item["candidate_id"]))
        return ContinuityReviewStatus(session_id, current_revision, tuple(rows))

    @staticmethod
    def _validate_decision_receipt(
        receipt: LearningDecisionReceipt,
        decision: LearningDecision,
        *,
        expected_statuses: set[str],
    ) -> None:
        if not all(
            (
                receipt.candidate_id == decision.candidate_id,
                receipt.candidate_revision == decision.candidate_revision,
                receipt.candidate_sha256 == decision.candidate_sha256,
                receipt.decision_occurrence_id == decision.decision_occurrence_id,
                receipt.status in expected_statuses,
            )
        ):
            raise ContinuityReviewDecisionPersistenceUnverified(
                "ContinuityReviewDecisionReceiptMismatch"
            )
        if receipt.status == "committed" and not receipt.authority_occurrence_id:
            raise ContinuityReviewDecisionPersistenceUnverified(
                "ContinuityReviewAuthorityOccurrenceMissing"
            )
        if receipt.status != "committed" and receipt.authority_occurrence_id:
            raise ContinuityReviewDecisionPersistenceUnverified(
                "ContinuityReviewPrematureAuthorityOccurrence"
            )

    async def decide(
        self,
        actor: ContinuityReviewActorContext,
        *,
        session_id: str,
        candidate_id: str,
        candidate_revision: int,
        candidate_sha256: str,
        expected_subject_revision: str,
        decision_kind: LearningDecisionKind,
        reason: str,
        delivery_receipt: Mapping[str, Any] | None = None,
    ) -> ContinuityDecisionPersisted:
        await self._ensure_active(actor)
        candidate, prepare_action = await self._candidate(
            actor,
            session_id=session_id,
            candidate_id=candidate_id,
            candidate_revision=candidate_revision,
            candidate_sha256=candidate_sha256,
            expected_subject_revision=expected_subject_revision,
        )
        current_revision = (await self._snapshot()).revision
        if current_revision != candidate.subject_revision:
            raise ContinuityReviewStale("ContinuityReviewDecisionSourceStale")
        action_digest = _short_occurrence(actor.action_occurrence_id)
        if action_digest == prepare_action:
            raise ContinuityReviewIndependentDecisionRequired(
                "ContinuityReviewDecisionRequiresIndependentActionOccurrence"
            )
        if decision_kind not in {"accept_requested", "rejected", "kept_open"}:
            raise ContinuityReviewInputError("decision_kind is invalid")
        verified = False
        accepted_bytes = b""
        accepted_sha256 = ""
        target_path = None
        if decision_kind == "accept_requested":
            if delivery_receipt is None:
                raise ContinuityReviewDeliveryUnverified(
                    "ContinuityReviewExactCandidateDeliveryReceiptRequired"
                )
            if self._delivery_verifier is None:
                raise ContinuityReviewDeliveryProofUnavailable(
                    "ContinuityReviewTrustedDeliveryVerifierUnavailable"
                )
            receipt = CandidateDeliveryReceipt.from_payload(delivery_receipt)
            if not all(
                (
                    receipt.candidate_id == candidate.candidate_id,
                    receipt.candidate_revision == candidate.candidate_revision,
                    receipt.candidate_sha256 == candidate.candidate_sha256,
                    receipt.total_bytes == len(candidate.candidate_content_bytes),
                )
            ):
                raise ContinuityReviewDeliveryUnverified(
                    "ContinuityReviewCandidateDeliveryIdentityMismatch"
                )
            verified = await self._delivery_verifier.verify_exact_candidate_delivery(
                receipt, candidate
            )
            if not verified:
                raise ContinuityReviewDeliveryUnverified(
                    "ContinuityReviewCandidateDeliveryNotVerified"
                )
            accepted_bytes = candidate.candidate_content_bytes
            accepted_sha256 = candidate.candidate_sha256
            target_path = "MEMORY.md"
        elif delivery_receipt is not None:
            raise ContinuityReviewInputError(
                "rejected/kept_open decisions must not claim delivery proof"
            )
        decision_occurrence = (
            f"continuity-review:{_session_digest(session_id)}:decision:{action_digest}"
        )
        decision_reason = _prose(reason, "reason")
        decision = LearningDecision(
            decision_occurrence_id=decision_occurrence,
            decision_kind=decision_kind,
            candidate_id=candidate.candidate_id,
            candidate_revision=candidate.candidate_revision,
            candidate_sha256=candidate.candidate_sha256,
            candidate_occurrence_id=candidate.candidate_occurrence_id,
            actor_consciousness_instance_id=actor.consciousness_instance_id,
            expected_subject_revision=candidate.subject_revision,
            occurred_at=actor.occurred_at,
            reason=decision_reason,
            target_path=target_path,
            accepted_content_bytes=accepted_bytes,
            accepted_content_sha256=accepted_sha256,
            provenance={
                "authority": "existing_learning_ledger",
                "continuity_review_session": session_id,
                "exact_candidate_delivery_verified": verified,
                "infrastructure_generated_subject_text": False,
            },
        )
        authority_error_type = ""
        if decision_kind == "accept_requested":
            try:
                persisted = await self._candidate_ledger.accept_subject_candidate(
                    decision
                )
            except SubjectAuthorityUnavailable:
                persisted = await self._candidate_ledger.record_decision(decision)
                authority_error_type = "SubjectAuthorityUnavailable"
                expected_statuses = {"accept_requested"}
            else:
                expected_statuses = {"committed"}
        else:
            persisted = await self._candidate_ledger.record_decision(decision)
            expected_statuses = {decision_kind}
        self._validate_decision_receipt(
            persisted,
            decision,
            expected_statuses=expected_statuses,
        )
        outcome_recording = ContinuityReviewOutcomeRecording(
            status="not_required",
            outcome_kind="",
            outcome_occurrence_id="",
        )
        if persisted.status in {"rejected", "kept_open", "committed"}:
            outcome_kind = persisted.status
            outcome_occurrence_id = (
                f"{decision.decision_occurrence_id}:subject-review-outcome"
            )
            subject_revision_after = candidate.subject_revision
            snapshot_error: Exception | None = None
            if persisted.status == "committed":
                try:
                    subject_revision_after = _sha256_text(
                        candidate.provenance.get(
                            "expected_subject_revision_after_accept", ""
                        ),
                        "expected_subject_revision_after_accept",
                    )
                except ContinuityReviewInputError as exc:
                    snapshot_error = exc
            if (
                persisted.status == "committed"
                and self._outcome_recorder is not None
                and snapshot_error is None
            ):
                try:
                    await self._snapshot()
                except Exception as exc:  # noqa: BLE001 - commit already persisted
                    snapshot_error = exc
            if snapshot_error is not None:
                outcome_recording = ContinuityReviewOutcomeRecording(
                    status="failed",
                    outcome_kind=outcome_kind,
                    outcome_occurrence_id=outcome_occurrence_id,
                    error_type=type(snapshot_error).__name__,
                )
            else:
                outcome_recording = await self._record_outcome(
                    ContinuityReviewOutcome(
                        outcome_occurrence_id=outcome_occurrence_id,
                        outcome_kind=outcome_kind,
                        target_path="MEMORY.md",
                        candidate_occurrence_id=(candidate.candidate_occurrence_id),
                        candidate_id=candidate.candidate_id,
                        candidate_revision=candidate.candidate_revision,
                        candidate_sha256=candidate.candidate_sha256,
                        subject_revision_before=candidate.subject_revision,
                        subject_revision_after=subject_revision_after,
                        reason=decision_reason,
                        actor_consciousness_instance_id=(
                            actor.consciousness_instance_id
                        ),
                        source_occurrence_id=actor.source_occurrence_id,
                        action_occurrence_id=actor.action_occurrence_id,
                        occurred_at=actor.occurred_at,
                        decision_occurrence_id=(decision.decision_occurrence_id),
                        authority_occurrence_id=(persisted.authority_occurrence_id),
                    )
                )
        return ContinuityDecisionPersisted(
            session_id=session_id,
            decision_kind=decision_kind,
            receipt=persisted,
            exact_delivery_verified=verified,
            authority_error_type=authority_error_type,
            outcome_recording=outcome_recording,
        )


__all__ = [
    "CONTINUITY_REVIEW_MAX_TEXT_EDITS",
    "CONTINUITY_REVIEW_MAX_TEXT_EDIT_BYTES",
    "CONTINUITY_REVIEW_MAX_TEXT_REPLACEMENT_BYTES",
    "AuxiliarySubjectDocumentReadPort",
    "AuxiliarySubjectSegmentPlan",
    "AuxiliarySubjectSourceOpened",
    "AuxiliarySubjectSourceRead",
    "BoundaryAnchorEdit",
    "CandidateDeliveryReceipt",
    "ContinuityBoundaryPlan",
    "ContinuityCandidatePrepared",
    "ContinuityCandidateRead",
    "ContinuityDecisionPersisted",
    "ContinuityReviewActorContext",
    "ContinuityReviewActorInactive",
    "ContinuityReviewAuxiliarySourceNotFound",
    "ContinuityReviewCandidateNotFound",
    "ContinuityReviewDecisionPersistenceUnverified",
    "ContinuityReviewDeliveryProofUnavailable",
    "ContinuityReviewDeliveryUnverified",
    "ContinuityReviewError",
    "ContinuityReviewIndependentDecisionRequired",
    "ContinuityReviewInputError",
    "ContinuityReviewOpened",
    "ContinuityReviewOutcome",
    "ContinuityReviewOutcomeRecorder",
    "ContinuityReviewOutcomeRecording",
    "ContinuityReviewRuntimeUnavailable",
    "ContinuityReviewSession",
    "ContinuityReviewSessionMismatch",
    "ContinuityReviewStale",
    "ContinuityReviewStatus",
    "ExactCandidateDeliveryVerifier",
    "ExactTextPage",
    "MemoryBoundarySessionPort",
    "ReviewedMemorySegmentPlan",
    "SubjectTextEdit",
]
