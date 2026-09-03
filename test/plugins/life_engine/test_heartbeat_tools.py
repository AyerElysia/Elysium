"""Heartbeat tool allowlist and the learning skill door."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.learning.learn_tool import (
    LEARNING_SKILL_RELATIVE,
    NucleusLearnTool,
    learn_call_counts_as_activity,
    normalize_learn_action,
)
from plugins.life_engine.learning.tools import (
    LEARNING_TOOLS,
    LifeListInsightsTool,
    LifeReviewSubjectDocumentTool,
)
from plugins.life_engine.opportunity.producers import collect_learning_invitation
from plugins.life_engine.prompts.sections import (
    DEFAULT_HEARTBEAT_SECTIONS,
    HeartbeatCapabilityCatalogSection,
    SectionContext,
)
from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.service.tool_manifests import HEARTBEAT_TOOL_NAMES
from src.kernel.llm import ToolRegistry


def _plugin(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(
            settings=SimpleNamespace(workspace_path=str(tmp_path))
        )
    )


def test_heartbeat_pool_matches_allowlist_exactly() -> None:
    service = LifeEngineService.__new__(LifeEngineService)
    tools = service._get_nucleus_tools()
    names = tuple(tool.tool_name for tool in tools)
    assert names == HEARTBEAT_TOOL_NAMES
    assert "nucleus_learn" in names
    assert "nucleus_write_narrative" in names
    assert "nucleus_memory_continuity_review" in names
    banned = {
        "nucleus_reflect_now",
        "nucleus_review_subject_document",
        "nucleus_list_subject_candidates",
        "nucleus_bash",
        "nucleus_run_agent",
        "nucleus_view_screen",
        "nucleus_web_search",
        "nucleus_browser_fetch",
        "platform_action",
        "nucleus_trace",
        "nucleus_relations",
        "nucleus_memory_stats",
        "conversation_evidence",
        "fetch_chat_history",
        "fetch_life_memory",
    }
    assert banned.isdisjoint(names)
    schema_names = [tool.to_schema()["function"]["name"] for tool in tools]
    assert "tool-nucleus_learn" in schema_names
    assert "tool-nucleus_reflect_now" not in schema_names


def test_learning_tool_classes_still_exist_outside_heartbeat() -> None:
    names = {cls.tool_name for cls in LEARNING_TOOLS}
    assert "nucleus_reflect_now" in names
    assert "nucleus_review_subject_document" in names
    assert "nucleus_learn" not in names


def test_nucleus_learn_schema_is_a_thin_door() -> None:
    schema = NucleusLearnTool.to_schema()["function"]
    assert schema["name"] == "tool-nucleus_learn"
    props = schema["parameters"]["properties"]
    assert set(props) == {"action", "arguments"}
    assert props["arguments"]["type"] == "object"
    assert props["arguments"].get("additionalProperties") is not False
    assert "reflection_text" not in props


def test_normalize_learn_action_accepts_legacy_names() -> None:
    assert normalize_learn_action("help") == "help"
    assert normalize_learn_action("read_skill") == "help"
    assert normalize_learn_action("nucleus_reflect_now") == "reflect_now"
    assert normalize_learn_action("review_subject_document") == (
        "review_subject_document"
    )


def test_learn_idle_treats_help_and_reads_as_observation() -> None:
    assert learn_call_counts_as_activity("help", {}) is False
    assert learn_call_counts_as_activity("list_insights", {}) is False
    assert learn_call_counts_as_activity(
        "review_subject_document",
        {"arguments": {"action": "status"}},
    ) is False
    assert learn_call_counts_as_activity(
        "knowledge_candidates",
        {"arguments": {"action": "list"}},
    ) is False
    assert learn_call_counts_as_activity(
        "reflect_now",
        {"arguments": {"reflection_text": "一段经历"}},
    ) is True
    assert learn_call_counts_as_activity(
        "review_subject_document",
        {"arguments": {"action": "propose"}},
    ) is True


def test_heartbeat_idle_strips_tool_prefix_for_learn_and_todo() -> None:
    assert (
        LifeEngineService._heartbeat_tool_call_counts_as_activity(
            "tool-nucleus_learn",
            {"action": "help"},
        )
        is False
    )
    assert (
        LifeEngineService._heartbeat_tool_call_counts_as_activity(
            "nucleus_todo",
            {"action": "list"},
        )
        is False
    )
    assert (
        LifeEngineService._heartbeat_tool_call_counts_as_activity(
            "tool-nucleus_todo",
            {"action": "write"},
        )
        is True
    )


async def test_unknown_heartbeat_tool_fails_closed() -> None:
    service = LifeEngineService.__new__(LifeEngineService)
    service.plugin = SimpleNamespace()
    registry = ToolRegistry()
    for cls in service._get_nucleus_tools():
        registry.register(cls)
    result, ok = await service._run_heartbeat_tool_call_execution(
        "tool-nucleus_bash",
        {},
        registry,
    )
    assert ok is False
    assert "未知工具" in str(result)


def test_heartbeat_tool_prefix_resolves_resident_schema() -> None:
    service = LifeEngineService.__new__(LifeEngineService)
    service.plugin = SimpleNamespace()
    registry = ToolRegistry()
    for cls in service._get_nucleus_tools():
        registry.register(cls)

    todo_cls = service._resolve_heartbeat_tool_class(registry, "nucleus_todo")
    prefixed = service._resolve_heartbeat_tool_class(
        registry,
        "tool-nucleus_proactive_command",
    )
    bare = service._resolve_heartbeat_tool_class(
        registry,
        "nucleus_proactive_command",
    )
    assert todo_cls is not None
    assert prefixed is not None
    assert bare is prefixed


async def test_help_reads_packaged_skill_and_seeds_workspace(tmp_path) -> None:
    tool = NucleusLearnTool(plugin=_plugin(tmp_path))
    dest = tmp_path / LEARNING_SKILL_RELATIVE
    assert not dest.exists()
    ok, payload = await tool.execute(action="help")
    assert ok is True
    assert isinstance(payload, dict)
    assert "nucleus_learn" in str(payload.get("content") or "")
    assert dest.is_file()
    assert payload.get("skill") == "learning"
    assert "第一人称自我叙事" in str(payload.get("content") or "")


async def test_help_does_not_overwrite_existing_workspace_skill(tmp_path) -> None:
    dest = tmp_path / LEARNING_SKILL_RELATIVE
    dest.parent.mkdir(parents=True)
    dest.write_text("EXISTING_CUSTOM_SKILL\n", encoding="utf-8")
    tool = NucleusLearnTool(plugin=_plugin(tmp_path))
    ok, payload = await tool.execute(action="help")
    assert ok is True
    assert isinstance(payload, dict)
    assert "EXISTING_CUSTOM_SKILL" in str(payload.get("content") or "")
    assert dest.read_text(encoding="utf-8") == "EXISTING_CUSTOM_SKILL\n"


async def test_learn_delegates_list_and_legacy_review_name(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, dict[str, Any]]] = []

    async def list_execute(self: Any, **kwargs: Any) -> tuple[bool, dict[str, Any]]:
        captured.append(("list", dict(kwargs)))
        return True, {"action": "list_insights", "count": 0}

    async def review_execute(self: Any, **kwargs: Any) -> tuple[bool, dict[str, Any]]:
        captured.append(("review", dict(kwargs)))
        return True, {"action": "subject_review_status"}

    monkeypatch.setattr(LifeListInsightsTool, "execute", list_execute)
    monkeypatch.setattr(LifeReviewSubjectDocumentTool, "execute", review_execute)
    tool = NucleusLearnTool(plugin=_plugin(tmp_path))

    ok, listed = await tool.execute(action="list_insights", arguments={"limit": 4})
    assert ok is True
    assert listed["count"] == 0

    ok, reviewed = await tool.execute(
        action="nucleus_review_subject_document",
        arguments={"action": "status", "target_path": "SOUL.md"},
    )
    assert ok is True
    assert reviewed["action"] == "subject_review_status"
    assert captured[0][0] == "list"
    assert captured[0][1]["limit"] == 4
    assert captured[1][1]["action"] == "status"
    assert captured[1][1]["target_path"] == "SOUL.md"


async def test_unknown_learn_operation_is_explicit() -> None:
    tool = NucleusLearnTool(plugin=_plugin("/tmp/unused"))
    ok, result = await tool.execute(action="invented_operation")
    assert ok is False
    assert "未知学习操作" in str(result)


@pytest.mark.asyncio
async def test_learning_invitation_points_at_skill_door_not_sixteen_tools() -> None:
    async def collect() -> dict[str, Any]:
        return {"due_count": 2, "documents": [{"target_path": "SOUL.md"}]}

    service = SimpleNamespace(
        _learning_scheduler=SimpleNamespace(
            projector_owner=True,
            collect_subject_review_offer_facts=collect,
        )
    )
    collected = await collect_learning_invitation(service)
    assert collected is not None
    assert collected.offer.disclosure_ref == ("nucleus_learn", "skills/learning")
    assert "nucleus_review_subject_document" not in collected.offer.disclosure_ref
    assert "SKILL.md" not in str(collected.offer.facts)


@pytest.mark.asyncio
async def test_capability_catalog_is_static_and_does_not_load_skill(
    tmp_path,
) -> None:
    section_ids = [section.section_id for section in DEFAULT_HEARTBEAT_SECTIONS]
    assert "capability_catalog" in section_ids
    service = SimpleNamespace(
        get_skill=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("get_skill must not auto-run")
        )
    )
    ctx = SectionContext(
        service=service,
        config=SimpleNamespace(),
        today_str="2026-09-03",
    )
    text = await HeartbeatCapabilityCatalogSection().render(ctx)
    assert text is not None
    assert "nucleus_learn action=help" in text
    assert "不会自动打开 skill 正文" in text
    assert "ROLE.TOOL" in text
    assert "get_skill" not in text
