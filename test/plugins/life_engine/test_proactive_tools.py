"""Model-facing contracts for the single proactive query/command surface."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.proactive.runtime import open_local_proactive_runtime
from plugins.life_engine.proactive.tools import (
    PROACTIVE_TOOLS,
    LifeEngineProactiveCommandTool,
    LifeEngineProactiveQueryTool,
    _decision_occurrence,
    _service_actor,
    _source_instance,
)
from src.core.models.message import Message


class _Registry:
    def __init__(self, *, active: bool = True) -> None:
        self._active = active

    def get_for_stream(self, stream_id: str) -> object | None:
        if stream_id not in {"stream:proactive", "chat_global"}:
            return None
        return SimpleNamespace(instance_id="chat_global", is_active=self._active)

    def get(self, instance_id: str) -> object | None:
        if instance_id != "chat_global":
            return None
        return SimpleNamespace(instance_id=instance_id, is_active=self._active)


async def _active(instance_id: str) -> bool:
    return instance_id == "chat_global"


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        local_database_path="runtime/proactive/proactive.sqlite3",
        local_authority_state_path="runtime/proactive/authority.json",
        authority_lease_seconds=30,
        authority_renew_interval_seconds=5,
    )


def _bind(tool: object, *, tool_call_id: str) -> None:
    tool._bind_runtime_context(  # type: ignore[attr-defined]
        stream_id="stream:proactive",
        message=Message(
            message_id="message:proactive:1",
            time=1785960000.0,
            stream_id="stream:proactive",
        ),
        tool_call_id=tool_call_id,
    )
    tool._life_source_occurrence_id = "life:event:proactive:1"
    tool._life_source_occurred_at = "2026-08-23T12:00:00+00:00"
    tool._life_source_instance_id = "chat_global"


@pytest.mark.asyncio
async def test_unified_tool_commits_and_reads_one_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_active,
    )
    service = SimpleNamespace(
        consciousness_registry=_Registry(),
        proactive_authority=runtime.authority,
        page_attention_threads=runtime.authority.page_attention,
        decide_attention_thread=runtime.authority.decide_attention,
        list_initiative_seeds=runtime.authority.list_initiatives,
        get_initiative_seed=runtime.authority.get_initiative,
        decide_initiative_seed=runtime.authority.decide_initiative,
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.registry.get_life_engine_service",
        lambda: service,
    )
    command = LifeEngineProactiveCommandTool(SimpleNamespace())
    _bind(command, tool_call_id="call:proactive:1")
    query = LifeEngineProactiveQueryTool(SimpleNamespace())
    _bind(query, tool_call_id="call:proactive:query")

    try:
        first_ok, first = await command.execute(
            action="attention.open",
            expected_revision=0,
            statement="我明确选择保留这条持续关注。",
        )
        replay_ok, replay = await command.execute(
            action="attention.open",
            expected_revision=0,
            statement="我明确选择保留这条持续关注。",
        )
        changed_ok, changed = await command.execute(
            action="attention.open",
            expected_revision=0,
            statement="同一个调用不能偷换成另一条决定。",
        )
        cross_family_ok, cross_family = await command.execute(
            action="initiative.hold",
            expected_revision=0,
            statement="同一个调用也不能跨记录族再写一次。",
        )
        query_ok, projection = await query.execute(resource="attention")

        assert first_ok and replay_ok and query_ok
        assert isinstance(first, dict) and isinstance(replay, dict)
        assert first["authority_committed"] is True
        assert first["record_family"] == "attention"
        assert replay["record_id"] == first["record_id"]
        assert replay["idempotent_replay"] is True
        assert changed_ok is False
        assert isinstance(changed, dict)
        assert changed["error"] == "AttentionThreadConflict"
        assert cross_family_ok is False
        assert isinstance(cross_family, dict)
        assert cross_family["error"] == "InitiativeConflict"
        assert isinstance(projection, dict)
        assert projection["resource"] == "attention"
        assert str(first["record_id"]) in str(projection["content"])
        events = await runtime.authority.attention_event_page(
            str(first["record_id"])
        )
        assert len(events.items) == 1
        assert await runtime.authority.list_initiatives() == ()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_unified_command_requires_an_active_bound_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SimpleNamespace(consciousness_registry=_Registry(active=False))
    monkeypatch.setattr(
        "plugins.life_engine.service.registry.get_life_engine_service",
        lambda: service,
    )
    command = LifeEngineProactiveCommandTool(SimpleNamespace())
    _bind(command, tool_call_id="call:inactive")

    ok, result = await command.execute(
        action="attention.open",
        statement="后台不能冒充主体写下这句话。",
    )

    assert ok is False
    assert result == {
        "error": "PermissionError",
        "error_message": "ProactiveActorIsNotActive",
        "operation": "attention.open",
        "authority_committed": False,
    }


def test_model_facing_surface_contains_only_unified_proactive_tools() -> None:
    assert [tool.tool_name for tool in PROACTIVE_TOOLS] == [
        "nucleus_proactive_query",
        "nucleus_proactive_command",
    ]


def test_decision_identity_is_content_independent_and_source_bound() -> None:
    tool = SimpleNamespace(
        _tool_call_id="call:stable",
        _life_source_occurrence_id="life:event:stable",
        get_current_stream_id=lambda: "stream:proactive",
    )
    first = _decision_occurrence(tool)  # type: ignore[arg-type]
    tool.unrelated_mutable_content = "这不会改变 occurrence"
    assert _decision_occurrence(tool) == first  # type: ignore[arg-type]

    tool._life_source_occurrence_id = "life:event:other"
    assert _decision_occurrence(tool) != first  # type: ignore[arg-type]


def test_unknown_surface_cannot_borrow_chat_global_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SimpleNamespace(consciousness_registry=_Registry())
    monkeypatch.setattr(
        "plugins.life_engine.service.registry.get_life_engine_service",
        lambda: service,
    )
    tool = SimpleNamespace(
        get_current_stream_id=lambda: "unknown:surface",
        _runtime_task_name="life_chatter",
    )
    with pytest.raises(PermissionError, match="ProactiveActorIsNotActive"):
        _service_actor(tool)  # type: ignore[arg-type]


def test_source_instance_never_defaults_to_actor_outside_core_heartbeat() -> None:
    tool = SimpleNamespace(
        _life_source_instance_id="",
        trigger_message=SimpleNamespace(extra={}),
        _runtime_task_name="life_chatter",
        get_current_stream_id=lambda: "stream:proactive",
    )
    with pytest.raises(RuntimeError, match="ProactiveSourceInstanceRequired"):
        _source_instance(tool, "chat_global")  # type: ignore[arg-type]


def test_heartbeat_source_time_prefers_timestamp_over_missing_occurred_at() -> None:
    from plugins.life_engine.service.core import LifeEngineService

    event = SimpleNamespace(timestamp="2026-09-03T01:00:00+08:00")
    assert (
        LifeEngineService._heartbeat_source_occurred_at(event)
        == "2026-09-03T01:00:00+08:00"
    )
    assert LifeEngineService._heartbeat_source_occurred_at(
        SimpleNamespace(occurred_at="2026-09-03T02:00:00+00:00")
    ) == "2026-09-03T02:00:00+00:00"
    fallback = LifeEngineService._heartbeat_source_occurred_at(SimpleNamespace())
    assert fallback.endswith("+00:00") or fallback.endswith("+08:00") or "+" in fallback


@pytest.mark.asyncio
async def test_heartbeat_attention_open_commits_using_event_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.life_engine.service.core import LifeEngineService
    from src.kernel.llm import ToolRegistry

    runtime = await open_local_proactive_runtime(
        workspace_path=tmp_path,
        config=_config(),
        validate_active_actor=_active,
    )
    service = SimpleNamespace(
        consciousness_registry=_Registry(),
        proactive_authority=runtime.authority,
        page_attention_threads=runtime.authority.page_attention,
        decide_attention_thread=runtime.authority.decide_attention,
        list_initiative_seeds=runtime.authority.list_initiatives,
        get_initiative_seed=runtime.authority.get_initiative,
        decide_initiative_seed=runtime.authority.decide_initiative,
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.registry.get_life_engine_service",
        lambda: service,
    )
    heartbeat = LifeEngineService.__new__(LifeEngineService)
    heartbeat.plugin = SimpleNamespace()
    registry = ToolRegistry()
    registry.register(LifeEngineProactiveCommandTool, name="nucleus_proactive_command")
    source_occurred_at = LifeEngineService._heartbeat_source_occurred_at(
        SimpleNamespace(timestamp="2026-09-03T01:00:00+08:00")
    )
    try:
        result, success = await heartbeat._run_heartbeat_tool_call_execution(
            "nucleus_proactive_command",
            {
                "action": "attention.open",
                "expected_revision": 0,
                "statement": "心跳用 timestamp 留下这条关注。",
            },
            registry,
            tool_call_id="call:heartbeat-timestamp",
            source_occurrence_id="heartbeat:run:1",
            source_occurred_at=source_occurred_at,
        )
        assert success is True
        assert isinstance(result, dict)
        assert result["authority_committed"] is True
        assert result["record_family"] == "attention"
        view = await runtime.authority.get_attention(str(result["record_id"]))
        assert view is not None
        assert view.current_statement == "心跳用 timestamp 留下这条关注。"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_command_missing_source_time_returns_error_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SimpleNamespace(consciousness_registry=_Registry())
    monkeypatch.setattr(
        "plugins.life_engine.service.registry.get_life_engine_service",
        lambda: service,
    )
    command = LifeEngineProactiveCommandTool(SimpleNamespace())
    command._bind_runtime_context(
        stream_id="stream:proactive",
        tool_call_id="call:missing-time",
    )
    command._life_source_occurrence_id = "life:event:missing-time"
    command._life_source_instance_id = "chat_global"
    command._runtime_task_name = "core"

    ok, result = await command.execute(
        action="attention.open",
        statement="没有来源时间就不能提交。",
    )

    assert ok is False
    assert isinstance(result, dict)
    assert result["error"] == "RuntimeError"
    assert result["error_message"] == "ProactiveSourceTimeRequired"
    assert result["authority_committed"] is False
    assert result["operation"] == "attention.open"
