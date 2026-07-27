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

# 认知修正的语言标记：命中任一即认为这条洞察是"我之前理解错了"
_CORRECTION_MARKERS: tuple[str, ...] = (
    "不是",
    "其实",
    "修正",
    "更正",
    "之前以为",
    "原以为",
    "我错了",
    "并非",
    "误解",
    "重新理解",
    "推翻",
)

# 单次反思最多落多少条显式修正（防止一次反思刷满修正表）
_MAX_AUTO_CORRECTIONS_PER_RUN = 2
# 为一条修正检索关联记忆文件时的候选数
_CORRECTION_PATH_TOP_K = 3
# 一条修正最多绑定多少个记忆文件
_MAX_CORRECTION_PATHS = 2


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
        persisted: list[Insight] = []
        for candidate, reinforces_id in candidates:
            candidate.source_events = source_event_ids

            # 优先尝试强化：同一模式在不同情境复现 → 累积为确认证据
            if candidate.evidence:
                # 她明确指名要挂到哪条，就听她的；指名无效才回落到语义匹配
                target = None
                if reinforces_id:
                    target = self._store.get_insight(reinforces_id)
                    if target is None:
                        logger.debug(f"reinforces 指向的洞察不存在: {reinforces_id}")
                if target is None:
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
                        persisted.append(candidate)
                        continue

            # 否则作为新洞察写入（带去重 + topic 饱和守卫）
            # topic 饱和守卫：同 topic 已有 >= 3 条活跃洞察时，强制尝试强化而非新建
            if candidate.topic_key:
                same_topic_count = sum(
                    1 for ins in self._store.list_all()
                    if ins.topic_key == candidate.topic_key and not ins.is_terminal
                )
                if same_topic_count >= 3:
                    # 强制尝试强化最相似的那条
                    target = self._store.find_reinforce_target(candidate)
                    if target is not None and candidate.evidence:
                        self._store.reinforce_insight(
                            target.insight_id,
                            candidate.evidence[0],
                            source_events=source_event_ids,
                        )
                        logger.info(
                            f"🚫 topic '{candidate.topic_key}' 已饱和({same_topic_count}条)，"
                            f"强制强化 [{target.insight_id}]"
                        )
                        persisted.append(candidate)
                        continue

            if self._store.add_insight(candidate):
                results.append(candidate)
                persisted.append(candidate)
                logger.info(
                    f"💡 新洞察 [{candidate.category}]: {candidate.claim[:50]}..."
                )

        # 记忆演化：把"修正型洞察"落成显式修正记录，挂到相关记忆文件上
        if persisted:
            await self._auto_record_corrections(persisted, reflection_type=reflection_type)

        return results

    async def _auto_record_corrections(
        self,
        insights: list[Insight],
        *,
        reflection_type: str,
    ) -> None:
        """把"修正型洞察"落成显式记忆修正记录。

        反思环里最有价值的一类洞察是"我之前理解错了"。这类认知转折如果只
        存在于 insight 里，检索记忆时是看不见的——她会重新读到旧理解，却不
        知道自己已经修正过。所以这里把它们写进 memory_corrections，并尽量
        绑定到真实记忆节点上，让下次检索该文件时修正会跟着一起浮现。
        """
        if self._memory_service is None or not insights:
            return

        seen: set[str] = set()
        recorded = 0
        for insight in insights:
            if recorded >= _MAX_AUTO_CORRECTIONS_PER_RUN:
                break

            message = self._extract_correction_message(insight)
            if not message:
                continue
            dedup_key = message[:120]
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            topic = (insight.topic_key or insight.category or "自我认知").strip()
            related_paths = await self._resolve_correction_paths(insight)

            if await self._write_correction(
                topic=topic,
                message=message,
                related_paths=related_paths,
                query=insight.claim,
            ):
                recorded += 1
                logger.info(
                    f"🔧 记忆修正已记录 [{topic}] -> "
                    f"{related_paths or '（未绑定文件）'}: {message[:50]}..."
                )

        if recorded:
            logger.debug(
                f"反思({reflection_type}) 产生 {recorded} 条显式记忆修正"
            )

    def _extract_correction_message(self, insight: Insight) -> str:
        """判断洞察是否属于"认知修正"，并组装修正说明。"""
        claim = (insight.claim or "").strip()
        if len(claim) < 6:
            return ""

        haystack = " ".join(
            part for part in (claim, insight.rationale, insight.revision_note) if part
        )
        if not any(marker in haystack for marker in _CORRECTION_MARKERS):
            return ""

        rationale = (insight.rationale or "").strip()
        if rationale and rationale not in claim:
            return f"{claim}（依据：{rationale[:120]}）"
        return claim

    async def _resolve_correction_paths(self, insight: Insight) -> list[str]:
        """检索这条修正应该挂到哪些记忆文件上。"""
        query = (insight.claim or "").strip()
        if not query:
            return []
        try:
            results = await self._memory_service.search_memory(
                query=query,
                top_k=_CORRECTION_PATH_TOP_K,
                enable_association=False,
                return_bundles=False,
            )
        except Exception as exc:  # 检索失败不应阻断修正记录
            logger.debug(f"修正记录检索关联文件失败: {exc}")
            return []

        paths: list[str] = []
        for item in results or []:
            path = str(getattr(item, "file_path", "") or "").strip()
            if not path or not path.endswith(".md"):
                continue
            if path not in paths:
                paths.append(path)
            if len(paths) >= _MAX_CORRECTION_PATHS:
                break
        return paths

    async def _write_correction(
        self,
        *,
        topic: str,
        message: str,
        related_paths: list[str],
        query: str,
    ) -> bool:
        """写入修正记录；路径不可索引时降级为不绑定文件。"""
        try:
            await self._memory_service.record_memory_correction(
                topic=topic,
                message=message,
                related_paths=related_paths or None,
                source="reflection",
                query=query,
            )
            return True
        except ValueError as exc:
            if not related_paths:
                logger.debug(f"记忆修正写入被拒绝: {exc}")
                return False
            logger.debug(f"修正关联路径不可索引，降级为不绑定文件: {exc}")
            try:
                await self._memory_service.record_memory_correction(
                    topic=topic,
                    message=message,
                    related_paths=None,
                    source="reflection",
                    query=query,
                )
                return True
            except Exception as inner:
                logger.debug(f"记忆修正写入失败: {inner}")
                return False
        except Exception as exc:
            logger.warning(f"记忆修正写入失败: {exc}")
            return False

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
        "这条要挂到哪条已有洞察上"——填了就按她说的挂，没填才走语义匹配。
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
            # 不写也完全正常——系统会自己做语义匹配。
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
    
        # 每个 topic 最多展示 2 条（避免过长）
        shown_count = 0
        for topic, group in sorted(by_topic.items(), key=lambda x: -len(x[1])):
            if shown_count >= 12:
                lines.append(f"… 还有 {len(by_topic) - shown_count} 个 topic 未展示")
                break
            lines.append(f"【{topic}】({len(group)} 条)")
            for ins in group[:2]:
                status_mark = {"validated": "✓", "rejected": "✗", "candidate": "?"}.get(ins.status, "·")
                lines.append(
                    f"  {status_mark} {ins.insight_id} 证据×{len(ins.evidence)} | {ins.claim[:60]}"
                )
                suggestion = self._latest_suggestion(ins)
                if suggestion:
                    lines.append(f"    ↳ 审计: {suggestion[:100]}")
            if len(group) > 2:
                lines.append(f"  … 及另外 {len(group)-2} 条")
            shown_count += 1
            lines.append("")
    
        return format_existing_insights_summary("\n".join(lines), max_chars=3000)

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
                skill.skill_id, f"[反思] {observation[:200]}"
            )
            logger.debug(f"📝 技能反馈记录: {skill_name} -> {observation[:60]}")


