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
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
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
class ContinuityBoundaryPlan:
    """Subject-authored Boundary semantics plus exact mechanical ranges."""

    boundary_id: str
    title: str
    scope: str
    current_meaning: str
    non_generalization: str
    expected_head_revision: int
    visibility: str
    segments: tuple[ReviewedMemorySegmentPlan, ...]

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
        segments = tuple(
            ReviewedMemorySegmentPlan.from_payload(_mapping(item, "segment"))
            for item in raw_segments
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
        end = _positive_int(payload["byte_end"], "byte_end")
        if end <= start:
            raise ContinuityReviewInputError("anchor edit range must be non-empty")
        anchor = _prose(payload["anchor_text"], "anchor_text")
        if any(character in anchor for character in "[]\r\n"):
            raise ContinuityReviewInputError("anchor_text is not Markdown-safe")
        return cls(slot, start, end, anchor)


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

    def as_dict(self) -> dict[str, object]:
        receipt = self.receipt
        committed = receipt.status == "committed"
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
    ) -> None:
        self._subject_authority = subject_authority
        self._boundary_repository = boundary_repository
        self._candidate_ledger = candidate_ledger
        self._validate_active_actor = validate_active_actor
        self._delivery_verifier = delivery_verifier

    async def _ensure_active(self, actor: ContinuityReviewActorContext) -> None:
        if not await self._validate_active_actor(actor.consciousness_instance_id):
            raise ContinuityReviewActorInactive(
                "ContinuityReviewActorConsciousnessInstanceInactive"
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

    @staticmethod
    def _validate_plans(
        memory: bytes,
        boundaries: Sequence[ContinuityBoundaryPlan],
        edits: Sequence[BoundaryAnchorEdit],
    ) -> tuple[BoundaryAnchorEdit, ...]:
        if not boundaries or len(boundaries) > CONTINUITY_REVIEW_MAX_BOUNDARIES:
            raise ContinuityReviewInputError("boundary plan count is outside limits")
        if len(boundaries) != len(edits):
            raise ContinuityReviewInputError(
                "each Boundary requires exactly one mechanical anchor edit"
            )
        if len({item.boundary_id for item in boundaries}) != len(boundaries):
            raise ContinuityReviewInputError("boundary_id must be unique in one plan")
        if {item.boundary_slot for item in edits} != set(range(len(boundaries))):
            raise ContinuityReviewInputError("boundary_slot must cover every Boundary")
        ordered = tuple(sorted(edits, key=lambda item: item.byte_start))
        previous_end = 0
        for edit in ordered:
            if edit.byte_start < previous_end or edit.byte_end > len(memory):
                raise ContinuityReviewInputError(
                    "anchor edits must be ordered, in range, and non-overlapping"
                )
            boundary = boundaries[edit.boundary_slot]
            segments = tuple(
                sorted(boundary.segments, key=lambda item: item.byte_start)
            )
            cursor = edit.byte_start
            for segment in segments:
                if segment.byte_start != cursor or segment.byte_end > edit.byte_end:
                    raise ContinuityReviewInputError(
                        "Boundary segments must exactly and contiguously cover its edit"
                    )
                try:
                    memory[segment.byte_start : segment.byte_end].decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ContinuityReviewInputError(
                        "Boundary segment range is not on UTF-8 boundaries"
                    ) from exc
                cursor = segment.byte_end
            if cursor != edit.byte_end:
                raise ContinuityReviewInputError(
                    "Boundary segments must not omit bytes from the replaced source"
                )
            previous_end = edit.byte_end
        return ordered

    @staticmethod
    def _source_ref(
        *,
        version_id: str,
        memory_sha256: str,
        byte_start: int,
        byte_end: int,
        range_sha256: str,
    ) -> str:
        return (
            "subject://life_engine_workspace/MEMORY.md@"
            + quote(version_id, safe="._:-")
            + f"#sha256={memory_sha256}"
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
        ordered_edits = self._validate_plans(memory, boundaries, edits)
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
                content_bytes = memory[segment_plan.byte_start : segment_plan.byte_end]
                content = content_bytes.decode("utf-8")
                content_hash = _sha256_bytes(content_bytes)
                segments.append(
                    MemoryBoundarySegment.create(
                        segment_id=segment_plan.segment_id,
                        title=segment_plan.title,
                        content=content,
                        source_refs=(
                            self._source_ref(
                                version_id=version_id,
                                memory_sha256=memory_hash,
                                byte_start=segment_plan.byte_start,
                                byte_end=segment_plan.byte_end,
                                range_sha256=content_hash,
                            ),
                        ),
                        source_occurrence_ids=(actor.source_occurrence_id,),
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

        proposed = bytearray()
        cursor = 0
        for edit in ordered_edits:
            proposed.extend(memory[cursor : edit.byte_start])
            exact_uri = stored_by_slot[edit.boundary_slot].exact_uri
            proposed.extend(f"[{edit.anchor_text}]({exact_uri})".encode())
            cursor = edit.byte_end
        proposed.extend(memory[cursor:])
        stewardship = ContinuityMemoryStewardship(
            self._boundary_repository,  # type: ignore[arg-type]
            self._candidate_ledger,
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
        candidate = proposal.candidate
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
            reason=_prose(reason, "reason"),
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
        return ContinuityDecisionPersisted(
            session_id=session_id,
            decision_kind=decision_kind,
            receipt=persisted,
            exact_delivery_verified=verified,
            authority_error_type=authority_error_type,
        )


__all__ = [
    "BoundaryAnchorEdit",
    "CandidateDeliveryReceipt",
    "ContinuityBoundaryPlan",
    "ContinuityCandidatePrepared",
    "ContinuityCandidateRead",
    "ContinuityDecisionPersisted",
    "ContinuityReviewActorContext",
    "ContinuityReviewActorInactive",
    "ContinuityReviewCandidateNotFound",
    "ContinuityReviewDecisionPersistenceUnverified",
    "ContinuityReviewDeliveryProofUnavailable",
    "ContinuityReviewDeliveryUnverified",
    "ContinuityReviewError",
    "ContinuityReviewIndependentDecisionRequired",
    "ContinuityReviewInputError",
    "ContinuityReviewOpened",
    "ContinuityReviewRuntimeUnavailable",
    "ContinuityReviewSession",
    "ContinuityReviewSessionMismatch",
    "ContinuityReviewStale",
    "ContinuityReviewStatus",
    "ExactCandidateDeliveryVerifier",
    "ExactTextPage",
    "MemoryBoundarySessionPort",
    "ReviewedMemorySegmentPlan",
]
