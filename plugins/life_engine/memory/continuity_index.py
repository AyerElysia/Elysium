"""Pure continuity-index projection derived from accepted ``MEMORY.md`` bytes.

This module only recognizes explicit Markdown links to immutable memory-boundary
artifacts.  It does not infer memories from prose, resolve or mutate bundles, or
decide whether any memory is important.  Every result is rebuildable from one
exact subject-document version and one unified subject revision.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Final

CONTINUITY_MEMORY_SOFT_TARGET_BYTES: Final = 16 * 1024
CONTINUITY_MEMORY_REVIEW_PRESSURE_BYTES: Final = 24 * 1024
CONTINUITY_MEMORY_REVIEW_GROWTH_BYTES: Final = 4 * 1024
CONTINUITY_MEMORY_INDEX_AUTHORITY: Final = "derived_non_authoritative"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ENTRY_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
_ARTIFACT_VERSION_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}"
_BOUNDARY_URI_RE = re.compile(
    rf"memory://boundary/(?P<entry_id>{_ENTRY_ID_PATTERN})"
    rf"@(?P<artifact_version_id>{_ARTIFACT_VERSION_ID_PATTERN})"
    r"#sha256=(?P<root_sha256>[0-9a-f]{64})"
)
_BOUNDARY_HINT_RE = re.compile(r"memory://boundary", re.IGNORECASE)
_MARKDOWN_LINK_RE = re.compile(
    r"(?<![!\\])\[(?P<anchor>(?:\\.|[^\]\\\r\n])*)\]"
    r"\((?P<target>[^()\s]+)\)"
)


class ContinuityMemoryIndexError(ValueError):
    """Base error for an invalid continuity-index projection input."""


class MalformedContinuityMemoryReference(ContinuityMemoryIndexError):
    """Raised when an attempted ``memory://boundary`` reference is invalid."""


class DuplicateContinuityMemoryEntry(ContinuityMemoryIndexError):
    """Raised when one exact document declares an entry identity more than once."""


