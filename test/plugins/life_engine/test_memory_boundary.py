"""Contracts for immutable, complete long-term memory boundaries."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest

from plugins.life_engine.memory.boundary import (
    MEMORY_BOUNDARY_ARTIFACT_KIND,
    MEMORY_BOUNDARY_LOGICAL_KEY_PREFIX,
    MEMORY_BOUNDARY_MAX_BYTES,
    MEMORY_BOUNDARY_MAX_SEGMENTS,
    MEMORY_BOUNDARY_MAX_SOURCE_LABELS,
    MemoryBoundaryBundleTooLarge,
    MemoryBoundaryIntegrityError,
    MemoryBoundaryManifest,
    MemoryBoundaryNotFound,
    MemoryBoundaryOperationConflict,
    MemoryBoundaryReference,
    MemoryBoundaryRepository,
    MemoryBoundarySegment,
    MemoryBoundaryValidationError,
)
from plugins.life_engine.memory.living import (
    ArtifactHead,
    ArtifactHeadConflict,
    MemoryArtifactDescriptor,
    MemoryArtifactVersion,
    MemoryDerivation,
)


class FakeLivingMemoryStore:
    """Minimal faithful fake for the living artifact contract."""

    def __init__(self) -> None:
        self.artifacts: dict[str, MemoryArtifactVersion] = {}
        self.history: dict[str, list[MemoryArtifactVersion]] = defaultdict(list)
        self.heads: dict[str, ArtifactHead] = {}
        self.derivations: list[MemoryDerivation] = []
        self.full_history_reads = 0

    async def append_artifact(
        self,
        version: MemoryArtifactVersion,
        *,
        derivations: Sequence[MemoryDerivation] = (),
        expected_head_revision: int,
    ) -> MemoryArtifactVersion:
        head = self.heads.get(version.logical_key)
        current_revision = head.revision if head is not None else 0
        if current_revision != expected_head_revision:
            raise ArtifactHeadConflict(
                f"expected={expected_head_revision}, actual={current_revision}"
            )
        existing = self.artifacts.get(version.artifact_id)
        if existing is not None and existing != version:
            raise ValueError(f"ArtifactIdentityConflict:{version.artifact_id}")
        if existing is None:
            for parent_id in version.parent_artifact_ids:
                if parent_id not in self.artifacts:
                    raise ValueError(f"ArtifactParentMissing:{parent_id}")
            self.artifacts[version.artifact_id] = version
            self.history[version.logical_key].append(version)
            self.derivations.extend(derivations)
        if head is not None and head.artifact_id == version.artifact_id:
            return version
        self.heads[version.logical_key] = ArtifactHead(
            logical_key=version.logical_key,
            artifact_id=version.artifact_id,
            projected_at=version.recorded_at,
            revision=current_revision + 1,
        )
        return version

    async def get_artifact_head(self, logical_key: str) -> ArtifactHead | None:
        return self.heads.get(logical_key)

    async def get_artifact_version(
        self,
        artifact_id: str,
    ) -> MemoryArtifactVersion | None:
        return self.artifacts.get(artifact_id)

    async def list_artifact_descriptors(
        self,
        logical_key: str,
    ) -> list[MemoryArtifactDescriptor]:
        return [
            MemoryArtifactDescriptor(
                artifact_id=item.artifact_id,
                logical_key=item.logical_key,
                artifact_kind=item.artifact_kind,
                content_hash=item.content_hash,
                content_byte_length=len(item.content.encode("utf-8")),
                recorded_at=item.recorded_at,
                authored_by=item.authored_by,
                consciousness_instance_id=item.consciousness_instance_id,
                stream_scope=item.stream_scope,
                visibility=item.visibility,
                parent_artifact_ids=item.parent_artifact_ids,
                metadata=dict(item.metadata),
            )
            for item in self.history.get(logical_key, ())
        ]

    async def list_artifact_history(
        self,
        logical_key: str,
    ) -> list[MemoryArtifactVersion]:
        self.full_history_reads += 1
        return list(self.history.get(logical_key, ()))

    def replace_artifact(self, artifact: MemoryArtifactVersion) -> None:
        old = self.artifacts[artifact.artifact_id]
        items = self.history[old.logical_key]
        index = next(
            index
            for index, item in enumerate(items)
            if item.artifact_id == artifact.artifact_id
        )
        items[index] = artifact
        self.artifacts[artifact.artifact_id] = artifact


def _segment(
    *,
    segment_id: str = "segment-origin",
    content: str = "那天的完整内容：她停下来，认真确认了彼此的边界。",
    source_refs: Sequence[str] = ("experience:event-001",),
    source_occurrence_ids: Sequence[str] = ("occurrence-event-001",),
) -> MemoryBoundarySegment:
    return MemoryBoundarySegment.create(
        segment_id=segment_id,
        title="事情发生时",
        content=content,
        source_refs=source_refs,
        source_occurrence_ids=source_occurrence_ids,
        scope="这次经历本身",
        visibility="private",
    )


def _manifest(
    *,
    revision: int = 1,
    operation: str = "boundary-operation-001",
    current_meaning: str = "这段经历提醒我在解释之前先确认。",
    segments: Sequence[MemoryBoundarySegment] | None = None,
) -> MemoryBoundaryManifest:
    return MemoryBoundaryManifest(
        boundary_id="relationship-boundary-001",
        manifest_revision=revision,
        operation_occurrence_id=operation,
        title="一次改变理解方式的谈话",
        scope="这段谈话以及由她本人确认的后续理解",
        current_meaning=current_meaning,
        non_generalization="它不能被泛化成对任何人的永久判断。",
        actor_id="elysia",
        consciousness_instance_id="chat_global",
        stream_scope="chat:global",
        decision_occurrence_id=f"decision-{operation}",
        source_occurrence_id="review-source-001",
        subject_revision=hashlib.sha256(
            f"subject-revision-{revision}".encode()
        ).hexdigest(),
        segments=tuple(segments) if segments is not None else (_segment(),),
        visibility="private",
    )


def test_manifest_canonical_json_and_root_are_stable() -> None:
    manifest = _manifest()
    restored = MemoryBoundaryManifest.from_canonical_json(manifest.canonical_bytes)

    assert restored == manifest
    assert restored.canonical_json == manifest.canonical_json
    assert restored.root_sha256 == hashlib.sha256(manifest.canonical_bytes).hexdigest()
    assert json.loads(manifest.canonical_json)["schema_version"] == 1
    assert "importance" not in manifest.canonical_json
    assert "score" not in manifest.canonical_json
    assert "category" not in manifest.canonical_json


def test_boundary_identity_and_subject_revision_are_canonical() -> None:
    with pytest.raises(MemoryBoundaryValidationError):
        replace(_manifest(), boundary_id="含空格的边界")
    with pytest.raises(MemoryBoundaryValidationError):
        replace(_manifest(), subject_revision="not-a-unified-revision")


def test_reference_rejects_noncanonical_percent_encoded_identity() -> None:
    manifest = _manifest()
    reference = MemoryBoundaryReference(
        boundary_id=manifest.boundary_id,
        artifact_id="artifact_" + "a" * 64,
        root_sha256=manifest.root_sha256,
    )
    assert MemoryBoundaryReference.parse(reference.uri) == reference
    with pytest.raises(MemoryBoundaryValidationError):
        MemoryBoundaryReference.parse(reference.uri.replace("-", "%2D", 1))


def test_chinese_segment_uses_utf8_bytes_and_is_never_truncated() -> None:
    content = "记忆边界不是摘要。🌸\n细节必须完整保留。"
    segment = _segment(content=content)
    manifest = _manifest(segments=(segment,))

    assert segment.byte_length == len(content.encode("utf-8"))
    assert segment.content_sha256 == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert (
        MemoryBoundaryManifest.from_canonical_json(manifest.canonical_json)
        .segments[0]
        .content
        == content
    )


def test_duplicate_segments_and_missing_sources_fail_closed() -> None:
    first = _segment()
    duplicate = _segment(content="另一段完整内容")
    with pytest.raises(
        MemoryBoundaryValidationError,
        match="MemoryBoundarySegmentIdDuplicate",
    ):
        _manifest(segments=(first, duplicate))

    with pytest.raises(
        MemoryBoundaryValidationError,
        match="MemoryBoundarySourceRequired:segment.source_refs",
    ):
        _segment(source_refs=())
    with pytest.raises(
        MemoryBoundaryValidationError,
        match="MemoryBoundarySourceRequired:segment.source_occurrence_ids",
    ):
        _segment(source_occurrence_ids=())
    with pytest.raises(
        MemoryBoundaryValidationError,
        match="MemoryBoundaryDuplicateValue:segment.source_refs",
    ):
        _segment(source_refs=("experience:one", "experience:one"))


def test_segment_rejects_mismatched_hash_and_byte_length() -> None:
    valid = _segment(content="中文")
    with pytest.raises(
        MemoryBoundaryValidationError,
        match="MemoryBoundarySegmentContentHashMismatch",
    ):
        replace(valid, content_sha256="0" * 64)
    with pytest.raises(
        MemoryBoundaryValidationError,
        match="MemoryBoundarySegmentByteLengthMismatch",
    ):
        replace(valid, byte_length=len(valid.content))

    with pytest.raises(
        MemoryBoundaryValidationError,
        match="MemoryBoundarySegmentIdInvalid",
    ):
        replace(valid, segment_id="segment#unsafe")


def test_noncanonical_or_semantically_extended_payload_is_rejected() -> None:
    manifest = _manifest()
    pretty = json.dumps(manifest.to_payload(), ensure_ascii=False, indent=2)
    with pytest.raises(
        MemoryBoundaryIntegrityError,
        match="MemoryBoundaryManifestNotCanonical",
    ):
        MemoryBoundaryManifest.from_canonical_json(pretty)

    extended: dict[str, Any] = manifest.to_payload()
    extended["importance"] = 0.99
    with pytest.raises(
        MemoryBoundaryValidationError,
        match="MemoryBoundarySchemaMismatch:manifest",
    ):
        MemoryBoundaryManifest.from_canonical_json(
            json.dumps(
                extended,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )


def test_complete_bundle_over_technical_cap_requires_split() -> None:
    content = "忆" * (MEMORY_BOUNDARY_MAX_BYTES // 3 + 1)
    with pytest.raises(
        MemoryBoundaryBundleTooLarge,
        match="split_into_multiple_boundaries_required",
    ):
        _manifest(segments=(_segment(content=content),))


def test_backend_identity_and_collection_bounds_fail_closed() -> None:
    with pytest.raises(
        MemoryBoundaryValidationError,
        match="MemoryBoundaryFieldTooLong:actor_id",
    ):
        replace(_manifest(), actor_id="a" * 513)

    with pytest.raises(
        MemoryBoundaryValidationError,
        match=r"MemoryBoundaryFieldTooLong:segment.source_refs\[0\]",
    ):
        _segment(source_refs=("s" * 1025,))

    with pytest.raises(
        MemoryBoundaryValidationError,
        match="MemoryBoundarySequenceTooLong:segment.source_refs",
    ):
        _segment(
            source_refs=tuple(
                f"source:{index}"
                for index in range(MEMORY_BOUNDARY_MAX_SOURCE_LABELS + 1)
            )
        )

    with pytest.raises(
        MemoryBoundaryValidationError,
        match="MemoryBoundarySegmentsTooMany",
    ):
        _manifest(
            segments=tuple(
                _segment() for _ in range(MEMORY_BOUNDARY_MAX_SEGMENTS + 1)
            )
        )


async def test_repository_appends_with_cas_and_preserves_history() -> None:
    store = FakeLivingMemoryStore()
    repository = MemoryBoundaryRepository(store)
    first = _manifest()
    first_record = await repository.append(
        first,
        expected_head_revision=0,
        recorded_at="2026-08-10T10:00:00+08:00",
    )
    second = _manifest(
        revision=2,
        operation="boundary-operation-002",
        current_meaning="现在我也会保留当时尚未理解的部分。",
    )
    second_record = await repository.append(
        second,
        expected_head_revision=1,
        recorded_at="2026-08-10T11:00:00+08:00",
    )

    assert first_record.artifact.artifact_kind == MEMORY_BOUNDARY_ARTIFACT_KIND
    assert first_record.artifact.logical_key.startswith(
        MEMORY_BOUNDARY_LOGICAL_KEY_PREFIX
    )
    assert first_record.artifact.content == first.canonical_json
    assert first_record.artifact.stream_scope == "chat:global"
    assert first_record.artifact.stream_scope != first.scope
    assert second_record.artifact.parent_artifact_ids == (
        first_record.artifact.artifact_id,
    )
    assert len(store.derivations) == 1
    assert store.derivations[0].used_artifact_id == first_record.artifact.artifact_id
    assert (await repository.read_current(first.boundary_id)).manifest == second
    assert (await repository.read_revision(first.boundary_id, 1)).manifest == first
    descriptors = await repository.history_descriptors(first.boundary_id)
    assert [item.manifest_revision for item in descriptors] == [1, 2]
    assert [item.root_sha256 for item in descriptors] == [
        first.root_sha256,
        second.root_sha256,
    ]
    assert descriptors[0].exact_uri == first_record.exact_uri

    stale = _manifest(revision=3, operation="boundary-operation-003")
    with pytest.raises(ArtifactHeadConflict, match="expected=0, actual=2"):
        await repository.append(stale, expected_head_revision=0)


async def test_same_operation_and_same_content_is_idempotent() -> None:
    store = FakeLivingMemoryStore()
    repository = MemoryBoundaryRepository(store)
    manifest = _manifest()

    first = await repository.append(
        manifest,
        expected_head_revision=0,
        recorded_at="2026-08-10T10:00:00+08:00",
    )
    replay = await repository.append(
        manifest,
        expected_head_revision=0,
        recorded_at="2026-08-10T12:00:00+08:00",
    )

    assert replay == first
    assert len(store.history[manifest.logical_key]) == 1
    assert store.heads[manifest.logical_key].revision == 1


async def test_same_operation_with_different_content_conflicts() -> None:
    store = FakeLivingMemoryStore()
    repository = MemoryBoundaryRepository(store)
    original = _manifest()
    await repository.append(original, expected_head_revision=0)
    conflicting = _manifest(
        operation=original.operation_occurrence_id,
        current_meaning="同一个操作身份却带来了不同内容。",
    )

    with pytest.raises(
        MemoryBoundaryOperationConflict,
        match="MemoryBoundaryOperationIdentityConflict",
    ):
        await repository.append(conflicting, expected_head_revision=1)
    assert (await repository.read_current(original.boundary_id)).manifest == original


async def test_old_operation_replay_after_new_head_is_exactly_idempotent() -> None:
    store = FakeLivingMemoryStore()
    repository = MemoryBoundaryRepository(store)
    first = _manifest()
    first_receipt = await repository.append(first, expected_head_revision=0)
    second_receipt = await repository.append(
        _manifest(revision=2, operation="boundary-operation-002"),
        expected_head_revision=1,
    )

    replay = await repository.append(first, expected_head_revision=0)

    assert replay.artifact == first_receipt.artifact
    assert replay.is_current is False
    assert replay.current_head_revision == 2
    assert (await repository.read_current(first.boundary_id)) == second_receipt


async def test_exact_uri_pins_artifact_and_root() -> None:
    store = FakeLivingMemoryStore()
    repository = MemoryBoundaryRepository(store)
    stored = await repository.append(_manifest(), expected_head_revision=0)

    reference = MemoryBoundaryReference.parse(stored.exact_uri)
    assert reference.boundary_id == stored.manifest.boundary_id
    assert reference.artifact_id == stored.artifact.artifact_id
    assert reference.root_sha256 == stored.manifest.root_sha256
    assert await repository.read_exact(stored.exact_uri) == stored
    assert store.full_history_reads == 0

    changed_last = "0" if reference.root_sha256[-1] != "0" else "1"
    tampered_root = reference.root_sha256[:-1] + changed_last
    with pytest.raises(
        MemoryBoundaryIntegrityError,
        match="MemoryBoundaryReferenceRootMismatch",
    ):
        await repository.read_exact(replace(reference, root_sha256=tampered_root).uri)
    with pytest.raises(
        MemoryBoundaryValidationError,
        match="MemoryBoundaryReferenceInvalid",
    ):
        MemoryBoundaryReference.parse(
            f"memory://boundary/{reference.boundary_id}@{reference.artifact_id}"
        )


async def test_tampered_persisted_artifact_fails_closed() -> None:
    store = FakeLivingMemoryStore()
    repository = MemoryBoundaryRepository(store)
    stored = await repository.append(_manifest(), expected_head_revision=0)
    store.replace_artifact(
        replace(stored.artifact, content=stored.artifact.content + " ")
    )

    with pytest.raises(
        MemoryBoundaryIntegrityError,
        match="MemoryBoundaryArtifactContentHashMismatch",
    ):
        await repository.read_current(stored.manifest.boundary_id)


async def test_exact_revision_and_unknown_reference_never_float_to_head() -> None:
    store = FakeLivingMemoryStore()
    repository = MemoryBoundaryRepository(store)
    first = await repository.append(_manifest(), expected_head_revision=0)
    second = await repository.append(
        _manifest(revision=2, operation="boundary-operation-002"),
        expected_head_revision=1,
    )

    assert (
        await repository.read_exact(first.exact_uri)
    ).manifest.manifest_revision == 1
    assert (await repository.read_current(first.manifest.boundary_id)) == second
    with pytest.raises(MemoryBoundaryNotFound, match="RevisionNotFound"):
        await repository.read_revision(first.manifest.boundary_id, 3)
    unknown = replace(
        MemoryBoundaryReference.parse(first.exact_uri),
        artifact_id="artifact_missing",
    )
    with pytest.raises(MemoryBoundaryNotFound, match="ArtifactNotFound"):
        await repository.read_exact(unknown.uri)
