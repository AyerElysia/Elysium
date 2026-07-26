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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import json_repair

from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
from src.kernel.llm import LLMPayload, ROLE, Text

from .models import (
    AuditRecord,
    AuditVerdict,
    Insight,
    InsightNextAction,
    InsightStatus,
)
from .prompts import (
    AUDITOR_SYSTEM_PROMPT,
    AUDITOR_USER_TEMPLATE,
    format_evidence_for_auditor,
)
from .store import InsightStore

logger = logging.getLogger("life_engine.learning.auditor")

# 每次审计最多处理的洞察数
_DEFAULT_BATCH_SIZE = 3


class InsightAuditor:
    """审计环：独立验证洞察、检测偏误。"""

    def __init__(
        self,
        *,
        store: InsightStore,
        workspace_path: str | Path,
        model_task_name: str = "life",
        timeout_seconds: float = 60.0,
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ) -> None:
        self._store = store
        self._workspace = Path(workspace_path).resolve()
        self._model_task_name = str(model_task_name or "life").strip() or "life"
        self._timeout = max(15.0, float(timeout_seconds or 60.0))
        self._batch_size = max(1, int(batch_size or _DEFAULT_BATCH_SIZE))
        self._lock = asyncio.Lock()

    async def run_audit_cycle(self) -> list[AuditRecord]:
        """执行一轮审计：取出候选 → 逐条审计 → 更新状态。

        Returns:
            本轮产生的审计记录列表
        """
        async with self._lock:
            candidates = self._store.list_candidates_for_review()
            if not candidates:
                logger.debug("无待审洞察")
                return []

            batch = candidates[: self._batch_size]
            records: list[AuditRecord] = []

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

    async def _audit_single(self, insight: Insight) -> AuditRecord | None:
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
        except Exception as exc:
            logger.warning(f"审计 LLM 调用失败 [{insight.insight_id}]: {exc}")
            # 回退为 candidate
            self._store.transition_status(
                insight.insight_id,
                InsightStatus.CANDIDATE,
                next_action=InsightNextAction.AWAIT_REVIEW,
                reason=f"审计调用失败: {exc}",
            )
            return None

        # 构建审计记录
        record = AuditRecord(
            audit_id=f"audit_{uuid4().hex[:12]}",
            insight_id=insight.insight_id,
            timestamp=_now_iso(),
            verdict=verdict_data.get("verdict", AuditVerdict.NEEDS_MORE_EVIDENCE.value),
            reasoning=verdict_data.get("reasoning", ""),
            bias_detected=verdict_data.get("bias_detected", []),
            evidence_sufficiency=float(verdict_data.get("evidence_sufficiency", 0.0) or 0.0),
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
                reason=f"审计通过: {record.reasoning[:100]}",
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
                reason=f"审计否定: {record.reasoning[:100]}",
                audit_record=record,
            )

        elif verdict == AuditVerdict.BIASED.value:
            # 偏误：回退为 candidate，要求修正
            self._store.transition_status(
                insight.insight_id,
                InsightStatus.CANDIDATE,
                next_action=InsightNextAction.REVISE,
                reason=f"检测到偏误 {record.bias_detected}: {record.reasoning[:80]}",
                audit_record=record,
            )

        else:  # needs_more_evidence
            self._store.transition_status(
                insight.insight_id,
                InsightStatus.CANDIDATE,
                next_action=InsightNextAction.GATHER_EVIDENCE,
                reason=f"证据不足: {record.reasoning[:80]}",
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
            confidence=f"{insight.confidence:.2f}",
            review_count=insight.review_count,
            max_reviews=insight.max_reviews,
            evidence_text=evidence_text,
            context_text="（暂无额外上下文）",
        )

        request = create_llm_request(
            get_model_set_by_task(self._model_task_name),
            request_name="life_learning_audit",
        )
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(AUDITOR_SYSTEM_PROMPT)))
        request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))

        response = await asyncio.wait_for(
            request.send(auto_append_response=False, stream=False),
            timeout=self._timeout,
        )
        raw_text = await asyncio.wait_for(response, timeout=self._timeout)
        return str(raw_text or "")

    def _parse_verdict(self, raw_text: str) -> dict[str, Any]:
        """解析审计 LLM 输出。"""
        parsed: Any
        try:
            parsed = json.loads(raw_text)
        except (json.JSONDecodeError, ValueError):
            parsed = json_repair.repair_json(raw_text, return_objects=True)

        if not isinstance(parsed, dict):
            return {
                "verdict": AuditVerdict.NEEDS_MORE_EVIDENCE.value,
                "reasoning": "审计输出解析失败",
                "evidence_sufficiency": 0.0,
                "bias_detected": [],
                "suggestions": "",
            }

        # 规范化 verdict
        verdict_raw = str(parsed.get("verdict", "") or "").strip().lower()
        valid_verdicts = {v.value for v in AuditVerdict}
        verdict = verdict_raw if verdict_raw in valid_verdicts else AuditVerdict.NEEDS_MORE_EVIDENCE.value

        # 规范化 bias_detected
        bias_raw = parsed.get("bias_detected")
        bias = [str(b).strip() for b in bias_raw if b] if isinstance(bias_raw, list) else []

        return {
            "verdict": verdict,
            "reasoning": str(parsed.get("reasoning", "") or ""),
            "evidence_sufficiency": max(0.0, min(1.0, float(parsed.get("evidence_sufficiency", 0.0) or 0.0))),
            "bias_detected": bias,
            "suggestions": str(parsed.get("suggestions", "") or ""),
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()
