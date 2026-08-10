"""Subject-candidate boundary for continuity-memory index maintenance."""

from __future__ import annotations

import hashlib

import pytest

from plugins.life_engine.learning.decisions import (
    LearningCandidate,
    LearningDecisionReceipt,
)
from plugins.life_engine.memory.boundary import MemoryBoundaryNotFound
from plugins.life_engine.memory.continuity_stewardship import (
    CONTINUITY_MEMORY_CANDIDATE_MAX_BYTES,
    ContinuityMemoryCandidateTooLarge,
    ContinuityMemoryCurrentVersionConflict,
    ContinuityMemoryStewardship,
)

SUBJECT_REVISION = "d" * 64
ROOT = "a" * 64
URI = "memory://boundary/a-long-memory@artifact_" + "b" * 64 + "#sha256=" + ROOT


class _Repository:
    def __init__(self, accepted: set[str]) -> None:
        self.accepted = set(accepted)
        self.reads: list[str] = []

    async def read_exact(self, uri: str) -> object:
        self.reads.append(uri)
        if uri not in self.accepted:
            raise MemoryBoundaryNotFound(uri)
        return object()


class _Ledger:
    def __init__(self) -> None:
        self.candidates: list[LearningCandidate] = []

    async def append_candidate(
        self,
        candidate: LearningCandidate,
    ) -> LearningDecisionReceipt:
        self.candidates.append(candidate)
        return LearningDecisionReceipt(
            candidate_id=candidate.candidate_id,
            candidate_revision=candidate.candidate_revision,
            candidate_sha256=candidate.candidate_sha256,
            status="open",
            decision_occurrence_id="",
        )


def _link(anchor: str = "我愿意沿着这条边界回忆") -> bytes:
    return f"# MEMORY\n\n[{anchor}]({URI})\n".encode()


async def _propose(
    *,
    repository: _Repository,
    ledger: _Ledger,
    current: bytes = b"# MEMORY\n",
    proposed: bytes | None = None,
    reviewed_hash: str = "",
):
    stewardship = ContinuityMemoryStewardship(repository, ledger)  # type: ignore[arg-type]
    return await stewardship.propose(
        current_memory_bytes=current,
        current_memory_version_id="subject-memory-version-7",
        reviewed_current_memory_sha256=(
            reviewed_hash or hashlib.sha256(current).hexdigest()
        ),
        proposed_memory_bytes=proposed if proposed is not None else _link(),
        unified_subject_revision=SUBJECT_REVISION,
        actor_consciousness_instance_id="chat-main",
        source_occurrence_id="message:review-request",
        proposal_occurrence_id="subject-review:memory:one",
        reason="我想让正文保持轻，完整细节仍然可以沿索引找回。",
        stream_scope="chat:one",
    )


async def test_validated_revision_stays_open_until_separate_subject_decision() -> None:
    repository = _Repository({URI})
    ledger = _Ledger()

    result = await _propose(repository=repository, ledger=ledger)

    assert result.receipt.status == "open"
    assert len(ledger.candidates) == 1
    candidate = ledger.candidates[0]
    assert candidate.target_path == "MEMORY.md"
    assert candidate.candidate_content_bytes == _link()
    assert candidate.actor_consciousness_instance_id == "chat-main"
    assert candidate.subject_revision == SUBJECT_REVISION
    assert candidate.provenance["authority"] == "candidate_only"
    assert candidate.provenance["automatic_importance_judgment"] is False
    assert result.verified_boundary_count == 1
    assert result.lifecycle.activated == ("a-long-memory",)
    assert repository.reads == [URI]


async def test_boundary_target_must_exist_at_the_exact_artifact_and_root() -> None:
    repository = _Repository(set())
    ledger = _Ledger()

    with pytest.raises(MemoryBoundaryNotFound):
        await _propose(repository=repository, ledger=ledger)

    assert ledger.candidates == []


async def test_removing_index_only_deactivates_pointer_and_keeps_bundle_untouched() -> (
    None
):
    repository = _Repository({URI})
    ledger = _Ledger()
    current = _link("旧的主体文字")
    proposed = "# MEMORY\n\n这次我选择不让它常驻当前索引。\n".encode()

    result = await _propose(
        repository=repository,
        ledger=ledger,
        current=current,
        proposed=proposed,
    )

    assert result.lifecycle.deactivated == ("a-long-memory",)
    assert result.verified_boundary_count == 0
    assert repository.reads == []
    assert ledger.candidates[0].provenance["deactivation_deletes_boundary"] is False


async def test_reviewed_hash_conflict_fails_before_candidate_or_boundary_read() -> None:
    repository = _Repository({URI})
    ledger = _Ledger()

    with pytest.raises(ContinuityMemoryCurrentVersionConflict):
        await _propose(
            repository=repository,
            ledger=ledger,
            reviewed_hash="0" * 64,
        )

    assert repository.reads == []
    assert ledger.candidates == []


async def test_complete_candidate_is_rejected_instead_of_truncated() -> None:
    repository = _Repository(set())
    ledger = _Ledger()
    proposed = b"x" * (CONTINUITY_MEMORY_CANDIDATE_MAX_BYTES + 1)

    with pytest.raises(ContinuityMemoryCandidateTooLarge):
        await _propose(
            repository=repository,
            ledger=ledger,
            proposed=proposed,
        )

    assert ledger.candidates == []


async def test_same_occurrence_and_bytes_produce_stable_candidate_identity() -> None:
    repository = _Repository({URI})
    first_ledger = _Ledger()
    second_ledger = _Ledger()

    first = await _propose(repository=repository, ledger=first_ledger)
    second = await _propose(repository=repository, ledger=second_ledger)

    assert first.candidate.candidate_id == second.candidate.candidate_id
    assert first.candidate.candidate_sha256 == second.candidate.candidate_sha256
    assert (
        first.candidate.candidate_occurrence_id
        == second.candidate.candidate_occurrence_id
    )


async def test_complete_candidate_can_repair_a_malformed_current_index() -> None:
    repository = _Repository({URI})
    ledger = _Ledger()
    current = (
        "# MEMORY\n\n"
        "[损坏的旧索引](memory://boundary/a-long-memory@artifact-bad#sha256=short)\n"
    ).encode()

    result = await _propose(
        repository=repository,
        ledger=ledger,
        current=current,
        proposed=_link("修复后的精确索引"),
    )

    assert result.current_index_issue_count == 1
    assert result.current_index_issues_sha256
    assert result.lifecycle.activated == ("a-long-memory",)
    assert result.lifecycle.deactivated == ()
    provenance = ledger.candidates[0].provenance
    assert provenance["repairs_malformed_current_index"] is True
    assert (
        provenance["malformed_current_entries_are_not_classified_as_deactivated"]
        is True
    )
