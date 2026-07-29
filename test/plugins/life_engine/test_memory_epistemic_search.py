"""记忆检索模式与修正来源权限回归测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

from plugins.life_engine.memory.epistemic import (
    AuthorityClass,
    MemoryStateEvent,
    append_claim,
    append_state_event,
    create_epistemic_schema,
    new_claim,
)
from plugins.life_engine.memory.experience import (
    EpistemicKind,
    EvidenceAwareMemoryResult,
    MemorySearchMode,
    WitnessMemory,
)
from plugins.life_engine.memory.lineage import (
    MemoryCorrection,
    MemoryEvidence,
)
from plugins.life_engine.memory.service import LifeMemoryService


def _service(tmp_path: Path) -> LifeMemoryService:
    return LifeMemoryService(tmp_path)


def test_reflection_is_candidate_not_current_truth(tmp_path: Path) -> None:
    service = _service(tmp_path)
    evidence = [
        MemoryEvidence(
            file_path="notes/current.md",
            title="当前记录",
            snippet="原始证据内容",
            relevance=1.0,
            source="direct",
        )
    ]
    reflection = MemoryCorrection(
        correction_id="reflection-1",
        topic="主题",
        message="自动反思得出的新解释",
        source="reflection",
        created_at=2.0,
    )

    current = service._build_current_understanding(
        "notes/current.md",
        evidence,
        [reflection],
    )
    uncertainty = service._build_bundle_uncertainty(
        "notes/current.md",
        "notes/current.md",
        evidence,
        [reflection],
    )

    assert "自动反思得出的新解释" not in current
    assert "原始证据内容" in current
    assert "尚未被确认" in uncertainty
    assert "不能覆盖原始经历" in uncertainty


def test_explicit_user_correction_can_be_current_understanding(tmp_path: Path) -> None:
    service = _service(tmp_path)
    evidence = [
        MemoryEvidence(
            file_path="notes/current.md",
            title="当前记录",
            snippet="旧状态",
            relevance=1.0,
            source="direct",
        )
    ]
    corrections = [
        MemoryCorrection(
            correction_id="reflection-1",
            topic="主题",
            message="更晚但未确认的自动反思",
            source="reflection",
            created_at=5.0,
        ),
        MemoryCorrection(
            correction_id="user-1",
            topic="主题",
            message="用户明确确认的新状态",
            source="user",
            created_at=3.0,
        ),
    ]

    current = service._build_current_understanding(
        "notes/current.md",
        evidence,
        corrections,
    )

    assert current == "已确认修正：用户明确确认的新状态"


async def test_evidence_search_keeps_rank_and_confidence_separate(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)

    async def _documents(*_args: object, **_kwargs: object) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                file_path="notes/fact.md",
                title="事实记录",
                snippet="文档证据",
                relevance=0.8,
                source="direct",
                score_kind="rank",
                association_path=[],
                association_reason="",
            )
        ]

    async def _witnesses(*_args: object, **_kwargs: object) -> list[object]:
        return []

    service.search_memory = _documents  # type: ignore[method-assign]
    service.search_witness_memories = _witnesses  # type: ignore[method-assign]

    results = await service.search_evidence_aware(
        "查询",
        mode=MemorySearchMode.CURRENT_FACT,
    )

    assert results == [
        EvidenceAwareMemoryResult(
            record_id="notes/fact.md",
            kind="document_evidence",
            content="文档证据",
            rank_score=0.8,
            confidence=None,
            source="document_direct",
            provenance=("notes/fact.md",),
            metadata={
                "title": "事实记录",
                "score_kind": "rank",
                "association_path": [],
                "association_reason": "",
            },
        )
    ]


async def test_current_fact_search_returns_active_claim_before_document(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service._db = sqlite3.connect(":memory:", check_same_thread=False)
    service._db.row_factory = sqlite3.Row
    service._db.execute("PRAGMA foreign_keys = ON")
    create_epistemic_schema(service._db)
    claim = append_claim(
        service._db,
        new_claim(
            claim_id="fact-claim",
            subject_key="elysia:location",
            content="爱莉现在在花园",
            claim_kind="world_fact",
            source="user",
            valid_from="2026-07-01T00:00:00+08:00",
            recorded_at="2026-07-01T00:01:00+08:00",
        ),
    )
    append_state_event(
        service._db,
        MemoryStateEvent(
            event_id="confirm-fact",
            entity_type="claim",
            entity_id=claim.claim_id,
            event_type="claim_confirmed",
            actor="user",
            authority=AuthorityClass.EXPLICIT_USER.value,
            reason="明确确认",
            recorded_at="2026-07-01T00:02:00+08:00",
            valid_at="2026-07-01T00:00:00+08:00",
        ),
    )

    async def _documents(*_args: object, **_kwargs: object) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                file_path="notes/location.md",
                title="位置记录",
                snippet="爱莉以前在别处",
                relevance=1.0,
                source="direct",
                score_kind="rank",
                association_path=[],
                association_reason="",
            )
        ]

    async def _witnesses(*_args: object, **_kwargs: object) -> list[object]:
        return []

    service.search_memory = _documents  # type: ignore[method-assign]
    service.search_witness_memories = _witnesses  # type: ignore[method-assign]
    results = await service.search_evidence_aware(
        "爱莉",
        mode=MemorySearchMode.CURRENT_FACT,
        valid_at="2026-07-02T00:00:00+08:00",
        recorded_as_of="2026-07-02T00:00:00+08:00",
    )

    assert results[0].record_id == claim.claim_id
    assert results[0].kind == "epistemic_claim"
    assert results[0].confidence is None
    assert results[0].metadata["authority"] == "explicit_user"
    assert "not truth confidence" in results[0].metadata["epistemic_note"]


async def test_sealed_witness_cannot_reenter_through_document_projection(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    projected = WitnessMemory(
        witness_id="sealed-witness",
        content="已经被主体封存的见证",
        consciousness_instance_id="memory_witness",
        perspective_subject_id="elysia",
        epistemic_kind=EpistemicKind.SUBJECTIVE_WITNESS.value,
        source_kind="experience_window",
        status="privacy_sealed",
        stream_scope="stream-1",
        visibility="private",
        valid_from="2026-07-29T08:00:00+08:00",
        valid_to="2026-07-29T08:05:00+08:00",
        recorded_at="2026-07-29T08:06:00+08:00",
        source_sequence_start=1,
        source_sequence_end=2,
        source_event_ids=("event-1", "event-2"),
        projection_path="diaries/witness/2026-07/2026-07-29/1-2-scope.md",
        projection_status="ready",
    )

    async def _documents(*_args: object, **_kwargs: object) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                file_path=projected.projection_path,
                title="见证投影",
                snippet=projected.content,
                relevance=0.95,
                source="semantic",
                score_kind="rank",
                association_path=[],
                association_reason="",
            )
        ]

    async def _witnesses(*_args: object, **_kwargs: object) -> list[object]:
        return []

    async def _projection(_path: str) -> WitnessMemory:
        return projected

    service.search_memory = _documents  # type: ignore[method-assign]
    service.search_witness_memories = _witnesses  # type: ignore[method-assign]
    service.get_witness_by_projection_path = _projection  # type: ignore[method-assign]

    results = await service.search_evidence_aware(
        "封存",
        mode=MemorySearchMode.AUTOBIOGRAPHICAL,
        stream_scope="stream-1",
    )

    assert results == []
