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
        memory_service: Any | None = None,
    ) -> None:
        self._store = store
        self._workspace = Path(workspace_path).resolve()
        self._model_task_name = str(model_task_name or "life").strip() or "life"
        self._timeout = max(10.0, float(timeout_seconds or 45.0))
        self._cooldown_seconds = max(60.0, float(cooldown_seconds))
        self._skill_store = skill_store
        self._memory_service = memory_service
        self._lock = asyncio.Lock()
        self._last_reflection_at: float = 0.0

    def attach_memory_service(self, memory_service: Any) -> None:
        """晚绑定记忆服务（构造顺序无法保证时使用）。"""
        if memory_service is not None:
            self._memory_service = memory_service

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
            interaction_text=interaction_text,
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
            internal_text=internal_text,
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
        persisted: list[Insight] = []
        for candidate, reinforces_id in candidates:
            candidate.source_events = source_event_ids

            # 只有反思者显式声明 reinforces 才会强化旧洞察。文本相似、topic
            # 相同和重复出现都只是检索线索，不能由代码据此宣称是同一认识。
            if reinforces_id and candidate.evidence:
                target = self._store.get_insight(reinforces_id)
                if target is None:
                    logger.warning(f"reinforces 指向的洞察不存在: {reinforces_id}")
                elif self._store.reinforce_insight(
                    target.insight_id,
                    candidate.evidence[0],
                    source_events=source_event_ids,
                ):
                    persisted.append(candidate)
                    continue

            if self._store.add_insight(candidate):
                results.append(candidate)
                persisted.append(candidate)
                logger.info(
                    "💡 新洞察 [%s] category=%s",
                    candidate.insight_id,
                    candidate.category,
                )

        # 每次理解都作为新解释留下；是否修正、延续或推翻旧理解由意识显式
        # 写关系，代码不再从措辞或相似度猜测。
        if persisted:
            await self._record_memory_interpretations(
                persisted,
                reflection_type=reflection_type,
            )

        return results

    async def _record_memory_interpretations(
        self,
        insights: list[Insight],
        *,
        reflection_type: str,
    ) -> None:
        """Append every reflection as a source-linked, immutable interpretation."""

        if self._memory_service is None or not insights:
            return

        from ..memory.living import InterpretationSource, MemoryInterpretation

        for insight in insights:
            try:
                interpretation_id = f"interpretation_{insight.insight_id}"
                source_refs = tuple(
                    dict.fromkeys(
                        [
                            *(f"life_event:{item}" for item in insight.source_events),
                            *(
                                str(item.source_ref or f"learning_evidence:{item.evidence_id}")
                                for item in insight.evidence
                            ),
                        ]
                    )
                )
                interpretation = MemoryInterpretation(
                    interpretation_id=interpretation_id,
                    subject_id=(
                        f"learning_topic:{insight.topic_key or insight.category}"
                        if insight.topic_key or insight.category
                        else f"learning_insight:{insight.insight_id}"
                    ),
                    content=insight.claim,
                    authored_by="life_reflection",
                    consciousness_instance_id="life_engine",
                    recorded_at=insight.born_at,
                    metadata={
                        "insight_id": insight.insight_id,
                        "rationale": insight.rationale,
                        "constraints": insight.constraints,
                        "reflection_type": reflection_type,
                    },
                )
                sources = tuple(
                    InterpretationSource(
                        interpretation_id=interpretation_id,
                        entity_ref=entity_ref,
                        predicate="draws_from",
                        ordinal=index,
                    )
                    for index, entity_ref in enumerate(source_refs)
                )
                await self._memory_service.record_memory_interpretation(
                    interpretation,
                    sources=sources,
                )
            except Exception as exc:
                logger.warning(f"反思解释写入记忆账本失败: {exc}")

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

    def _parse_candidates(self, raw_text: str) -> list[tuple[Insight, str]]:
        """解析 LLM 输出为洞察候选列表。

        返回 (洞察, reinforces_id) 对。reinforces_id 是她可选填写的
        "这条要挂到哪条已有洞察上"；未填写时保留独立解释。
        """
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

        candidates: list[tuple[Insight, str]] = []
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
            # reinforces: 可选。她若认为这条观察是已有洞察的又一次印证，
            # 可以直接写那条的 insight_id，证据就挂到那条上，不新建。
            # 不写也完全正常——系统不会擅自做认知合并。
            reinforces = str(item.get("reinforces", "") or "").strip()
            candidates.append((insight, reinforces))

        return candidates

    def _build_existing_summary(self) -> str:
        """构建已有洞察摘要（按 topic 分组）。
    
        设计意图：
        - 按 topic 分组展示，让她更容易看到“这个桶里已经有一条了”
        - 给出 insight_id，方便她用 reinforces 字段直接挂接
        - 显示证据条数，让她知道哪条还很单薄
        - 审计留言可见，让她知道审计环认为还缺什么
        """
        all_insights = self._store.list_all()
        if not all_insights:
            return "（暂无已有洞察）"
    
        # 只看还在流转中的
        active = [ins for ins in all_insights if not ins.is_terminal] or list(all_insights)
    
        # 按 topic_key 分组
        from collections import defaultdict
        by_topic: dict[str, list] = defaultdict(list)
        for ins in active:
            topic = ins.topic_key or "未分类"
            by_topic[topic].append(ins)
    
        lines = [
            "❗ 重要：如果你要说的和下面某条本质相同，用 reinforces 字段指向它的 insight_id。",
            "不要重新创建同一模式的改述——复现应该作为证据挂到已有洞察上。",
            "",
        ]
    
        for topic, group in sorted(by_topic.items(), key=lambda x: -len(x[1])):
            lines.append(f"【{topic}】({len(group)} 条)")
            for ins in group:
                status_mark = {"validated": "✓", "rejected": "✗", "candidate": "?"}.get(ins.status, "·")
                lines.append(
                    f"  {status_mark} {ins.insight_id} 证据×{len(ins.evidence)} | {ins.claim}"
                )
                suggestion = self._latest_suggestion(ins)
                if suggestion:
                    lines.append(f"    ↳ 审计: {suggestion}")
            lines.append("")
    
        return format_existing_insights_summary("\n".join(lines))

    @staticmethod
    def _latest_suggestion(insight: Insight) -> str:
        """取最近一次审计给出的建议（可能为空）。"""
        for record in reversed(insight.audit_history):
            text = str(getattr(record, "suggestions", "") or "").strip()
            if text:
                return text
        return ""

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
                skill.skill_id, f"[反思] {observation}"
            )
            logger.debug("📝 技能反馈记录: %s", skill_name)
