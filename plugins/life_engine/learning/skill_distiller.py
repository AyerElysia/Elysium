"""SkillDistiller：将 validated 洞察蒸馏为技能模式。

慢环扩展，与 SelfKnowledgeCompressor 并行：
- Compressor 处理 self_knowledge/emotional_pattern → self_knowledge.md（"我是谁"）
- Distiller 处理 social_strategy/communication_style/behavioral_pattern → skills（"我怎么做"）

设计原则：
- 有界编辑（SkillOpt 纪律）：每次最多改 1-2 处
- 内省门控（替代 benchmark score）："这真的更像我吗？"
- 拒绝缓存：试过的弯路不重复
- protected lessons：embodied + protected 的技能不被快更新覆盖
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

from .models import Insight, InsightNextAction, InsightStatus
from .prompts import (
    SKILL_DISTILL_SYSTEM,
    SKILL_DISTILL_USER,
    SKILL_GATE_SYSTEM,
    SKILL_GATE_USER,
    format_insights_for_compression,
)
from .skill_store import SkillMaturity, SkillPattern, SkillStore
from .store import InsightStore

logger = logging.getLogger("life_engine.learning.skill_distiller")

# 默认参数
_DEFAULT_TRIGGER_COUNT = 3       # 触发蒸馏的 validated 技能类洞察数
_DEFAULT_INTERVAL_HOURS = 24.0   # 蒸馏最小间隔
_DEFAULT_MAX_EDITS = 2           # 每次最多编辑数


class SkillDistiller:
    """将 validated 洞察蒸馏为技能模式（程序性记忆）。"""

    def __init__(
        self,
        *,
        store: InsightStore,
        skill_store: SkillStore,
        workspace_path: str | Path,
        model_task_name: str = "life",
        timeout_seconds: float = 90.0,
        trigger_count: int = _DEFAULT_TRIGGER_COUNT,
        interval_hours: float = _DEFAULT_INTERVAL_HOURS,
        max_edits: int = _DEFAULT_MAX_EDITS,
    ) -> None:
        self._store = store
        self._skill_store = skill_store
        self._workspace = Path(workspace_path).resolve()
        self._model_task_name = str(model_task_name or "life").strip() or "life"
        self._timeout = max(30.0, float(timeout_seconds or 90.0))
        self._trigger_count = max(1, int(trigger_count or _DEFAULT_TRIGGER_COUNT))
        self._interval_hours = max(6.0, float(interval_hours or _DEFAULT_INTERVAL_HOURS))
        self._max_edits = max(1, int(max_edits or _DEFAULT_MAX_EDITS))
        self._lock = asyncio.Lock()

    def should_distill(self) -> bool:
        """判断是否应该触发蒸馏。"""
        distillable = self._collect_distillable_insights()
        if len(distillable) >= self._trigger_count:
            return True

        # 检查时间间隔
        state = self._store.load_state()
        last_distill = state.get("last_skill_distill_at", "")
        if not last_distill and distillable:
            return True
        if last_distill:
            try:
                last_dt = datetime.fromisoformat(last_distill)
                now = datetime.now(timezone.utc).astimezone()
                hours_elapsed = (now - last_dt).total_seconds() / 3600.0
                if hours_elapsed >= self._interval_hours and distillable:
                    return True
            except (ValueError, TypeError):
                pass
        return False

    async def run_distillation(self) -> bool:
        """执行一次蒸馏周期。返回是否成功产出/更新技能。"""
        async with self._lock:
            distillable = self._collect_distillable_insights()
            if not distillable:
                logger.debug("无可蒸馏的程序性记忆类 validated 洞察")
                return False

            logger.info(f"🔧 开始技能蒸馏: {len(distillable)} 条 validated 洞察")

            # 按 topic 分组，找是否已有相关 skill
            topic = distillable[0].topic_key or ""
            existing_skill = self._find_matching_skill(topic, distillable)

            # 调用 LLM 蒸馏
            result = await self._distill(
                validated_insights=distillable,
                existing_skill=existing_skill,
            )
            if result is None:
                logger.info("蒸馏未产出结果")
                return False

            # 内省门控
            if existing_skill:
                old_content = f"{existing_skill.description}\n{existing_skill.instructions}"
                new_content = f"{result.get('description', '')}\n{result.get('instructions', '')}"
                promote = await self._introspective_gate(
                    old_content=old_content,
                    new_content=new_content,
                    insight_count=len(distillable),
                )
            else:
                # 新技能：首次创建默认接受（bootstrap）
                promote = True

            if not promote:
                # 记录到拒绝缓存
                if existing_skill:
                    self._skill_store.append_rejected_edit(
                        existing_skill.skill_id,
                        edit_summary=result.get("description", "")[:100],
                        reason="introspective_gate_rejected",
                    )
                logger.info("⏸️ 技能蒸馏未通过内省门控")
                return False

            # 应用结果
            if existing_skill:
                existing_skill.description = result.get("description", existing_skill.description)
                existing_skill.instructions = result.get("instructions", existing_skill.instructions)
                existing_skill.last_refined_at = _now_iso()
                # 追加 origin
                for ins in distillable:
                    if ins.insight_id not in existing_skill.origin_insight_ids:
                        existing_skill.origin_insight_ids.append(ins.insight_id)
                self._skill_store.update_skill(existing_skill)
                logger.info(f"✅ 技能精炼: {existing_skill.name}")
            else:
                name = result.get("name", "") or self._derive_name(topic)
                new_skill = SkillPattern.create(
                    name=name,
                    description=result.get("description", ""),
                    instructions=result.get("instructions", ""),
                    maturity=SkillMaturity.EMERGING,
                    origin_insight_ids=[ins.insight_id for ins in distillable],
                )
                self._skill_store.add_skill(new_skill)
                logger.info(f"✅ 新技能诞生: {new_skill.name}")

            # 标记已蒸馏的洞察
            for ins in distillable:
                ins.next_action = InsightNextAction.ARCHIVE.value
                self._store.update_insight(ins)

            # 更新状态
            state = self._store.load_state()
            state["last_skill_distill_at"] = _now_iso()
            self._store.save_state(state)

            return True

    # ── 内部方法 ─────────────────────────────────────────────

    def _collect_distillable_insights(self) -> list[Insight]:
        """收集所有 validated 且待蒸馏的洞察。"""
        validated = self._store.list_by_status(InsightStatus.VALIDATED)
        return [
            ins for ins in validated
            if ins.next_action != InsightNextAction.ARCHIVE.value
        ]

    def _find_matching_skill(
        self, topic: str, insights: list[Insight]
    ) -> SkillPattern | None:
        """查找与洞察主题匹配的已有技能。"""
        if not topic:
            return None
        skills = self._skill_store.list_skills()
        topic_lower = topic.lower().strip()
        for s in skills:
            # 匹配 name 或 description 中包含 topic
            if topic_lower in s.name.lower() or topic_lower in s.description.lower():
                return s
        return None

    def _derive_name(self, topic: str) -> str:
        """从 topic 派生 kebab-case 名称。"""
        import re
        name = topic.strip().lower()
        name = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", name)
        name = re.sub(r"-{2,}", "-", name).strip("-")
        return name[:40] or "new-skill"

    async def _distill(
        self,
        *,
        validated_insights: list[Insight],
        existing_skill: SkillPattern | None,
    ) -> dict[str, str] | None:
        """调用 LLM 执行蒸馏/精炼。"""
        system_prompt = SKILL_DISTILL_SYSTEM.format(max_edits=self._max_edits)

        # 构建 current_skill 文本
        if existing_skill:
            current_skill = (
                f"name: {existing_skill.name}\n"
                f"description: {existing_skill.description}\n"
                f"instructions: {existing_skill.instructions}"
            )
            action_hint = "对这个已有技能做有界精炼"
            rejected_text = "\n".join(
                f"- {r.get('summary', '')} ({r.get('reason', '')})"
                for r in existing_skill.rejected_edits[-5:]
            ) or "（暂无）"
            observations_text = "\n".join(
                existing_skill.use_observations[-5:]
            ) or "（暂无）"
        else:
            current_skill = "（这是一个新技能，还没有记录。）"
            action_hint = "蒸馏出一条新技能"
            rejected_text = "（暂无）"
            observations_text = "（暂无）"

        user_prompt = SKILL_DISTILL_USER.format(
            validated_insights=format_insights_for_compression(
                [ins.to_dict() for ins in validated_insights]
            ),
            current_skill=current_skill,
            rejected_edits=rejected_text,
            use_observations=observations_text,
            action_hint=action_hint,
            max_edits=self._max_edits,
        )

        try:
            request = create_llm_request(
                get_model_set_by_task(self._model_task_name),
                request_name="life_skill_distill",
            )
            request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))
            request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))

            response = await asyncio.wait_for(
                request.send(auto_append_response=False, stream=False),
                timeout=self._timeout,
            )
            raw_text = await asyncio.wait_for(response, timeout=self._timeout)
            return self._parse_distill_result(str(raw_text or ""))
        except Exception as exc:
            logger.warning(f"技能蒸馏 LLM 调用失败: {exc}")
            return None

    async def _introspective_gate(
        self,
        *,
        old_content: str,
        new_content: str,
        insight_count: int,
    ) -> bool:
        """内省验证门控：这真的更像我吗？"""
        user_prompt = SKILL_GATE_USER.format(
            old_content=old_content[:2000],
            new_content=new_content[:2000],
            insight_count=insight_count,
        )

        try:
            request = create_llm_request(
                get_model_set_by_task(self._model_task_name),
                request_name="life_skill_gate",
            )
            request.add_payload(LLMPayload(ROLE.SYSTEM, Text(SKILL_GATE_SYSTEM)))
            request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))

            response = await asyncio.wait_for(
                request.send(auto_append_response=False, stream=False),
                timeout=self._timeout,
            )
            raw_text = await asyncio.wait_for(response, timeout=self._timeout)
            return self._parse_gate_result(str(raw_text or ""))
        except Exception as exc:
            logger.warning(f"内省门控调用失败，默认接受: {exc}")
            return True  # 门控失败时不阻塞（与 knowledge.py 的保守策略不同：技能更轻量）

    def _parse_distill_result(self, raw_text: str) -> dict[str, str] | None:
        """解析蒸馏 LLM 返回的 JSON。"""
        parsed: Any
        try:
            parsed = json.loads(raw_text)
        except (json.JSONDecodeError, ValueError):
            parsed = json_repair.repair_json(raw_text, return_objects=True)

        if not isinstance(parsed, dict):
            return None
        # 至少需要 description
        if not parsed.get("description"):
            return None
        return {
            "name": str(parsed.get("name", "") or ""),
            "description": str(parsed.get("description", "") or ""),
            "instructions": str(parsed.get("instructions", "") or ""),
        }

    def _parse_gate_result(self, raw_text: str) -> bool:
        """解析内省门控结果。"""
        parsed: Any
        try:
            parsed = json.loads(raw_text)
        except (json.JSONDecodeError, ValueError):
            parsed = json_repair.repair_json(raw_text, return_objects=True)

        if not isinstance(parsed, dict):
            return True  # 解析失败默认接受
        return bool(parsed.get("promote", False))


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()
