"""Governance binds the caller, rather than accepting model-authored authority."""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.life_engine.opportunity.tools import (
    OPPORTUNITY_TOOLS,
    LifeEngineCapabilityCallTool,
    LifeEngineOpportunityCommandTool,
    LifeEngineOpportunityQueryTool,
    _decision_occurrence,
    _source_instance,
)
from src.core.models.message import Message


def _tool(cls, monkeypatch, *, active=True):
    instance = SimpleNamespace(instance_id="chat_global", is_active=active)
    service = SimpleNamespace(
        consciousness_registry=SimpleNamespace(
            get_for_stream=lambda stream: instance if stream == "stream:test" else None,
            get=lambda identity: instance if identity == "chat_global" else None,
        ),
        query_opportunity_runtime=AsyncMock(return_value={"items": []}),
        manage_opportunity_runtime=AsyncMock(
            return_value={"authority_committed": True}
        ),
        call_opportunity_capability=AsyncMock(return_value={"success": True}),
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.registry.get_life_engine_service",
        lambda: service,
    )
    tool = cls(SimpleNamespace())
    tool._bind_runtime_context(
        stream_id="stream:test",
        message=Message(
            message_id="msg:test", time=1788600000.0, stream_id="stream:test"
        ),
        tool_call_id="call:test",
    )
    tool._life_source_instance_id = "chat_global"
    tool._life_source_occurrence_id = "life:event:test"
    tool._life_source_occurred_at = "2026-09-05T00:00:00+00:00"
    return tool, service


@pytest.mark.asyncio
async def test_uninstall_is_a_subject_command_with_stable_identity(monkeypatch):
    tool, service = _tool(LifeEngineOpportunityCommandTool, monkeypatch)
    ok, result = await tool.execute(
        action="capability.uninstall",
        target_id="life.learning",
        expected_revision=4,
        reason="暂时不需要这套学习流程",
        arguments={"impact_sha256": "a" * 64},
    )
    assert ok and result["authority_committed"]
    command = service.manage_opportunity_runtime.call_args.kwargs
    assert command["actor_consciousness_instance_id"] == "chat_global"
    assert command["source_instance_id"] == "chat_global"
    assert command["source_occurrence_id"] == "life:event:test"
    assert command["expected_revision"] == 4
    identity = command["decision_occurrence_id"]
    await tool.execute(action="capability.pause", target_id="life.learning")
    assert (
        service.manage_opportunity_runtime.call_args.kwargs["decision_occurrence_id"]
        == identity
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False])
async def test_inactive_actor_cannot_manage(monkeypatch, active):
    tool, service = _tool(LifeEngineOpportunityCommandTool, monkeypatch, active=active)
    ok, result = await tool.execute(
        action="capability.uninstall", target_id="life.learning"
    )
    assert not ok and result["error"] == "PermissionError"
    service.manage_opportunity_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_stream_cannot_borrow_heartbeat_actor(monkeypatch):
    tool, service = _tool(LifeEngineOpportunityCommandTool, monkeypatch)
    tool._bind_runtime_context(stream_id="stream:unknown", tool_call_id="call:unknown")
    tool._runtime_task_name = "core"
    ok, _ = await tool.execute(action="capability.install", target_id="life.learning")
    assert not ok
    service.manage_opportunity_runtime.assert_not_awaited()