def _validate_opaque_identity(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value or value != value.strip():
        raise ContinuityMemoryIndexError(
            f"{field_name} must be a non-empty canonical identity"
        )
    if len(value.encode("utf-8")) > 512 or any(ord(char) < 32 for char in value):
        raise ContinuityMemoryIndexError(f"{field_name} is not a valid identity")
    return value


def _validate_sha256(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ContinuityMemoryIndexError(
            f"{field_name} must be a lowercase 64-character SHA-256 digest"
        )
    return value


@dataclass(frozen=True, slots=True)
class ContinuityMemoryIndexEntry:
    """One source-positioned boundary link parsed from exact ``MEMORY.md`` bytes.

    ``anchor_text`` is the exact Markdown anchor source, not a semantic summary
    produced by infrastructure.  Byte offsets are half-open and address the
    entire Markdown link in the accepted source document.
    """

    anchor_text: str
    entry_id: str
    artifact_version_id: str
    boundary_root_sha256: str
    byte_start: int
    byte_end: int
    entry_sha256: str
    subject_document_version_id: str
    unified_subject_revision: str

    def __post_init__(self) -> None:
        if not self.anchor_text.strip():
            raise ContinuityMemoryIndexError("memory boundary anchor_text is empty")
        if not re.fullmatch(_ENTRY_ID_PATTERN, self.entry_id):
            raise ContinuityMemoryIndexError("memory boundary entry_id is invalid")
        if not re.fullmatch(
            _ARTIFACT_VERSION_ID_PATTERN,
            self.artifact_version_id,
        ):
            raise ContinuityMemoryIndexError(
                "memory boundary artifact_version_id is invalid"
            )
        _validate_sha256(
            self.boundary_root_sha256,
            field_name="boundary_root_sha256",
        )
        _validate_sha256(self.entry_sha256, field_name="entry_sha256")
        _validate_opaque_identity(
            self.subject_document_version_id,
            field_name="subject_document_version_id",
        )
        _validate_sha256(
            self.unified_subject_revision,
            field_name="unified_subject_revision",
        )
        if self.byte_start < 0 or self.byte_end <= self.byte_start:
            raise ContinuityMemoryIndexError(
                "memory boundary byte offsets must form a non-empty half-open range"
            )

    @property
    def boundary_id(self) -> str:
        """Return the boundary identity encoded by the entry URI."""

        return self.entry_id

    @property
    def artifact_id(self) -> str:
        """Return the pinned immutable artifact-version identity."""

        return self.artifact_version_id

    @property
    def root_sha256(self) -> str:
        """Return the pinned boundary-manifest root digest."""

        return self.boundary_root_sha256


@dataclass(frozen=True, slots=True)
class ContinuityMemoryIndex:
    """A rebuildable projection of one exact accepted ``MEMORY.md`` version."""

    subject_document_version_id: str
    unified_subject_revision: str
    source_document_sha256: str
    source_document_byte_length: int
    entries: tuple[ContinuityMemoryIndexEntry, ...]

    def __post_init__(self) -> None:
        _validate_opaque_identity(
            self.subject_document_version_id,
            field_name="subject_document_version_id",
        )
        _validate_sha256(
            self.unified_subject_revision,
            field_name="unified_subject_revision",
        )
        _validate_sha256(
            self.source_document_sha256,
            field_name="source_document_sha256",
        )
        if self.source_document_byte_length < 0:
            raise ContinuityMemoryIndexError(
                "source_document_byte_length must not be negative"
            )

        seen: set[str] = set()
        previous_end = 0
        for entry in self.entries:
            if entry.entry_id in seen:
                raise DuplicateContinuityMemoryEntry(
                    f"duplicate continuity memory entry_id: {entry.entry_id}"
                )
            seen.add(entry.entry_id)
            if (
                entry.subject_document_version_id != self.subject_document_version_id
                or entry.unified_subject_revision != self.unified_subject_revision
            ):
                raise ContinuityMemoryIndexError(
                    "continuity entry source identity does not match its projection"
                )
            if entry.byte_start < previous_end:
                raise ContinuityMemoryIndexError(
                    "continuity entries must be source ordered and non-overlapping"
                )
            if entry.byte_end > self.source_document_byte_length:
                raise ContinuityMemoryIndexError(
                    "continuity entry byte range exceeds the source document"
                )
            previous_end = entry.byte_end


@dataclass(frozen=True, slots=True)
class ContinuityMemoryIndexIssue:
    """Content-free location and fingerprint of one malformed index attempt."""

    byte_offset: int
    error_type: str
    reason_code: str
    attempted_reference_sha256: str


@dataclass(frozen=True, slots=True)
class ContinuityMemoryIndexDiagnostics:
    """Tolerant current-version projection used only to construct repairs."""

    index: ContinuityMemoryIndex
    issues: tuple[ContinuityMemoryIndexIssue, ...]
    issues_sha256: str


@dataclass(frozen=True, slots=True)
class ContinuityMemoryIndexHealth:
    """Content-free integrity and engineering pressure for one projection.

    Size thresholds only invite engineering review.  They never imply that an
    entry is unimportant and never recommend or authorize deletion.
    """

    source_bytes: int
    entry_count: int
    broken_reference_count: int
    soft_target_bytes: int
    review_pressure_bytes: int
    soft_target_exceeded: bool
    review_pressure_reached: bool
    authority: str
    pressure_semantics: str
    automatic_deletion_recommended: bool

    def as_dict(self) -> dict[str, int | bool | str]:
        """Return a serializable snapshot without anchors or memory identities."""

        return {
            "bytes": self.source_bytes,
            "count": self.entry_count,
            "broken": self.broken_reference_count,
            "soft_target_bytes": self.soft_target_bytes,
            "review_pressure_bytes": self.review_pressure_bytes,
            "soft_target_exceeded": self.soft_target_exceeded,
            "review_pressure_reached": self.review_pressure_reached,
            "authority": self.authority,
            "pressure_semantics": self.pressure_semantics,
            "automatic_deletion_recommended": (self.automatic_deletion_recommended),
        }


@dataclass(frozen=True, slots=True)
class ContinuityMemoryRetargeting:
    """Content-free proof that one stable entry now pins a different target."""

    entry_id: str
    previous_artifact_version_id: str
    current_artifact_version_id: str
    previous_root_sha256: str
    current_root_sha256: str
    previous_entry_sha256: str
    current_entry_sha256: str


@dataclass(frozen=True, slots=True)
class ContinuityMemoryLifecycleDiff:
    """Technical current-vs-previous presence and target changes only.

    ``deactivated`` means that an index entry is absent from the current
    accepted document.  It does not delete, devalue, or mutate a boundary
    bundle or any earlier subject-document version.
    """

    previous_subject_document_version_id: str
    current_subject_document_version_id: str
    previous_unified_subject_revision: str
    current_unified_subject_revision: str
    activated: tuple[str, ...]
    deactivated: tuple[str, ...]
    rewritten: tuple[str, ...]
    retargeted: tuple[ContinuityMemoryRetargeting, ...]


def _character_byte_offsets(text: str) -> list[int]:
    offsets = [0]
    total = 0
    for char in text:
        total += len(char.encode("utf-8"))
        offsets.append(total)
    return offsets


def parse_continuity_memory_index(
    exact_memory_bytes: bytes,
    *,
    subject_document_version_id: str,
    unified_subject_revision: str,
) -> ContinuityMemoryIndex:
    """Parse explicit boundary links from one exact accepted ``MEMORY.md``.

    Bare prose is never interpreted as a memory index.  Conversely, any
    attempted ``memory://boundary`` occurrence must be a canonical Markdown
    link; malformed or duplicate references fail the complete projection
    instead of disappearing silently.
    """

    diagnostics = diagnose_continuity_memory_index(
        exact_memory_bytes,
        subject_document_version_id=subject_document_version_id,
        unified_subject_revision=unified_subject_revision,
    )
    if diagnostics.issues:
        first = diagnostics.issues[0]
        if first.error_type == "duplicate_entry_id":
            text = exact_memory_bytes.decode("utf-8")
            seen: set[str] = set()
            duplicate = ""
            for match in _BOUNDARY_URI_RE.finditer(text):
                entry_id = match.group("entry_id")
                if entry_id in seen:
                    duplicate = entry_id
                    break
                seen.add(entry_id)
            raise DuplicateContinuityMemoryEntry(
                f"duplicate continuity memory entry_id: {duplicate or 'unknown'}"
            )
        raise MalformedContinuityMemoryReference(
            "malformed memory boundary reference at byte "
            f"{first.byte_offset}: {first.reason_code}"
        )
    return diagnostics.index


def diagnose_continuity_memory_index(
    exact_memory_bytes: bytes,
    *,
    subject_document_version_id: str,
    unified_subject_revision: str,
) -> ContinuityMemoryIndexDiagnostics:
    """Recover valid links and content-free issues from exact current bytes.

    This tolerant view may be used to propose a complete corrected candidate.
    It is never accepted as a valid subject index and never hides its issues.
    """

    if not isinstance(exact_memory_bytes, bytes):
        raise TypeError("exact_memory_bytes must be immutable bytes")
    document_version_id = _validate_opaque_identity(
        subject_document_version_id,
        field_name="subject_document_version_id",
    )
    subject_revision = _validate_sha256(
        unified_subject_revision,
        field_name="unified_subject_revision",
    )
    try:
        text = exact_memory_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MalformedContinuityMemoryReference(
            f"MEMORY.md exact bytes are not valid UTF-8 at byte {exc.start}"
        ) from exc

    offsets = _character_byte_offsets(text)
    entries: list[ContinuityMemoryIndexEntry] = []
    issues: list[ContinuityMemoryIndexIssue] = []
    attempted_target_spans: list[tuple[int, int]] = []
    seen_entry_ids: set[str] = set()

    def issue(
        character_offset: int,
        *,
        error_type: str,
        reason_code: str,
        attempted: str,
    ) -> None:
        issues.append(
            ContinuityMemoryIndexIssue(
                byte_offset=offsets[character_offset],
                error_type=error_type,
                reason_code=reason_code,
                attempted_reference_sha256=hashlib.sha256(
                    attempted.encode("utf-8")
                ).hexdigest(),
            )
        )

    for match in _MARKDOWN_LINK_RE.finditer(text):
        target = match.group("target")
        if _BOUNDARY_HINT_RE.search(target) is None:
            continue
        attempted_target_spans.append((match.start("target"), match.end("target")))
        uri_match = _BOUNDARY_URI_RE.fullmatch(target)
        if uri_match is None:
            issue(
                match.start("target"),
                error_type="invalid_boundary_uri",
                reason_code="uri_not_canonical",
                attempted=target,
            )
            continue
        anchor_text = match.group("anchor")
        if not anchor_text.strip():
            issue(
                match.start(),
                error_type="empty_anchor",
                reason_code="markdown_anchor_empty",
                attempted=match.group(0),
            )
            continue
        entry_id = uri_match.group("entry_id")
        if entry_id in seen_entry_ids:
            issue(
                match.start(),
                error_type="duplicate_entry_id",
                reason_code="entry_id_repeated",
                attempted=entry_id,
            )
            continue
        seen_entry_ids.add(entry_id)
        byte_start = offsets[match.start()]
        byte_end = offsets[match.end()]
        entries.append(
            ContinuityMemoryIndexEntry(
                anchor_text=anchor_text,
                entry_id=entry_id,
                artifact_version_id=uri_match.group("artifact_version_id"),
                boundary_root_sha256=uri_match.group("root_sha256"),
                byte_start=byte_start,
                byte_end=byte_end,
                entry_sha256=hashlib.sha256(
                    exact_memory_bytes[byte_start:byte_end]
                ).hexdigest(),
                subject_document_version_id=document_version_id,
                unified_subject_revision=subject_revision,
            )
        )

    for hint in _BOUNDARY_HINT_RE.finditer(text):
        if any(
            start <= hint.start() and hint.end() <= end
            for start, end in attempted_target_spans
        ):
            continue
        line_end = text.find("\n", hint.start())
        attempted = text[hint.start() : line_end if line_end >= 0 else len(text)]
        issue(
            hint.start(),
            error_type="boundary_reference_not_markdown_link",
            reason_code="reference_not_canonical_markdown_link",
            attempted=attempted,
        )

    index = ContinuityMemoryIndex(
        subject_document_version_id=document_version_id,
        unified_subject_revision=subject_revision,
        source_document_sha256=hashlib.sha256(exact_memory_bytes).hexdigest(),
        source_document_byte_length=len(exact_memory_bytes),
        entries=tuple(entries),
    )
    issue_payload = [
        {
            "byte_offset": item.byte_offset,
            "error_type": item.error_type,
            "reason_code": item.reason_code,
            "attempted_reference_sha256": item.attempted_reference_sha256,
        }
        for item in issues
    ]
    issues_sha256 = hashlib.sha256(repr(issue_payload).encode("utf-8")).hexdigest()
    return ContinuityMemoryIndexDiagnostics(
        index=index,
        issues=tuple(issues),
        issues_sha256=issues_sha256,
    )


def build_continuity_memory_index_health(
    index: ContinuityMemoryIndex,
) -> ContinuityMemoryIndexHealth:
    """Build content-free health without assigning semantic importance."""

    source_bytes = index.source_document_byte_length
    return ContinuityMemoryIndexHealth(
        source_bytes=source_bytes,
        entry_count=len(index.entries),
        broken_reference_count=0,
        soft_target_bytes=CONTINUITY_MEMORY_SOFT_TARGET_BYTES,
        review_pressure_bytes=CONTINUITY_MEMORY_REVIEW_PRESSURE_BYTES,
        soft_target_exceeded=(source_bytes > CONTINUITY_MEMORY_SOFT_TARGET_BYTES),
        review_pressure_reached=(
            source_bytes >= CONTINUITY_MEMORY_REVIEW_PRESSURE_BYTES
        ),
        authority=CONTINUITY_MEMORY_INDEX_AUTHORITY,
        pressure_semantics="engineering_review_only",
        automatic_deletion_recommended=False,
    )


def diff_continuity_memory_indexes(
    previous: ContinuityMemoryIndex,
    current: ContinuityMemoryIndex,
) -> ContinuityMemoryLifecycleDiff:
    """Compare exact projections without judging or mutating their memories."""

    previous_by_id = {entry.entry_id: entry for entry in previous.entries}
    current_by_id = {entry.entry_id: entry for entry in current.entries}

    activated = tuple(
        entry.entry_id
        for entry in current.entries
        if entry.entry_id not in previous_by_id
    )
    deactivated = tuple(
        entry.entry_id
        for entry in previous.entries
        if entry.entry_id not in current_by_id
    )
    retargeted: list[ContinuityMemoryRetargeting] = []
    rewritten: list[str] = []
    for current_entry in current.entries:
        previous_entry = previous_by_id.get(current_entry.entry_id)
        if previous_entry is None:
            continue
        previous_target = (
            previous_entry.artifact_version_id,
            previous_entry.boundary_root_sha256,
        )
        current_target = (
            current_entry.artifact_version_id,
            current_entry.boundary_root_sha256,
        )
        if previous_target == current_target:
            if previous_entry.entry_sha256 != current_entry.entry_sha256:
                rewritten.append(current_entry.entry_id)
            continue
        retargeted.append(
            ContinuityMemoryRetargeting(
                entry_id=current_entry.entry_id,
                previous_artifact_version_id=(previous_entry.artifact_version_id),
                current_artifact_version_id=(current_entry.artifact_version_id),
                previous_root_sha256=(previous_entry.boundary_root_sha256),
                current_root_sha256=current_entry.boundary_root_sha256,
                previous_entry_sha256=previous_entry.entry_sha256,
                current_entry_sha256=current_entry.entry_sha256,
            )
        )

    return ContinuityMemoryLifecycleDiff(
        previous_subject_document_version_id=(previous.subject_document_version_id),
        current_subject_document_version_id=current.subject_document_version_id,
        previous_unified_subject_revision=previous.unified_subject_revision,
        current_unified_subject_revision=current.unified_subject_revision,
        activated=activated,
        deactivated=deactivated,
        rewritten=tuple(rewritten),
        retargeted=tuple(retargeted),
    )


__all__ = [
    "CONTINUITY_MEMORY_INDEX_AUTHORITY",
    "CONTINUITY_MEMORY_REVIEW_GROWTH_BYTES",
    "CONTINUITY_MEMORY_REVIEW_PRESSURE_BYTES",
    "CONTINUITY_MEMORY_SOFT_TARGET_BYTES",
    "ContinuityMemoryIndex",
    "ContinuityMemoryIndexDiagnostics",
    "ContinuityMemoryIndexEntry",
    "ContinuityMemoryIndexError",
    "ContinuityMemoryIndexHealth",
    "ContinuityMemoryIndexIssue",
    "ContinuityMemoryLifecycleDiff",
    "ContinuityMemoryRetargeting",
    "DuplicateContinuityMemoryEntry",
    "MalformedContinuityMemoryReference",
    "build_continuity_memory_index_health",
    "diagnose_continuity_memory_index",
    "diff_continuity_memory_indexes",
    "parse_continuity_memory_index",
]
