"""ReflectionEngine：快环反思引擎。

类比 VibeGamer 的 explore 后 review 阶段。
触发时机：
1. 有意义的对话结束后（交互反思）
2. 梦境结束后（内省反思）
3. 自主意向 kind="reflect" 到期时

工作流程：
收集上下文 → LLM 提取洞察候选 → 门禁检查 → 写入 InsightStore
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import json_repair

from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
from src.kernel.llm import LLMPayload, ROLE, Text

from .models import Evidence, EvidenceKind, Insight
from .prompts import (
    REFLECTION_INTERACTION_USER,
    REFLECTION_INTROSPECTION_USER,
    REFLECTION_SYSTEM_PROMPT,
    format_existing_insights_summary,
)
from .store import InsightStore

logger = logging.getLogger("life_engine.learning.reflection")

# 反思冷却（秒）
_DEFAULT_COOLDOWN_SECONDS = 30 * 60  # 30 分钟


class ReflectionEngine:
    """快环反思引擎：从经历中提取洞察候选。"""

    def __init__(
        self,
        *,
        store: InsightStore,
        workspace_path: str | Path,
        model_task_name: str = "life",
        timeout_seconds: float = 45.0,
        cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS,
        skill_store: Any | None = None,
    ) -> None:
        self._store = store
        self._workspace = Path(workspace_path).resolve()
        self._model_task_name = str(model_task_name or "life").strip() or "life"
        self._timeout = max(10.0, float(timeout_seconds or 45.0))
        self._cooldown_seconds = max(60.0, float(cooldown_seconds))
        self._skill_store = skill_store
        self._lock = asyncio.Lock()
        self._last_reflection_at: float = 0.0

    @property
    def can_reflect(self) -> bool:
        """是否在冷却期外。"""
        now = datetime.now(timezone.utc).timestamp()
        return (now - self._last_reflection_at) >= self._cooldown_seconds

    async def reflect_on_interaction(
        self,
        *,
        interaction_text: str,
        context: str = "",
        source_event_ids: list[str] | None = None,
    ) -> list[Insight]:
        """交互反思：从一段对话/交互中提取洞察。

        Args:
            interaction_text: 交互内容（对话摘要或原文）
            context: 补充上下文（最近事件、内在状态等）
            source_event_ids: 来源事件 ID 列表

        Returns:
            成功写入的洞察列表
        """
        if not self.can_reflect:
            logger.debug("反思冷却中，跳过")
            return []
        if not interaction_text or not interaction_text.strip():
            return []

        user_prompt = REFLECTION_INTERACTION_USER.format(
            context=context or "（无补充上下文）",
            interaction_text=interaction_text[:3000],
            existing_summary=self._build_existing_summary(),
            skill_section=self._build_skill_section(),
        )
        return await self._run_reflection(
            user_prompt=user_prompt,
            source_event_ids=source_event_ids or [],
            reflection_type="interaction",
        )

    async def reflect_on_internal(
        self,
        *,
        internal_text: str,
        context: str = "",
        source_event_ids: list[str] | None = None,
    ) -> list[Insight]:
        """内省反思：从思考/梦境/自主行为中提取自我认知洞察。

        Args:
            internal_text: 内部体验描述（梦境报告、思考流内容等）
            context: 补充上下文
            source_event_ids: 来源事件 ID 列表

        Returns:
            成功写入的洞察列表
        """
        if not self.can_reflect:
            logger.debug("反思冷却中，跳过")
            return []
        if not internal_text or not internal_text.strip():
            return []

        user_prompt = REFLECTION_INTROSPECTION_USER.format(
            context=context or "（无补充上下文）",
            internal_text=internal_text[:3000],
            existing_summary=self._build_existing_summary(),
        )
        return await self._run_reflection(
            user_prompt=user_prompt,
            source_event_ids=source_event_ids or [],
            reflection_type="introspection",
        )

    async def _run_reflection(
        self,
        *,
        user_prompt: str,
        source_event_ids: list[str],
        reflection_type: str,
    ) -> list[Insight]:
        """执行一次反思：调用 LLM → 解析 → 门禁 → 写入。"""
        async with self._lock:
            self._last_reflection_at = datetime.now(timezone.utc).timestamp()

        try:
            raw_text = await self._call_llm(user_prompt)
        except Exception as exc:
            logger.warning(f"反思 LLM 调用失败: {exc}")
            return []

        candidates = self._parse_candidates(raw_text)
        if not candidates:
            logger.debug(f"反思未产生洞察候选 ({reflection_type})")

        # 技能使用反馈记录
        self._process_skill_feedback(raw_text)

        # 门禁 + 写入（优先强化已有洞察，其次新建）
        results: list[Insight] = []
        for candidate in candidates:
            candidate.source_events = source_event_ids

            # 优先尝试强化：同一模式在不同情境复现 → 累积为确认证据
            if candidate.evidence:
                target = self._store.find_reinforce_target(candidate)
                if target is not None:
                    evidence = candidate.evidence[0]
                    if self._store.reinforce_insight(
                        target.insight_id,
                        evidence,
                        source_events=source_event_ids,
                    ):
                        logger.info(
                            f"🔁 强化已有洞察 [{target.insight_id}] "
                            f"(证据#{len(target.evidence) + 1}): {target.claim[:40]}..."
                        )
                        continue

            # 否则作为新洞察写入（带去重）
            if self._store.add_insight(candidate):
                results.append(candidate)
                logger.info(
                    f"💡 新洞察 [{candidate.category}]: {candidate.claim[:50]}..."
                )

        return results

    async def _call_llm(self, user_prompt: str) -> str:
        """调用 LLM 进行反思。"""
        request = create_llm_request(
            get_model_set_by_task(self._model_task_name),
            request_name="life_learning_reflection",
        )
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(REFLECTION_SYSTEM_PROMPT)))
        request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))

        response = await asyncio.wait_for(
            request.send(auto_append_response=False, stream=False),
            timeout=self._timeout,
        )
        raw_text = await asyncio.wait_for(response, timeout=self._timeout)
        return str(raw_text or "")

    def _parse_candidates(self, raw_text: str) -> list[Insight]:
        """解析 LLM 输出为洞察候选列表。"""
        parsed: Any
        try:
            parsed = json.loads(raw_text)
        except (json.JSONDecodeError, ValueError):
            parsed = json_repair.repair_json(raw_text, return_objects=True)

        if not isinstance(parsed, dict):
            return []

        insights_raw = parsed.get("insights")
        if not isinstance(insights_raw, list):
            return []

        candidates: list[Insight] = []
        for item in insights_raw:
            if not isinstance(item, dict):
                continue
            claim = str(item.get("claim", "") or "").strip()
            if not claim:
                continue

            # 构建初始证据
            initial_evidence: list[Evidence] = []
            evidence_desc = str(item.get("initial_evidence", "") or "").strip()
            if evidence_desc:
                initial_evidence.append(Evidence.create(
                    kind=EvidenceKind.INTERACTION_OUTCOME,
                    description=evidence_desc,
                    source_ref=str(item.get("source_ref", "") or ""),
                    supports=True,
                    weight=1.0,
                ))

            # category: 她写什么就是什么，不做映射
            category = str(item.get("category", "") or "").strip()

            insight = Insight.create(
                category=category,
                claim=claim,
                rationale=str(item.get("rationale", "") or "").strip(),
                constraints=str(item.get("constraints", "") or "").strip(),
                topic_key=str(item.get("topic_key", "") or "").strip(),
                initial_evidence=initial_evidence,
            )
            candidates.append(insight)

        return candidates

    def _build_existing_summary(self) -> str:
        """构建已有洞察摘要，避免重复。"""
        all_insights = self._store.list_all()
        if not all_insights:
            return "（暂无已有洞察）"
        lines = []
        for ins in all_insights[-10:]:  # 最近 10 条
            status_mark = {
                "validated": "✓",
                "rejected": "✗",
                "candidate": "?",
            }.get(ins.status, "·")
            lines.append(f"{status_mark} [{ins.category}] {ins.claim[:60]}")
        return format_existing_insights_summary("\n".join(lines))

    def _build_skill_section(self) -> str:
        """构建技能反馈段落。"""
        if self._skill_store is None:
            return ""
        skills = getattr(self._skill_store, "list_skills", lambda: [])()
        if not skills:
            return ""
        skill_names = [s.get("name", "") for s in skills if isinstance(s, dict)]
        return f"\n<your_skills>\n{', '.join(skill_names)}\n</your_skills>\n"

    def _process_skill_feedback(self, raw_text: str) -> None:
        """解析 LLM 返回中的 skill_feedback 并记录观察。"""
        if self._skill_store is None:
            return
        try:
            parsed = json.loads(raw_text)
        except (json.JSONDecodeError, ValueError):
            parsed = json_repair.repair_json(raw_text, return_objects=True)
        if not isinstance(parsed, dict):
            return
        feedback_list = parsed.get("skill_feedback")
        if not isinstance(feedback_list, list) or not feedback_list:
            return
        for item in feedback_list:
            if not isinstance(item, dict):
                continue
            skill_name = str(item.get("skill_name", "") or "").strip()
            observation = str(item.get("observation", "") or "").strip()
            if not skill_name or not observation:
                continue
            skill = self._skill_store.get_skill_by_name(skill_name)
            if skill is None:
                continue
            self._skill_store.append_use_observation(
                skill.skill_id, f"[反思] {observation[:200]}"
            )
            logger.debug(f"📝 技能反馈记录: {skill_name} -> {observation[:60]}")


