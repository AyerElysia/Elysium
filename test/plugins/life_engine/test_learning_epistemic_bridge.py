"""Learning → Epistemic 桥的契约测试。

学习系统只能提供有来源的候选认识。这里锁住历史 validated 洞察的首次心跳
回填、审计通过后的实时投影、稳定 claim id 和幂等边界。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from plugins.life_engine.learning.models import (
    AuditRecord,
    AuditVerdict,
    Evidence,
    EvidenceKind,
    Insight,
    InsightNextAction,
    InsightStatus,
)
from plugins.life_engine.learning.scheduler import LearningScheduler


class _EpistemicMemoryStub:
    def __init__(self, *, existing: set[str] | None = None) -> None:
        self.existing = set(existing or ())
        self.claims: list[Any] = []
        self.evidence: list[Any] = []
        self.lookups: list[str] = []

    async def get_memory_claim_state(self, claim_id: str) -> object | None:
        self.lookups.append(claim_id)
        return object() if claim_id in self.existing else None

    async def append_memory_claim(self, claim: Any) -> None:
        self.claims.append(claim)
        self.existing.add(claim.claim_id)

    async def append_claim_evidence(self, evidence: Any) -> None:
        self.evidence.append(evidence)


def _validated_insight(scheduler: LearningScheduler, *, claim: str) -> Insight:
    insight = Insight.create(
        category="行为模式",
        claim=claim,
        rationale="在多次经历中观察到",
        initial_evidence=[
            Evidence.create(
                kind=EvidenceKind.SELF_OBSERVATION,
                description="一次可追溯的自我观察",
                source_ref="event-1",
            )
        ],
    )
    scheduler.store.add_insight(insight)
    scheduler.store.transition_status(
        insight.insight_id,
        InsightStatus.VALIDATED,
        next_action=InsightNextAction.PROMOTE,
        reason="证据足够",
    )
    return scheduler.store.get_insight(insight.insight_id)  # type: ignore[return-value]


def _audit(insight_id: str, verdict: AuditVerdict) -> AuditRecord:
    return AuditRecord(
        audit_id=f"audit_{insight_id}",
        insight_id=insight_id,
        timestamp="2026-07-29T17:00:00+08:00",
        verdict=verdict.value,
        reasoning="测试裁决",
        evidence_sufficiency=0.9,
    )


def _disable_heartbeat_side_effects(scheduler: LearningScheduler) -> None:
    async def _noop() -> None:
        return None

    scheduler._maybe_run_audit = _noop  # type: ignore[method-assign]
    scheduler._maybe_run_compression = _noop  # type: ignore[method-assign]
    scheduler._maybe_run_distillation = _noop  # type: ignore[method-assign]
    scheduler._maybe_snapshot_metrics = _noop  # type: ignore[method-assign]
    scheduler._maybe_check_staleness = _noop  # type: ignore[method-assign]


async def test_first_heartbeat_backfills_validated_insight_once(tmp_path) -> None:
    memory = _EpistemicMemoryStub()
    scheduler = LearningScheduler(workspace_path=tmp_path, memory_service=memory)
    insight = _validated_insight(scheduler, claim="我会先确认情绪，再决定是否给建议")
    _disable_heartbeat_side_effects(scheduler)

    await scheduler.on_heartbeat()
    await scheduler.on_heartbeat()

    assert scheduler._epistemic_backfilled is True
    assert len(memory.claims) == 1
    claim = memory.claims[0]
    assert claim.claim_id == f"insight_{insight.insight_id}"
    assert claim.subject_key == f"learning_insight:{insight.insight_id}"
    assert claim.content == insight.claim
    assert claim.claim_kind == "learning_candidate_observation"
    assert claim.source == "learning_system"
    assert claim.authority == "learning_audit_observation"
    assert claim.metadata == {
        "insight_id": insight.insight_id,
        "evidence_count": 1,
        "confidence_as_reported_by_learning_system": insight.confidence,
        "category": "行为模式",
        "epistemic_note": "audit output and retrieval frequency are not truth",
    }
    assert len(memory.evidence) == 1
    assert memory.evidence[0].claim_id == claim.claim_id
    assert memory.evidence[0].evidence_ref == "event-1"


async def test_backfill_skips_claim_already_present_in_epistemic_store(tmp_path) -> None:
    scheduler = LearningScheduler(workspace_path=tmp_path)
    insight = _validated_insight(scheduler, claim="重复投影不应生成第二条 claim")
    claim_id = f"insight_{insight.insight_id}"
    memory = _EpistemicMemoryStub(existing={claim_id})
    scheduler.attach_memory_service(memory)

    await scheduler._backfill_epistemic_claims()

    assert memory.lookups == [claim_id]
    assert memory.claims == []
    assert len(memory.evidence) == 1


async def test_audit_projection_only_accepts_validated_records(tmp_path) -> None:
    memory = _EpistemicMemoryStub()
    scheduler = LearningScheduler(workspace_path=tmp_path, memory_service=memory)
    accepted = _validated_insight(scheduler, claim="通过审计的洞察应实时进入认识论层")
    rejected = Insight.create(
        category="行为模式",
        claim="被否定的洞察不能进入认识论层",
        rationale="测试",
    )
    scheduler.store.add_insight(rejected)

    await scheduler._project_validated_to_epistemic(
        [
            _audit(accepted.insight_id, AuditVerdict.VALIDATED),
            _audit(rejected.insight_id, AuditVerdict.REJECTED),
        ]
    )

    assert [claim.claim_id for claim in memory.claims] == [
        f"insight_{accepted.insight_id}"
    ]


async def test_projection_tolerates_lookup_failure_but_preserves_stable_id(tmp_path) -> None:
    class _LookupFailureMemory(_EpistemicMemoryStub):
        async def get_memory_claim_state(self, claim_id: str) -> object | None:
            self.lookups.append(claim_id)
            raise RuntimeError("temporary lookup failure")

    memory = _LookupFailureMemory()
    scheduler = LearningScheduler(workspace_path=tmp_path, memory_service=memory)
    insight = _validated_insight(scheduler, claim="查询失败时仍可依赖存储层幂等写入")

    await scheduler._project_validated_to_epistemic(
        [_audit(insight.insight_id, AuditVerdict.VALIDATED)]
    )

    assert memory.claims[0].claim_id == f"insight_{insight.insight_id}"


async def test_bridge_is_noop_without_memory_service(tmp_path) -> None:
    scheduler = LearningScheduler(workspace_path=tmp_path)
    insight = _validated_insight(scheduler, claim="记忆服务未就绪时不能破坏学习循环")

    await scheduler._backfill_epistemic_claims()
    await scheduler._project_validated_to_epistemic(
        [SimpleNamespace(insight_id=insight.insight_id, verdict="validated")]
    )
