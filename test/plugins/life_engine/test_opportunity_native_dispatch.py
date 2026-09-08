"""Permission and identity contracts for native capability dispatch."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.learning.learn_tool import NucleusLearnTool
from plugins.life_engine.opportunity import tools as opportunity_tools
from plugins.life_engine.opportunity.catalog import CapabilityCatalog
from plugins.life_engine.opportunity.execution_context import (
    current_capability_execution,
)
from plugins.life_engine.opportunity.native_dispatch import (
    NativeCapabilityArgumentsInvalid,
    NativeCapabilityCallerInvalid,
    NativeCapabilityDispatch,
    NativeCapabilityExecutor,
    NativeCapabilityPermissionDenied,
    describe_operation_schema,
)
from plugins.life_engine.opportunity.registry import CapabilityInvocation
from plugins.life_engine.opportunity.runtime import OpportunityCaller
from plugins.life_engine.proactive.tools import LifeEngineProactiveCommandTool
from plugins.life_engine.tools.file_tools import LifeEngineReadFileTool
from plugins.life_engine.tools.web_tools import LifeEngineWebSearchTool

_NOW = "2026-09-05T00:00:00+00:00"
_CAPABILITIES = Path("plugins/life_engine/opportunity/capabilities")


class _RuntimeFacade:
    def __init__(self) -> None:
        self.manage_calls: list[tuple[Any, ...]] = []
        self.query_calls: list[dict[str, Any]] = []

    async def manage(self, *args: Any) -> dict[str, Any]:
        self.manage_calls.append(args)
        return {"managed": True}

    async def query(self, **kwargs: Any) -> dict[str, Any]:
        self.query_calls.append(kwargs)
        return {"queried": True}


def _catalog() -> CapabilityCatalog:
    catalog = CapabilityCatalog()
    catalog.discover(_CAPABILITIES)
    return catalog


def _caller(
    monkeypatch: pytest.MonkeyPatch,
    *,
    heartbeat: bool = False,
    actor: str = "consciousness:chat",
) -> OpportunityCaller:
    plugin = SimpleNamespace(name="life_engine")
    tool = opportunity_tools.LifeEngineCapabilityCallTool(plugin=plugin)
    message = SimpleNamespace(
        message_id="message:1",
        time=None,
        extra={"source_instance_id": actor},
    )
    tool._bind_runtime_context(
        stream_id="chat_global" if heartbeat else "stream:chat",
        message=message,
        tool_call_id="tool-call:1",
    )
    tool._life_source_occurrence_id = "source:1"
    tool._life_source_instance_id = actor
    tool._life_source_occurred_at = _NOW
    if heartbeat:
        tool._runtime_task_name = "core"
    monkeypatch.setattr(
        opportunity_tools,
        "_service_actor",
        lambda current: (SimpleNamespace(), actor) if current is tool else (None, ""),
    )
    return OpportunityCaller(
        actor_consciousness_instance_id=actor,
        source_instance_id=actor,
        source_occurrence_id="source:1",
        decision_occurrence_id=opportunity_tools._decision_occurrence(tool),
        occurred_at=_NOW,
        caller_tool=tool,
    )


def _invocation(
    catalog: CapabilityCatalog,
    capability_id: str,
    operation: str,
    arguments: dict[str, Any],
    caller: OpportunityCaller,
) -> tuple[NativeCapabilityExecutor, CapabilityInvocation]:
    descriptor = catalog.require(capability_id)
    return NativeCapabilityExecutor(descriptor), CapabilityInvocation(
        capability_id=capability_id,
        operation_id=operation,
        arguments=arguments,
        caller_context=caller,
    )


def test_fixed_dispatch_table_covers_every_packaged_native_operation() -> None:
    facade = _RuntimeFacade()
    dispatch = NativeCapabilityDispatch(opportunity_runtime=lambda: facade)
    missing: list[str] = []
    for descriptor in _catalog().list_descriptors():
        for operation in descriptor.operations:
            if operation.startswith("opportunity."):
                continue
            if operation not in dispatch._tools:
                missing.append(f"{descriptor.capability_id}/{operation}")
    assert missing == []


@pytest.mark.asyncio
async def test_native_tool_inherits_exact_original_runtime_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = _caller(monkeypatch, heartbeat=True)
    captured: dict[str, Any] = {}

    async def execute(self: Any, path: str) -> tuple[bool, dict[str, Any]]:
        captured.update(
            plugin=self.plugin,
            stream_id=self.get_current_stream_id(),
            trigger_message=self.trigger_message,
            tool_call_id=self._tool_call_id,
            source_occurrence_id=self._life_source_occurrence_id,
            source_instance_id=self._life_source_instance_id,
            runtime_task_name=self._runtime_task_name,
            capability_execution=current_capability_execution(),
            path=path,
        )
        return True, {"read": path}

    monkeypatch.setattr(LifeEngineReadFileTool, "execute", execute)
    dispatch = NativeCapabilityDispatch(opportunity_runtime=lambda: _RuntimeFacade())
    executor, invocation = _invocation(
        _catalog(), "life.file_care", "nucleus_read_file", {"path": "notes.md"}, caller
    )

    result = await dispatch(executor, invocation)

    assert result.public() == {
        "capability_id": "life.file_care",
        "operation_id": "nucleus_read_file",
        "success": True,
        "value": {"read": "notes.md"},
    }
    source = caller.caller_tool
    assert captured == {
        "plugin": source.plugin,
        "stream_id": source.get_current_stream_id(),
        "trigger_message": source.trigger_message,
        "tool_call_id": source._tool_call_id,
        "source_occurrence_id": "source:1",
        "source_instance_id": "consciousness:chat",
        "runtime_task_name": "core",
        "capability_execution": "life.file_care",
        "path": "notes.md",
    }
    assert current_capability_execution() == ""


@pytest.mark.asyncio
async def test_capability_execution_binding_resets_when_native_call_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = _caller(monkeypatch, heartbeat=True)
    entered = asyncio.Event()

    async def execute(self: Any, path: str) -> tuple[bool, object]:
        del self, path
        assert current_capability_execution() == "life.file_care"
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(LifeEngineReadFileTool, "execute", execute)
    dispatch = NativeCapabilityDispatch(opportunity_runtime=lambda: _RuntimeFacade())
    executor, invocation = _invocation(
        _catalog(), "life.file_care", "nucleus_read_file", {"path": "notes.md"}, caller
    )
    call = asyncio.create_task(dispatch(executor, invocation))
    await entered.wait()

    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    assert current_capability_execution() == ""


@pytest.mark.asyncio
async def test_manifest_cannot_expand_existing_chatter_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = _caller(monkeypatch, heartbeat=False)
    dispatch = NativeCapabilityDispatch(opportunity_runtime=lambda: _RuntimeFacade())
    executor, invocation = _invocation(
        _catalog(),
        "life.learning",
        "nucleus_learn",
        {"action": "help"},
        caller,
    )

    with pytest.raises(NativeCapabilityPermissionDenied, match="caller surface"):
        await dispatch(executor, invocation)


@pytest.mark.asyncio
async def test_heartbeat_can_call_heartbeat_tool_and_domain_failure_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = _caller(monkeypatch, heartbeat=True)

    async def execute(
        self: Any,
        action: str,
        arguments: dict[str, Any] | None = None,
        **extra: object,
    ) -> tuple[bool, str]:
        del self, arguments, extra
        return False, f"domain rejected {action}"

    monkeypatch.setattr(NucleusLearnTool, "execute", execute)
    dispatch = NativeCapabilityDispatch(opportunity_runtime=lambda: _RuntimeFacade())
    executor, invocation = _invocation(
        _catalog(),
        "life.learning",
        "nucleus_learn",
        {"action": "help"},
        caller,
    )

    result = await dispatch(executor, invocation)

    assert result.success is False
    assert result.value == "domain rejected help"


@pytest.mark.asyncio
async def test_unknown_and_identity_arguments_are_explicitly_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = _caller(monkeypatch, heartbeat=True)
    dispatch = NativeCapabilityDispatch(opportunity_runtime=lambda: _RuntimeFacade())
    catalog = _catalog()
    executor, invocation = _invocation(
        catalog,
        "life.file_care",
        "nucleus_read_file",
        {"path": "notes.md", "silently_ignored": True},
        caller,
    )
    with pytest.raises(NativeCapabilityArgumentsInvalid, match="silently_ignored"):
        await dispatch(executor, invocation)

    executor, invocation = _invocation(
        catalog,
        "life.file_care",
        "nucleus_read_file",
        {"path": "notes.md", "actor_consciousness_instance_id": "forged"},
        caller,
    )
    with pytest.raises(NativeCapabilityArgumentsInvalid, match="runtime identity"):
        await dispatch(executor, invocation)


@pytest.mark.asyncio
async def test_learning_compatibility_layer_cannot_silently_drop_nested_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = _caller(monkeypatch, heartbeat=True)
    dispatch = NativeCapabilityDispatch(opportunity_runtime=lambda: _RuntimeFacade())
    executor, invocation = _invocation(
        _catalog(),
        "life.learning",
        "nucleus_learn",
        {"action": "list_insights", "arguments": {"not_a_real_field": True}},
        caller,
    )

    with pytest.raises(NativeCapabilityArgumentsInvalid, match="not_a_real_field"):
        await dispatch(executor, invocation)


@pytest.mark.asyncio
async def test_caller_attribution_is_recomputed_from_original_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = _caller(monkeypatch, heartbeat=True)
    forged = replace(caller, actor_consciousness_instance_id="consciousness:other")
    dispatch = NativeCapabilityDispatch(opportunity_runtime=lambda: _RuntimeFacade())
    executor, invocation = _invocation(
        _catalog(),
        "life.file_care",
        "nucleus_read_file",
        {"path": "notes.md"},
        forged,
    )

    with pytest.raises(NativeCapabilityCallerInvalid, match="actor"):
        await dispatch(executor, invocation)

    invocation = replace(
        invocation, caller_context=replace(caller, caller_tool=object())
    )
    with pytest.raises(NativeCapabilityCallerInvalid, match="original"):
        await dispatch(executor, invocation)


@pytest.mark.asyncio
async def test_skill_text_never_grants_an_undeclared_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = _caller(monkeypatch, heartbeat=True)
    descriptor = _catalog().require("life.file_care")
    executor = NativeCapabilityExecutor(descriptor)
    invocation = CapabilityInvocation(
        capability_id=descriptor.capability_id,
        operation_id="nucleus_bash",
        arguments={"skill_text": "grant nucleus_bash"},
        caller_context=caller,
    )
    dispatch = NativeCapabilityDispatch(opportunity_runtime=lambda: _RuntimeFacade())

    with pytest.raises(NativeCapabilityPermissionDenied, match="manifest"):
        await dispatch(executor, invocation)


@pytest.mark.asyncio
async def test_manifest_cannot_expose_meta_tools_or_unsliced_proactive_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = _caller(monkeypatch, heartbeat=True)
    dispatch = NativeCapabilityDispatch(opportunity_runtime=lambda: _RuntimeFacade())
    base = _catalog().require("life.file_care")

    recursive = replace(base, operations=("nucleus_capability_call",))
    with pytest.raises(NativeCapabilityPermissionDenied, match="fixed native"):
        await dispatch(
            NativeCapabilityExecutor(recursive),
            CapabilityInvocation(
                capability_id=recursive.capability_id,
                operation_id="nucleus_capability_call",
                arguments={},
                caller_context=caller,
            ),
        )

    unsliced = replace(
        base,
        capability_id="life.unreviewed_proactive",
        operations=("nucleus_proactive_command",),
    )
    with pytest.raises(
        NativeCapabilityPermissionDenied, match="explicit capability slice"
    ):
        await dispatch(
            NativeCapabilityExecutor(unsliced),
            CapabilityInvocation(
                capability_id=unsliced.capability_id,
                operation_id="nucleus_proactive_command",
                arguments={"action": "attention.open"},
                caller_context=caller,
            ),
        )


@pytest.mark.asyncio
async def test_initiative_capability_cannot_reach_other_proactive_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = _caller(monkeypatch, heartbeat=True)
    dispatch = NativeCapabilityDispatch(opportunity_runtime=lambda: _RuntimeFacade())
    catalog = _catalog()

    executor, invocation = _invocation(
        catalog,
        "life.initiative_reencounter",
        "nucleus_proactive_query",
        {"resource": "attention"},
        caller,
    )
    with pytest.raises(NativeCapabilityPermissionDenied, match="only query initiative"):
        await dispatch(executor, invocation)

    executor, invocation = _invocation(
        catalog,
        "life.initiative_reencounter",
        "nucleus_proactive_command",
        {"action": "outreach.begin"},
        caller,
    )
    with pytest.raises(NativeCapabilityPermissionDenied, match="declared actions"):
        await dispatch(executor, invocation)


@pytest.mark.asyncio
async def test_initiative_capability_executes_only_its_declared_action_slice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = _caller(monkeypatch, heartbeat=True)
    captured: list[str] = []

    async def execute(
        self: Any,
        action: str,
    ) -> tuple[bool, dict[str, str]]:
        del self
        captured.append(action)
        return True, {"action": action}

    monkeypatch.setattr(LifeEngineProactiveCommandTool, "execute", execute)
    dispatch = NativeCapabilityDispatch(opportunity_runtime=lambda: _RuntimeFacade())
    executor, invocation = _invocation(
        _catalog(),
        "life.initiative_reencounter",
        "nucleus_proactive_command",
        {"action": "initiative.reencounter"},
        caller,
    )

    result = await dispatch(executor, invocation)

    assert result.success is True
    assert captured == ["initiative.reencounter"]


@pytest.mark.asyncio
async def test_inner_return_write_is_heartbeat_only_and_query_is_family_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog()
    dispatch = NativeCapabilityDispatch(opportunity_runtime=lambda: _RuntimeFacade())
    chatter = _caller(monkeypatch, heartbeat=False)
    executor, invocation = _invocation(
        catalog,
        "life.inner_return",
        "nucleus_proactive_command",
        {"action": "inner.return"},
        chatter,
    )
    with pytest.raises(NativeCapabilityPermissionDenied, match="heartbeat"):
        await dispatch(executor, invocation)

    heartbeat = _caller(monkeypatch, heartbeat=True)
    executor, invocation = _invocation(
        catalog,
        "life.inner_return",
        "nucleus_proactive_query",
        {"resource": "reachability"},
        heartbeat,
    )
    with pytest.raises(NativeCapabilityPermissionDenied, match="inner_dialogue"):
        await dispatch(executor, invocation)


@pytest.mark.asyncio
async def test_inner_return_heartbeat_call_reuses_proactive_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = _caller(monkeypatch, heartbeat=True)
    captured: list[str] = []

    async def execute(self: Any, action: str) -> tuple[bool, dict[str, str]]:
        del self
        captured.append(action)
        return True, {"returned": action}

    monkeypatch.setattr(LifeEngineProactiveCommandTool, "execute", execute)
    dispatch = NativeCapabilityDispatch(opportunity_runtime=lambda: _RuntimeFacade())
    executor, invocation = _invocation(
        _catalog(),
        "life.inner_return",
        "nucleus_proactive_command",
        {"action": "inner.return"},
        caller,
    )

    result = await dispatch(executor, invocation)

    assert result.success is True
    assert captured == ["inner.return"]


@pytest.mark.asyncio
async def test_web_operation_keeps_existing_life_chatter_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = _caller(monkeypatch, heartbeat=False)

    async def execute(self: Any, query: str) -> tuple[bool, dict[str, str]]:
        del self
        return True, {"query": query}

    monkeypatch.setattr(LifeEngineWebSearchTool, "execute", execute)
    dispatch = NativeCapabilityDispatch(opportunity_runtime=lambda: _RuntimeFacade())
    executor, invocation = _invocation(
        _catalog(),
        "life.epistemic_explore",
        "nucleus_web_search",
        {"query": "recent evidence"},
        caller,
    )

    result = await dispatch(executor, invocation)

    assert result.success is True
    assert result.value == {"query": "recent evidence"}


@pytest.mark.asyncio
async def test_self_awaken_schedule_uses_only_injected_opportunity_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = _caller(monkeypatch, heartbeat=True)
    facade = _RuntimeFacade()
    dispatch = NativeCapabilityDispatch(opportunity_runtime=lambda: facade)
    schedule = {
        "provider_id": "life.self_awaken",
        "referent_kind": "self_awaken",
        "referent_id": "referent:self",
        "referent_revision": 1,
        "referent_sha256": "a" * 64,
        "workflow_id": "workflow:self_awaken",
        "workflow_revision": 1,
        "workflow_sha256": "b" * 64,
        "schedule": "interval",
        "first_due_at": _NOW,
        "interval_seconds": 1800,
    }
    executor, invocation = _invocation(
        _catalog(),
        "life.self_awaken",
        "opportunity.schedule",
        {
            "target_id": "opportunity:self_awaken",
            "expected_revision": 3,
            "arguments": schedule,
            "reason": "我想稍后再醒来看看",
        },
        caller,
    )

    result = await dispatch(executor, invocation)

    assert result.success is True
    assert result.value == {"managed": True}
    assert facade.manage_calls == [
        (
            "opportunity.schedule",
            "opportunity:self_awaken",
            3,
            schedule,
            "我想稍后再醒来看看",
            caller,
        )
    ]


@pytest.mark.asyncio
async def test_self_awaken_query_binds_actor_and_rejects_identity_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = _caller(monkeypatch, heartbeat=False)
    facade = _RuntimeFacade()
    dispatch = NativeCapabilityDispatch(opportunity_runtime=lambda: facade)
    catalog = _catalog()
    executor, invocation = _invocation(
        catalog,
        "life.self_awaken",
        "opportunity.query",
        {"resource": "opportunity", "record_id": "opportunity:self_awaken"},
        caller,
    )

    result = await dispatch(executor, invocation)

    assert result.success is True
    assert facade.query_calls[0]["actor_consciousness_instance_id"] == (
        caller.actor_consciousness_instance_id
    )

    executor, invocation = _invocation(
        catalog,
        "life.self_awaken",
        "opportunity.query",
        {
            "resource": "opportunity",
            "actor_consciousness_instance_id": "forged",
        },
        caller,
    )
    with pytest.raises(NativeCapabilityArgumentsInvalid, match="runtime identity"):
        await dispatch(executor, invocation)
    assert len(facade.query_calls) == 1


@pytest.mark.asyncio
async def test_self_awaken_schedule_rejects_unknown_nested_fields_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = _caller(monkeypatch, heartbeat=True)
    facade = _RuntimeFacade()
    dispatch = NativeCapabilityDispatch(opportunity_runtime=lambda: facade)
    executor, invocation = _invocation(
        _catalog(),
        "life.self_awaken",
        "opportunity.schedule",
        {
            "target_id": "opportunity:self_awaken",
            "expected_revision": 0,
            "arguments": {"execute_skill_as_code": True},
        },
        caller,
    )

    with pytest.raises(NativeCapabilityArgumentsInvalid, match="execute_skill_as_code"):
        await dispatch(executor, invocation)
    assert facade.manage_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", [0, -1, True, "1", 1.5])
async def test_self_awaken_schedule_requires_existing_integer_revision(
    monkeypatch: pytest.MonkeyPatch, revision: object,
) -> None:
    caller = _caller(monkeypatch, heartbeat=True)
    facade = _RuntimeFacade()
    dispatch = NativeCapabilityDispatch(opportunity_runtime=lambda: facade)
    executor, invocation = _invocation(
        _catalog(), "life.self_awaken", "opportunity.schedule",
        {
            "target_id": "opportunity:self_awaken",
            "expected_revision": revision,
            "arguments": {"schedule": "at", "first_due_at": _NOW},
        },
        caller,
    )
    with pytest.raises(NativeCapabilityArgumentsInvalid, match="opportunity.open"):
        await dispatch(executor, invocation)
    assert facade.manage_calls == []


def test_self_awaken_schema_distinguishes_initial_open_from_rescheduling() -> None:
    from plugins.life_engine.opportunity.native_dispatch import _self_awaken_schema

    _description, schema = _self_awaken_schema("opportunity.schedule")
    assert schema["properties"]["expected_revision"]["minimum"] == 1
    assert "opportunity.open" in schema["properties"]["arguments"]["description"]


@pytest.mark.parametrize("action", ["opportunity.schedule", "opportunity.snooze"])
def test_registration_protocol_explains_creation_boundary(action: str) -> None:
    from plugins.life_engine.opportunity.protocol import describe_protocol

    protocol = describe_protocol(action)
    assert "positive revision" in protocol["common"]["expected_revision"]
    assert "opportunity.open" in protocol["common"]["expected_revision"]
    opened = describe_protocol("opportunity.open")
    assert opened["common"]["expected_revision"] == "0 for the first registration only"


@pytest.mark.asyncio
async def test_cancelled_native_tool_propagates_without_translation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    caller = _caller(monkeypatch, heartbeat=True)

    async def execute(self: Any, path: str) -> tuple[bool, Any]:
        del self, path
        raise asyncio.CancelledError

    monkeypatch.setattr(LifeEngineReadFileTool, "execute", execute)
    dispatch = NativeCapabilityDispatch(opportunity_runtime=lambda: _RuntimeFacade())
    executor, invocation = _invocation(
        _catalog(),
        "life.file_care",
        "nucleus_read_file",
        {"path": "notes.md"},
        caller,
    )

    with pytest.raises(asyncio.CancelledError):
        await dispatch(executor, invocation)


_NINE_CAPABILITY_MINIMAL_CALLS = (
    ("life.epistemic_explore", "nucleus_grep_events", {}),
    ("life.file_care", "nucleus_list_files", {}),
    (
        "life.initiative_reencounter",
        "nucleus_proactive_query",
        {"resource": "initiative"},
    ),
    (
        "life.inner_return",
        "nucleus_proactive_query",
        {"resource": "inner_dialogue"},
    ),
    ("life.learning", "nucleus_learn", {"action": "help"}),
    (
        "life.memory_review",
        "nucleus_memory_continuity_review",
        {"action": "status"},
    ),
    ("life.narrative_review", "nucleus_write_narrative", {}),
    ("life.self_awaken", "opportunity.query", {"resource": "catalog"}),
    ("life.todo_reminder", "nucleus_todo", {}),
)


def _assert_schema_accepts(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(arguments) <= set(schema["properties"])
    assert set(schema.get("required", ())) <= set(arguments)
    for name, value in arguments.items():
        field = schema["properties"][name]
        if "const" in field:
            assert value == field["const"]
        if "enum" in field:
            assert value in field["enum"]


@pytest.mark.parametrize(
    ("capability_id", "operation", "arguments"),
    _NINE_CAPABILITY_MINIMAL_CALLS,
)
def test_every_packaged_system_discloses_a_strict_legal_call_shape(
    capability_id: str,
    operation: str,
    arguments: dict[str, Any],
) -> None:
    descriptor = _catalog().require(capability_id)
    assert descriptor.declares_operation(operation)

    disclosed = describe_operation_schema(capability_id, operation)

    assert disclosed["capability_id"] == capability_id
    assert disclosed["operation"] == operation
    assert disclosed["identity_bound_by_runtime"] is True
    assert disclosed["accepts_identity_arguments"] is False
    assert disclosed["grants_permission"] is False
    assert disclosed["workflow_text_executable"] is False
    _assert_schema_accepts(disclosed["arguments_schema"], arguments)


@pytest.mark.asyncio
async def test_every_packaged_system_minimal_call_passes_native_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = _caller(monkeypatch, heartbeat=True)
    facade = _RuntimeFacade()
    dispatch = NativeCapabilityDispatch(opportunity_runtime=lambda: facade)
    catalog = _catalog()

    for capability_id, operation, arguments in _NINE_CAPABILITY_MINIMAL_CALLS:
        if operation.startswith("opportunity."):
            executor, invocation = _invocation(
                catalog, capability_id, operation, dict(arguments), caller
            )
            result = await dispatch(executor, invocation)
            assert result.success is True
            continue

        tool_cls = dispatch._tools[operation]
        original = tool_cls.execute

        @wraps(original)
        async def accepted(self: Any, *args: Any, **kwargs: Any) -> tuple[bool, Any]:
            del self, args
            return True, dict(kwargs)

        monkeypatch.setattr(tool_cls, "execute", accepted)
        executor, invocation = _invocation(
            catalog, capability_id, operation, dict(arguments), caller
        )
        result = await dispatch(executor, invocation)
        assert result.success is True
        assert result.value == arguments


def test_learning_schema_progressively_discloses_exact_inner_arguments() -> None:
    listing = describe_operation_schema("life.learning", "nucleus_learn")
    assert "list_insights" in listing["actions"]
    assert (
        listing["arguments_schema"]["properties"]["action"]["enum"]
        == (listing["actions"])
    )

    detail = describe_operation_schema(
        "life.learning",
        "nucleus_learn",
        action="nucleus_list_insights",
    )

    assert detail["action"] == "list_insights"
    assert detail["inner_operation"] == "list_insights"
    assert detail["arguments_schema"]["properties"]["action"]["const"] == (
        "list_insights"
    )
    assert (
        detail["arguments_schema"]["properties"]["arguments"]
        == (detail["inner_arguments_schema"])
    )
    assert detail["inner_arguments_schema"]["additionalProperties"] is False

    with pytest.raises(NativeCapabilityArgumentsInvalid, match="unknown learning"):
        describe_operation_schema(
            "life.learning", "nucleus_learn", action="invented_action"
        )


@pytest.mark.asyncio
async def test_proactive_slice_rejects_parameters_the_domain_would_ignore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = _caller(monkeypatch, heartbeat=True)
    dispatch = NativeCapabilityDispatch(opportunity_runtime=lambda: _RuntimeFacade())
    catalog = _catalog()

    executor, invocation = _invocation(
        catalog,
        "life.initiative_reencounter",
        "nucleus_proactive_query",
        {"resource": "initiative", "audience_ref": "ignored-before"},
        caller,
    )
    with pytest.raises(NativeCapabilityArgumentsInvalid, match="audience_ref"):
        await dispatch(executor, invocation)

    executor, invocation = _invocation(
        catalog,
        "life.inner_return",
        "nucleus_proactive_command",
        {"action": "inner.return", "expected_revision": 7},
        caller,
    )
    with pytest.raises(NativeCapabilityArgumentsInvalid, match="expected_revision"):
        await dispatch(executor, invocation)


def test_schema_disclosure_cannot_escape_fixed_capability_slices() -> None:
    with pytest.raises(NativeCapabilityPermissionDenied, match="belong only"):
        describe_operation_schema("life.file_care", "opportunity.query")
    with pytest.raises(NativeCapabilityPermissionDenied, match="explicit capability"):
        describe_operation_schema("life.file_care", "nucleus_proactive_command")
    with pytest.raises(NativeCapabilityPermissionDenied, match="fixed native"):
        describe_operation_schema("life.file_care", "nucleus_bash")
