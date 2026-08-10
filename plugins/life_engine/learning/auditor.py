"""InsightAuditor：审计环——独立 LLM 验证。

类比 VibeGamer 的 Retry Reviewer + Validation Guardrails。
核心原则：审计者是独立角色，不是主体本身。
用不同的 prompt、不同的视角来评判洞察是否成立。

触发时机：
- 定期（每 N 小时或积累 M 条 candidate 后）
- 由 LearningScheduler 调度
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import json_repair

from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
from src.kernel.llm import ROLE, LLMPayload, Text

from .models import (
    AuditRecord,
    AuditVerdict,
    Insight,
    InsightNextAction,
    InsightStatus,
)
from .projection import project_learning_text
from .prompts import (
    AUDITOR_SYSTEM_PROMPT,
    AUDITOR_USER_TEMPLATE,
    format_evidence_for_auditor,
)
from .store import InsightStore
from .timeouts import (
    DEFAULT_LEARNING_LLM_TIMEOUT_SECONDS,
    resolve_timeout_seconds,
    send_with_deadline,
)

logger = logging.getLogger("life_engine.learning.auditor")

# 每次审计最多处理的洞察数
_DEFAULT_BATCH_SIZE = 3

# 后台独立审计的单次 LLM 往返总预算；质量优先且仍保持有界，见 timeouts 模块。
_DEFAULT_TIMEOUT_SECONDS = DEFAULT_LEARNING_LLM_TIMEOUT_SECONDS
_MIN_TIMEOUT_SECONDS = 15.0
_TIMEOUT_ENV_VAR = "ELYSIUM_AUDIT_TIMEOUT_SECONDS"


def _resolve_timeout_seconds(explicit: float | None) -> float:
    """Resolve the audit LLM timeout from an explicit value or the env var.

    Thin binding of :func:`~.timeouts.resolve_timeout_seconds` to this ring's
    env var and defaults.

    Args:
        explicit: Caller-supplied timeout in seconds, or ``None`` to resolve
            from the environment.

    Returns:
        The timeout in seconds, never below :data:`_MIN_TIMEOUT_SECONDS`.

    Raises:
        ValueError: If the env var is set but is not a positive finite number.
    """

    return resolve_timeout_seconds(
        explicit,
        env_var=_TIMEOUT_ENV_VAR,
        default=_DEFAULT_TIMEOUT_SECONDS,
        minimum=_MIN_TIMEOUT_SECONDS,
    )


class InsightAuditor:
    """审计环：独立验证洞察、检测偏误。"""

    def __init__(
        self,
        *,
        store: InsightStore,
        workspace_path: str | Path,
        model_task_name: str = "life",
        timeout_seconds: float | None = None,
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ) -> None:
        self._store = store
        self._workspace = Path(workspace_path).resolve()
        self._model_task_name = str(model_task_name or "life").strip() or "life"
        self._timeout = _resolve_timeout_seconds(timeout_seconds)
        self._batch_size = max(1, int(batch_size or _DEFAULT_BATCH_SIZE))
        self._lock = asyncio.Lock()
        self._last_projection_stats: dict[str, Any] = {}

    def projection_health(self) -> dict[str, Any]:
        """Return content-free trace metadata for the last audit request."""

        return dict(self._last_projection_stats)

    async def run_audit_cycle(self) -> list[AuditRecord]:
        """执行一轮审计：取出候选 → 独立审计 → 更新状态。

        Returns:
            本轮产生的审计记录列表
        """
        async with self._lock:
            self._reclaim_stranded_reviews()
            candidates = self._store.list_candidates_for_review()
            if not candidates:
                logger.debug("无待审洞察")
                return []

            records: list[AuditRecord] = []

            # 证据数量、重复次数和跨日期出现只提供给独立审计者阅读，代码
            # 不据此自动晋升。批大小只控制本轮工作量，不改变任何洞察结论。
            batch = candidates[: self._batch_size]
            for insight in batch:
                record = await self._audit_single(insight)
                if record is not None:
                    records.append(record)

            if records:
                logger.info(
                    f"🔍 审计完成: {len(records)} 条 | "
                    f"validated={sum(1 for r in records if r.verdict == AuditVerdict.VALIDATED.value)} "
                    f"rejected={sum(1 for r in records if r.verdict == AuditVerdict.REJECTED.value)} "
                    f"biased={sum(1 for r in records if r.verdict == AuditVerdict.BIASED.value)}"
                )
            return records

    async def reclaim_stranded_reviews(self) -> int:
        """Reclaim orphaned ``under_review`` insights under the audit lock.

        The scheduler must call this *before* it decides whether to audit. Its
        gate requires a non-empty candidate list, and a stranded insight is by
        definition absent from that list, so a system whose only remaining work
        is stranded would never open an audit cycle — the recovery path and the
        gate would wait on each other forever.

        Returns:
            The number of insights returned to the queue.
        """

        async with self._lock:
            return self._reclaim_stranded_reviews()

    def _reclaim_stranded_reviews(self) -> int:
        """Return insights stuck in ``under_review`` to the audit queue.

        ``under_review`` is written in exactly one place — the moment before this
        ring dispatches its LLM call — and cleared when the verdict lands or the
        call fails. So a lingering ``under_review`` means the audit was cut off
        between those two writes: the process was killed, or the heartbeat task
        was cancelled. ``CancelledError`` derives from ``BaseException``, so the
        rollback in :meth:`_audit_single` never used to run for it.

        That leaves the insight invisible: ``can_review`` only admits
        ``candidate`` and ``needs_more_evidence``, so a stranded belief can never
        be audited again — in production 5 of 313 dispatches ended this way and
        sat unreachable for over 87 hours.

        This is crash recovery, not a judgement. It restores exactly the state
        this ring's own failure path writes (``candidate`` + ``await_review``),
        re-offering the insight for review without touching its content,
        verdict, confidence, or history. Callers must hold :attr:`_lock`, which
        is what makes a surviving ``under_review`` provably orphaned rather than
        an audit in flight.

        Returns:
            The number of insights returned to the queue.
        """

        stranded = self._store.list_by_status(InsightStatus.UNDER_REVIEW)
        if not stranded:
            return 0

        for insight in stranded:
            self._store.transition_status(
                insight.insight_id,
                InsightStatus.CANDIDATE,
                next_action=InsightNextAction.AWAIT_REVIEW,
                reason="审计中断遗留，回到待审队列",
            )
        logger.warning(
            "回收审计中断遗留的洞察: %d 条重新进入待审队列",
            len(stranded),
        )
        return len(stranded)

    async def _audit_single(self, insight: Insight) -> AuditRecord:
        """审计单条洞察。"""
        # 标记为 under_review
        self._store.transition_status(
            insight.insight_id,
            InsightStatus.UNDER_REVIEW,
            reason="审计环调度",
        )

        try:
            raw_text = await self._call_llm(insight)
            verdict_data = self._parse_verdict(raw_text)
        except BaseException as exc:
            # 必须接 BaseException：CancelledError 不是 Exception，而进程关停时
            # 心跳任务正是被 cancel 的。漏掉它，这条洞察就永久卡在 under_review，
            # 而 can_review 不收 under_review——等于这段信念再也不会被审视。
            logger.warning(
                "审计 LLM 调用失败 [%s]: %s",
                insight.insight_id,
                type(exc).__name__,
            )
            # 回退为 candidate
            self._store.transition_status(
                insight.insight_id,
                InsightStatus.CANDIDATE,
                next_action=InsightNextAction.AWAIT_REVIEW,
                reason=f"审计调用失败: {type(exc).__name__}",
            )
            # 取消和普通失败都继续向上传播：前者维持结构化关停，后者让
            # LearningMaintenanceEvent 正确记录 failed，而不是伪造 succeeded。
            # 状态已经回滚，所以传播不会留下 under_review 残留。
            raise

        # 构建审计记录
        record = AuditRecord(
            audit_id=f"audit_{uuid4().hex[:12]}",
            insight_id=insight.insight_id,
            timestamp=_now_iso(),
            verdict=verdict_data.get("verdict", AuditVerdict.NEEDS_MORE_EVIDENCE.value),
            reasoning=verdict_data.get("reasoning", ""),
            bias_detected=verdict_data.get("bias_detected", []),
            evidence_sufficiency=float(
                verdict_data.get("evidence_sufficiency", 0.0) or 0.0
            ),
            suggestions=verdict_data.get("suggestions", ""),
        )

        # 根据裁决执行状态流转
        self._apply_verdict(insight, record)
        return record

    def _apply_verdict(self, insight: Insight, record: AuditRecord) -> None:
        """根据审计裁决执行状态流转。"""
        verdict = record.verdict

        if verdict == AuditVerdict.VALIDATED.value:
            self._store.transition_status(
                insight.insight_id,
                InsightStatus.VALIDATED,
                next_action=InsightNextAction.PROMOTE,
                reason=f"审计通过: {record.reasoning}",
                audit_record=record,
            )
            # 更新置信度和验证时间
            ins = self._store.get_insight(insight.insight_id)
            if ins:
                ins.confidence = max(ins.confidence, record.evidence_sufficiency)
                ins.last_validated_at = _now_iso()
                self._store.update_insight(ins)

        elif verdict == AuditVerdict.REJECTED.value:
            self._store.transition_status(
                insight.insight_id,
                InsightStatus.REJECTED,
                next_action=InsightNextAction.ARCHIVE,
                reason=f"审计否定: {record.reasoning}",
                audit_record=record,
            )

        elif verdict == AuditVerdict.BIASED.value:
            # 偏误：回退为 candidate，要求修正
            self._store.transition_status(
                insight.insight_id,
                InsightStatus.CANDIDATE,
                next_action=InsightNextAction.REVISE,
                reason=f"检测到偏误 {record.bias_detected}: {record.reasoning}",
                audit_record=record,
            )

        else:  # needs_more_evidence
            self._store.transition_status(
                insight.insight_id,
                InsightStatus.CANDIDATE,
                next_action=InsightNextAction.GATHER_EVIDENCE,
                reason=f"证据不足: {record.reasoning}",
                audit_record=record,
            )

    async def _call_llm(self, insight: Insight) -> str:
        """调用审计 LLM。"""
        evidence_text = format_evidence_for_auditor(
            [ev.to_dict() for ev in insight.evidence]
        )
        user_prompt = AUDITOR_USER_TEMPLATE.format(
            insight_id=insight.insight_id,
            category=insight.category,
            claim=insight.claim,
            rationale=insight.rationale,
            constraints=insight.constraints or "（未指定）",
            topic_key=insight.topic_key or "（未分类）",
            evidence_text=evidence_text,
            audit_history=(
                json.dumps(
                    [record.to_dict() for record in insight.audit_history],
                    ensure_ascii=False,
                    indent=2,
                )
                if insight.audit_history
                else "（暂无历史审计）"
            ),
            context_text="（暂无额外上下文）",
        )
        delivered = project_learning_text(
            user_prompt,
            max_bytes=64 * 1024,
            projection_kind="learning_audit_request",
        )
        self._last_projection_stats = delivered.stats()

        request = create_llm_request(
            get_model_set_by_task(self._model_task_name),
            request_name="life_learning_audit",
        )
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(AUDITOR_SYSTEM_PROMPT)))
        request.add_payload(LLMPayload(ROLE.USER, Text(delivered.text)))

        return await send_with_deadline(request, self._timeout)

    def _parse_verdict(self, raw_text: str) -> dict[str, Any]:
        """解析审计 LLM 输出。"""
        parsed: Any
        try:
            parsed = json.loads(raw_text)
        except (json.JSONDecodeError, ValueError):
            parsed = json_repair.repair_json(raw_text, return_objects=True)

        if not isinstance(parsed, dict):
            raise ValueError("AuditOutputMustBeObject")

        # 规范化 verdict
        verdict_raw = str(parsed.get("verdict", "") or "").strip().lower()
        valid_verdicts = {v.value for v in AuditVerdict}
        if verdict_raw not in valid_verdicts:
            raise ValueError(f"AuditVerdictMissingOrUnknown:{verdict_raw}")
        verdict = verdict_raw

        # 规范化 bias_detected
        bias_raw = parsed.get("bias_detected")
        bias = (
            [str(b).strip() for b in bias_raw if b]
            if isinstance(bias_raw, list)
            else []
        )

        return {
            "verdict": verdict,
            "reasoning": str(parsed.get("reasoning", "") or ""),
            "evidence_sufficiency": max(
                0.0, min(1.0, float(parsed.get("evidence_sufficiency", 0.0) or 0.0))
            ),
            "bias_detected": bias,
            "suggestions": str(parsed.get("suggestions", "") or ""),
        }


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat()
