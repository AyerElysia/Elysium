"""Consumer contracts for the public Minecraft tool."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from plugins.life_engine.minecraft.tools import LifeEngineMinecraftTool
from src.core.managers.tool_manager.tool_use import ToolUse
from src.core.utils.llm_tool_call import exec_llm_usable


class _Session:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def preflight(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("preflight", kwargs))
        return {"success": True, "ready_to_start": True}

    async def get_status(self) -> dict[str, Any]:
        self.calls.append(("status", {}))
        return {"active": False, "readiness": "ready"}


async def test_tool_consumes_service_owned_session_without_learning_scheduler() -> None:
    """The public tool resolves the independent service-owned MC session."""

    session = _Session()
    plugin = SimpleNamespace(
        service=SimpleNamespace(
            minecraft_session=session,
            _learning_scheduler=None,
        )
    )
    tool = LifeEngineMinecraftTool(plugin=plugin)

    success, payload = await tool.execute(action="preflight", body_name="agent")
    result = json.loads(payload)

    assert success is True
    assert result == {"success": True, "ready_to_start": True}
    assert session.calls == [("preflight", {"body_name": "agent"})]


async def test_tool_reports_disabled_session_without_private_scheduler_fallback() -> (
    None
):
    """Missing public session ownership is a diagnosable disabled state."""

    plugin = SimpleNamespace(service=SimpleNamespace(minecraft_session=None))
    tool = LifeEngineMinecraftTool(plugin=plugin)

    success, payload = await tool.execute(action="status")
    result = json.loads(payload)

    assert success is False
    assert result["success"] is False


async def test_status_query_succeeds_when_body_is_inactive() -> None:
    """Inactive is valid status data rather than a tool execution failure."""

    session = _Session()
    plugin = SimpleNamespace(service=SimpleNamespace(minecraft_session=session))
    tool = LifeEngineMinecraftTool(plugin=plugin)

    success, payload = await tool.execute(action="status")

    assert success is True
    assert json.loads(payload) == {"active": False, "readiness": "ready"}
    assert session.calls == [("status", {})]


async def test_tool_use_injects_the_owning_plugin_without_public_plugin_argument(
    monkeypatch,
) -> None:
    """The production ToolUse path must resolve the service-owned MC session."""

    session = _Session()
    plugin = SimpleNamespace(service=SimpleNamespace(minecraft_session=session))
    registry = SimpleNamespace(
        get=lambda signature: (
            LifeEngineMinecraftTool
            if signature == "life_engine:tool:nucleus_minecraft"
            else None
        )
    )
    monkeypatch.setattr(
        "src.core.managers.tool_manager.tool_use.get_global_registry",
        lambda: registry,
    )

    success, result = await ToolUse().execute_tool(
        "life_engine:tool:nucleus_minecraft",
        plugin,
        SimpleNamespace(),
        action="preflight",
        body_name="agent",
    )

    assert success is True
    assert json.loads(result) == {"success": True, "ready_to_start": True}
    assert session.calls == [("preflight", {"body_name": "agent"})]


async def test_llm_runtime_injects_the_owning_plugin_without_public_plugin_argument() -> (
    None
):
    """The actual chatter execution path constructs the tool with its owner."""

    session = _Session()
    plugin = SimpleNamespace(service=SimpleNamespace(minecraft_session=session))

    success, result = await exec_llm_usable(
        LifeEngineMinecraftTool,
        plugin=plugin,
        kwargs={"action": "preflight", "body_name": "agent"},
    )

    assert success is True
    assert json.loads(result) == {"success": True, "ready_to_start": True}
    assert session.calls == [("preflight", {"body_name": "agent"})]
