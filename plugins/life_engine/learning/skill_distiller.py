"""Distil validated experiences into traceable, subject-chosen skill patterns.

The orchestration layer schedules work and persists decisions.  It never guesses
which skill an insight belongs to from names, topics, keywords, or similarity.
The integration model sees every current skill and explicitly selects an exact
``skill_id`` or decides that a genuinely new skill is needed.  A separate gate
reviews both new and edited skills; unavailable or malformed review never means
approval.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import json_repair

from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
from src.kernel.llm import ROLE, LLMPayload, Text

from .models import Insight, InsightNextAction, InsightStatus
from .projection import project_learning_text
from .prompts import (
    SKILL_DISTILL_SYSTEM,
    SKILL_DISTILL_USER,
    SKILL_GATE_SYSTEM,
    SKILL_GATE_USER,
    format_insights_for_compression,
)
from .skill_store import SkillCandidate, SkillPattern, SkillStore
from .store import InsightStore

logger = logging.getLogger("life_engine.learning.skill_distiller")

_DEFAULT_TRIGGER_COUNT = 3
_DEFAULT_INTERVAL_HOURS = 24.0


class SkillDistiller:
    """Turn validated insights into procedural memory without code-side judgment."""

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
        max_edits: int | None = None,
        current_subject_revision: Callable[[], Awaitable[str]] | None = None,
    ) -> None:
        self._store = store
        self._skill_store = skill_store
        self._workspace = Path(workspace_path).resolve()
        self._model_task_name = str(model_task_name or "life").strip() or "life"
        self._timeout = max(30.0, float(timeout_seconds or 90.0))
        self._trigger_count = max(1, int(trigger_count or _DEFAULT_TRIGGER_COUNT))
        self._interval_hours = max(
            6.0, float(interval_hours or _DEFAULT_INTERVAL_HOURS)
        )
        # Retained only so old callers/configuration continue to load.  The model
        # decides the coherent scope of an edit; orchestration does not cap it.
        del max_edits
        self._lock = asyncio.Lock()
        self._last_projection_stats: dict[str, Any] = {}
        self._current_subject_revision = current_subject_revision

    def projection_health(self) -> dict[str, Any]:
        """Return content-free trace metadata for the last skill requests."""

        return dict(self._last_projection_stats)

    def should_distill(self) -> bool:
        distillable = self._collect_distillable_insights()
        if len(distillable) >= self._trigger_count:
            return True

        state = self._store.load_state()
        last_distill = state.get("last_skill_distill_at", "")
        if not last_distill and distillable:
            return True
        if last_distill:
            try:
                last_dt = datetime.fromisoformat(last_distill)
                now = datetime.now(UTC).astimezone()
                hours_elapsed = (now - last_dt).total_seconds() / 3600.0
                if hours_elapsed >= self._interval_hours and distillable:
                    return True
            except (ValueError, TypeError):
                pass
        return False

    def pending_count(self) -> int:
        """Return a content-free count for health and scheduling evidence."""

        return len(self._collect_distillable_insights())

    async def run_distillation(self) -> bool:
        async with self._lock:
            distillable = self._collect_distillable_insights()
            if not distillable:
                logger.debug("No validated insights are waiting for skill distillation")
                return False

            current_skills = self._skill_store.list_skills()
            result = await self._distill(
                validated_insights=distillable,
                existing_skills=current_skills,
            )
            if result is None:
                logger.info("Skill distillation produced no valid proposal")
                return False

            target_skill_id = result["target_skill_id"]
            existing_skill = (
                self._skill_store.get_skill(target_skill_id)
                if target_skill_id
                else None
            )
            if target_skill_id and existing_skill is None:
                logger.warning(
                    "Skill proposal named an unknown target_skill_id; proposal retained "
                    "only in logs and insights remain pending: %s",
                    target_skill_id,
                )
                return False

            if existing_skill is None and not result["name"]:
                logger.warning(
                    "New skill proposal has no subject-chosen name; insights remain pending"
                )
                return False

            old_content = self._skill_content(existing_skill)
            new_content = self._proposal_content(result)
            gate_recommended = await self._introspective_gate(
                old_content=old_content,
                new_content=new_content,
                insight_count=len(distillable),
            )
            if self._current_subject_revision is None:
                logger.warning(
                    "Skill proposal cannot be recorded without an exact current "
                    "SOUL+USER+MEMORY revision"
                )
                return False
            try:
                subject_revision = await self._current_subject_revision()
            except Exception as exc:
                logger.warning(
                    "Skill proposal subject revision is unavailable: %s",
                    type(exc).__name__,
                )
                return False

            insight_ids = [insight.insight_id for insight in distillable]
            source_event_ids = sorted(
                {
                    source_event_id
                    for insight in distillable
                    for source_event_id in insight.source_events
                    if source_event_id
                }
            )
            source_material = json.dumps(
                {
                    "insight_ids": insight_ids,
                    "source_event_ids": source_event_ids,
                    "subject_revision": subject_revision,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            proposed_skill_id = (
                existing_skill.skill_id
                if existing_skill is not None
                else SkillPattern.create(
                    name=result["name"],
                    description=result["description"],
                    instructions=result["instructions"],
                ).skill_id
            )
            candidate = SkillCandidate.create(
                subject_revision=subject_revision,
                source_occurrence_id=(
                    "skill_distillation:"
                    + hashlib.sha256(source_material.encode("utf-8")).hexdigest()
                ),
                target_skill_id=(existing_skill.skill_id if existing_skill else ""),
                proposed_skill_id=proposed_skill_id,
                name=result["name"] or (existing_skill.name if existing_skill else ""),
                description=result["description"],
                instructions=result["instructions"],
                insight_ids=insight_ids,
                source_event_ids=source_event_ids,
                gate_recommended=gate_recommended,
            )
            self._skill_store.append_candidate(candidate)

            state = self._store.load_state()
            state["last_skill_distill_at"] = _now_iso()
            self._store.save_state(state)
            return True

    def _collect_distillable_insights(self) -> list[Insight]:
        already_proposed = {
            insight_id
            for candidate in self._skill_store.list_candidates()
            for insight_id in candidate.insight_ids
        }
        validated = self._store.list_by_status(InsightStatus.VALIDATED)
        return [
            insight
            for insight in validated
            if insight.next_action != InsightNextAction.ARCHIVE.value
            and insight.insight_id not in already_proposed
        ]

    async def _distill(
        self,
        *,
        validated_insights: list[Insight],
        existing_skills: list[SkillPattern],
    ) -> dict[str, str] | None:
        user_prompt = SKILL_DISTILL_USER.format(
            validated_insights=format_insights_for_compression(
                [insight.to_dict() for insight in validated_insights]
            ),
            existing_skills=self._format_existing_skills(existing_skills),
        )
        delivered = project_learning_text(
            user_prompt,
            max_bytes=96 * 1024,
            projection_kind="skill_distillation_request",
        )
        self._last_projection_stats["distillation"] = delivered.stats()

        try:
            request = create_llm_request(
                get_model_set_by_task(self._model_task_name),
                request_name="life_skill_distill",
            )
            request.add_payload(LLMPayload(ROLE.SYSTEM, Text(SKILL_DISTILL_SYSTEM)))
            request.add_payload(LLMPayload(ROLE.USER, Text(delivered.text)))

            response = await asyncio.wait_for(
                request.send(auto_append_response=False, stream=False),
                timeout=self._timeout,
            )
            raw_text = await asyncio.wait_for(response, timeout=self._timeout)
            return self._parse_distill_result(str(raw_text or ""))
        except Exception as exc:
            logger.warning(
                "Skill distillation request failed: %s",
                type(exc).__name__,
            )
            return None

    async def _introspective_gate(
        self,
        *,
        old_content: str,
        new_content: str,
        insight_count: int,
    ) -> bool:
        user_prompt = SKILL_GATE_USER.format(
            old_content=old_content,
            new_content=new_content,
            insight_count=insight_count,
        )
        delivered = project_learning_text(
            user_prompt,
            max_bytes=64 * 1024,
            projection_kind="skill_gate_request",
        )
        self._last_projection_stats["gate"] = delivered.stats()

        try:
            request = create_llm_request(
                get_model_set_by_task(self._model_task_name),
                request_name="life_skill_gate",
            )
            request.add_payload(LLMPayload(ROLE.SYSTEM, Text(SKILL_GATE_SYSTEM)))
            request.add_payload(LLMPayload(ROLE.USER, Text(delivered.text)))

            response = await asyncio.wait_for(
                request.send(auto_append_response=False, stream=False),
                timeout=self._timeout,
            )
            raw_text = await asyncio.wait_for(response, timeout=self._timeout)
            return self._parse_gate_result(str(raw_text or ""))
        except Exception as exc:
            logger.warning(
                "Introspective skill gate failed; proposal not accepted: %s",
                type(exc).__name__,
            )
            return False

    @staticmethod
    def _format_existing_skills(skills: list[SkillPattern]) -> str:
        if not skills:
            return "（暂无已有技能）"
        return json.dumps(
            [skill.to_dict() for skill in skills],
            ensure_ascii=False,
            indent=2,
        )

    @staticmethod
    def _skill_content(skill: SkillPattern | None) -> str:
        if skill is None:
            return "（尚无对应技能）"
        return json.dumps(skill.to_dict(), ensure_ascii=False, indent=2)

    @staticmethod
    def _proposal_content(result: dict[str, str]) -> str:
        return json.dumps(result, ensure_ascii=False, indent=2)

    @staticmethod
    def _parse_distill_result(raw_text: str) -> dict[str, str] | None:
        parsed: Any
        try:
            parsed = json.loads(raw_text)
        except (json.JSONDecodeError, ValueError):
            parsed = json_repair.repair_json(raw_text, return_objects=True)

        if not isinstance(parsed, dict):
            return None
        description = str(parsed.get("description", "") or "").strip()
        instructions = str(parsed.get("instructions", "") or "").strip()
        if not description or not instructions:
            return None
        return {
            "target_skill_id": str(parsed.get("target_skill_id", "") or "").strip(),
            "name": str(parsed.get("name", "") or "").strip(),
            "description": description,
            "instructions": instructions,
        }

    @staticmethod
    def _parse_gate_result(raw_text: str) -> bool:
        parsed: Any
        try:
            parsed = json.loads(raw_text)
        except (json.JSONDecodeError, ValueError):
            parsed = json_repair.repair_json(raw_text, return_objects=True)

        if not isinstance(parsed, dict):
            return False
        return parsed.get("promote") is True


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat()
