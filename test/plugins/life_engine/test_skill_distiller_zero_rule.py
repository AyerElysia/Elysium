from __future__ import annotations

import json

import pytest

from plugins.life_engine.learning.models import (
    Insight,
    InsightNextAction,
    InsightStatus,
)
from plugins.life_engine.learning.skill_distiller import SkillDistiller
from plugins.life_engine.learning.skill_store import SkillPattern, SkillStore
from plugins.life_engine.learning.store import InsightStore


def _validated_insight(store: InsightStore, *, topic: str = "same-topic") -> Insight:
    insight = Insight.create(
        category="subject-named-category",
        claim="This experienced way of acting may be worth retaining.",
        rationale="It came from a concrete interaction.",
        constraints="Only in the experienced context.",
        topic_key=topic,
    )
    insight.status = InsightStatus.VALIDATED.value
    insight.next_action = InsightNextAction.PROMOTE.value
    assert store.add_insight(insight) is True
    return insight


def _distiller(tmp_path):
    store = InsightStore(tmp_path)
    skill_store = SkillStore(tmp_path)

    async def current_subject_revision() -> str:
        return "a" * 64

    return (
        SkillDistiller(
            store=store,
            skill_store=skill_store,
            workspace_path=tmp_path,
            current_subject_revision=current_subject_revision,
        ),
        store,
        skill_store,
    )


@pytest.mark.asyncio
async def test_existing_skill_proposal_is_selected_only_by_explicit_id(
    tmp_path,
) -> None:
    distiller, store, skill_store = _distiller(tmp_path)
    insight = _validated_insight(store)
    first = SkillPattern.create(
        name="same-topic",
        description="A name that old substring matching would have selected.",
        instructions="Old instructions.",
    )
    second = SkillPattern.create(
        name="different-name",
        description="The subject intentionally selects this skill.",
        instructions="Other old instructions.",
    )
    assert skill_store.add_skill(first) is True
    assert skill_store.add_skill(second) is True

    async def propose(**_kwargs):
        return {
            "target_skill_id": second.skill_id,
            "name": second.name,
            "description": "A revised description.",
            "instructions": "Revised instructions with their boundary.",
        }

    async def accept(**_kwargs):
        return True

    distiller._distill = propose  # type: ignore[method-assign]
    distiller._introspective_gate = accept  # type: ignore[method-assign]

    assert await distiller.run_distillation() is True
    assert skill_store.get_skill(first.skill_id).description == first.description
    assert skill_store.get_skill(second.skill_id).description == second.description
    candidates = skill_store.list_candidates(status="open")
    assert len(candidates) == 1
    assert candidates[0].target_skill_id == second.skill_id
    assert candidates[0].description == "A revised description."
    assert candidates[0].insight_ids == [insight.insight_id]
    assert store.get_insight(insight.insight_id).next_action == (
        InsightNextAction.PROMOTE.value
    )


@pytest.mark.asyncio
async def test_unknown_explicit_target_is_not_reinterpreted_as_new_skill(tmp_path) -> None:
    distiller, store, skill_store = _distiller(tmp_path)
    insight = _validated_insight(store)

    async def propose(**_kwargs):
        return {
            "target_skill_id": "skl_missing",
            "name": "invented-fallback",
            "description": "A proposal.",
            "instructions": "Some instructions.",
        }

    distiller._distill = propose  # type: ignore[method-assign]

    assert await distiller.run_distillation() is False
    assert skill_store.list_skills() == []
    assert store.get_insight(insight.insight_id).next_action == InsightNextAction.PROMOTE.value


@pytest.mark.asyncio
async def test_introspective_gate_is_only_a_recorded_recommendation(tmp_path) -> None:
    distiller, store, skill_store = _distiller(tmp_path)
    insight = _validated_insight(store)
    gate_calls = 0

    async def propose(**_kwargs):
        return {
            "target_skill_id": "",
            "name": "subject-chosen-name",
            "description": "A possible new way of acting.",
            "instructions": "Use it only within the experienced boundary.",
        }

    async def reject(**_kwargs):
        nonlocal gate_calls
        gate_calls += 1
        return False

    distiller._distill = propose  # type: ignore[method-assign]
    distiller._introspective_gate = reject  # type: ignore[method-assign]

    assert await distiller.run_distillation() is True
    assert gate_calls == 1
    assert skill_store.list_skills() == []
    candidates = skill_store.list_candidates(status="open")
    assert len(candidates) == 1
    assert candidates[0].gate_recommended is False
    assert store.get_insight(insight.insight_id).next_action == (
        InsightNextAction.PROMOTE.value
    )


@pytest.mark.asyncio
async def test_new_skill_without_subject_chosen_name_has_no_fallback(tmp_path) -> None:
    distiller, store, skill_store = _distiller(tmp_path)
    _validated_insight(store)

    async def propose(**_kwargs):
        return {
            "target_skill_id": "",
            "name": "",
            "description": "A possible new way of acting.",
            "instructions": "Concrete instructions.",
        }

    distiller._distill = propose  # type: ignore[method-assign]

    assert await distiller.run_distillation() is False
    assert skill_store.list_skills() == []


def test_existing_skill_context_keeps_complete_history() -> None:
    skill = SkillPattern.create(
        name="history-bearing-skill",
        description="Description.",
        instructions="Instructions.",
    )
    skill.use_observations = [f"observation-{index}" for index in range(8)]
    skill.rejected_edits = [
        {"summary": f"rejected-{index}", "reason": "reviewed"}
        for index in range(8)
    ]

    payload = json.loads(SkillDistiller._format_existing_skills([skill]))

    assert payload[0]["use_observations"] == skill.use_observations
    assert payload[0]["rejected_edits"] == skill.rejected_edits


def test_malformed_gate_response_never_means_acceptance() -> None:
    with pytest.raises(ValueError, match="SkillGateOutputMustBeObject"):
        SkillDistiller._parse_gate_result("not-json")
    with pytest.raises(ValueError, match="SkillGateDecisionMissing"):
        SkillDistiller._parse_gate_result('{"reason": "missing decision"}')
    assert SkillDistiller._parse_gate_result('{"promote": true}') is True
