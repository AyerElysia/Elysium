"""Immutable, versioned boundaries for complete long-term memories.

A boundary manifest preserves subject-authored meaning and complete source
material without deciding whether a memory is important. The repository
stores each canonical manifest as an existing living-memory artifact and uses
the existing artifact-head revision as its compare-and-swap boundary.

This module intentionally contains no retrieval ranking, score, importance,
automatic category, or subject-document mutation path.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .living import (
    ArtifactHead,
    ArtifactHeadConflict,
    MemoryArtifactDescriptor,
    MemoryArtifactVersion,
    MemoryDerivation,
    new_artifact_version,
)

if TYPE_CHECKING:
    from ..storage.memory.contracts import LivingMemoryStore


MEMORY_BOUNDARY_SCHEMA_VERSION = 1
MEMORY_BOUNDARY_ARTIFACT_KIND = "memory_boundary_manifest/v1"
MEMORY_BOUNDARY_LOGICAL_KEY_PREFIX = "memory_boundary_manifest:"
MEMORY_BOUNDARY_MAX_BYTES = 512 * 1024
MEMORY_BOUNDARY_MAX_SEGMENTS = 256
MEMORY_BOUNDARY_MAX_SOURCE_LABELS = 1024

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BOUNDARY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "boundary_id",
        "manifest_revision",
        "operation_occurrence_id",
        "title",
        "scope",
        "current_meaning",
        "non_generalization",
        "actor_id",
        "consciousness_instance_id",
        "stream_scope",
        "decision_occurrence_id",
        "source_occurrence_id",
        "subject_revision",
        "visibility",
        "segments",
    }
)
_SEGMENT_KEYS = frozenset(
    {
        "segment_id",
        "title",
        "content",
        "content_sha256",
        "byte_length",
        "source_refs",
        "source_occurrence_ids",
        "scope",
        "visibility",
    }
)


class MemoryBoundaryError(RuntimeError):
    """Base class for memory-boundary failures."""


class MemoryBoundaryValidationError(ValueError, MemoryBoundaryError):
    """Raised when a proposed boundary violates the technical contract."""


class MemoryBoundaryBundleTooLarge(MemoryBoundaryValidationError):
    """Raised when a complete manifest must be split instead of truncated."""


class MemoryBoundaryIntegrityError(MemoryBoundaryError):
    """Raised when persisted history is missing, non-canonical, or tampered."""


class MemoryBoundaryNotFound(LookupError, MemoryBoundaryError):
    """Raised when an exact boundary revision or reference does not exist."""


class MemoryBoundaryOperationConflict(MemoryBoundaryError):
    """Raised when one operation occurrence is reused with different bytes."""


class MemoryBoundaryStaleOperationReplay(ArtifactHeadConflict):
    """Raised when an old, once-valid operation is replayed after a new head."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat()


