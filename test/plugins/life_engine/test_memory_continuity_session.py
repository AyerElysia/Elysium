"""Contracts for the single-tool continuity review session domain."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.learning.decisions import (
    LearningCandidate,
    LearningDecision,
    LearningDecisionReceipt,
    SubjectAuthorityUnavailable,
)
from plugins.life_engine.memory import continuity_tools
from plugins.life_engine.memory.boundary import (
    MEMORY_BOUNDARY_ARTIFACT_KIND,
    MemoryBoundaryManifest,
    MemoryBoundaryNotFound,
    StoredMemoryBoundary,
    memory_boundary_uri,
)
from plugins.life_engine.memory.continuity_session import (
    BoundaryAnchorEdit,
    CandidateDeliveryReceipt,
    ContinuityBoundaryPlan,
    ContinuityReviewActorContext,
    ContinuityReviewDeliveryProofUnavailable,
    ContinuityReviewIndependentDecisionRequired,
    ContinuityReviewInputError,
    ContinuityReviewSession,
    ContinuityReviewStale,
    ReviewedMemorySegmentPlan,
)
from plugins.life_engine.memory.continuity_tools import (
    CONTINUITY_REVIEW_TOOLS,
    ContinuityReviewToolRuntime,
    LifeMemoryContinuityReviewSessionTool,
)
from plugins.life_engine.memory.living import new_artifact_version
from plugins.life_engine.storage.subject_contracts import (
    SUBJECT_AUTHORITY_PATHS,
    SubjectAuthoritySnapshot,
    SubjectDocumentCommit,
    SubjectDocumentHead,
    SubjectDocumentVersion,
    subject_revision_from_contents,
)

MEMORY_TEXT = "开头。\n那一天发生了很长、很重要的事情。\n结尾。\n"
MEMORY_BYTES = MEMORY_TEXT.encode("utf-8")
RANGE_BYTES = "那一天发生了很长、很重要的事情。".encode()
RANGE_START = MEMORY_BYTES.index(RANGE_BYTES)
RANGE_END = RANGE_START + len(RANGE_BYTES)


class _Authority:
    def __init__(self, memory: bytes = MEMORY_BYTES) -> None:
        self.contents = {
            "SOUL.md": b"soul\n",
            "USER.md": b"user\n",
            "MEMORY.md": bytes(memory),
        }
        self.accept_calls = 0

    @property
    def revision(self) -> str:
        return subject_revision_from_contents(self.contents)  # type: ignore[arg-type]

    async def current_subject_revision(self) -> str:
        return self.revision

    async def current_subject_change_marker(self) -> str:
        return "marker:" + self.revision

    async def read_subject_authority(self) -> SubjectAuthoritySnapshot:
        commits: dict[str, SubjectDocumentCommit] = {}
        for index, path in enumerate(SUBJECT_AUTHORITY_PATHS, start=1):
            content = self.contents[path]
            digest = hashlib.sha256(content).hexdigest()
            version_id = f"subject-{path.lower()}-v{index}"
            version = SubjectDocumentVersion(
                version_id=version_id,
                document_id=f"document-{path.lower()}",
                logical_path=path,
                parent_version_id="",
                occurrence_id=f"seed:{path}",
                semantic_actor_id="chat-main",
                semantic_source_id="test",
                occurred_at="2026-08-12T00:00:00+00:00",
                recorded_by="test",
                recorded_source="test",
                recorded_at="2026-08-12T00:00:00+00:00",
                provenance_status="complete",
                content_bytes=content,
                content_hash=digest,
                byte_length=len(content),
                byte_fidelity="exact_bytes",
                encoding="utf-8",
                newline_style="lf",
                change_context={},
            )
            head = SubjectDocumentHead(
                document_id=version.document_id,
                logical_path=path,
                declared_owner="Elysia",
                current_version_id=version_id,
                revision=index,
            )
            commits[path] = SubjectDocumentCommit(version=version, head=head)
        return SubjectAuthoritySnapshot(  # type: ignore[arg-type]
            commits=commits,
            revision=self.revision,
            change_marker="marker:" + self.revision,
        )

    async def accept_candidate(self, command: Any) -> None:
        self.accept_calls += 1
        raise AssertionError("continuity session must never call SubjectAuthority")


def _stored(manifest: MemoryBoundaryManifest) -> StoredMemoryBoundary:
    artifact = new_artifact_version(
        logical_key=manifest.logical_key,
        artifact_kind=MEMORY_BOUNDARY_ARTIFACT_KIND,
        content=manifest.canonical_json,
        authored_by=manifest.actor_id,
        consciousness_instance_id=manifest.consciousness_instance_id,
        stream_scope=manifest.stream_scope,
        visibility=manifest.visibility,
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
        current_head_revision=manifest.manifest_revision,
        is_current=True,
    )


class _Repository:
    def __init__(self) -> None:
        self.by_uri: dict[str, StoredMemoryBoundary] = {}
        self.by_occurrence: dict[str, StoredMemoryBoundary] = {}
        self.heads: dict[str, StoredMemoryBoundary] = {}
        self.append_attempts = 0

    async def append(
        self,
        manifest: MemoryBoundaryManifest,
        *,
        expected_head_revision: int,
    ) -> StoredMemoryBoundary:
        self.append_attempts += 1
        replay = self.by_occurrence.get(manifest.operation_occurrence_id)
        if replay is not None:
            if replay.manifest != manifest:
                raise RuntimeError("operation occurrence conflict")
            return replay
        head = self.heads.get(manifest.boundary_id)
        actual = head.manifest.manifest_revision if head is not None else 0
        if actual != expected_head_revision:
            raise RuntimeError("head revision conflict")
        stored = _stored(manifest)
        self.by_uri[stored.exact_uri] = stored
        self.by_occurrence[manifest.operation_occurrence_id] = stored
        self.heads[manifest.boundary_id] = stored
        return stored

    async def read_exact(self, uri: str) -> StoredMemoryBoundary:
        if uri not in self.by_uri:
            raise MemoryBoundaryNotFound(uri)
        return self.by_uri[uri]


class _Ledger:
    def __init__(
        self,
        *,
        fail_once: bool = False,
        authority_available: bool = True,
    ) -> None:
        self.candidates: dict[str, LearningCandidate] = {}
        self.statuses: dict[str, str] = {}
        self.decisions: dict[str, LearningDecision] = {}
        self.decision_by_candidate: dict[str, str] = {}
        self.authority_occurrences: dict[str, str] = {}
        self.fail_once = fail_once
        self.authority_available = authority_available
        self.append_attempts = 0
        self.authority_accept_calls = 0

    async def append_candidate(
        self,
        candidate: LearningCandidate,
    ) -> LearningDecisionReceipt:
        self.append_attempts += 1
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("simulated ledger outage")
        existing = self.candidates.get(candidate.candidate_id)
        if existing is not None and existing != candidate:
            raise RuntimeError("candidate conflict")
        self.candidates[candidate.candidate_id] = candidate
        self.statuses.setdefault(candidate.candidate_id, "open")
        return LearningDecisionReceipt(
            candidate_id=candidate.candidate_id,
            candidate_revision=candidate.candidate_revision,
            candidate_sha256=candidate.candidate_sha256,
            status=self.statuses[candidate.candidate_id],
            decision_occurrence_id="",
        )

    def _receipt(self, decision: LearningDecision) -> LearningDecisionReceipt:
        candidate_id = decision.candidate_id
        return LearningDecisionReceipt(
            candidate_id=candidate_id,
            candidate_revision=decision.candidate_revision,
            candidate_sha256=decision.candidate_sha256,
            status=self.statuses[candidate_id],
            decision_occurrence_id=decision.decision_occurrence_id,
            authority_occurrence_id=self.authority_occurrences.get(candidate_id, ""),
        )

    async def record_decision(
        self,
        decision: LearningDecision,
    ) -> LearningDecisionReceipt:
        candidate = self.candidates.get(decision.candidate_id)
        if candidate is None or not all(
            (
                candidate.candidate_revision == decision.candidate_revision,
                candidate.candidate_sha256 == decision.candidate_sha256,
                candidate.candidate_occurrence_id == decision.candidate_occurrence_id,
            )
        ):
            raise RuntimeError("decision candidate mismatch")
        existing = self.decisions.get(decision.decision_occurrence_id)
        if existing is not None:
            if existing != decision:
                raise RuntimeError("decision occurrence conflict")
            return self._receipt(decision)
        self.decisions[decision.decision_occurrence_id] = decision
        self.decision_by_candidate[decision.candidate_id] = (
            decision.decision_occurrence_id
        )
        self.statuses[decision.candidate_id] = decision.decision_kind
        return self._receipt(decision)

    async def accept_subject_candidate(
        self,
        decision: LearningDecision,
    ) -> LearningDecisionReceipt:
        requested = await self.record_decision(decision)
        if requested.status == "committed":
            return requested
        if not self.authority_available:
            raise SubjectAuthorityUnavailable("authority unavailable")
        self.authority_accept_calls += 1
        authority_occurrence = (
            "subject-authority:"
            + hashlib.sha256(decision.decision_occurrence_id.encode()).hexdigest()[:24]
        )
        self.statuses[decision.candidate_id] = "committed"
        self.authority_occurrences[decision.candidate_id] = authority_occurrence
        return self._receipt(decision)

    async def read_candidate(self, candidate_id: str) -> LearningCandidate | None:
        return self.candidates.get(candidate_id)

    async def list_candidates(
        self,
        *,
        status: str = "open",
        limit: int = 20,
    ) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for candidate_id, candidate in sorted(self.candidates.items()):
            candidate_status = self.statuses[candidate_id]
            if status != "all" and candidate_status != status:
                continue
            rows.append(
                {
                    "candidate_id": candidate_id,
                    "candidate_revision": candidate.candidate_revision,
                    "candidate_sha256": candidate.candidate_sha256,
                    "candidate_occurrence_id": candidate.candidate_occurrence_id,
                    "candidate_kind": candidate.candidate_kind,
                    "target_path": candidate.target_path or "",
                    "subject_revision": candidate.subject_revision,
                    "status": candidate_status,
                    "decision_occurrence_id": self.decision_by_candidate.get(
                        candidate_id, ""
                    ),
                    "authority_occurrence_id": self.authority_occurrences.get(
                        candidate_id, ""
                    ),
                }
            )
            if len(rows) >= limit:
                break
        return rows


class _Verifier:
    def __init__(self, trusted_delivery_id: str) -> None:
        self.trusted_delivery_id = trusted_delivery_id
        self.calls = 0

    async def verify_exact_candidate_delivery(
        self,
        receipt: CandidateDeliveryReceipt,
        candidate: LearningCandidate,
    ) -> bool:
        self.calls += 1
        return (
            receipt.delivery_id == self.trusted_delivery_id
            and receipt.candidate_id == candidate.candidate_id
            and receipt.candidate_sha256 == candidate.candidate_sha256
            and receipt.total_bytes == len(candidate.candidate_content_bytes)
        )


async def _active(actor_id: str) -> bool:
    return actor_id == "chat-main"


def _actor(action: str = "tool-call:open") -> ContinuityReviewActorContext:
    return ContinuityReviewActorContext(
        consciousness_instance_id="chat-main",
        stream_scope="chat:one",
        source_occurrence_id="message:one",
        action_occurrence_id=action,
        occurred_at="2026-08-12T00:00:00+00:00",
    )


def _session(
    authority: _Authority,
    repository: _Repository,
    ledger: _Ledger,
    *,
    verifier: _Verifier | None = None,
) -> ContinuityReviewSession:
    return ContinuityReviewSession(
        subject_authority=authority,  # type: ignore[arg-type]
        boundary_repository=repository,
        candidate_ledger=ledger,
        validate_active_actor=_active,
        delivery_verifier=verifier,
    )


def _plans() -> tuple[
    tuple[ContinuityBoundaryPlan, ...], tuple[BoundaryAnchorEdit, ...]
]:
    segment = ReviewedMemorySegmentPlan(
        segment_id="story",
        title="那一天的完整经过",
        byte_start=RANGE_START,
        byte_end=RANGE_END,
        scope="只覆盖那一天的完整经过",
        visibility="private",
    )
    boundary = ContinuityBoundaryPlan(
        boundary_id="important-day",
        title="那一天",
        scope="这段经历本身，不泛化到所有关系",
        current_meaning="我现在仍把它视为重要的共同经历",
        non_generalization="不能据此推断未来一定重复",
        expected_head_revision=0,
        visibility="private",
        segments=(segment,),
    )
    edit = BoundaryAnchorEdit(
        boundary_slot=0,
        byte_start=RANGE_START,
        byte_end=RANGE_END,
        anchor_text="那一天的完整记忆",
    )
    return (boundary,), (edit,)


async def _prepare(
    session: ContinuityReviewSession,
    *,
    action: str = "tool-call:prepare",
):
    opened = await session.open(_actor())
    boundaries, edits = _plans()
    prepared = await session.prepare_candidate(
        _actor(action),
        session_id=opened.session_id,
        expected_subject_revision=opened.subject_revision,
        memory_version_id=opened.memory_version_id,
        memory_sha256=opened.memory_sha256,
        boundaries=boundaries,
        edits=edits,
        reason="我决定把完整经过移到边界，只在 MEMORY 留下索引。",
    )
    return opened, prepared


async def test_open_and_read_source_are_exact_pinned_utf8_pages() -> None:
    authority = _Authority()
    session = _session(authority, _Repository(), _Ledger())

    opened = await session.open(_actor(), max_bytes=10)
    assert opened.memory_sha256 == hashlib.sha256(MEMORY_BYTES).hexdigest()
    assert (
        opened.page.text.encode("utf-8") == MEMORY_BYTES[: opened.page.delivered_bytes]
    )
    assert opened.page.next_offset is not None

    second = await session.read_source(
        _actor("tool-call:read"),
        session_id=opened.session_id,
        expected_subject_revision=opened.subject_revision,
        memory_version_id=opened.memory_version_id,
        memory_sha256=opened.memory_sha256,
        offset=opened.page.next_offset,
        max_bytes=12,
    )
    assert (
        second.text.encode("utf-8")
        == MEMORY_BYTES[second.offset : second.offset + second.delivered_bytes]
    )
    assert authority.accept_calls == 0


async def test_prepare_creates_exact_boundary_and_open_candidate_only() -> None:
    authority = _Authority()
    repository = _Repository()
    ledger = _Ledger()
    session = _session(authority, repository, ledger)

    opened, prepared = await _prepare(session)

    assert prepared.status == "open"
    assert len(repository.heads) == 1
    stored = repository.heads["important-day"]
    segment = stored.manifest.segments[0]
    assert segment.content.encode("utf-8") == RANGE_BYTES
    assert f"bytes={RANGE_START}-{RANGE_END}" in segment.source_refs[0]
    assert f"sha256={opened.memory_sha256}" in segment.source_refs[0]
    candidate = ledger.candidates[prepared.candidate_id]
    expected = (
        MEMORY_BYTES[:RANGE_START]
        + f"[那一天的完整记忆]({stored.exact_uri})".encode()
        + MEMORY_BYTES[RANGE_END:]
    )
    assert candidate.candidate_content_bytes == expected
    assert authority.accept_calls == 0


def test_plan_rejects_caller_supplied_boundary_content_or_source_refs() -> None:
    payload: dict[str, Any] = {
        "boundary_id": "important-day",
        "title": "那一天",
        "scope": "范围",
        "current_meaning": "意义",
        "non_generalization": "不泛化",
        "expected_head_revision": 0,
        "visibility": "private",
        "segments": [
            {
                "segment_id": "story",
                "title": "经过",
                "byte_start": RANGE_START,
                "byte_end": RANGE_END,
                "scope": "范围",
                "visibility": "private",
                "content": "基础设施不得接收这段伪造正文",
                "source_refs": ["forged"],
            }
        ],
    }
    with pytest.raises(ContinuityReviewInputError, match="unexpected"):
        ContinuityBoundaryPlan.from_payload(payload)


async def test_prepare_rejects_segment_gap_before_any_append() -> None:
    authority = _Authority()
    repository = _Repository()
    session = _session(authority, repository, _Ledger())
    opened = await session.open(_actor())
    boundaries, edits = _plans()
    gap = replace(
        boundaries[0],
        segments=(replace(boundaries[0].segments[0], byte_end=RANGE_END - 3),),
    )

    with pytest.raises(ContinuityReviewInputError, match="omit bytes"):
        await session.prepare_candidate(
            _actor("tool-call:prepare"),
            session_id=opened.session_id,
            expected_subject_revision=opened.subject_revision,
            memory_version_id=opened.memory_version_id,
            memory_sha256=opened.memory_sha256,
            boundaries=(gap,),
            edits=edits,
            reason="明确计划",
        )
    assert repository.append_attempts == 0


async def test_stale_source_fails_before_boundary_append() -> None:
    authority = _Authority()
    repository = _Repository()
    session = _session(authority, repository, _Ledger())
    opened = await session.open(_actor())
    authority.contents["MEMORY.md"] += b"changed\n"
    boundaries, edits = _plans()

    with pytest.raises(ContinuityReviewStale):
        await session.prepare_candidate(
            _actor("tool-call:prepare"),
            session_id=opened.session_id,
            expected_subject_revision=opened.subject_revision,
            memory_version_id=opened.memory_version_id,
            memory_sha256=opened.memory_sha256,
            boundaries=boundaries,
            edits=edits,
            reason="明确计划",
        )
    assert repository.append_attempts == 0


async def test_retry_recovers_from_boundaries_after_candidate_ledger_outage() -> None:
    authority = _Authority()
    repository = _Repository()
    ledger = _Ledger(fail_once=True)
    session = _session(authority, repository, ledger)

    with pytest.raises(RuntimeError, match="ledger outage"):
        await _prepare(session)
    assert len(repository.heads) == 1

    _, prepared = await _prepare(session)
    assert prepared.candidate_id in ledger.candidates
    assert len(repository.heads) == 1
    assert len(repository.by_occurrence) == 1
    assert repository.append_attempts == 2


async def test_new_domain_object_recovers_candidate_from_existing_ledgers() -> None:
    authority = _Authority()
    repository = _Repository()
    ledger = _Ledger()
    first = _session(authority, repository, ledger)
    opened, prepared = await _prepare(first)

    recovered = _session(authority, repository, ledger)
    status = await recovered.status(
        _actor("tool-call:status"),
        session_id=opened.session_id,
        candidate_id=prepared.candidate_id,
    )
    assert status.candidates[0]["status"] == "open"
    read = await recovered.read_candidate(
        _actor("tool-call:read-candidate"),
        session_id=opened.session_id,
        candidate_id=prepared.candidate_id,
        candidate_revision=prepared.candidate_revision,
        candidate_sha256=prepared.candidate_sha256,
        expected_subject_revision=prepared.subject_revision,
        offset=0,
        max_bytes=32768,
    )
    assert (
        read.page.text.encode("utf-8")
        == ledger.candidates[prepared.candidate_id].candidate_content_bytes
    )
    assert read.as_dict()["delivery_binding_is_not_receipt"] is True


async def test_accept_decision_requires_trusted_exact_delivery_and_commits() -> None:
    authority = _Authority()
    repository = _Repository()
    ledger = _Ledger()
    without_verifier = _session(authority, repository, ledger)
    opened, prepared = await _prepare(without_verifier)
    candidate = ledger.candidates[prepared.candidate_id]
    receipt = {
        "delivery_id": "trusted-delivery",
        "candidate_id": candidate.candidate_id,
        "candidate_revision": candidate.candidate_revision,
        "candidate_sha256": candidate.candidate_sha256,
        "delivered_bytes": len(candidate.candidate_content_bytes),
        "total_bytes": len(candidate.candidate_content_bytes),
    }

    with pytest.raises(ContinuityReviewDeliveryProofUnavailable):
        await without_verifier.decide(
            _actor("tool-call:decide"),
            session_id=opened.session_id,
            candidate_id=candidate.candidate_id,
            candidate_revision=candidate.candidate_revision,
            candidate_sha256=candidate.candidate_sha256,
            expected_subject_revision=candidate.subject_revision,
            decision_kind="accept_requested",
            reason="我完整核对后决定接受。",
            delivery_receipt=receipt,
        )

    verifier = _Verifier("trusted-delivery")
    verified_session = _session(authority, repository, ledger, verifier=verifier)
    persisted = await verified_session.decide(
        _actor("tool-call:decide"),
        session_id=opened.session_id,
        candidate_id=candidate.candidate_id,
        candidate_revision=candidate.candidate_revision,
        candidate_sha256=candidate.candidate_sha256,
        expected_subject_revision=candidate.subject_revision,
        decision_kind="accept_requested",
        reason="我完整核对后决定接受。",
        delivery_receipt=receipt,
    )
    payload = persisted.as_dict()
    assert payload["status"] == "committed"
    assert payload["decision_recorded"] is True
    assert payload["subject_authority_committed"] is True
    assert payload["authority_occurrence_id"]
    assert "accepted_content_bytes" not in payload
    assert verifier.calls == 1
    assert authority.accept_calls == 0
    assert ledger.authority_accept_calls == 1
    assert ledger.statuses[candidate.candidate_id] == "committed"


async def test_decide_requires_an_action_occurrence_independent_from_prepare() -> None:
    authority = _Authority()
    repository = _Repository()
    ledger = _Ledger()
    session = _session(authority, repository, ledger)
    opened, prepared = await _prepare(session, action="tool-call:prepare")

    with pytest.raises(ContinuityReviewIndependentDecisionRequired):
        await session.decide(
            _actor("tool-call:prepare"),
            session_id=opened.session_id,
            candidate_id=prepared.candidate_id,
            candidate_revision=prepared.candidate_revision,
            candidate_sha256=prepared.candidate_sha256,
            expected_subject_revision=prepared.subject_revision,
            decision_kind="rejected",
            reason="我决定拒绝。",
        )


async def test_reject_is_persisted_content_free_and_idempotent() -> None:
    authority = _Authority()
    repository = _Repository()
    ledger = _Ledger()
    session = _session(authority, repository, ledger)
    opened, prepared = await _prepare(session)

    persisted = await session.decide(
        _actor("tool-call:reject"),
        session_id=opened.session_id,
        candidate_id=prepared.candidate_id,
        candidate_revision=prepared.candidate_revision,
        candidate_sha256=prepared.candidate_sha256,
        expected_subject_revision=prepared.subject_revision,
        decision_kind="rejected",
        reason="这不是我现在愿意保留的组织方式。",
    )
    replay = await session.decide(
        _actor("tool-call:reject"),
        session_id=opened.session_id,
        candidate_id=prepared.candidate_id,
        candidate_revision=prepared.candidate_revision,
        candidate_sha256=prepared.candidate_sha256,
        expected_subject_revision=prepared.subject_revision,
        decision_kind="rejected",
        reason="这不是我现在愿意保留的组织方式。",
    )
    assert replay == persisted
    payload = persisted.as_dict()
    assert payload["status"] == "rejected"
    assert payload["decision_recorded"] is True
    assert payload["subject_authority_committed"] is False
    assert "accepted_content_bytes" not in payload
    assert ledger.statuses[prepared.candidate_id] == "rejected"


async def test_kept_open_is_persisted_without_calling_subject_authority() -> None:
    authority = _Authority()
    ledger = _Ledger()
    session = _session(authority, _Repository(), ledger)
    opened, prepared = await _prepare(session)

    persisted = await session.decide(
        _actor("tool-call:keep-open"),
        session_id=opened.session_id,
        candidate_id=prepared.candidate_id,
        candidate_revision=prepared.candidate_revision,
        candidate_sha256=prepared.candidate_sha256,
        expected_subject_revision=prepared.subject_revision,
        decision_kind="kept_open",
        reason="我还需要继续想，暂时保持开放。",
    )
    assert persisted.receipt.status == "kept_open"
    assert ledger.statuses[prepared.candidate_id] == "kept_open"
    assert ledger.authority_accept_calls == 0
    assert authority.accept_calls == 0


async def test_authority_unavailable_leaves_accept_requested_durable() -> None:
    authority = _Authority()
    repository = _Repository()
    ledger = _Ledger(authority_available=False)
    verifier = _Verifier("trusted-delivery")
    session = _session(authority, repository, ledger, verifier=verifier)
    opened, prepared = await _prepare(session)
    candidate = ledger.candidates[prepared.candidate_id]
    receipt = {
        "delivery_id": "trusted-delivery",
        "candidate_id": candidate.candidate_id,
        "candidate_revision": candidate.candidate_revision,
        "candidate_sha256": candidate.candidate_sha256,
        "delivered_bytes": len(candidate.candidate_content_bytes),
        "total_bytes": len(candidate.candidate_content_bytes),
    }

    pending = await session.decide(
        _actor("tool-call:accept-pending"),
        session_id=opened.session_id,
        candidate_id=candidate.candidate_id,
        candidate_revision=candidate.candidate_revision,
        candidate_sha256=candidate.candidate_sha256,
        expected_subject_revision=candidate.subject_revision,
        decision_kind="accept_requested",
        reason="我已确认接受，等待正式权威提交。",
        delivery_receipt=receipt,
    )
    assert pending.receipt.status == "accept_requested"
    assert pending.authority_error_type == "SubjectAuthorityUnavailable"
    assert ledger.statuses[candidate.candidate_id] == "accept_requested"
    assert ledger.decision_by_candidate[candidate.candidate_id]
    assert ledger.authority_accept_calls == 0

    ledger.authority_available = True
    committed = await session.decide(
        _actor("tool-call:accept-pending"),
        session_id=opened.session_id,
        candidate_id=candidate.candidate_id,
        candidate_revision=candidate.candidate_revision,
        candidate_sha256=candidate.candidate_sha256,
        expected_subject_revision=candidate.subject_revision,
        decision_kind="accept_requested",
        reason="我已确认接受，等待正式权威提交。",
        delivery_receipt=receipt,
    )
    assert committed.receipt.status == "committed"
    assert committed.receipt.decision_occurrence_id == (
        pending.receipt.decision_occurrence_id
    )
    assert ledger.authority_accept_calls == 1


async def test_single_tool_fails_closed_without_public_runtime_and_dispatches_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert CONTINUITY_REVIEW_TOOLS == [LifeMemoryContinuityReviewSessionTool]
    tool = LifeMemoryContinuityReviewSessionTool(plugin=SimpleNamespace())
    failed, error = await tool.execute("open")
    assert failed is False
    assert error["error"] == "ContinuityReviewRuntimeUnavailable"
    assert error["authority_written"] is False

    authority = _Authority()
    domain = _session(authority, _Repository(), _Ledger())

    async def _runtime(_tool: Any) -> ContinuityReviewToolRuntime:
        return ContinuityReviewToolRuntime(domain, _actor())

    monkeypatch.setattr(
        continuity_tools,
        "resolve_continuity_review_tool_runtime",
        _runtime,
    )
    ok, payload = await tool.execute("open", max_bytes=16)
    assert ok is True
    assert payload["action"] == "opened"
    assert payload["authority_written"] is False
    assert authority.accept_calls == 0
