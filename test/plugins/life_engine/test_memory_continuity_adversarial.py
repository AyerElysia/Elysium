"""Content-neutral adversarial contracts for continuity-memory convergence."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any

import pytest

from plugins.life_engine.learning.decisions import LearningCandidate
from plugins.life_engine.memory.boundary import (
    MemoryBoundaryIntegrityError,
    MemoryBoundaryManifest,
    MemoryBoundaryNotFound,
    MemoryBoundaryReference,
    MemoryBoundaryRepository,
    MemoryBoundarySegment,
    MemoryBoundaryValidationError,
)
from plugins.life_engine.memory.continuity_delivery import (
    ContinuityCandidateDeliveryCoordinator,
)
from plugins.life_engine.memory.continuity_health import (
    collect_continuity_memory_health,
)
from plugins.life_engine.memory.continuity_index import (
    DuplicateContinuityMemoryEntry,
    MalformedContinuityMemoryReference,
    diagnose_continuity_memory_index,
    parse_continuity_memory_index,
)
from plugins.life_engine.memory.continuity_session import (
    CONTINUITY_REVIEW_MAX_PAGE_BYTES,
    CandidateDeliveryReceipt,
    ContinuityReviewDeliveryUnverified,
    ContinuityReviewInputError,
    _exact_page,
)
from plugins.life_engine.memory.service import LifeMemoryService
from plugins.life_engine.storage.subject_contracts import (
    SUBJECT_AUTHORITY_PATHS,
    SubjectAuthoritySnapshot,
    SubjectDocumentCommit,
    SubjectDocumentHead,
    SubjectDocumentVersion,
    subject_authority_logical_path,
    subject_revision_from_contents,
)
from src.kernel.llm.context_delivery import EffectiveContextReceipt
from src.kernel.llm.payload import ToolResult

SUBJECT_REVISION = "a" * 64
BOUNDARY_ROOT = "b" * 64


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _SubjectStore:
    def __init__(self, contents: dict[str, bytes]) -> None:
        revision = subject_revision_from_contents(contents)  # type: ignore[arg-type]
        commits: dict[str, SubjectDocumentCommit] = {}
        for index, path in enumerate(SUBJECT_AUTHORITY_PATHS, start=1):
            content = contents[path]
            version_id = f"subject-version-{index}"
            logical_path = subject_authority_logical_path(path)
            commits[path] = SubjectDocumentCommit(
                version=SubjectDocumentVersion(
                    version_id=version_id,
                    document_id=f"subject-document-{index}",
                    logical_path=logical_path,
                    parent_version_id="",
                    occurrence_id=f"subject-occurrence-{index}",
                    semantic_actor_id="subject-test-actor",
                    semantic_source_id="continuity-adversarial-test",
                    occurred_at="2026-08-13T00:00:00+00:00",
                    recorded_by="test",
                    recorded_source="test",
                    recorded_at="2026-08-13T00:00:00+00:00",
                    provenance_status="complete",
                    content_bytes=content,
                    content_hash=hashlib.sha256(content).hexdigest(),
                    byte_length=len(content),
                    byte_fidelity="exact_bytes",
                    encoding="utf-8",
                    newline_style="LF",
                    change_context={},
                ),
                head=SubjectDocumentHead(
                    document_id=f"subject-document-{index}",
                    logical_path=logical_path,
                    declared_owner="subject-test-actor",
                    current_version_id=version_id,
                    revision=1,
                ),
            )
        self._snapshot = SubjectAuthoritySnapshot(  # type: ignore[arg-type]
            commits=commits,
            revision=revision,
        )

    async def read_subject_authority(self) -> SubjectAuthoritySnapshot:
        return self._snapshot


def _boundary_link(
    entry_id: str,
    *,
    artifact_id: str = "artifact-version-one",
    root_sha256: str = BOUNDARY_ROOT,
    anchor: str = "boundary anchor",
) -> str:
    return (
        f"[{anchor}](memory://boundary/{entry_id}@{artifact_id}"
        f"#sha256={root_sha256})"
    )


def _manifest() -> MemoryBoundaryManifest:
    private_body = "private boundary body with multibyte marker: 花"
    segment = MemoryBoundarySegment.create(
        segment_id="segment-adversarial",
        title="source segment",
        content=private_body,
        source_refs=("experience:adversarial",),
        source_occurrence_ids=("occurrence-adversarial",),
        scope="exact source only",
        visibility="private",
    )
    return MemoryBoundaryManifest(
        boundary_id="boundary-adversarial",
        manifest_revision=1,
        operation_occurrence_id="operation-adversarial",
        title="adversarial boundary",
        scope="exact boundary verification",
        current_meaning="meaning remains subject-authored",
        non_generalization="infrastructure does not generalize this material",
        actor_id="subject-test-actor",
        consciousness_instance_id="consciousness-adversarial",
        stream_scope="test:adversarial",
        decision_occurrence_id="decision-adversarial",
        source_occurrence_id="source-adversarial",
        subject_revision=SUBJECT_REVISION,
        segments=(segment,),
        visibility="private",
    )


def _candidate(content: bytes, *, revision: int = 7) -> LearningCandidate:
    return LearningCandidate.create(
        candidate_id="candidate-adversarial",
        candidate_revision=revision,
        candidate_kind="continuity_memory_review",
        candidate_occurrence_id=f"candidate-occurrence:{revision}",
        candidate_content_bytes=content,
        source_occurrence_id="source-occurrence",
        source="continuity-review",
        actor_consciousness_instance_id="consciousness-adversarial",
        subject_revision=SUBJECT_REVISION,
        target_path="MEMORY.md",
        occurred_at="2026-08-13T00:00:00+00:00",
    )


def _candidate_page(
    candidate: LearningCandidate,
    *,
    delivery_id: str,
    offset: int,
    end: int,
) -> dict[str, Any]:
    content = candidate.candidate_content_bytes
    delivered = bytes(content[offset:end])
    text = delivered.decode("utf-8")
    page_sha256 = hashlib.sha256(delivered).hexdigest()
    next_offset = end if end < len(content) else None
    return {
        "action": "candidate_read",
        "session_id": "continuity-review-" + "c" * 32,
        "candidate_id": candidate.candidate_id,
        "candidate_revision": candidate.candidate_revision,
        "candidate_sha256": candidate.candidate_sha256,
        "subject_revision": candidate.subject_revision,
        "page": {
            "offset": offset,
            "next_offset": next_offset,
            "delivered_bytes": len(delivered),
            "total_bytes": len(content),
            "page_sha256": page_sha256,
            "text": text,
        },
        "delivery_binding": {
            "delivery_id": delivery_id,
            "candidate_id": candidate.candidate_id,
            "candidate_revision": candidate.candidate_revision,
            "candidate_sha256": candidate.candidate_sha256,
            "page_offset": offset,
            "page_sha256": page_sha256,
            "page_bytes": len(delivered),
            "total_bytes": len(content),
        },
        "delivery_binding_is_not_receipt": True,
        "authority_written": False,
    }


def _register_page(
    coordinator: ContinuityCandidateDeliveryCoordinator,
    payload: dict[str, Any],
):
    result = ToolResult(
        value=payload,
        call_id="call-adversarial",
        name="tool-nucleus_review_memory_continuity",
    )
    return coordinator.register_pending_tool_result(payload, result.to_text())


def _effective_receipt(expectation) -> EffectiveContextReceipt:
    return EffectiveContextReceipt(
        delivery_id=expectation.delivery_id,
        exact_present=True,
        expected_utf8_bytes=expectation.expected_utf8_bytes,
        expected_sha256=expectation.expected_sha256,
        effective_utf8_bytes=expectation.expected_utf8_bytes,
        effective_sha256=expectation.expected_sha256,
        part_kind="tool_result",
    )


def _claim(
    candidate: LearningCandidate,
    delivery_id: str,
    **overrides: Any,
) -> CandidateDeliveryReceipt:
    values: dict[str, Any] = {
        "delivery_id": delivery_id,
        "candidate_id": candidate.candidate_id,
        "candidate_revision": candidate.candidate_revision,
        "candidate_sha256": candidate.candidate_sha256,
        "delivered_bytes": len(candidate.candidate_content_bytes),
        "total_bytes": len(candidate.candidate_content_bytes),
    }
    values.update(overrides)
    return CandidateDeliveryReceipt(**values)


def _utf8_page_ranges(content: bytes) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    offset = 0
    while offset < len(content):
        end = min(len(content), offset + CONTINUITY_REVIEW_MAX_PAGE_BYTES)
        while end > offset:
            try:
                content[offset:end].decode("utf-8")
            except UnicodeDecodeError:
                end -= 1
            else:
                break
        assert end > offset
        ranges.append((offset, end))
        offset = end
    return tuple(ranges)


def test_malformed_duplicate_markdown_reports_exact_utf8_offsets_without_body() -> None:
    prefix = "花✨ prefix\n"
    malformed = (
        "[unsafe](memory://boundary/%2e%2e@artifact-one#sha256="
        + BOUNDARY_ROOT
        + ")"
    )
    exact = (prefix + malformed).encode("utf-8")

    diagnostics = diagnose_continuity_memory_index(
        exact,
        subject_document_version_id="subject-memory-version",
        unified_subject_revision=SUBJECT_REVISION,
    )

    assert diagnostics.index.entries == ()
    assert len(diagnostics.issues) == 1
    assert diagnostics.issues[0].byte_offset == len(
        (prefix + "[unsafe](").encode("utf-8")
    )
    assert diagnostics.issues[0].error_type == "invalid_boundary_uri"
    assert malformed not in repr(diagnostics.issues)
    with pytest.raises(MalformedContinuityMemoryReference):
        parse_continuity_memory_index(
            exact,
            subject_document_version_id="subject-memory-version",
            unified_subject_revision=SUBJECT_REVISION,
        )

    duplicate = "\n".join(
        (
            _boundary_link("same-entry", artifact_id="artifact-one"),
            _boundary_link(
                "same-entry",
                artifact_id="artifact-two",
                root_sha256="c" * 64,
            ),
        )
    ).encode()
    with pytest.raises(DuplicateContinuityMemoryEntry, match="same-entry"):
        parse_continuity_memory_index(
            duplicate,
            subject_document_version_id="subject-memory-version",
            unified_subject_revision=SUBJECT_REVISION,
        )


def test_boundary_uri_rejects_ambiguous_and_hash_tampered_references() -> None:
    reference = MemoryBoundaryReference(
        boundary_id="boundary-adversarial",
        artifact_id="artifact-one",
        root_sha256=BOUNDARY_ROOT,
    )

    for hostile in (
        reference.uri + "?float=true",
        reference.uri.replace("boundary-adversarial", "%2e%2e"),
        reference.uri.replace("#sha256=", "#sha256=00"),
        reference.uri.replace("memory://", "Memory://"),
    ):
        with pytest.raises(MemoryBoundaryValidationError):
            MemoryBoundaryReference.parse(hostile)


def test_session_paging_rejects_mid_codepoint_and_incomplete_claims() -> None:
    content = "花a".encode()
    page = _exact_page(content, offset=0, max_bytes=3)
    assert page.text == "花"
    assert page.delivered_bytes == 3
    assert page.next_offset == 3

    with pytest.raises(ContinuityReviewInputError, match="UTF-8 boundary"):
        _exact_page(content, offset=1, max_bytes=3)
    with pytest.raises(ContinuityReviewInputError, match="cannot fit"):
        _exact_page(content, offset=0, max_bytes=1)
    with pytest.raises(
        ContinuityReviewDeliveryUnverified,
        match="DeliveryIncomplete",
    ):
        CandidateDeliveryReceipt.from_payload(
            {
                "delivery_id": "delivery-incomplete",
                "candidate_id": "candidate-incomplete",
                "candidate_revision": 1,
                "candidate_sha256": "d" * 64,
                "delivered_bytes": 3,
                "total_bytes": 4,
            }
        )


async def test_large_candidate_requires_every_exact_page_and_process_local_proof() -> (
    None
):
    body = ("continuity-花✨" * 7000).encode("utf-8")
    assert len(body) > CONTINUITY_REVIEW_MAX_PAGE_BYTES
    candidate = _candidate(body)
    clock = _Clock()
    coordinator = ContinuityCandidateDeliveryCoordinator(
        pending_ttl_seconds=5,
        coverage_ttl_seconds=10,
        clock=clock,
    )
    ranges = _utf8_page_ranges(body)
    assert len(ranges) > 1

    committed_ids: list[str] = []
    first_payload = _candidate_page(
        candidate,
        delivery_id="delivery-page-0",
        offset=ranges[0][0],
        end=ranges[0][1],
    )
    first = _register_page(coordinator, first_payload)
    assert coordinator.commit_effective_context_receipt(_effective_receipt(first))
    committed_ids.append(first.delivery_id)

    claim = _claim(candidate, first.delivery_id)
    assert not await coordinator.verify_exact_candidate_delivery(claim, candidate)
    assert not await coordinator.verify_exact_candidate_delivery(
        replace(claim, candidate_revision=candidate.candidate_revision + 1),
        candidate,
    )
    assert not await coordinator.verify_exact_candidate_delivery(
        replace(claim, candidate_sha256="f" * 64),
        candidate,
    )

    for index, (offset, end) in enumerate(ranges[1:], start=1):
        expectation = _register_page(
            coordinator,
            _candidate_page(
                candidate,
                delivery_id=f"delivery-page-{index}",
                offset=offset,
                end=end,
            ),
        )
        assert coordinator.commit_effective_context_receipt(
            _effective_receipt(expectation)
        )
        committed_ids.append(expectation.delivery_id)

    assert await coordinator.verify_exact_candidate_delivery(claim, candidate)
    restarted = ContinuityCandidateDeliveryCoordinator()
    assert not await restarted.verify_exact_candidate_delivery(claim, candidate)

    clock.now += 10
    assert not await coordinator.verify_exact_candidate_delivery(claim, candidate)
    assert coordinator.snapshot().committed_pages == 0
    assert len(committed_ids) == len(ranges)


async def test_health_is_not_healthy_when_any_boundary_is_unresolvable(
    tmp_path,
) -> None:
    memory = LifeMemoryService(tmp_path)
    memory._vector_backend_enabled = False
    await memory.initialize()
    try:
        living = memory._require_memory_storage().living
        stored = await MemoryBoundaryRepository(living).append(
            _manifest(),
            expected_head_revision=0,
        )
        missing = (
            "memory://boundary/boundary-missing@artifact-missing#sha256="
            + "e" * 64
        )
        malformed = (
            "memory://boundary/%2e%2e@artifact-hostile#sha256=" + "f" * 64
        )
        contents = {
            "SOUL.md": b"# subject fixture\n",
            "USER.md": b"# user fixture\n",
            "MEMORY.md": (
                "# memory fixture\n"
                f"[valid]({stored.exact_uri})\n"
                f"[missing]({missing})\n"
                f"[malformed]({malformed})\n"
            ).encode(),
        }

        health = await collect_continuity_memory_health(
            subject_store=_SubjectStore(contents),  # type: ignore[arg-type]
            living_store=living,
            delivery_coordinator=ContinuityCandidateDeliveryCoordinator(),
        )

        assert health["status"] == "degraded"
        assert health["index_entry_count"] == 2
        assert health["verified_boundary_count"] == 1
        assert health["broken_boundary_count"] == 1
        assert health["syntax_issue_count"] == 1
        assert health["unchecked_boundary_count"] == 0
        assert health["automatic_importance_judgment"] is False
        assert "private boundary body" not in repr(health)

        with pytest.raises(MemoryBoundaryNotFound):
            await MemoryBoundaryRepository(living).read_exact(missing)
        wrong_root = stored.exact_uri.rsplit("=", 1)[0] + "=" + "0" * 64
        with pytest.raises(MemoryBoundaryIntegrityError):
            await MemoryBoundaryRepository(living).read_exact(wrong_root)
    finally:
        await memory.close()
