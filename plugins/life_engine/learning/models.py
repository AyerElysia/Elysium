"""自学习系统数据模型。

三环自学习/自反思系统的核心数据结构：
- Insight: 洞察（类比 VibeGamer 的 Hypothesis）
- Evidence: 证据
- AuditRecord: 审计记录
- KnowledgeVersion: 自我认知版本
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


# ── 枚举 ──────────────────────────────────────────────────────


class InsightStatus(str, Enum):
    """洞察生命周期状态。

    流转：
    candidate -> under_review -> validated | rejected | needs_more_evidence
    needs_more_evidence -> candidate (回退收集证据)
    candidate -> archived (终身审计满/手动归档)
    """

    CANDIDATE = "candidate"                # 刚提出，等待审计
    UNDER_REVIEW = "under_review"          # 正在被审计环审查
    VALIDATED = "validated"                # 已验证，可进入自我认知压缩池
    REJECTED = "rejected"                  # 已否定，保留为反例
    NEEDS_MORE_EVIDENCE = "needs_more_evidence"  # 证据不足，回退收集
    ARCHIVED = "archived"                  # 归档（终身审计满/手动）


class InsightNextAction(str, Enum):
    """调度闸门：决定下一步对该洞察做什么。"""

    AWAIT_REVIEW = "await_review"          # 等待审计环调度
    GATHER_EVIDENCE = "gather_evidence"    # 需要收集更多证据
    PROMOTE = "promote"                    # 可被慢环压缩进自我认知
    ARCHIVE = "archive"                    # 停止调度
    REVISE = "revise"                      # 需要修正后重审


class EvidenceKind(str, Enum):
    """证据类型。"""

    INTERACTION_OUTCOME = "interaction_outcome"    # 交互结果反馈
    SELF_OBSERVATION = "self_observation"          # 自我观察
    PATTERN_MATCH = "pattern_match"                # 模式匹配
    COUNTER_EXAMPLE = "counter_example"            # 反例
    EXTERNAL_FEEDBACK = "external_feedback"        # 外部反馈
    DREAM_INSIGHT = "dream_insight"                # 梦境启发
    VALIDATION_EXPERIMENT = "validation_experiment" # 验证实验：将预测转化为可测试的结果
    EMBODIED_VALIDATION = "embodied_validation"    # 具身验证：Minecraft等环境中的实测


class AuditVerdict(str, Enum):
    """审计裁决。"""

    VALIDATED = "validated"                    # 证据充分，无偏误
    REJECTED = "rejected"                      # 证据否定或严重偏误
    NEEDS_MORE_EVIDENCE = "needs_more_evidence"  # 证据不足
    BIASED = "biased"                          # 检测到偏误，需修正


class BiasType(str, Enum):
    """自欺/偏误类型。"""

    CONFIRMATION_BIAS = "confirmation_bias"      # 确认偏误：只看支持证据
    OVERGENERALIZATION = "overgeneralization"    # 过度泛化：样本不足
    RECENCY_BIAS = "recency_bias"                # 近因偏误：只基于最近经历
    SELF_SERVING = "self_serving"                # 自我服务：服务于自我安慰
    UNFALSIFIABLE = "unfalsifiable"              # 不可证伪：无法被否定
    ANCHORING = "anchoring"                      # 锚定效应：被第一印象锁定


# ── 数据模型 ──────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _gen_id(prefix: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{prefix}_{ts}_{uuid4().hex[:6]}"


@dataclass(slots=True)
class Evidence:
    """一条证据。"""

    evidence_id: str
    timestamp: str
    kind: str                  # EvidenceKind value
    description: str
    source_ref: str = ""       # trace_id / event_id 引用
    supports: bool = True      # True=正面证据, False=反面证据
    weight: float = 1.0        # 权重 0.0-2.0
    context: str = ""          # 补充上下文

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Evidence":
        return cls(
            evidence_id=str(data.get("evidence_id", "") or _gen_id("ev")),
            timestamp=str(data.get("timestamp", "") or _now_iso()),
            kind=str(data.get("kind", "") or EvidenceKind.SELF_OBSERVATION.value),
            description=str(data.get("description", "") or ""),
            source_ref=str(data.get("source_ref", "") or ""),
            supports=bool(data.get("supports", True)),
            weight=max(0.0, min(2.0, float(data.get("weight", 1.0) or 1.0))),
            context=str(data.get("context", "") or ""),
        )

    @classmethod
    def create(
        cls,
        *,
        kind: EvidenceKind | str,
        description: str,
        source_ref: str = "",
        supports: bool = True,
        weight: float = 1.0,
        context: str = "",
    ) -> "Evidence":
        return cls(
            evidence_id=_gen_id("ev"),
            timestamp=_now_iso(),
            kind=kind.value if isinstance(kind, EvidenceKind) else str(kind),
            description=str(description or "").strip(),
            source_ref=str(source_ref or "").strip(),
            supports=supports,
            weight=max(0.0, min(2.0, weight)),
            context=str(context or "").strip(),
        )


@dataclass(slots=True)
class AuditRecord:
    """一次审计记录。"""

    audit_id: str
    insight_id: str
    timestamp: str
    verdict: str               # AuditVerdict value
    reasoning: str
    bias_detected: list[str] = field(default_factory=list)
    evidence_sufficiency: float = 0.0   # 0-1
    suggestions: str = ""               # 给主体的建议

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuditRecord":
        bias_raw = data.get("bias_detected")
        bias = [str(b) for b in bias_raw if b] if isinstance(bias_raw, list) else []
        return cls(
            audit_id=str(data.get("audit_id", "") or _gen_id("audit")),
            insight_id=str(data.get("insight_id", "") or ""),
            timestamp=str(data.get("timestamp", "") or _now_iso()),
            verdict=str(data.get("verdict", "") or AuditVerdict.NEEDS_MORE_EVIDENCE.value),
            reasoning=str(data.get("reasoning", "") or ""),
            bias_detected=bias,
            evidence_sufficiency=max(0.0, min(1.0, float(data.get("evidence_sufficiency", 0.0) or 0.0))),
            suggestions=str(data.get("suggestions", "") or ""),
        )


@dataclass(slots=True)
class Insight:
    """一条洞察——自学习系统的核心单元。

    类比 VibeGamer 的 Hypothesis，但面向社交/自我认知领域。
    """

    insight_id: str
    category: str              # 自由命名，无枚举约束
    claim: str                 # 洞察陈述
    rationale: str             # 为什么这么认为
    constraints: str           # 适用边界
    status: str = InsightStatus.CANDIDATE.value
    next_action: str = InsightNextAction.AWAIT_REVIEW.value
    evidence: list[Evidence] = field(default_factory=list)
    source_events: list[str] = field(default_factory=list)
    topic_key: str = ""
    confidence: float = 0.3
    born_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    last_validated_at: str = ""  # 最后一次被验证通过的时间
    review_count: int = 0
    max_reviews: int = 3
    touch_count: int = 0       # 被触碰次数
    last_touched_at: str = ""
    contradiction_count: int = 0  # 被反例挑战的次数
    anti_bias_flags: list[str] = field(default_factory=list)
    audit_history: list[AuditRecord] = field(default_factory=list)
    revision_note: str = ""    # 修正说明

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["evidence"] = [ev.to_dict() for ev in self.evidence]
        d["audit_history"] = [ar.to_dict() for ar in self.audit_history]
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Insight":
        evidence_raw = data.get("evidence")
        evidence = [
            Evidence.from_dict(ev) for ev in evidence_raw if isinstance(ev, dict)
        ] if isinstance(evidence_raw, list) else []
        audit_raw = data.get("audit_history")
        audit_history = [
            AuditRecord.from_dict(ar) for ar in audit_raw if isinstance(ar, dict)
        ] if isinstance(audit_raw, list) else []
        source_raw = data.get("source_events")
        source_events = [str(s) for s in source_raw if s] if isinstance(source_raw, list) else []
        bias_raw = data.get("anti_bias_flags")
        anti_bias = [str(b) for b in bias_raw if b] if isinstance(bias_raw, list) else []

        return cls(
            insight_id=str(data.get("insight_id", "") or _gen_id("ins")),
            category=str(data.get("category", "") or ""),
            claim=str(data.get("claim", "") or ""),
            rationale=str(data.get("rationale", "") or ""),
            constraints=str(data.get("constraints", "") or ""),
            status=str(data.get("status", "") or InsightStatus.CANDIDATE.value),
            next_action=str(data.get("next_action", "") or InsightNextAction.AWAIT_REVIEW.value),
            evidence=evidence,
            source_events=source_events,
            topic_key=str(data.get("topic_key", "") or ""),
            confidence=max(0.0, min(1.0, float(data.get("confidence", 0.3) or 0.3))),
            born_at=str(data.get("born_at", "") or _now_iso()),
            updated_at=str(data.get("updated_at", "") or _now_iso()),
            last_validated_at=str(data.get("last_validated_at", "") or ""),
            review_count=int(data.get("review_count", 0) or 0),
            max_reviews=int(data.get("max_reviews", 3) or 3),
            touch_count=int(data.get("touch_count", 0) or 0),
            last_touched_at=str(data.get("last_touched_at", "") or ""),
            contradiction_count=int(data.get("contradiction_count", 0) or 0),
            anti_bias_flags=anti_bias,
            audit_history=audit_history,
            revision_note=str(data.get("revision_note", "") or ""),
        )

    @classmethod
    def create(
        cls,
        *,
        category: str,
        claim: str,
        rationale: str,
        constraints: str = "",
        topic_key: str = "",
        source_events: list[str] | None = None,
        initial_evidence: list[Evidence] | None = None,
    ) -> "Insight":
        return cls(
            insight_id=_gen_id("ins"),
            category=str(category).strip(),
            claim=str(claim or "").strip(),
            rationale=str(rationale or "").strip(),
            constraints=str(constraints or "").strip(),
            topic_key=str(topic_key or "").strip(),
            source_events=source_events or [],
            evidence=initial_evidence or [],
        )

    # ── 便捷方法 ──

    @property
    def is_terminal(self) -> bool:
        """是否处于终态（不再被调度）。"""
        return self.status in (
            InsightStatus.VALIDATED.value,
            InsightStatus.REJECTED.value,
            InsightStatus.ARCHIVED.value,
        )

    @property
    def can_review(self) -> bool:
        """是否还能被审计。

        信念永远可重新审视——不用硬上限机械禁止。
        review_count 仅作信息记录，审计频次预算由调度器控制（脚手架），
        但不作为“你只能想 N 次”的认知禁令。

        两条进入审计队列的路径：
        1. 明确在等待审计（await_review）
        2. 上次审计之后又有新证据到达——上次判"证据不足"或"需修正"
           所依据的前提已经变了，值得再看一次。

        第 2 条是必要的：审计环把绝大多数洞察打回 gather_evidence / revise，
        若只认第 1 条，这些洞察就永久离开队列，再也不会被重新审视——
        那与本方法文档承诺的"信念永远可重新审视"直接矛盾。
        反过来，没有新证据时不进队列，避免重复消耗审计调用去看同一份材料。
        """
        if self.status not in (
            InsightStatus.CANDIDATE.value,
            InsightStatus.NEEDS_MORE_EVIDENCE.value,
        ):
            return False
        if self.next_action == InsightNextAction.AWAIT_REVIEW.value:
            return True
        if self.next_action in (
            InsightNextAction.GATHER_EVIDENCE.value,
            InsightNextAction.REVISE.value,
        ):
            return self.has_new_evidence_since_last_review
        return False

    @property
    def has_new_evidence_since_last_review(self) -> bool:
        """上次审计之后是否有新证据到达。"""
        if not self.evidence:
            return False
        if not self.audit_history:
            return True

        last_audit_ts = self.audit_history[-1].timestamp

        def _parse(ts: str) -> datetime | None:
            try:
                return datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                return None

        audit_dt = _parse(last_audit_ts)
        if audit_dt is None:
            return True
        for ev in self.evidence:
            ev_dt = _parse(ev.timestamp)
            if ev_dt is not None and ev_dt > audit_dt:
                return True
        return False

    @property
    def positive_evidence_count(self) -> int:
        return sum(1 for ev in self.evidence if ev.supports)

    @property
    def negative_evidence_count(self) -> int:
        return sum(1 for ev in self.evidence if not ev.supports)

    def add_evidence(self, ev: Evidence) -> None:
        self.evidence.append(ev)
        self.touch_count += 1
        self.last_touched_at = _now_iso()
        self.updated_at = _now_iso()

    def record_touch(self) -> None:
        self.touch_count += 1
        self.last_touched_at = _now_iso()

    def record_contradiction(self) -> None:
        """记录一次反例挑战"""
        self.contradiction_count += 1
        self.updated_at = _now_iso()

    def get_staleness_days(self) -> int:
        """计算距上次验证的天数"""
        if not self.last_validated_at:
            # 未验证过的，用创建时间
            ref_time = self.born_at
        else:
            ref_time = self.last_validated_at

        try:
            ref_dt = datetime.fromisoformat(ref_time)
            now = datetime.now(timezone.utc).astimezone()
            return (now - ref_dt).days
        except (ValueError, TypeError):
            return 0


@dataclass(slots=True)
class ValidationExperiment:
    """验证实验：将洞察转化为可测试的预测。

    借鉴 VibeGamer 的 runExperiment，为洞察提供真实世界反馈闭环。
    """

    experiment_id: str
    insight_id: str
    hypothesis: str           # 可测试的预测："如果...那么..."
    test_scenario: str        # 测试场景描述
    expected_outcome: str     # 预期结果
    created_at: str
    actual_outcome: str = ""  # 实际结果（填写后）
    result_type: str = ""     # "confirmed" / "contradicted" / "inconclusive"
    completed_at: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ValidationExperiment":
        return cls(
            experiment_id=str(data.get("experiment_id", "") or _gen_id("exp")),
            insight_id=str(data.get("insight_id", "") or ""),
            hypothesis=str(data.get("hypothesis", "") or ""),
            test_scenario=str(data.get("test_scenario", "") or ""),
            expected_outcome=str(data.get("expected_outcome", "") or ""),
            created_at=str(data.get("created_at", "") or _now_iso()),
            actual_outcome=str(data.get("actual_outcome", "") or ""),
            result_type=str(data.get("result_type", "") or ""),
            completed_at=str(data.get("completed_at", "") or ""),
            notes=str(data.get("notes", "") or ""),
        )

    @classmethod
    def create(
        cls,
        *,
        insight_id: str,
        hypothesis: str,
        test_scenario: str,
        expected_outcome: str,
    ) -> "ValidationExperiment":
        return cls(
            experiment_id=_gen_id("exp"),
            insight_id=insight_id,
            hypothesis=hypothesis,
            test_scenario=test_scenario,
            expected_outcome=expected_outcome,
            created_at=_now_iso(),
        )

    @property
    def is_completed(self) -> bool:
        return bool(self.result_type and self.completed_at)


@dataclass(slots=True)
class KnowledgeVersion:
    """自我认知文档版本记录。"""

    version: int
    timestamp: str
    file_path: str             # 相对路径 knowledge/vN.md
    insight_ids: list[str]     # 本次压缩涉及的洞察
    edit_count: int            # 本次编辑数
    promoted: bool = False     # 是否被提升为当前版本
    selection_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KnowledgeVersion":
        ids_raw = data.get("insight_ids")
        ids = [str(i) for i in ids_raw if i] if isinstance(ids_raw, list) else []
        return cls(
            version=int(data.get("version", 0) or 0),
            timestamp=str(data.get("timestamp", "") or _now_iso()),
            file_path=str(data.get("file_path", "") or ""),
            insight_ids=ids,
            edit_count=int(data.get("edit_count", 0) or 0),
            promoted=bool(data.get("promoted", False)),
            selection_reason=str(data.get("selection_reason", "") or ""),
        )


@dataclass(slots=True)
class LearningMetricsPoint:
    """学习曲线数据点。"""

    timestamp: str
    total_insights: int = 0
    validated_count: int = 0
    rejected_count: int = 0
    candidate_count: int = 0
    validation_rate: float = 0.0
    bias_detection_count: int = 0
    knowledge_version: int = 0
    topic_coverage: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LearningMetricsPoint":
        topic_raw = data.get("topic_coverage")
        topics = {str(k): int(v) for k, v in topic_raw.items()} if isinstance(topic_raw, dict) else {}
        return cls(
            timestamp=str(data.get("timestamp", "") or _now_iso()),
            total_insights=int(data.get("total_insights", 0) or 0),
            validated_count=int(data.get("validated_count", 0) or 0),
            rejected_count=int(data.get("rejected_count", 0) or 0),
            candidate_count=int(data.get("candidate_count", 0) or 0),
            validation_rate=float(data.get("validation_rate", 0.0) or 0.0),
            bias_detection_count=int(data.get("bias_detection_count", 0) or 0),
            knowledge_version=int(data.get("knowledge_version", 0) or 0),
            topic_coverage=topics,
        )