def test_opportunity_source_instance_reads_nested_life_turn_scope() -> None:
    tool = SimpleNamespace(
        _life_source_instance_id="",
        trigger_message=SimpleNamespace(
            extra={"life_turn_scope": {"consciousness_instance_id": "chat_global"}}
        ),
        _runtime_task_name="life_chatter",
        get_current_stream_id=lambda: "kook-stream",
    )
    assert _source_instance(tool, "chat_global") == "chat_global"  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"actor_consciousness_instance_id": "someone-else"},
        {"writer_claim": "forged"},
        {"source_occurrence_id": "forged"},
        {"caller_tool": "forged"},
    ],
)
async def test_arguments_cannot_replace_bound_authority(monkeypatch, arguments):
    tool, service = _tool(LifeEngineOpportunityCommandTool, monkeypatch)
    ok, _ = await tool.execute(action="capability.install", arguments=arguments)
    assert not ok
    service.manage_opportunity_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_tool_call_identity_fails_closed(monkeypatch):
    tool, service = _tool(LifeEngineOpportunityCommandTool, monkeypatch)
    tool._tool_call_id = ""
    ok, result = await tool.execute(action="capability.pause")
    assert not ok and result["error"] == "RuntimeError"
    service.manage_opportunity_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_query_is_bounded_and_never_submits_a_decision(monkeypatch):
    tool, service = _tool(LifeEngineOpportunityQueryTool, monkeypatch)
    ok, _ = await tool.execute(resource="catalog", max_bytes=10**9, limit=10**9)
    assert ok
    args = service.query_opportunity_runtime.call_args.kwargs
    assert args["max_bytes"] == 32768 and args["limit"] == 100
    service.manage_opportunity_runtime.assert_not_awaited()
    service.call_opportunity_capability.assert_not_awaited()


@pytest.mark.asyncio
async def test_operation_schema_query_preserves_exact_operation_and_action(monkeypatch):
    tool, service = _tool(LifeEngineOpportunityQueryTool, monkeypatch)
    ok, _ = await tool.execute(
        resource="operation_schema",
        record_id="life.learning",
        operation="nucleus_learn",
        action="reflect_now",
        offset_bytes=1024,
    )
    assert ok
    args = service.query_opportunity_runtime.await_args.kwargs
    assert args["resource"] == "operation_schema"
    assert args["record_id"] == "life.learning"
    assert args["operation"] == "nucleus_learn"
    assert args["action"] == "reflect_now"
    assert args["offset_bytes"] == 1024
    service.manage_opportunity_runtime.assert_not_awaited()
    service.call_opportunity_capability.assert_not_awaited()


@pytest.mark.asyncio
async def test_capability_call_preserves_the_actual_invoking_tool(monkeypatch):
    tool, service = _tool(LifeEngineCapabilityCallTool, monkeypatch)
    ok, _ = await tool.execute(
        capability_id="life.learning",
        operation="nucleus_learn",
        arguments={"action": "list_insights"},
    )
    assert ok
    args = service.call_opportunity_capability.call_args.kwargs
    assert args["caller_tool"] is tool
    assert args["arguments"] == {"action": "list_insights"}
    assert args["actor_consciousness_instance_id"] == "chat_global"


@pytest.mark.asyncio
async def test_capability_failure_is_not_promoted_to_success(monkeypatch):
    tool, service = _tool(LifeEngineCapabilityCallTool, monkeypatch)
    service.call_opportunity_capability.return_value = {
        "success": False,
        "value": {"error": "domain_failed"},
    }
    ok, value = await tool.execute(
        capability_id="life.learning", operation="nucleus_learn"
    )
    assert ok is False and value == {"error": "domain_failed"}


@pytest.mark.asyncio
async def test_cancellation_propagates_and_errors_do_not_echo_private_text(monkeypatch):
    tool, service = _tool(LifeEngineCapabilityCallTool, monkeypatch)
    service.call_opportunity_capability.side_effect = RuntimeError(
        "private raw content"
    )
    ok, result = await tool.execute(
        capability_id="life.learning", operation="nucleus_learn"
    )
    assert not ok and "private" not in str(result)
    service.call_opportunity_capability.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await tool.execute(capability_id="life.learning", operation="nucleus_learn")


def test_surface_has_no_actor_or_runtime_parameter():
    for cls in OPPORTUNITY_TOOLS:
        parameters = inspect.signature(cls.execute).parameters
        assert "actor_consciousness_instance_id" not in parameters
        assert "runtime" not in parameters
        assert "writer_claim" not in parameters


def test_decision_identity_is_source_bound():
    tool = SimpleNamespace(
        _tool_call_id="call:test",
        _life_source_occurrence_id="event:1",
        get_current_stream_id=lambda: "stream:test",
    )
    first = _decision_occurrence(tool)
    tool._life_source_occurrence_id = "event:2"
    assert first != _decision_occurrence(tool)