def _require_text(value: Any, field_name: str, *, identifier: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MemoryBoundaryValidationError(f"MemoryBoundaryFieldRequired:{field_name}")
    if identifier and value != value.strip():
        raise MemoryBoundaryValidationError(
            f"MemoryBoundaryIdentifierWhitespace:{field_name}"
        )
    return value


def _require_max_chars(value: str, field_name: str, max_chars: int) -> str:
    if len(value) > max_chars:
        raise MemoryBoundaryValidationError(
            f"MemoryBoundaryFieldTooLong:{field_name}:"
            f"chars={len(value)}:max_chars={max_chars}"
        )
    return value


def _require_positive_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MemoryBoundaryValidationError("MemoryBoundaryManifestRevisionInvalid")
    return value


def _require_sha256(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise MemoryBoundaryValidationError(f"MemoryBoundarySha256Invalid:{field_name}")
    return value


def _string_tuple(
    value: Any,
    field_name: str,
    *,
    max_item_chars: int = 1024,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise MemoryBoundaryValidationError(
            f"MemoryBoundarySequenceRequired:{field_name}"
        )
    if len(value) > MEMORY_BOUNDARY_MAX_SOURCE_LABELS:
        raise MemoryBoundaryValidationError(
            f"MemoryBoundarySequenceTooLong:{field_name}:"
            f"items={len(value)}:max_items={MEMORY_BOUNDARY_MAX_SOURCE_LABELS}"
        )
    result = tuple(
        _require_max_chars(
            _require_text(item, f"{field_name}[{index}]", identifier=True),
            f"{field_name}[{index}]",
            max_item_chars,
        )
        for index, item in enumerate(value)
    )
    if not result:
        raise MemoryBoundaryValidationError(
            f"MemoryBoundarySourceRequired:{field_name}"
        )
    if len(set(result)) != len(result):
        raise MemoryBoundaryValidationError(
            f"MemoryBoundaryDuplicateValue:{field_name}"
        )
    return result


def _require_exact_keys(
    payload: Mapping[str, Any],
    expected: frozenset[str],
    object_name: str,
) -> None:
    actual = frozenset(payload)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    raise MemoryBoundaryValidationError(
        f"MemoryBoundarySchemaMismatch:{object_name}:"
        f"missing={missing!r}:unexpected={unexpected!r}"
    )


def memory_boundary_logical_key(boundary_id: str) -> str:
    """Return the dedicated living-artifact logical key for one boundary."""

    identifier = _require_text(boundary_id, "boundary_id", identifier=True)
    if _BOUNDARY_ID_RE.fullmatch(identifier) is None:
        raise MemoryBoundaryValidationError("MemoryBoundaryIdInvalid")
    return f"{MEMORY_BOUNDARY_LOGICAL_KEY_PREFIX}{identifier}"


@dataclass(frozen=True, slots=True)
class MemoryBoundarySegment:
    """One ordered, complete segment and its immutable source references."""

    segment_id: str
    title: str
    content: str
    content_sha256: str
    byte_length: int
    source_refs: tuple[str, ...]
    source_occurrence_ids: tuple[str, ...]
    scope: str
    visibility: str

    def __post_init__(self) -> None:
        segment_id = _require_text(self.segment_id, "segment_id", identifier=True)
        if _BOUNDARY_ID_RE.fullmatch(segment_id) is None:
            raise MemoryBoundaryValidationError("MemoryBoundarySegmentIdInvalid")
        _require_max_chars(
            _require_text(self.title, "segment.title"),
            "segment.title",
            16 * 1024,
        )
        _require_text(self.content, "segment.content")
        _require_max_chars(
            _require_text(self.scope, "segment.scope"),
            "segment.scope",
            16 * 1024,
        )
        _require_max_chars(
            _require_text(self.visibility, "segment.visibility", identifier=True),
            "segment.visibility",
            128,
        )
        refs = _string_tuple(self.source_refs, "segment.source_refs")
        occurrences = _string_tuple(
            self.source_occurrence_ids,
            "segment.source_occurrence_ids",
            max_item_chars=512,
        )
        object.__setattr__(self, "source_refs", refs)
        object.__setattr__(self, "source_occurrence_ids", occurrences)
        content_bytes = self.content.encode("utf-8")
        if (
            isinstance(self.byte_length, bool)
            or not isinstance(self.byte_length, int)
            or self.byte_length != len(content_bytes)
        ):
            raise MemoryBoundaryValidationError(
                f"MemoryBoundarySegmentByteLengthMismatch:{self.segment_id}"
            )
        _require_sha256(self.content_sha256, "segment.content_sha256")
        if self.content_sha256 != _sha256_bytes(content_bytes):
            raise MemoryBoundaryValidationError(
                f"MemoryBoundarySegmentContentHashMismatch:{self.segment_id}"
            )

    @classmethod
    def create(
        cls,
        *,
        segment_id: str,
        title: str,
        content: str,
        source_refs: Sequence[str],
        source_occurrence_ids: Sequence[str],
        scope: str,
        visibility: str,
    ) -> MemoryBoundarySegment:
        """Build a segment without normalizing or truncating subject content."""

        exact_content = _require_text(content, "segment.content")
        content_bytes = exact_content.encode("utf-8")
        return cls(
            segment_id=segment_id,
            title=title,
            content=exact_content,
            content_sha256=_sha256_bytes(content_bytes),
            byte_length=len(content_bytes),
            source_refs=tuple(source_refs),
            source_occurrence_ids=tuple(source_occurrence_ids),
            scope=scope,
            visibility=visibility,
        )

    def to_payload(self) -> dict[str, Any]:
        """Return the exact schema payload used by the manifest root hash."""

        return {
            "segment_id": self.segment_id,
            "title": self.title,
            "content": self.content,
            "content_sha256": self.content_sha256,
            "byte_length": self.byte_length,
            "source_refs": list(self.source_refs),
            "source_occurrence_ids": list(self.source_occurrence_ids),
            "scope": self.scope,
            "visibility": self.visibility,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> MemoryBoundarySegment:
        """Load one segment from the strict v1 schema."""

        _require_exact_keys(payload, _SEGMENT_KEYS, "segment")
        return cls(
            segment_id=payload["segment_id"],
            title=payload["title"],
            content=payload["content"],
            content_sha256=payload["content_sha256"],
            byte_length=payload["byte_length"],
            source_refs=_string_tuple(payload["source_refs"], "segment.source_refs"),
            source_occurrence_ids=_string_tuple(
                payload["source_occurrence_ids"],
                "segment.source_occurrence_ids",
                max_item_chars=512,
            ),
            scope=payload["scope"],
            visibility=payload["visibility"],
        )


@dataclass(frozen=True, slots=True)
class MemoryBoundaryManifest:
    """A complete subject-authored boundary bundle at one exact revision."""

    boundary_id: str
    manifest_revision: int
    operation_occurrence_id: str
    title: str
    scope: str
    current_meaning: str
    non_generalization: str
    actor_id: str
    consciousness_instance_id: str
    stream_scope: str
    decision_occurrence_id: str
    source_occurrence_id: str
    subject_revision: str
    segments: tuple[MemoryBoundarySegment, ...]
    visibility: str = "private"

    def __post_init__(self) -> None:
        memory_boundary_logical_key(self.boundary_id)
        _require_positive_revision(self.manifest_revision)
        for field_name, value in (
            ("title", self.title),
            ("scope", self.scope),
            ("current_meaning", self.current_meaning),
            ("non_generalization", self.non_generalization),
        ):
            _require_max_chars(
                _require_text(value, field_name),
                field_name,
                64 * 1024,
            )
        _require_max_chars(
            _require_text(self.actor_id, "actor_id", identifier=True),
            "actor_id",
            512,
        )
        _require_max_chars(
            _require_text(
                self.consciousness_instance_id,
                "consciousness_instance_id",
                identifier=True,
            ),
            "consciousness_instance_id",
            255,
        )
        _require_max_chars(
            _require_text(self.stream_scope, "stream_scope", identifier=True),
            "stream_scope",
            512,
        )
        for field_name, value in (
            ("operation_occurrence_id", self.operation_occurrence_id),
            ("decision_occurrence_id", self.decision_occurrence_id),
            ("source_occurrence_id", self.source_occurrence_id),
        ):
            _require_max_chars(
                _require_text(value, field_name, identifier=True),
                field_name,
                512,
            )
        _require_sha256(self.subject_revision, "subject_revision")
        _require_max_chars(
            _require_text(self.visibility, "visibility", identifier=True),
            "visibility",
            128,
        )
        if not isinstance(self.segments, (list, tuple)) or not self.segments:
            raise MemoryBoundaryValidationError("MemoryBoundarySegmentsRequired")
        segments = tuple(self.segments)
        if len(segments) > MEMORY_BOUNDARY_MAX_SEGMENTS:
            raise MemoryBoundaryValidationError(
                "MemoryBoundarySegmentsTooMany:"
                f"items={len(segments)}:max_items={MEMORY_BOUNDARY_MAX_SEGMENTS}"
            )
        if not all(isinstance(item, MemoryBoundarySegment) for item in segments):
            raise MemoryBoundaryValidationError("MemoryBoundarySegmentTypeInvalid")
        segment_ids = tuple(item.segment_id for item in segments)
        if len(set(segment_ids)) != len(segment_ids):
            raise MemoryBoundaryValidationError("MemoryBoundarySegmentIdDuplicate")
        object.__setattr__(self, "segments", segments)
        delivered_bytes = len(self.canonical_bytes)
        if delivered_bytes > MEMORY_BOUNDARY_MAX_BYTES:
            raise MemoryBoundaryBundleTooLarge(
                "MemoryBoundaryBundleTooLarge:"
                f"bytes={delivered_bytes}:max_bytes={MEMORY_BOUNDARY_MAX_BYTES}:"
                "split_into_multiple_boundaries_required"
            )

    @property
    def logical_key(self) -> str:
        """Return the dedicated logical key shared by all of its revisions."""

        return memory_boundary_logical_key(self.boundary_id)

    def to_payload(self) -> dict[str, Any]:
        """Return the strict, self-contained v1 payload."""

        return {
            "schema_version": MEMORY_BOUNDARY_SCHEMA_VERSION,
            "boundary_id": self.boundary_id,
            "manifest_revision": self.manifest_revision,
            "operation_occurrence_id": self.operation_occurrence_id,
            "title": self.title,
            "scope": self.scope,
            "current_meaning": self.current_meaning,
            "non_generalization": self.non_generalization,
            "actor_id": self.actor_id,
            "consciousness_instance_id": self.consciousness_instance_id,
            "stream_scope": self.stream_scope,
            "decision_occurrence_id": self.decision_occurrence_id,
            "source_occurrence_id": self.source_occurrence_id,
            "subject_revision": self.subject_revision,
            "visibility": self.visibility,
            "segments": [item.to_payload() for item in self.segments],
        }

    @property
    def canonical_json(self) -> str:
        """Return deterministic UTF-8 JSON without changing subject text."""

        return _canonical_json(self.to_payload())

    @property
    def canonical_bytes(self) -> bytes:
        """Return the exact bytes stored in the living-memory artifact."""

        return self.canonical_json.encode("utf-8")

    @property
    def root_sha256(self) -> str:
        """Return the root hash pinning every field and complete segment byte."""

        return _sha256_bytes(self.canonical_bytes)

    @classmethod
    def from_canonical_json(cls, value: str | bytes) -> MemoryBoundaryManifest:
        """Load and verify one exact canonical v1 manifest."""

        if isinstance(value, bytes):
            if len(value) > MEMORY_BOUNDARY_MAX_BYTES:
                raise MemoryBoundaryBundleTooLarge(
                    "MemoryBoundaryBundleTooLarge:"
                    f"bytes={len(value)}:max_bytes={MEMORY_BOUNDARY_MAX_BYTES}:"
                    "split_into_multiple_boundaries_required"
                )
            try:
                text = value.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise MemoryBoundaryIntegrityError(
                    "MemoryBoundaryManifestNotUtf8"
                ) from exc
        elif isinstance(value, str):
            text = value
        else:
            raise MemoryBoundaryValidationError("MemoryBoundaryManifestTextRequired")
        text_bytes = text.encode("utf-8")
        if len(text_bytes) > MEMORY_BOUNDARY_MAX_BYTES:
            raise MemoryBoundaryBundleTooLarge(
                "MemoryBoundaryBundleTooLarge:"
                f"bytes={len(text_bytes)}:max_bytes={MEMORY_BOUNDARY_MAX_BYTES}:"
                "split_into_multiple_boundaries_required"
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MemoryBoundaryIntegrityError(
                "MemoryBoundaryManifestJsonInvalid"
            ) from exc
        if not isinstance(payload, dict):
            raise MemoryBoundaryIntegrityError("MemoryBoundaryManifestObjectRequired")
        _require_exact_keys(payload, _MANIFEST_KEYS, "manifest")
        schema_version = payload["schema_version"]
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != MEMORY_BOUNDARY_SCHEMA_VERSION
        ):
            raise MemoryBoundaryValidationError(
                f"MemoryBoundarySchemaVersionUnsupported:{schema_version!r}"
            )
        raw_segments = payload["segments"]
        if not isinstance(raw_segments, list):
            raise MemoryBoundaryValidationError("MemoryBoundarySegmentsListRequired")
        manifest = cls(
            boundary_id=payload["boundary_id"],
            manifest_revision=_require_positive_revision(payload["manifest_revision"]),
            operation_occurrence_id=payload["operation_occurrence_id"],
            title=payload["title"],
            scope=payload["scope"],
            current_meaning=payload["current_meaning"],
            non_generalization=payload["non_generalization"],
            actor_id=payload["actor_id"],
            consciousness_instance_id=payload["consciousness_instance_id"],
            stream_scope=payload["stream_scope"],
            decision_occurrence_id=payload["decision_occurrence_id"],
            source_occurrence_id=payload["source_occurrence_id"],
            subject_revision=payload["subject_revision"],
            visibility=payload["visibility"],
            segments=tuple(
                MemoryBoundarySegment.from_payload(item)
                if isinstance(item, dict)
                else _invalid_segment_payload()
                for item in raw_segments
            ),
        )
        if manifest.canonical_json != text:
            raise MemoryBoundaryIntegrityError("MemoryBoundaryManifestNotCanonical")
        return manifest


def _invalid_segment_payload() -> MemoryBoundarySegment:
    raise MemoryBoundaryValidationError("MemoryBoundarySegmentObjectRequired")


@dataclass(frozen=True, slots=True)
class MemoryBoundaryReference:
    """An exact URI reference pinned to one artifact and one root hash."""

    boundary_id: str
    artifact_id: str
    root_sha256: str

    def __post_init__(self) -> None:
        memory_boundary_logical_key(self.boundary_id)
        artifact_id = _require_text(
            self.artifact_id,
            "reference.artifact_id",
            identifier=True,
        )
        if _ARTIFACT_ID_RE.fullmatch(artifact_id) is None:
            raise MemoryBoundaryValidationError("MemoryBoundaryArtifactIdInvalid")
        _require_sha256(self.root_sha256, "reference.root_sha256")

    @property
    def uri(self) -> str:
        return (
            f"memory://boundary/{self.boundary_id}@{self.artifact_id}"
            f"#sha256={self.root_sha256}"
        )

    @classmethod
    def parse(cls, uri: str) -> MemoryBoundaryReference:
        """Parse an exact memory URI; unpinned or ambiguous forms are invalid."""

        _require_text(uri, "reference.uri")
        parsed = urlsplit(uri)
        if (
            parsed.scheme != "memory"
            or parsed.netloc != "boundary"
            or parsed.query
            or not parsed.path.startswith("/")
            or not parsed.fragment.startswith("sha256=")
        ):
            raise MemoryBoundaryValidationError("MemoryBoundaryReferenceInvalid")
        raw_target = parsed.path[1:]
        if raw_target.count("@") != 1:
            raise MemoryBoundaryValidationError("MemoryBoundaryReferenceTargetInvalid")
        raw_boundary, raw_artifact = raw_target.split("@", 1)
        reference = cls(
            boundary_id=raw_boundary,
            artifact_id=raw_artifact,
            root_sha256=parsed.fragment.removeprefix("sha256="),
        )
        if reference.uri != uri:
            raise MemoryBoundaryValidationError("MemoryBoundaryReferenceNotCanonical")
        return reference


def memory_boundary_uri(
    boundary_id: str,
    artifact_id: str,
    root_sha256: str,
) -> str:
    """Build an exact URI that cannot silently float to a newer revision."""

    return MemoryBoundaryReference(
        boundary_id=boundary_id,
        artifact_id=artifact_id,
        root_sha256=root_sha256,
    ).uri


@dataclass(frozen=True, slots=True)
class MemoryBoundaryRevisionDescriptor:
    """Content-free history metadata for one verified manifest revision."""

    boundary_id: str
    manifest_revision: int
    artifact_id: str
    root_sha256: str
    byte_length: int
    recorded_at: str
    actor_id: str
    consciousness_instance_id: str
    operation_occurrence_id: str
    decision_occurrence_id: str
    source_occurrence_id: str
    subject_revision: str
    exact_uri: str


@dataclass(frozen=True, slots=True)
class StoredMemoryBoundary:
    """One verified complete manifest together with its immutable artifact."""

    manifest: MemoryBoundaryManifest
    artifact: MemoryArtifactVersion
    head_revision: int
    exact_uri: str
    current_head_revision: int = 0
    is_current: bool = False

    @property
    def descriptor(self) -> MemoryBoundaryRevisionDescriptor:
        manifest = self.manifest
        return MemoryBoundaryRevisionDescriptor(
            boundary_id=manifest.boundary_id,
            manifest_revision=manifest.manifest_revision,
            artifact_id=self.artifact.artifact_id,
            root_sha256=manifest.root_sha256,
            byte_length=len(manifest.canonical_bytes),
            recorded_at=self.artifact.recorded_at,
            actor_id=manifest.actor_id,
            consciousness_instance_id=manifest.consciousness_instance_id,
            operation_occurrence_id=manifest.operation_occurrence_id,
            decision_occurrence_id=manifest.decision_occurrence_id,
            source_occurrence_id=manifest.source_occurrence_id,
            subject_revision=manifest.subject_revision,
            exact_uri=self.exact_uri,
        )


@dataclass(frozen=True, slots=True)
class _BoundaryState:
    head: ArtifactHead | None
    records: tuple[StoredMemoryBoundary, ...]


class MemoryBoundaryRepository:
    """Store complete boundary manifests through an existing LivingMemoryStore."""

    def __init__(self, store: LivingMemoryStore) -> None:
        self._store = store

    async def append(
        self,
        manifest: MemoryBoundaryManifest,
        *,
        expected_head_revision: int,
        recorded_at: str = "",
    ) -> StoredMemoryBoundary:
        """Append one revision with CAS and operation-occurrence idempotency."""

        expected = _normalize_expected_revision(expected_head_revision)
        head, descriptors = await self._read_stable_descriptors(manifest.boundary_id)
        replay = await self._match_operation_descriptor(
            head,
            descriptors,
            manifest,
        )
        if replay is not None:
            return replay
        actual_revision = head.revision if head is not None else 0
        if expected != actual_revision:
            raise ArtifactHeadConflict(
                "memory boundary head revision conflict: "
                f"boundary_id={manifest.boundary_id!r}, "
                f"expected={expected}, actual={actual_revision}"
            )
        required_manifest_revision = actual_revision + 1
        if manifest.manifest_revision != required_manifest_revision:
            raise MemoryBoundaryValidationError(
                "MemoryBoundaryManifestRevisionMismatch:"
                f"expected={required_manifest_revision}:"
                f"actual={manifest.manifest_revision}"
            )
        parent_ids = (head.artifact_id,) if head is not None else ()
        artifact = new_artifact_version(
            logical_key=manifest.logical_key,
            artifact_kind=MEMORY_BOUNDARY_ARTIFACT_KIND,
            content=manifest.canonical_json,
            parent_artifact_ids=parent_ids,
            recorded_at=recorded_at or _now_iso(),
            authored_by=manifest.actor_id,
            consciousness_instance_id=manifest.consciousness_instance_id,
            stream_scope=manifest.stream_scope,
            visibility=manifest.visibility,
            metadata=_artifact_metadata(manifest),
        )
        derivations = _revision_derivations(manifest, artifact, parent_ids)
        try:
            stored = await self._store.append_artifact(
                artifact,
                derivations=derivations,
                expected_head_revision=expected,
            )
        except ArtifactHeadConflict:
            refreshed_head, refreshed_descriptors = await self._read_stable_descriptors(
                manifest.boundary_id
            )
            replay = await self._match_operation_descriptor(
                refreshed_head,
                refreshed_descriptors,
                manifest,
            )
            if replay is not None:
                return replay
            raise
        verified = self._record_from_artifact(
            stored,
            current_head_revision=required_manifest_revision,
            is_current=True,
        )
        if verified.manifest != manifest:
            raise MemoryBoundaryIntegrityError(
                "MemoryBoundaryStoreReturnedDifferentManifest"
            )
        return verified

    async def read_current(
        self,
        boundary_id: str,
    ) -> StoredMemoryBoundary | None:
        """Read the current exact manifest, or ``None`` when never created."""

        head, descriptors = await self._read_stable_descriptors(boundary_id)
        if head is None:
            return None
        descriptor = next(
            (item for item in descriptors if item.artifact_id == head.artifact_id),
            None,
        )
        if descriptor is None:
            raise MemoryBoundaryIntegrityError(
                f"MemoryBoundaryHeadArtifactMissing:{boundary_id}"
            )
        return await self._record_from_descriptor(
            descriptor,
            current_head_revision=head.revision,
            is_current=True,
        )

    async def read_revision(
        self,
        boundary_id: str,
        manifest_revision: int,
    ) -> StoredMemoryBoundary:
        """Read one exact immutable revision without floating to the head."""

        revision = _require_positive_revision(manifest_revision)
        head, descriptors = await self._read_stable_descriptors(boundary_id)
        for descriptor in descriptors:
            if self._descriptor_manifest_revision(descriptor) == revision:
                return await self._record_from_descriptor(
                    descriptor,
                    current_head_revision=head.revision if head is not None else 0,
                    is_current=bool(
                        head is not None and head.artifact_id == descriptor.artifact_id
                    ),
                )
        raise MemoryBoundaryNotFound(
            f"MemoryBoundaryRevisionNotFound:{boundary_id}:{revision}"
        )

    async def read_exact(self, uri: str) -> StoredMemoryBoundary:
        """Resolve an artifact-and-root-pinned URI and verify both pins."""

        reference = MemoryBoundaryReference.parse(uri)
        head, descriptors = await self._read_stable_descriptors(reference.boundary_id)
        for descriptor in descriptors:
            if descriptor.artifact_id != reference.artifact_id:
                continue
            if descriptor.content_hash != reference.root_sha256:
                raise MemoryBoundaryIntegrityError(
                    "MemoryBoundaryReferenceRootMismatch:"
                    f"artifact_id={reference.artifact_id}"
                )
            return await self._record_from_descriptor(
                descriptor,
                current_head_revision=head.revision if head is not None else 0,
                is_current=bool(
                    head is not None and head.artifact_id == descriptor.artifact_id
                ),
            )
        raise MemoryBoundaryNotFound(
            "MemoryBoundaryArtifactNotFound:"
            f"{reference.boundary_id}:{reference.artifact_id}"
        )

    async def history_descriptors(
        self,
        boundary_id: str,
    ) -> tuple[MemoryBoundaryRevisionDescriptor, ...]:
        """Return verified, content-free descriptors in revision order."""

        _, descriptors = await self._read_stable_descriptors(boundary_id)
        return tuple(
            self._revision_descriptor_from_artifact_descriptor(item)
            for item in descriptors
        )

    async def _read_stable_descriptors(
        self,
        boundary_id: str,
    ) -> tuple[ArtifactHead | None, tuple[MemoryArtifactDescriptor, ...]]:
        """Read a coherent head plus content-free lineage; never load all bodies."""

        logical_key = memory_boundary_logical_key(boundary_id)
        for _attempt in range(2):
            head_before = await self._store.get_artifact_head(logical_key)
            descriptors = tuple(
                await self._store.list_artifact_descriptors(logical_key)
            )
            head_after = await self._store.get_artifact_head(logical_key)
            if head_before == head_after:
                self._validate_descriptors(boundary_id, head_after, descriptors)
                return head_after, tuple(
                    sorted(descriptors, key=self._descriptor_manifest_revision)
                )
        raise ArtifactHeadConflict(
            f"memory boundary head changed while reading: {boundary_id!r}"
        )

    @staticmethod
    def _descriptor_manifest_revision(descriptor: MemoryArtifactDescriptor) -> int:
        try:
            return _require_positive_revision(
                descriptor.metadata.get("manifest_revision")
            )
        except MemoryBoundaryValidationError as exc:
            raise MemoryBoundaryIntegrityError(
                f"MemoryBoundaryDescriptorRevisionInvalid:{descriptor.artifact_id}"
            ) from exc

    def _validate_descriptors(
        self,
        boundary_id: str,
        head: ArtifactHead | None,
        descriptors: Sequence[MemoryArtifactDescriptor],
    ) -> None:
        logical_key = memory_boundary_logical_key(boundary_id)
        if head is None:
            if descriptors:
                raise MemoryBoundaryIntegrityError(
                    f"MemoryBoundaryHistoryWithoutHead:{boundary_id}"
                )
            return
        if not descriptors:
            raise MemoryBoundaryIntegrityError(
                f"MemoryBoundaryHeadWithoutHistory:{boundary_id}"
            )
        if any(
            item.logical_key != logical_key
            or item.artifact_kind != MEMORY_BOUNDARY_ARTIFACT_KIND
            or item.metadata.get("boundary_id") != boundary_id
            or item.metadata.get("root_sha256") != item.content_hash
            or item.content_byte_length <= 0
            or item.content_byte_length > MEMORY_BOUNDARY_MAX_BYTES
            for item in descriptors
        ):
            raise MemoryBoundaryIntegrityError(
                f"MemoryBoundaryDescriptorIntegrityInvalid:{boundary_id}"
            )
        ordered = sorted(
            descriptors,
            key=self._descriptor_manifest_revision,
        )
        revisions = [self._descriptor_manifest_revision(item) for item in ordered]
        if revisions != list(range(1, len(ordered) + 1)):
            raise MemoryBoundaryIntegrityError(
                f"MemoryBoundaryRevisionHistoryInvalid:{boundary_id}"
            )
        for index, descriptor in enumerate(ordered):
            expected_parents = () if index == 0 else (ordered[index - 1].artifact_id,)
            if descriptor.parent_artifact_ids != expected_parents:
                raise MemoryBoundaryIntegrityError(
                    "MemoryBoundaryParentHistoryInvalid:"
                    f"artifact_id={descriptor.artifact_id}"
                )
        if head.logical_key != logical_key or head.revision != len(ordered):
            raise MemoryBoundaryIntegrityError(
                f"MemoryBoundaryHeadRevisionMismatch:{boundary_id}"
            )
        if ordered[-1].artifact_id != head.artifact_id:
            raise MemoryBoundaryIntegrityError(
                f"MemoryBoundaryHeadArtifactMismatch:{boundary_id}"
            )
        operation_ids = [
            str(item.metadata.get("operation_occurrence_id") or "") for item in ordered
        ]
        if any(not item for item in operation_ids) or len(operation_ids) != len(
            set(operation_ids)
        ):
            raise MemoryBoundaryIntegrityError(
                f"MemoryBoundaryOperationHistoryDuplicate:{boundary_id}"
            )

    async def _record_from_descriptor(
        self,
        descriptor: MemoryArtifactDescriptor,
        *,
        current_head_revision: int,
        is_current: bool,
    ) -> StoredMemoryBoundary:
        artifact = await self._store.get_artifact_version(descriptor.artifact_id)
        if artifact is None:
            raise MemoryBoundaryIntegrityError(
                f"MemoryBoundaryArtifactBodyMissing:{descriptor.artifact_id}"
            )
        if any(
            (
                artifact.logical_key != descriptor.logical_key,
                artifact.artifact_kind != descriptor.artifact_kind,
                artifact.content_hash != descriptor.content_hash,
                len(artifact.content.encode("utf-8")) != descriptor.content_byte_length,
                artifact.parent_artifact_ids != descriptor.parent_artifact_ids,
                artifact.metadata != descriptor.metadata,
            )
        ):
            raise MemoryBoundaryIntegrityError(
                f"MemoryBoundaryArtifactDescriptorMismatch:{descriptor.artifact_id}"
            )
        return self._record_from_artifact(
            artifact,
            current_head_revision=current_head_revision,
            is_current=is_current,
        )

    async def _match_operation_descriptor(
        self,
        head: ArtifactHead | None,
        descriptors: Sequence[MemoryArtifactDescriptor],
        manifest: MemoryBoundaryManifest,
    ) -> StoredMemoryBoundary | None:
        for descriptor in descriptors:
            if (
                str(descriptor.metadata.get("operation_occurrence_id") or "")
                != manifest.operation_occurrence_id
            ):
                continue
            if descriptor.content_hash != manifest.root_sha256:
                raise MemoryBoundaryOperationConflict(
                    "MemoryBoundaryOperationIdentityConflict:"
                    f"{manifest.operation_occurrence_id}"
                )
            return await self._record_from_descriptor(
                descriptor,
                current_head_revision=head.revision if head is not None else 0,
                is_current=bool(
                    head is not None and head.artifact_id == descriptor.artifact_id
                ),
            )
        return None

    @staticmethod
    def _revision_descriptor_from_artifact_descriptor(
        descriptor: MemoryArtifactDescriptor,
    ) -> MemoryBoundaryRevisionDescriptor:
        metadata = descriptor.metadata
        boundary_id = str(metadata.get("boundary_id") or "")
        root_sha256 = _require_sha256(metadata.get("root_sha256"), "root_sha256")
        return MemoryBoundaryRevisionDescriptor(
            boundary_id=boundary_id,
            manifest_revision=_require_positive_revision(
                metadata.get("manifest_revision")
            ),
            artifact_id=descriptor.artifact_id,
            root_sha256=root_sha256,
            byte_length=descriptor.content_byte_length,
            recorded_at=descriptor.recorded_at,
            actor_id=descriptor.authored_by,
            consciousness_instance_id=descriptor.consciousness_instance_id,
            operation_occurrence_id=str(metadata.get("operation_occurrence_id") or ""),
            decision_occurrence_id=str(metadata.get("decision_occurrence_id") or ""),
            source_occurrence_id=str(metadata.get("source_occurrence_id") or ""),
            subject_revision=_require_sha256(
                metadata.get("subject_revision"),
                "subject_revision",
            ),
            exact_uri=memory_boundary_uri(
                boundary_id,
                descriptor.artifact_id,
                root_sha256,
            ),
        )

    async def _audit_full_history(self, boundary_id: str) -> _BoundaryState:
        """Explicit deep audit path; ordinary read/append never calls this scan."""
        logical_key = memory_boundary_logical_key(boundary_id)
        for _attempt in range(2):
            head_before = await self._store.get_artifact_head(logical_key)
            history = await self._store.list_artifact_history(logical_key)
            head_after = await self._store.get_artifact_head(logical_key)
            if head_before == head_after:
                return self._validate_history(boundary_id, head_after, history)
        raise ArtifactHeadConflict(
            f"memory boundary head changed while reading: {boundary_id!r}"
        )

    def _validate_history(
        self,
        boundary_id: str,
        head: ArtifactHead | None,
        history: Sequence[MemoryArtifactVersion],
    ) -> _BoundaryState:
        if head is None:
            if history:
                raise MemoryBoundaryIntegrityError(
                    f"MemoryBoundaryHistoryWithoutHead:{boundary_id}"
                )
            return _BoundaryState(head=None, records=())
        if not history:
            raise MemoryBoundaryIntegrityError(
                f"MemoryBoundaryHeadWithoutHistory:{boundary_id}"
            )
        parsed = [
            self._record_from_artifact(
                artifact,
                current_head_revision=head.revision,
                is_current=(artifact.artifact_id == head.artifact_id),
            )
            for artifact in history
        ]
        parsed.sort(key=lambda item: item.manifest.manifest_revision)
        expected_revisions = list(range(1, len(parsed) + 1))
        actual_revisions = [item.manifest.manifest_revision for item in parsed]
        if actual_revisions != expected_revisions:
            raise MemoryBoundaryIntegrityError(
                "MemoryBoundaryRevisionHistoryInvalid:"
                f"expected={expected_revisions!r}:actual={actual_revisions!r}"
            )
        for index, record in enumerate(parsed):
            expected_parents = (
                () if index == 0 else (parsed[index - 1].artifact.artifact_id,)
            )
            if record.artifact.parent_artifact_ids != expected_parents:
                raise MemoryBoundaryIntegrityError(
                    "MemoryBoundaryParentHistoryInvalid:"
                    f"artifact_id={record.artifact.artifact_id}"
                )
        if head.logical_key != memory_boundary_logical_key(boundary_id):
            raise MemoryBoundaryIntegrityError(
                f"MemoryBoundaryHeadLogicalKeyMismatch:{boundary_id}"
            )
        if head.revision != len(parsed):
            raise MemoryBoundaryIntegrityError(
                "MemoryBoundaryHeadRevisionMismatch:"
                f"head={head.revision}:history={len(parsed)}"
            )
        if parsed[-1].artifact.artifact_id != head.artifact_id:
            raise MemoryBoundaryIntegrityError(
                f"MemoryBoundaryHeadArtifactMismatch:{boundary_id}"
            )
        operation_ids = [item.manifest.operation_occurrence_id for item in parsed]
        if len(operation_ids) != len(set(operation_ids)):
            raise MemoryBoundaryIntegrityError(
                f"MemoryBoundaryOperationHistoryDuplicate:{boundary_id}"
            )
        records = tuple(
            StoredMemoryBoundary(
                manifest=item.manifest,
                artifact=item.artifact,
                head_revision=item.manifest.manifest_revision,
                exact_uri=item.exact_uri,
            )
            for item in parsed
        )
        return _BoundaryState(head=head, records=records)

    def _record_from_artifact(
        self,
        artifact: MemoryArtifactVersion,
        *,
        current_head_revision: int,
        is_current: bool,
    ) -> StoredMemoryBoundary:
        if artifact.artifact_kind != MEMORY_BOUNDARY_ARTIFACT_KIND:
            raise MemoryBoundaryIntegrityError(
                f"MemoryBoundaryArtifactKindMismatch:{artifact.artifact_id}"
            )
        content_bytes = artifact.content.encode("utf-8")
        content_hash = _sha256_bytes(content_bytes)
        if content_hash != artifact.content_hash:
            raise MemoryBoundaryIntegrityError(
                f"MemoryBoundaryArtifactContentHashMismatch:{artifact.artifact_id}"
            )
        try:
            manifest = MemoryBoundaryManifest.from_canonical_json(artifact.content)
        except MemoryBoundaryValidationError as exc:
            raise MemoryBoundaryIntegrityError(
                f"MemoryBoundaryArtifactManifestInvalid:{artifact.artifact_id}"
            ) from exc
        if artifact.logical_key != manifest.logical_key:
            raise MemoryBoundaryIntegrityError(
                f"MemoryBoundaryArtifactLogicalKeyMismatch:{artifact.artifact_id}"
            )
        if manifest.root_sha256 != content_hash:
            raise MemoryBoundaryIntegrityError(
                f"MemoryBoundaryArtifactRootMismatch:{artifact.artifact_id}"
            )
        if artifact.authored_by != manifest.actor_id:
            raise MemoryBoundaryIntegrityError(
                f"MemoryBoundaryArtifactActorMismatch:{artifact.artifact_id}"
            )
        if artifact.consciousness_instance_id != manifest.consciousness_instance_id:
            raise MemoryBoundaryIntegrityError(
                f"MemoryBoundaryArtifactConsciousnessMismatch:{artifact.artifact_id}"
            )
        if artifact.stream_scope != manifest.stream_scope:
            raise MemoryBoundaryIntegrityError(
                f"MemoryBoundaryArtifactScopeMismatch:{artifact.artifact_id}"
            )
        if artifact.visibility != manifest.visibility:
            raise MemoryBoundaryIntegrityError(
                f"MemoryBoundaryArtifactVisibilityMismatch:{artifact.artifact_id}"
            )
        expected_metadata = _artifact_metadata(manifest)
        if any(
            artifact.metadata.get(key) != value
            for key, value in expected_metadata.items()
        ):
            raise MemoryBoundaryIntegrityError(
                f"MemoryBoundaryArtifactMetadataMismatch:{artifact.artifact_id}"
            )
        return StoredMemoryBoundary(
            manifest=manifest,
            artifact=artifact,
            head_revision=manifest.manifest_revision,
            exact_uri=memory_boundary_uri(
                manifest.boundary_id,
                artifact.artifact_id,
                manifest.root_sha256,
            ),
            current_head_revision=current_head_revision,
            is_current=is_current,
        )

    @staticmethod
    def _match_operation(
        state: _BoundaryState,
        manifest: MemoryBoundaryManifest,
    ) -> StoredMemoryBoundary | None:
        for record in state.records:
            if (
                record.manifest.operation_occurrence_id
                != manifest.operation_occurrence_id
            ):
                continue
            if record.manifest.root_sha256 != manifest.root_sha256:
                raise MemoryBoundaryOperationConflict(
                    "MemoryBoundaryOperationIdentityConflict:"
                    f"{manifest.operation_occurrence_id}"
                )
            return record
        return None


def _normalize_expected_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MemoryBoundaryValidationError("MemoryBoundaryExpectedHeadRevisionInvalid")
    return value


def _artifact_metadata(manifest: MemoryBoundaryManifest) -> dict[str, Any]:
    return {
        "schema_version": MEMORY_BOUNDARY_SCHEMA_VERSION,
        "boundary_id": manifest.boundary_id,
        "manifest_revision": manifest.manifest_revision,
        "root_sha256": manifest.root_sha256,
        "operation_occurrence_id": manifest.operation_occurrence_id,
        "decision_occurrence_id": manifest.decision_occurrence_id,
        "source_occurrence_id": manifest.source_occurrence_id,
        "subject_revision": manifest.subject_revision,
    }


def _revision_derivations(
    manifest: MemoryBoundaryManifest,
    artifact: MemoryArtifactVersion,
    parent_ids: tuple[str, ...],
) -> tuple[MemoryDerivation, ...]:
    if not parent_ids:
        return ()
    parent_id = parent_ids[0]
    identity = _sha256_text(
        artifact.artifact_id
        + "\0"
        + parent_id
        + "\0"
        + manifest.operation_occurrence_id
    )
    return (
        MemoryDerivation(
            derivation_id=f"memory_boundary_derivation_{identity}",
            generated_artifact_id=artifact.artifact_id,
            used_artifact_id=parent_id,
            predicate="revises_memory_boundary_manifest",
            reason="technical version lineage for an accepted boundary manifest",
            actor=manifest.actor_id,
            recorded_at=artifact.recorded_at,
            metadata={
                "operation_occurrence_id": manifest.operation_occurrence_id,
                "decision_occurrence_id": manifest.decision_occurrence_id,
            },
        ),
    )


__all__ = [
    "MEMORY_BOUNDARY_ARTIFACT_KIND",
    "MEMORY_BOUNDARY_LOGICAL_KEY_PREFIX",
    "MEMORY_BOUNDARY_MAX_BYTES",
    "MEMORY_BOUNDARY_SCHEMA_VERSION",
    "MemoryBoundaryBundleTooLarge",
    "MemoryBoundaryError",
    "MemoryBoundaryIntegrityError",
    "MemoryBoundaryManifest",
    "MemoryBoundaryNotFound",
    "MemoryBoundaryOperationConflict",
    "MemoryBoundaryReference",
    "MemoryBoundaryRepository",
    "MemoryBoundaryRevisionDescriptor",
    "MemoryBoundarySegment",
    "MemoryBoundaryStaleOperationReplay",
    "MemoryBoundaryValidationError",
    "StoredMemoryBoundary",
    "memory_boundary_logical_key",
    "memory_boundary_uri",
]
