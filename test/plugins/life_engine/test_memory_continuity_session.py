"""Contracts for the single-tool continuity review session domain."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
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
    CONTINUITY_REVIEW_MAX_TEXT_EDIT_BYTES,
    CONTINUITY_REVIEW_MAX_TEXT_EDITS,
    CONTINUITY_REVIEW_MAX_TEXT_REPLACEMENT_BYTES,
    AuxiliarySubjectSegmentPlan,
    BoundaryAnchorEdit,
    CandidateDeliveryReceipt,
    ContinuityBoundaryPlan,
    ContinuityReviewActorContext,
    ContinuityReviewActorInactive,
    ContinuityReviewAuxiliarySourceNotFound,
    ContinuityReviewDeliveryProofUnavailable,
    ContinuityReviewDeliveryUnverified,
    ContinuityReviewIndependentDecisionRequired,
    ContinuityReviewInputError,
    ContinuityReviewOutcome,
    ContinuityReviewRuntimeUnavailable,
    ContinuityReviewSession,
    ContinuityReviewStale,
    ReviewedMemorySegmentPlan,
    SubjectTextEdit,
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

AUXILIARY_PATH = (
    "life_engine_workspace/diaries/witness/2026-08/2026-08-12/"
    "000000000001-000000000004-a1b2c3d4.md"
)
AUXILIARY_TEXT = "见证标题。\n那一天的完整经过很长，也很重要。\n尾声。\n"
AUXILIARY_BYTES = AUXILIARY_TEXT.encode("utf-8")
AUXILIARY_RANGE_BYTES = "那一天的完整经过很长，也很重要。".encode()
AUXILIARY_RANGE_START = AUXILIARY_BYTES.index(AUXILIARY_RANGE_BYTES)
AUXILIARY_RANGE_END = AUXILIARY_RANGE_START + len(AUXILIARY_RANGE_BYTES)


class _Authority:
    def __init__(self, memory: bytes = MEMORY_BYTES) -> None:
        self.contents = {
            "SOUL.md": b"soul\n",
            "USER.md": b"user\n",
            "MEMORY.md": bytes(memory),
        }
        self.accept_calls = 0
        self.read_calls = 0

    @property
    def revision(self) -> str:
        return subject_revision_from_contents(self.contents)  # type: ignore[arg-type]

    async def current_subject_revision(self) -> str:
        return self.revision

    async def current_subject_change_marker(self) -> str:
        return "marker:" + self.revision

    async def read_subject_authority(self) -> SubjectAuthoritySnapshot:
        self.read_calls += 1
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


class _AuxiliarySubjectPortFake:
    """Content-identical fake for the public local/MySQL Subject read Port."""

    def __init__(
        self,
        backend: str,
        *,
        logical_path: str = AUXILIARY_PATH,
        content: bytes = AUXILIARY_BYTES,
    ) -> None:
        self.backend = backend
        self.get_head_calls: list[str] = []
        self.get_version_calls: list[str] = []
        content_hash = hashlib.sha256(content).hexdigest()
        version_id = f"subject-aux-{backend}-v1"
        self.version = SubjectDocumentVersion(
            version_id=version_id,
            document_id=f"document-aux-{backend}",
            logical_path=logical_path,
            parent_version_id="",
            occurrence_id=f"witness:{backend}:0001",
            semantic_actor_id="memory_witness",
            semantic_source_id=f"witness:{backend}:0001",
            occurred_at="2026-08-12T00:00:00+00:00",
            recorded_by="memory_witness",
            recorded_source="memory-witness",
            recorded_at="2026-08-12T00:00:00+00:00",
            provenance_status="complete",
            content_bytes=content,
            content_hash=content_hash,
            byte_length=len(content),
            byte_fidelity="exact_bytes",
            encoding="utf-8",
            newline_style="lf",
            change_context={"projection": "witness"},
        )
        self.head = SubjectDocumentHead(
            document_id=self.version.document_id,
            logical_path=logical_path,
            declared_owner="elysia",
            current_version_id=version_id,
            revision=1,
        )
        self.versions = {version_id: self.version}

    async def get_head(self, logical_path: str) -> SubjectDocumentHead | None:
        self.get_head_calls.append(logical_path)
        return self.head if logical_path == self.head.logical_path else None

    async def get_version(self, version_id: str) -> SubjectDocumentVersion:
        self.get_version_calls.append(version_id)
        if version_id not in self.versions:
            raise KeyError(version_id)
        return self.versions[version_id]


class _CoherentSubjectStoreFake(_Authority):
    """Match production: one store owns root authority and auxiliary reads."""

    def __init__(self, backend: str) -> None:
        super().__init__()
        self.auxiliary = _AuxiliarySubjectPortFake(backend)
        self.backend = backend

    async def get_head(self, logical_path: str) -> SubjectDocumentHead | None:
        return await self.auxiliary.get_head(logical_path)

    async def get_version(self, version_id: str) -> SubjectDocumentVersion:
        return await self.auxiliary.get_version(version_id)


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
        fail_after_append_once: bool = False,
        authority_available: bool = True,
    ) -> None:
        self.candidates: dict[str, LearningCandidate] = {}
        self.statuses: dict[str, str] = {}
        self.decisions: dict[str, LearningDecision] = {}
        self.decision_by_candidate: dict[str, str] = {}
        self.authority_occurrences: dict[str, str] = {}
        self.fail_once = fail_once
        self.authority_available = authority_available
        self.fail_after_append_once = fail_after_append_once
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
        if self.fail_after_append_once:
            self.fail_after_append_once = False
            raise RuntimeError("simulated response loss after candidate append")
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


class _AuthorityUpdatingLedger(_Ledger):
    def __init__(self, authority: _Authority) -> None:
        super().__init__()
        self._authority = authority

    async def accept_subject_candidate(
        self,
        decision: LearningDecision,
    ) -> LearningDecisionReceipt:
        receipt = await super().accept_subject_candidate(decision)
        if receipt.status == "committed":
            self._authority.contents["MEMORY.md"] = bytes(
                decision.accepted_content_bytes
            )
        return receipt


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


class _OutcomeRecorder:
    def __init__(self, *, fail_after_record_once: set[str] | None = None) -> None:
        self.events: dict[str, ContinuityReviewOutcome] = {}
        self.calls: list[ContinuityReviewOutcome] = []
        self.fail_after_record_once = set(fail_after_record_once or ())

    async def __call__(self, outcome: ContinuityReviewOutcome) -> None:
        self.calls.append(outcome)
        existing = self.events.get(outcome.outcome_occurrence_id)
        if existing is not None and existing != outcome:
            raise RuntimeError("outcome occurrence conflict")
        self.events[outcome.outcome_occurrence_id] = outcome
        if outcome.outcome_kind in self.fail_after_record_once:
            self.fail_after_record_once.remove(outcome.outcome_kind)
            raise RuntimeError("simulated outcome projection response loss")


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
    recorder: Any = None,
    subject_documents: Any = None,
) -> ContinuityReviewSession:
    return ContinuityReviewSession(
        subject_authority=authority,  # type: ignore[arg-type]
        boundary_repository=repository,
        candidate_ledger=ledger,
        validate_active_actor=_active,
        delivery_verifier=verifier,
        outcome_recorder=recorder,
        subject_documents=subject_documents,
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


def _auxiliary_plans(
    source: _AuxiliarySubjectPortFake,
    *,
    anchor_start: int,
    anchor_end: int,
    range_start: int = AUXILIARY_RANGE_START,
    range_end: int = AUXILIARY_RANGE_END,
    boundary_id: str = "external-important-day",
) -> tuple[tuple[ContinuityBoundaryPlan, ...], tuple[BoundaryAnchorEdit, ...]]:
    segment = AuxiliarySubjectSegmentPlan(
        segment_id="witness-story",
        title="完整见证",
        logical_path=source.version.logical_path,
        version_id=source.version.version_id,
        content_hash=source.version.content_hash,
        byte_start=range_start,
        byte_end=range_end,
        scope="只覆盖这个 Witness 中被明确选择的范围",
        visibility="private",
    )
    boundary = ContinuityBoundaryPlan(
        boundary_id=boundary_id,
        title="外部长记忆",
        scope="这一段经历本身，不泛化到其他经历",
        current_meaning="我选择在 MEMORY 中保留可追溯索引",
        non_generalization="不能据此推断所有未来关系",
        expected_head_revision=0,
        visibility="private",
        segments=(segment,),
    )
    edit = BoundaryAnchorEdit(
        boundary_slot=0,
        byte_start=anchor_start,
        byte_end=anchor_end,
        anchor_text="那一天的完整见证",
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


async def _prepare_text_edits(
    session: ContinuityReviewSession,
    text_edits: Sequence[SubjectTextEdit],
    *,
    action: str = "tool-call:prepare-text",
):
    opened = await session.open(_actor())
    prepared = await session.prepare_candidate(
        _actor(action),
        session_id=opened.session_id,
        expected_subject_revision=opened.subject_revision,
        memory_version_id=opened.memory_version_id,
        memory_sha256=opened.memory_sha256,
        boundaries=(),
        edits=(),
        text_edits=tuple(text_edits),
        reason="I explicitly choose this short MEMORY text change.",
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


@pytest.mark.parametrize("backend", ["local", "mysql"])
async def test_auxiliary_subject_source_is_exactly_paged_through_public_port(
    backend: str,
) -> None:
    authority = _CoherentSubjectStoreFake(backend)
    source = authority.auxiliary
    session = _session(authority, _Repository(), _Ledger())
    opened = await session.open(_actor())

    auxiliary = await session.open_auxiliary_source(
        _actor("tool-call:open-auxiliary"),
        session_id=opened.session_id,
        expected_subject_revision=opened.subject_revision,
        memory_version_id=opened.memory_version_id,
        memory_sha256=opened.memory_sha256,
        logical_path=AUXILIARY_PATH,
        max_bytes=13,
    )

    assert auxiliary.logical_path == AUXILIARY_PATH
    assert auxiliary.version_id == source.version.version_id
    assert auxiliary.content_hash == source.version.content_hash
    assert auxiliary.source_occurrence_id == source.version.occurrence_id
    assert auxiliary.source_kind == "witness_projection"
    assert (
        auxiliary.page.text.encode("utf-8")
        == AUXILIARY_BYTES[: auxiliary.page.delivered_bytes]
    )
    assert auxiliary.page.next_offset is not None
    assert source.get_head_calls == [AUXILIARY_PATH]

    second = await session.read_auxiliary_source(
        _actor("tool-call:read-auxiliary"),
        session_id=opened.session_id,
        expected_subject_revision=opened.subject_revision,
        memory_version_id=opened.memory_version_id,
        memory_sha256=opened.memory_sha256,
        logical_path=auxiliary.logical_path,
        version_id=auxiliary.version_id,
        content_hash=auxiliary.content_hash,
        offset=auxiliary.page.next_offset,
        max_bytes=17,
    )
    assert (
        second.page.text.encode("utf-8")
        == AUXILIARY_BYTES[
            second.page.offset : second.page.offset + second.page.delivered_bytes
        ]
    )
    assert second.source_occurrence_id == source.version.occurrence_id

    with pytest.raises(ContinuityReviewInputError, match="UTF-8 boundary"):
        await session.read_auxiliary_source(
            _actor("tool-call:read-auxiliary-split"),
            session_id=opened.session_id,
            expected_subject_revision=opened.subject_revision,
            memory_version_id=opened.memory_version_id,
            memory_sha256=opened.memory_sha256,
            logical_path=auxiliary.logical_path,
            version_id=auxiliary.version_id,
            content_hash=auxiliary.content_hash,
            offset=1,
            max_bytes=17,
        )


async def test_auxiliary_subject_source_notes_path_opens_with_note_kind() -> None:
    authority = _Authority()
    notes = _AuxiliarySubjectPortFake(
        "local",
        logical_path="life_engine_workspace/notes/relationships/xiaoxi.md",
        content="# 汐汐关系档案\n".encode("utf-8"),
    )
    session = _session(
        authority,
        _Repository(),
        _Ledger(),
        subject_documents=notes,
    )
    opened = await session.open(_actor())

    auxiliary = await session.open_auxiliary_source(
        _actor("tool-call:open-notes-auxiliary"),
        session_id=opened.session_id,
        expected_subject_revision=opened.subject_revision,
        memory_version_id=opened.memory_version_id,
        memory_sha256=opened.memory_sha256,
        logical_path="life_engine_workspace/notes/relationships/xiaoxi.md",
        max_bytes=1024,
    )

    assert auxiliary.source_kind == "note_document"


@pytest.mark.parametrize(
    "logical_path",
    [
        "SOUL.md",
        "USER.md",
        "MEMORY.md",
        "life_engine_workspace/MEMORY.md",
        "/absolute/diaries/witness.md",
        "C:/diaries/witness.md",
        "../diaries/witness.md",
        "life_engine_workspace/../diaries/witness.md",
        "life_engine_workspace/diaries/../../MEMORY.md",
        "life_engine_workspace\\diaries\\witness.md",
        "life_engine_workspace/notes/../../MEMORY.md",
    ],
)
async def test_auxiliary_subject_path_is_confined_to_controlled_diaries(
    logical_path: str,
) -> None:
    authority = _Authority()
    source = _AuxiliarySubjectPortFake("local")
    session = _session(
        authority,
        _Repository(),
        _Ledger(),
        subject_documents=source,
    )
    opened = await session.open(_actor())

    with pytest.raises(ContinuityReviewInputError, match="logical_path"):
        await session.open_auxiliary_source(
            _actor("tool-call:unsafe-auxiliary"),
            session_id=opened.session_id,
            expected_subject_revision=opened.subject_revision,
            memory_version_id=opened.memory_version_id,
            memory_sha256=opened.memory_sha256,
            logical_path=logical_path,
        )
    assert not source.get_head_calls


async def test_auxiliary_source_requires_public_reader_and_exact_version_pins() -> None:
    authority = _Authority()
    opened = await _session(authority, _Repository(), _Ledger()).open(_actor())
    unavailable = _session(authority, _Repository(), _Ledger())
    with pytest.raises(ContinuityReviewRuntimeUnavailable):
        await unavailable.open_auxiliary_source(
            _actor("tool-call:no-reader"),
            session_id=opened.session_id,
            expected_subject_revision=opened.subject_revision,
            memory_version_id=opened.memory_version_id,
            memory_sha256=opened.memory_sha256,
            logical_path=AUXILIARY_PATH,
        )

    source = _AuxiliarySubjectPortFake("mysql")
    session = _session(
        authority,
        _Repository(),
        _Ledger(),
        subject_documents=source,
    )
    auxiliary = await session.open_auxiliary_source(
        _actor("tool-call:open-pinned"),
        session_id=opened.session_id,
        expected_subject_revision=opened.subject_revision,
        memory_version_id=opened.memory_version_id,
        memory_sha256=opened.memory_sha256,
        logical_path=AUXILIARY_PATH,
    )
    common = {
        "session_id": opened.session_id,
        "expected_subject_revision": opened.subject_revision,
        "memory_version_id": opened.memory_version_id,
        "memory_sha256": opened.memory_sha256,
        "logical_path": auxiliary.logical_path,
        "offset": 0,
        "max_bytes": 32,
    }
    with pytest.raises(ContinuityReviewAuxiliarySourceNotFound):
        await session.read_auxiliary_source(
            _actor("tool-call:missing-version"),
            version_id="missing-version",
            content_hash=auxiliary.content_hash,
            **common,
        )
    with pytest.raises(ContinuityReviewStale, match="VersionPinsStale"):
        await session.read_auxiliary_source(
            _actor("tool-call:wrong-hash"),
            version_id=auxiliary.version_id,
            content_hash="0" * 64,
            **common,
        )

    source.versions[auxiliary.version_id] = replace(
        source.version,
        content_bytes=b"X" + source.version.content_bytes[1:],
    )
    with pytest.raises(ContinuityReviewStale, match="VersionPinsStale"):
        await session.read_auxiliary_source(
            _actor("tool-call:tampered-store-bytes"),
            version_id=auxiliary.version_id,
            content_hash=auxiliary.content_hash,
            **common,
        )
    source.versions[auxiliary.version_id] = source.version

    authority.contents["MEMORY.md"] += b"changed\n"
    with pytest.raises(ContinuityReviewStale, match="SourcePinsStale"):
        await session.read_auxiliary_source(
            _actor("tool-call:stale-root"),
            version_id=auxiliary.version_id,
            content_hash=auxiliary.content_hash,
            **common,
        )


async def test_auxiliary_source_rejects_non_utf8_authority_bytes() -> None:
    authority = _Authority()
    source = _AuxiliarySubjectPortFake("local", content=b"valid\n\xff")
    session = _session(
        authority,
        _Repository(),
        _Ledger(),
        subject_documents=source,
    )
    opened = await session.open(_actor())

    with pytest.raises(ContinuityReviewInputError, match="valid UTF-8"):
        await session.open_auxiliary_source(
            _actor("tool-call:binary-source"),
            session_id=opened.session_id,
            expected_subject_revision=opened.subject_revision,
            memory_version_id=opened.memory_version_id,
            memory_sha256=opened.memory_sha256,
            logical_path=AUXILIARY_PATH,
        )


async def test_unchanged_replays_one_immutable_outcome_without_candidate() -> None:
    authority = _Authority()
    repository = _Repository()
    ledger = _Ledger()
    recorder = _OutcomeRecorder(fail_after_record_once={"unchanged"})
    session = _session(
        authority,
        repository,
        ledger,
        recorder=recorder,
    )
    opened = await session.open(_actor())
    actor = _actor("tool-call:unchanged")
    kwargs = {
        "session_id": opened.session_id,
        "expected_subject_revision": opened.subject_revision,
        "memory_version_id": opened.memory_version_id,
        "memory_sha256": opened.memory_sha256,
        "reason": "I reviewed this exact version and choose no change.",
    }

    failed = await session.unchanged(actor, **kwargs)
    replayed = await session.unchanged(actor, **kwargs)

    assert failed.status == "failed"
    assert replayed.status == "recorded"
    assert failed.outcome_occurrence_id == replayed.outcome_occurrence_id
    assert len(recorder.events) == 1
    outcome = recorder.calls[-1]
    assert outcome.outcome_kind == "unchanged"
    assert outcome.subject_revision_before == opened.subject_revision
    assert outcome.subject_revision_after == opened.subject_revision
    assert outcome.candidate_id == ""
    assert outcome.snooze_hours == 0
    assert not ledger.candidates
    assert repository.append_attempts == 0
    assert authority.accept_calls == 0


async def test_snooze_is_pinned_bounded_and_creates_no_candidate() -> None:
    authority = _Authority()
    repository = _Repository()
    ledger = _Ledger()
    recorder = _OutcomeRecorder()
    session = _session(authority, repository, ledger, recorder=recorder)
    opened = await session.open(_actor())
    actor = _actor("tool-call:snooze")

    recorded = await session.snooze(
        actor,
        session_id=opened.session_id,
        expected_subject_revision=opened.subject_revision,
        memory_version_id=opened.memory_version_id,
        memory_sha256=opened.memory_sha256,
        reason="I choose to return to this review later.",
        snooze_hours=720,
    )

    assert recorded.status == "recorded"
    assert recorder.calls[-1].outcome_kind == "snooze"
    assert recorder.calls[-1].snooze_hours == 720
    assert not ledger.candidates
    assert repository.append_attempts == 0

    for invalid in (0, 721, True):
        with pytest.raises(ContinuityReviewInputError, match="snooze_hours"):
            await session.snooze(
                _actor(f"tool-call:snooze:{invalid}"),
                session_id=opened.session_id,
                expected_subject_revision=opened.subject_revision,
                memory_version_id=opened.memory_version_id,
                memory_sha256=opened.memory_sha256,
                reason="I supplied an invalid delay for this contract test.",
                snooze_hours=invalid,
            )
    assert len(recorder.calls) == 1


async def test_unchanged_and_snooze_validate_actor_and_pins_before_recording() -> None:
    authority = _Authority()
    recorder = _OutcomeRecorder()
    session = _session(authority, _Repository(), _Ledger(), recorder=recorder)
    opened = await session.open(_actor())
    common = {
        "session_id": opened.session_id,
        "expected_subject_revision": opened.subject_revision,
        "memory_version_id": opened.memory_version_id,
        "memory_sha256": opened.memory_sha256,
        "reason": "I reviewed this exact version.",
    }
    inactive = ContinuityReviewActorContext(
        consciousness_instance_id="inactive",
        stream_scope="chat:one",
        source_occurrence_id="message:one",
        action_occurrence_id="tool-call:inactive",
        occurred_at="2026-08-12T00:00:00+00:00",
    )

    with pytest.raises(ContinuityReviewActorInactive):
        await session.unchanged(inactive, **common)
    authority.contents["MEMORY.md"] += b"changed\n"
    with pytest.raises(ContinuityReviewStale):
        await session.snooze(_actor("tool-call:stale"), **common, snooze_hours=24)
    assert not recorder.calls


async def test_no_candidate_review_requires_an_outcome_recorder() -> None:
    authority = _Authority()
    session = _session(authority, _Repository(), _Ledger())
    opened = await session.open(_actor())

    result = await session.unchanged(
        _actor("tool-call:unchanged-without-recorder"),
        session_id=opened.session_id,
        expected_subject_revision=opened.subject_revision,
        memory_version_id=opened.memory_version_id,
        memory_sha256=opened.memory_sha256,
        reason="I choose no change, but the projection is unavailable.",
    )
    assert result.status == "not_configured"
    assert result.outcome_occurrence_id


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
        + f"[{_plans()[1][0].anchor_text}]({stored.exact_uri})".encode()
        + MEMORY_BYTES[RANGE_END:]
    )
    assert candidate.candidate_content_bytes == expected
    assert authority.accept_calls == 0


async def test_prepare_inserts_server_derived_auxiliary_boundary_index() -> None:
    authority = _Authority()
    source = _AuxiliarySubjectPortFake("mysql")
    repository = _Repository()
    ledger = _Ledger()
    session = _session(
        authority,
        repository,
        ledger,
        subject_documents=source,
    )
    opened = await session.open(_actor())
    auxiliary = await session.open_auxiliary_source(
        _actor("tool-call:open-witness"),
        session_id=opened.session_id,
        expected_subject_revision=opened.subject_revision,
        memory_version_id=opened.memory_version_id,
        memory_sha256=opened.memory_sha256,
        logical_path=AUXILIARY_PATH,
    )
    boundaries, edits = _auxiliary_plans(
        source,
        anchor_start=len(MEMORY_BYTES),
        anchor_end=len(MEMORY_BYTES),
    )

    prepared = await session.prepare_candidate(
        _actor("tool-call:prepare-witness-index"),
        session_id=opened.session_id,
        expected_subject_revision=opened.subject_revision,
        memory_version_id=opened.memory_version_id,
        memory_sha256=opened.memory_sha256,
        boundaries=boundaries,
        edits=edits,
        reason="I explicitly choose to index this exact immutable witness.",
    )

    stored = repository.heads["external-important-day"]
    segment = stored.manifest.segments[0]
    assert segment.content.encode("utf-8") == AUXILIARY_RANGE_BYTES
    assert segment.source_occurrence_ids == (source.version.occurrence_id,)
    assert len(segment.source_refs) == 1
    source_ref = segment.source_refs[0]
    assert source_ref.startswith(f"subject://{AUXILIARY_PATH}@")
    assert auxiliary.version_id in source_ref
    assert f"sha256={auxiliary.content_hash}" in source_ref
    assert f"bytes={AUXILIARY_RANGE_START}-{AUXILIARY_RANGE_END}" in source_ref
    assert "range_sha256=" in source_ref
    candidate = ledger.candidates[prepared.candidate_id]
    expected_link = f"[{edits[0].anchor_text}]({stored.exact_uri})".encode()
    assert candidate.candidate_content_bytes == MEMORY_BYTES + expected_link
    assert prepared.auxiliary_source_count == 1
    assert prepared.auxiliary_segment_count == 1
    assert len(prepared.boundary_source_plan_sha256) == 64
    assert candidate.provenance["auxiliary_subject_source_count"] == 1
    assert candidate.provenance["auxiliary_subject_segment_count"] == 1
    assert candidate.provenance["auxiliary_subject_content_server_derived"] is True
    assert candidate.provenance["caller_supplied_complete_candidate"] is False


async def test_auxiliary_boundary_can_replace_an_existing_memory_index() -> None:
    old_index = "[旧索引](memory://old)".encode()
    memory = b"before\n" + old_index + b"\nafter\n"
    authority = _Authority(memory)
    source = _AuxiliarySubjectPortFake("local")
    repository = _Repository()
    ledger = _Ledger()
    session = _session(
        authority,
        repository,
        ledger,
        subject_documents=source,
    )
    opened = await session.open(_actor())
    start = memory.index(old_index)
    boundaries, edits = _auxiliary_plans(
        source,
        anchor_start=start,
        anchor_end=start + len(old_index),
    )

    prepared = await session.prepare_candidate(
        _actor("tool-call:replace-old-index"),
        session_id=opened.session_id,
        expected_subject_revision=opened.subject_revision,
        memory_version_id=opened.memory_version_id,
        memory_sha256=opened.memory_sha256,
        boundaries=boundaries,
        edits=edits,
        reason="I choose to replace the stale index with this exact witness.",
    )

    candidate = ledger.candidates[prepared.candidate_id].candidate_content_bytes
    assert old_index not in candidate
    assert repository.heads["external-important-day"].exact_uri.encode() in candidate
    assert candidate.startswith(b"before\n[")
    assert candidate.endswith(b"\nafter\n")


async def test_auxiliary_current_memory_and_text_edits_share_one_delta_plan() -> None:
    authority = _Authority()
    source = _AuxiliarySubjectPortFake("local")
    repository = _Repository()
    ledger = _Ledger()
    session = _session(
        authority,
        repository,
        ledger,
        subject_documents=source,
    )
    opened = await session.open(_actor())
    memory_boundaries, memory_edits = _plans()
    external_boundaries, external_edits = _auxiliary_plans(
        source,
        anchor_start=len(MEMORY_BYTES),
        anchor_end=len(MEMORY_BYTES),
    )
    all_boundaries = memory_boundaries + external_boundaries
    all_edits = memory_edits + (replace(external_edits[0], boundary_slot=1),)
    text_edits = (SubjectTextEdit(0, 0, "新的开场。\n"),)

    prepared = await session.prepare_candidate(
        _actor("tool-call:combined-source-plan"),
        session_id=opened.session_id,
        expected_subject_revision=opened.subject_revision,
        memory_version_id=opened.memory_version_id,
        memory_sha256=opened.memory_sha256,
        boundaries=all_boundaries,
        edits=all_edits,
        text_edits=text_edits,
        reason="I explicitly combine these non-overlapping exact deltas.",
    )

    assert prepared.boundary_anchor_edit_count == 2
    assert prepared.subject_text_edit_count == 1
    assert prepared.auxiliary_source_count == 1
    candidate = ledger.candidates[prepared.candidate_id].candidate_content_bytes
    assert candidate.startswith("新的开场。\n".encode())
    assert len(repository.heads) == 2

    conflict_repository = _Repository()
    conflict = _session(
        authority,
        conflict_repository,
        _Ledger(),
        subject_documents=source,
    )
    conflicting_external, conflicting_edits = _auxiliary_plans(
        source,
        anchor_start=RANGE_START,
        anchor_end=RANGE_START,
    )
    with pytest.raises(ContinuityReviewInputError, match="ambiguous shared point"):
        await conflict.prepare_candidate(
            _actor("tool-call:overlapping-source-plan"),
            session_id=opened.session_id,
            expected_subject_revision=opened.subject_revision,
            memory_version_id=opened.memory_version_id,
            memory_sha256=opened.memory_sha256,
            boundaries=memory_boundaries + conflicting_external,
            edits=memory_edits + (replace(conflicting_edits[0], boundary_slot=1),),
            reason="This ambiguous plan must fail.",
        )
    assert conflict_repository.append_attempts == 0

    with pytest.raises(ContinuityReviewInputError, match="ambiguous shared point"):
        await conflict.prepare_candidate(
            _actor("tool-call:text-source-point-conflict"),
            session_id=opened.session_id,
            expected_subject_revision=opened.subject_revision,
            memory_version_id=opened.memory_version_id,
            memory_sha256=opened.memory_sha256,
            boundaries=conflicting_external,
            edits=(replace(conflicting_edits[0], byte_start=0, byte_end=0),),
            text_edits=(SubjectTextEdit(0, 0, "same point"),),
            reason="This same-point plan must fail.",
        )
    assert conflict_repository.append_attempts == 0


async def test_auxiliary_segment_utf8_and_replay_conflicts_fail_before_candidate() -> (
    None
):
    authority = _Authority()
    source = _AuxiliarySubjectPortFake("mysql")
    repository = _Repository()
    ledger = _Ledger()
    session = _session(
        authority,
        repository,
        ledger,
        subject_documents=source,
    )
    opened = await session.open(_actor())
    split_boundaries, split_edits = _auxiliary_plans(
        source,
        anchor_start=len(MEMORY_BYTES),
        anchor_end=len(MEMORY_BYTES),
        range_start=AUXILIARY_RANGE_START + 1,
    )
    with pytest.raises(ContinuityReviewInputError, match="UTF-8 byte boundaries"):
        await session.prepare_candidate(
            _actor("tool-call:split-external-range"),
            session_id=opened.session_id,
            expected_subject_revision=opened.subject_revision,
            memory_version_id=opened.memory_version_id,
            memory_sha256=opened.memory_sha256,
            boundaries=split_boundaries,
            edits=split_edits,
            reason="A split codepoint is invalid.",
        )
    assert repository.append_attempts == 0
    assert not ledger.candidates

    boundaries, edits = _auxiliary_plans(
        source,
        anchor_start=len(MEMORY_BYTES),
        anchor_end=len(MEMORY_BYTES),
    )
    kwargs = {
        "session_id": opened.session_id,
        "expected_subject_revision": opened.subject_revision,
        "memory_version_id": opened.memory_version_id,
        "memory_sha256": opened.memory_sha256,
        "boundaries": boundaries,
        "edits": edits,
        "reason": "I choose this exact external memory index.",
    }
    actor = _actor("tool-call:replay-external-boundary")
    first = await session.prepare_candidate(actor, **kwargs)
    replay = await session.prepare_candidate(actor, **kwargs)
    assert replay.candidate_id == first.candidate_id
    assert replay.candidate_sha256 == first.candidate_sha256
    assert len(repository.by_occurrence) == 1
    assert len(ledger.candidates) == 1

    changed_boundaries, _ = _auxiliary_plans(
        source,
        anchor_start=len(MEMORY_BYTES),
        anchor_end=len(MEMORY_BYTES),
        range_end=AUXILIARY_RANGE_END - len("。".encode()),
    )
    with pytest.raises(RuntimeError, match="operation occurrence conflict"):
        await session.prepare_candidate(
            actor,
            **{**kwargs, "boundaries": changed_boundaries},
        )
    assert len(ledger.candidates) == 1


async def test_auxiliary_candidate_keeps_exact_delivery_and_independent_decision_gates() -> (
    None
):
    authority = _Authority()
    source = _AuxiliarySubjectPortFake("local")
    repository = _Repository()
    ledger = _Ledger()
    verifier = _Verifier("pending")
    session = _session(
        authority,
        repository,
        ledger,
        verifier=verifier,
        subject_documents=source,
    )
    opened = await session.open(_actor())
    boundaries, edits = _auxiliary_plans(
        source,
        anchor_start=len(MEMORY_BYTES),
        anchor_end=len(MEMORY_BYTES),
    )
    prepare_actor = _actor("tool-call:external-prepare-for-accept")
    prepared = await session.prepare_candidate(
        prepare_actor,
        session_id=opened.session_id,
        expected_subject_revision=opened.subject_revision,
        memory_version_id=opened.memory_version_id,
        memory_sha256=opened.memory_sha256,
        boundaries=boundaries,
        edits=edits,
        reason="I choose this exact external memory index.",
    )
    read = await session.read_candidate(
        _actor("tool-call:external-read-candidate"),
        session_id=opened.session_id,
        candidate_id=prepared.candidate_id,
        candidate_revision=prepared.candidate_revision,
        candidate_sha256=prepared.candidate_sha256,
        expected_subject_revision=opened.subject_revision,
        offset=0,
        max_bytes=32768,
    )
    verifier.trusted_delivery_id = str(read.delivery_binding["delivery_id"])
    receipt = {
        "delivery_id": verifier.trusted_delivery_id,
        "candidate_id": prepared.candidate_id,
        "candidate_revision": prepared.candidate_revision,
        "candidate_sha256": prepared.candidate_sha256,
        "delivered_bytes": read.page.delivered_bytes,
        "total_bytes": read.page.total_bytes,
    }
    decide_kwargs = {
        "session_id": opened.session_id,
        "candidate_id": prepared.candidate_id,
        "candidate_revision": prepared.candidate_revision,
        "candidate_sha256": prepared.candidate_sha256,
        "expected_subject_revision": opened.subject_revision,
        "decision_kind": "accept_requested",
        "reason": "I independently accept the fully delivered candidate.",
        "delivery_receipt": receipt,
    }
    with pytest.raises(ContinuityReviewIndependentDecisionRequired):
        await session.decide(prepare_actor, **decide_kwargs)

    persisted = await session.decide(
        _actor("tool-call:external-independent-accept"),
        **decide_kwargs,
    )
    assert persisted.receipt.status == "committed"
    assert persisted.exact_delivery_verified is True
    assert verifier.calls == 1


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (
            SubjectTextEdit(
                byte_start=len(MEMORY_BYTES),
                byte_end=len(MEMORY_BYTES),
                replacement="新的长期线索。\n",
            ),
            MEMORY_BYTES + "新的长期线索。\n".encode(),
        ),
        (
            SubjectTextEdit(
                byte_start=RANGE_START,
                byte_end=RANGE_END,
                replacement="我现在选择用短句保留。",
            ),
            MEMORY_BYTES[:RANGE_START]
            + "我现在选择用短句保留。".encode()
            + MEMORY_BYTES[RANGE_END:],
        ),
        (
            SubjectTextEdit(
                byte_start=RANGE_START,
                byte_end=RANGE_END,
                replacement="",
            ),
            MEMORY_BYTES[:RANGE_START] + MEMORY_BYTES[RANGE_END:],
        ),
    ],
    ids=("append", "update", "delete"),
)
async def test_subject_text_edit_mechanically_builds_traceable_candidate(
    edit: SubjectTextEdit,
    expected: bytes,
) -> None:
    authority = _Authority()
    repository = _Repository()
    ledger = _Ledger()
    session = _session(authority, repository, ledger)

    opened, prepared = await _prepare_text_edits(session, (edit,))

    candidate = ledger.candidates[prepared.candidate_id]
    assert candidate.candidate_content_bytes == expected
    assert candidate.actor_consciousness_instance_id == "chat-main"
    assert candidate.source_occurrence_id == "message:one"
    assert candidate.subject_revision == opened.subject_revision
    assert candidate.occurred_at == "2026-08-12T00:00:00+00:00"
    assert candidate.provenance["reviewed_memory_sha256"] == opened.memory_sha256
    assert candidate.provenance["reviewed_memory_version_id"] == (
        opened.memory_version_id
    )
    assert prepared.boundary_anchor_edit_count == 0
    assert prepared.subject_text_edit_count == 1
    assert prepared.subject_text_replacement_bytes == len(
        edit.replacement.encode("utf-8")
    )
    assert len(prepared.edit_plan_sha256) == 64
    assert candidate.provenance["continuity_review_edit_plan_sha256"] == (
        prepared.edit_plan_sha256
    )
    assert candidate.provenance["subject_text_from_active_consciousness"] is True
    assert candidate.provenance["candidate_generated_mechanically"] is True
    assert candidate.provenance["caller_supplied_complete_candidate"] is False
    expected_contents = dict(authority.contents)
    expected_contents["MEMORY.md"] = expected
    assert candidate.provenance["expected_subject_revision_after_accept"] == (
        subject_revision_from_contents(expected_contents)  # type: ignore[arg-type]
    )
    assert prepared.as_dict()["candidate_generated_mechanically"] is True
    assert repository.append_attempts == 0
    assert authority.accept_calls == 0


async def test_boundary_and_subject_text_edits_share_one_stable_delta_plan() -> None:
    authority = _Authority()
    repository = _Repository()
    ledger = _Ledger()
    session = _session(authority, repository, ledger)
    opened = await session.open(_actor())
    boundaries, boundary_edits = _plans()
    text_edit = SubjectTextEdit(
        byte_start=len(MEMORY_BYTES),
        byte_end=len(MEMORY_BYTES),
        replacement="补充索引后的短句。\n",
    )

    prepared = await session.prepare_candidate(
        _actor("tool-call:prepare-combined"),
        session_id=opened.session_id,
        expected_subject_revision=opened.subject_revision,
        memory_version_id=opened.memory_version_id,
        memory_sha256=opened.memory_sha256,
        boundaries=boundaries,
        edits=boundary_edits,
        text_edits=(text_edit,),
        reason="I choose the boundary and this short addition together.",
    )

    stored = repository.heads["important-day"]
    candidate = ledger.candidates[prepared.candidate_id]
    expected = (
        MEMORY_BYTES[:RANGE_START]
        + f"[{boundary_edits[0].anchor_text}]({stored.exact_uri})".encode()
        + MEMORY_BYTES[RANGE_END:]
        + text_edit.replacement.encode()
    )
    assert candidate.candidate_content_bytes == expected
    assert prepared.boundary_anchor_edit_count == 1
    assert prepared.subject_text_edit_count == 1


async def test_text_edit_input_order_does_not_change_candidate_or_plan_hash() -> None:
    authority = _Authority(b"abcdef")
    repository = _Repository()
    ledger = _Ledger()
    recorder = _OutcomeRecorder(fail_after_record_once={"candidate_proposed"})
    session = _session(authority, repository, ledger, recorder=recorder)
    first = SubjectTextEdit(0, 1, "A")
    second = SubjectTextEdit(5, 6, "F")

    _, prepared = await _prepare_text_edits(session, (second, first))
    _, replayed = await _prepare_text_edits(session, (first, second))

    assert prepared.candidate_id == replayed.candidate_id
    assert prepared.candidate_sha256 == replayed.candidate_sha256
    assert prepared.edit_plan_sha256 == replayed.edit_plan_sha256
    assert ledger.candidates[prepared.candidate_id].candidate_content_bytes == b"AbcdeF"
    assert ledger.append_attempts == 2
    assert prepared.outcome_recording.status == "failed"
    assert replayed.outcome_recording.status == "recorded"
    assert prepared.outcome_recording.outcome_occurrence_id == (
        replayed.outcome_recording.outcome_occurrence_id
    )
    assert len(recorder.events) == 1
    assert len(recorder.calls) == 2
    outcome = recorder.calls[-1]
    assert outcome.outcome_kind == "candidate_proposed"
    assert outcome.candidate_occurrence_id == replayed.candidate_occurrence_id
    assert outcome.candidate_id == replayed.candidate_id
    assert outcome.candidate_sha256 == replayed.candidate_sha256
    assert outcome.subject_revision_before == replayed.subject_revision
    assert outcome.reason == "I explicitly choose this short MEMORY text change."
    assert outcome.actor_consciousness_instance_id == "chat-main"


async def test_text_edit_acceptance_requires_exact_delivery_and_new_action() -> None:
    authority = _Authority()
    repository = _Repository()
    ledger = _Ledger()
    unverified_session = _session(authority, repository, ledger)
    opened, prepared = await _prepare_text_edits(
        unverified_session,
        (
            SubjectTextEdit(
                byte_start=len(MEMORY_BYTES),
                byte_end=len(MEMORY_BYTES),
                replacement="我确认保留这句。\n",
            ),
        ),
    )
    candidate = ledger.candidates[prepared.candidate_id]

    with pytest.raises(ContinuityReviewDeliveryUnverified):
        await unverified_session.decide(
            _actor("tool-call:decide-text"),
            session_id=opened.session_id,
            candidate_id=candidate.candidate_id,
            candidate_revision=candidate.candidate_revision,
            candidate_sha256=candidate.candidate_sha256,
            expected_subject_revision=candidate.subject_revision,
            decision_kind="accept_requested",
            reason="I independently choose to accept the exact candidate.",
        )

    receipt = {
        "delivery_id": "trusted-text-delivery",
        "candidate_id": candidate.candidate_id,
        "candidate_revision": candidate.candidate_revision,
        "candidate_sha256": candidate.candidate_sha256,
        "delivered_bytes": len(candidate.candidate_content_bytes),
        "total_bytes": len(candidate.candidate_content_bytes),
    }
    verified_session = _session(
        authority,
        repository,
        ledger,
        verifier=_Verifier("trusted-text-delivery"),
    )
    persisted = await verified_session.decide(
        _actor("tool-call:decide-text"),
        session_id=opened.session_id,
        candidate_id=candidate.candidate_id,
        candidate_revision=candidate.candidate_revision,
        candidate_sha256=candidate.candidate_sha256,
        expected_subject_revision=candidate.subject_revision,
        decision_kind="accept_requested",
        reason="I independently choose to accept the exact candidate.",
        delivery_receipt=receipt,
    )
    assert persisted.receipt.status == "committed"
    assert ledger.authority_accept_calls == 1


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

    auxiliary = dict(payload)
    auxiliary["segments"] = [
        {
            "segment_id": "witness-story",
            "title": "完整见证",
            "logical_path": AUXILIARY_PATH,
            "version_id": "subject-aux-local-v1",
            "content_hash": "0" * 64,
            "byte_start": AUXILIARY_RANGE_START,
            "byte_end": AUXILIARY_RANGE_END,
            "scope": "范围",
            "visibility": "private",
            "content": "forged body",
            "source_refs": ["forged"],
            "source_occurrence_ids": ["forged"],
        }
    ]
    with pytest.raises(ContinuityReviewInputError, match="unexpected"):
        ContinuityBoundaryPlan.from_payload(auxiliary)


def test_subject_text_edit_payload_is_strict_and_supports_explicit_delete() -> None:
    deleted = SubjectTextEdit.from_payload(
        {"byte_start": 1, "byte_end": 2, "replacement": ""}
    )
    assert deleted.replacement == ""
    inserted = SubjectTextEdit.from_payload(
        {"byte_start": 2, "byte_end": 2, "replacement": "新"}
    )
    assert inserted.byte_start == inserted.byte_end

    with pytest.raises(ContinuityReviewInputError, match="fields mismatch"):
        SubjectTextEdit.from_payload(
            {
                "byte_start": 1,
                "byte_end": 2,
                "replacement": "x",
                "candidate": "forged full document",
            }
        )
    with pytest.raises(ContinuityReviewInputError, match="zero-length"):
        SubjectTextEdit(2, 2, "")
    with pytest.raises(ContinuityReviewInputError, match="per-edit"):
        SubjectTextEdit(0, 0, "x" * (CONTINUITY_REVIEW_MAX_TEXT_EDIT_BYTES + 1))
    with pytest.raises(ContinuityReviewInputError, match="exact text"):
        SubjectTextEdit.from_payload({"byte_start": 0, "byte_end": 1, "replacement": 1})


async def test_prepare_rejects_no_edit_and_invalid_utf8_boundary_before_writes() -> (
    None
):
    authority = _Authority()
    repository = _Repository()
    ledger = _Ledger()
    session = _session(authority, repository, ledger)
    opened = await session.open(_actor())

    with pytest.raises(ContinuityReviewInputError, match="at least one explicit"):
        await session.prepare_candidate(
            _actor("tool-call:no-edit"),
            session_id=opened.session_id,
            expected_subject_revision=opened.subject_revision,
            memory_version_id=opened.memory_version_id,
            memory_sha256=opened.memory_sha256,
            boundaries=(),
            edits=(),
            reason="I choose nothing.",
        )
    with pytest.raises(ContinuityReviewInputError, match="UTF-8"):
        await session.prepare_candidate(
            _actor("tool-call:split-codepoint"),
            session_id=opened.session_id,
            expected_subject_revision=opened.subject_revision,
            memory_version_id=opened.memory_version_id,
            memory_sha256=opened.memory_sha256,
            boundaries=(),
            edits=(),
            text_edits=(SubjectTextEdit(RANGE_START + 1, RANGE_START + 1, "x"),),
            reason="I choose an invalid byte position only for this contract test.",
        )
    assert repository.append_attempts == 0
    assert ledger.append_attempts == 0


@pytest.mark.parametrize(
    ("text_edits", "error"),
    [
        (
            (
                SubjectTextEdit(1, 4, "first"),
                SubjectTextEdit(3, 5, "second"),
            ),
            "overlap",
        ),
        (
            (
                SubjectTextEdit(2, 2, "first"),
                SubjectTextEdit(2, 2, "second"),
            ),
            "ambiguous shared point",
        ),
        (
            (
                SubjectTextEdit(1, 3, "range"),
                SubjectTextEdit(3, 3, "at-end"),
            ),
            "ambiguous shared point",
        ),
    ],
)
async def test_text_edit_overlap_and_same_point_conflicts_fail_closed(
    text_edits: tuple[SubjectTextEdit, ...],
    error: str,
) -> None:
    authority = _Authority(b"abcdef")
    ledger = _Ledger()
    session = _session(authority, _Repository(), ledger)
    opened = await session.open(_actor())

    with pytest.raises(ContinuityReviewInputError, match=error):
        await session.prepare_candidate(
            _actor("tool-call:conflict"),
            session_id=opened.session_id,
            expected_subject_revision=opened.subject_revision,
            memory_version_id=opened.memory_version_id,
            memory_sha256=opened.memory_sha256,
            boundaries=(),
            edits=(),
            text_edits=text_edits,
            reason="I supplied conflicting edits for this contract test.",
        )
    assert ledger.append_attempts == 0


async def test_boundary_and_text_edit_same_point_is_rejected_before_append() -> None:
    authority = _Authority()
    repository = _Repository()
    session = _session(authority, repository, _Ledger())
    opened = await session.open(_actor())
    boundaries, edits = _plans()

    with pytest.raises(ContinuityReviewInputError, match="ambiguous shared point"):
        await session.prepare_candidate(
            _actor("tool-call:boundary-point-conflict"),
            session_id=opened.session_id,
            expected_subject_revision=opened.subject_revision,
            memory_version_id=opened.memory_version_id,
            memory_sha256=opened.memory_sha256,
            boundaries=boundaries,
            edits=edits,
            text_edits=(SubjectTextEdit(RANGE_START, RANGE_START, "before"),),
            reason="I supplied an ambiguous point for this contract test.",
        )
    assert repository.append_attempts == 0


async def test_text_edit_count_and_total_utf8_budgets_are_hard_limits() -> None:
    authority = _Authority(b"x" * 128)
    ledger = _Ledger()
    session = _session(authority, _Repository(), ledger)
    opened = await session.open(_actor())
    too_many = tuple(
        SubjectTextEdit(position, position, "y")
        for position in range(CONTINUITY_REVIEW_MAX_TEXT_EDITS + 1)
    )

    with pytest.raises(ContinuityReviewInputError, match="count"):
        await session.prepare_candidate(
            _actor("tool-call:too-many"),
            session_id=opened.session_id,
            expected_subject_revision=opened.subject_revision,
            memory_version_id=opened.memory_version_id,
            memory_sha256=opened.memory_sha256,
            boundaries=(),
            edits=(),
            text_edits=too_many,
            reason="I supplied too many edits for this contract test.",
        )

    per_edit_bytes = CONTINUITY_REVIEW_MAX_TEXT_EDIT_BYTES
    required_edits = CONTINUITY_REVIEW_MAX_TEXT_REPLACEMENT_BYTES // per_edit_bytes + 1
    over_total = tuple(
        SubjectTextEdit(position, position, "y" * per_edit_bytes)
        for position in range(required_edits)
    )
    with pytest.raises(ContinuityReviewInputError, match="total UTF-8"):
        await session.prepare_candidate(
            _actor("tool-call:too-large"),
            session_id=opened.session_id,
            expected_subject_revision=opened.subject_revision,
            memory_version_id=opened.memory_version_id,
            memory_sha256=opened.memory_sha256,
            boundaries=(),
            edits=(),
            text_edits=over_total,
            reason="I supplied too many replacement bytes for this contract test.",
        )
    assert ledger.append_attempts == 0


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


async def test_retry_after_candidate_append_response_loss_is_idempotent() -> None:
    authority = _Authority()
    repository = _Repository()
    ledger = _Ledger(fail_after_append_once=True)
    session = _session(authority, repository, ledger)

    with pytest.raises(RuntimeError, match="response loss"):
        await _prepare(session)
    assert len(repository.heads) == 1
    assert len(repository.by_occurrence) == 1
    assert len(ledger.candidates) == 1
    persisted_before_retry = next(iter(ledger.candidates.values()))

    _, prepared = await _prepare(session)

    assert prepared.candidate_id == persisted_before_retry.candidate_id
    assert ledger.candidates[prepared.candidate_id] == persisted_before_retry
    assert len(repository.heads) == 1
    assert len(repository.by_occurrence) == 1
    assert len(ledger.candidates) == 1
    assert repository.append_attempts == 2
    assert ledger.append_attempts == 2


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
    ledger = _AuthorityUpdatingLedger(authority)
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
    recorder = _OutcomeRecorder()
    verified_session = _session(
        authority,
        repository,
        ledger,
        verifier=verifier,
        recorder=recorder,
    )
    reads_before_commit = authority.read_calls
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
    assert persisted.outcome_recording is not None
    assert persisted.outcome_recording.status == "recorded"
    committed = recorder.calls[-1]
    assert committed.outcome_kind == "committed"
    assert committed.subject_revision_before == candidate.subject_revision
    assert committed.subject_revision_after == authority.revision
    assert committed.subject_revision_after != committed.subject_revision_before
    assert (
        committed.authority_occurrence_id == persisted.receipt.authority_occurrence_id
    )
    assert authority.read_calls == reads_before_commit + 2


async def test_committed_outcome_failure_does_not_hide_authority_commit() -> None:
    authority = _Authority()
    repository = _Repository()
    ledger = _AuthorityUpdatingLedger(authority)
    recorder = _OutcomeRecorder(fail_after_record_once={"committed"})
    verifier = _Verifier("trusted-commit-projection")
    session = _session(
        authority,
        repository,
        ledger,
        verifier=verifier,
        recorder=recorder,
    )
    opened, prepared = await _prepare_text_edits(
        session,
        (
            SubjectTextEdit(
                len(MEMORY_BYTES),
                len(MEMORY_BYTES),
                "我明确接受的短句。\n",
            ),
        ),
    )
    candidate = ledger.candidates[prepared.candidate_id]
    receipt = {
        "delivery_id": "trusted-commit-projection",
        "candidate_id": candidate.candidate_id,
        "candidate_revision": candidate.candidate_revision,
        "candidate_sha256": candidate.candidate_sha256,
        "delivered_bytes": len(candidate.candidate_content_bytes),
        "total_bytes": len(candidate.candidate_content_bytes),
    }

    persisted = await session.decide(
        _actor("tool-call:commit-with-projection-failure"),
        session_id=opened.session_id,
        candidate_id=candidate.candidate_id,
        candidate_revision=candidate.candidate_revision,
        candidate_sha256=candidate.candidate_sha256,
        expected_subject_revision=candidate.subject_revision,
        decision_kind="accept_requested",
        reason="I accept the exact candidate despite this projection test.",
        delivery_receipt=receipt,
    )

    assert persisted.receipt.status == "committed"
    assert persisted.receipt.authority_occurrence_id
    assert persisted.outcome_recording is not None
    assert persisted.outcome_recording.status == "failed"
    assert persisted.outcome_recording.error_type == "RuntimeError"
    assert persisted.as_dict()["subject_authority_committed"] is True
    assert persisted.as_dict()["outcome_recording"]["status"] == "failed"
    assert authority.contents["MEMORY.md"] == candidate.candidate_content_bytes
    assert ledger.authority_accept_calls == 1


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
    recorder = _OutcomeRecorder(fail_after_record_once={"rejected"})
    session = _session(authority, repository, ledger, recorder=recorder)
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
    assert replay.receipt == persisted.receipt
    assert persisted.outcome_recording is not None
    assert persisted.outcome_recording.status == "failed"
    assert replay.outcome_recording is not None
    assert replay.outcome_recording.status == "recorded"
    assert persisted.outcome_recording.outcome_occurrence_id == (
        replay.outcome_recording.outcome_occurrence_id
    )
    payload = persisted.as_dict()
    assert payload["status"] == "rejected"
    assert payload["decision_recorded"] is True
    assert payload["subject_authority_committed"] is False
    assert "accepted_content_bytes" not in payload
    assert ledger.statuses[prepared.candidate_id] == "rejected"
    rejected = recorder.calls[-1]
    assert rejected.outcome_kind == "rejected"
    assert rejected.decision_occurrence_id == persisted.receipt.decision_occurrence_id
    assert rejected.subject_revision_before == prepared.subject_revision
    assert rejected.subject_revision_after == prepared.subject_revision


async def test_kept_open_is_persisted_without_calling_subject_authority() -> None:
    authority = _Authority()
    ledger = _Ledger()
    recorder = _OutcomeRecorder()
    session = _session(authority, _Repository(), ledger, recorder=recorder)
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
    assert persisted.outcome_recording is not None
    assert persisted.outcome_recording.status == "recorded"
    assert recorder.calls[-1].outcome_kind == "kept_open"


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
    assert "SubjectTextEdit" in LifeMemoryContinuityReviewSessionTool.tool_description
    assert "当前活跃意识" in LifeMemoryContinuityReviewSessionTool.tool_description
    assert (
        "精确 UTF-8 字节切片" in LifeMemoryContinuityReviewSessionTool.tool_description
    )
    assert "Witness" in LifeMemoryContinuityReviewSessionTool.tool_description
    assert "source_refs" in LifeMemoryContinuityReviewSessionTool.tool_description
    assert "unchanged" in LifeMemoryContinuityReviewSessionTool.tool_description
    assert "snooze" in LifeMemoryContinuityReviewSessionTool.tool_description
    tool = LifeMemoryContinuityReviewSessionTool(plugin=SimpleNamespace())
    failed, error = await tool.execute("open")
    assert failed is False
    assert error["error"] == "ContinuityReviewRuntimeUnavailable"
    assert error["authority_written"] is False

    authority = _Authority()
    source = _AuxiliarySubjectPortFake("local")
    domain = _session(
        authority,
        _Repository(),
        _Ledger(),
        subject_documents=source,
    )

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

    ok, auxiliary = await tool.execute(
        "open_auxiliary_source",
        session_id=str(payload["session_id"]),
        expected_subject_revision=str(payload["subject_revision"]),
        memory_version_id=str(payload["memory_version_id"]),
        memory_sha256=str(payload["memory_sha256"]),
        auxiliary_logical_path=AUXILIARY_PATH,
        max_bytes=16,
    )
    assert ok is True
    assert auxiliary["action"] == "auxiliary_source_opened"
    assert auxiliary["source_kind"] == "witness_projection"

    ok, read_auxiliary = await tool.execute(
        "read_auxiliary_source",
        session_id=str(payload["session_id"]),
        expected_subject_revision=str(payload["subject_revision"]),
        memory_version_id=str(payload["memory_version_id"]),
        memory_sha256=str(payload["memory_sha256"]),
        auxiliary_logical_path=str(auxiliary["logical_path"]),
        auxiliary_version_id=str(auxiliary["version_id"]),
        auxiliary_content_hash=str(auxiliary["content_hash"]),
        offset=int(auxiliary["page"]["next_offset"]),
        max_bytes=16,
    )
    assert ok is True
    assert read_auxiliary["action"] == "auxiliary_source_read"
    assert read_auxiliary["authority_written"] is False

    recorded, unchanged = await tool.execute(
        "unchanged",
        session_id=str(payload["session_id"]),
        expected_subject_revision=str(payload["subject_revision"]),
        memory_version_id=str(payload["memory_version_id"]),
        memory_sha256=str(payload["memory_sha256"]),
        reason="I explicitly choose no change.",
    )
    assert recorded is False
    assert unchanged["candidate_created"] is False
    assert unchanged["outcome_recording"]["status"] == "not_configured"

    ok, prepared = await tool.execute(
        "prepare_candidate",
        session_id=str(payload["session_id"]),
        expected_subject_revision=str(payload["subject_revision"]),
        memory_version_id=str(payload["memory_version_id"]),
        memory_sha256=str(payload["memory_sha256"]),
        text_edits=[
            {
                "byte_start": len(MEMORY_BYTES),
                "byte_end": len(MEMORY_BYTES),
                "replacement": "主体直接提供的短句。\n",
            }
        ],
        reason="I explicitly choose this tool-supplied short edit.",
    )
    assert ok is True
    assert prepared["action"] == "candidate_prepared"
    assert prepared["subject_text_edit_count"] == 1
    assert prepared["boundary_anchor_edit_count"] == 0


async def test_single_tool_dispatches_recorded_snooze(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _Authority()
    recorder = _OutcomeRecorder()
    domain = _session(
        authority,
        _Repository(),
        _Ledger(),
        recorder=recorder,
    )
    opened = await domain.open(_actor())

    async def _runtime(_tool: Any) -> ContinuityReviewToolRuntime:
        return ContinuityReviewToolRuntime(domain, _actor("tool-call:snooze-tool"))

    monkeypatch.setattr(
        continuity_tools,
        "resolve_continuity_review_tool_runtime",
        _runtime,
    )
    tool = LifeMemoryContinuityReviewSessionTool(plugin=SimpleNamespace())
    ok, payload = await tool.execute(
        "snooze",
        session_id=opened.session_id,
        expected_subject_revision=opened.subject_revision,
        memory_version_id=opened.memory_version_id,
        memory_sha256=opened.memory_sha256,
        snooze_hours=24,
        reason="I explicitly choose to revisit this exact version tomorrow.",
    )

    assert ok is True
    assert payload["action"] == "snooze"
    assert payload["snooze_hours"] == 24
    assert payload["outcome_recording"]["status"] == "recorded"
    assert recorder.calls[-1].snooze_hours == 24
