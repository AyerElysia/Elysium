"""Consumer contracts for the public Minecraft tool."""

from __future__ import annotations

import asyncio
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

    async def look(self) -> dict[str, Any]:
        self.calls.append(("look", {}))
        return {"success": True, "observation": {"sequence": len(self.calls)}}


def _bind_turn(tool: LifeEngineMinecraftTool, turn_key: str) -> None:
    tool._bind_runtime_context(
        stream_id="feishu.private.mc",
        message=SimpleNamespace(
            stream_id="feishu.private.mc",
            extra={"life_turn_scope": {"turn_key": turn_key}},
        ),
    )


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
    result = json.loads(payload)
    assert result["active"] is False
    assert result["readiness"] == "ready"
    assert result["status_query_only"] is True
    assert result["started_by_this_call"] is False
    assert "action='start'" in result["start_hint"]
    assert "body_name='bot'" in result["start_hint"]
    assert session.calls == [("status", {})]


async def test_same_turn_semantic_operation_is_replayed_without_reexecution() -> None:
    """Late chatter follow-ups must not execute the same embodied operation twice."""

    session = _Session()
    plugin = SimpleNamespace(service=SimpleNamespace(minecraft_session=session))
    first_tool = LifeEngineMinecraftTool(plugin=plugin)
    replay_tool = LifeEngineMinecraftTool(plugin=plugin)
    _bind_turn(first_tool, "stable-unread-turn")
    _bind_turn(replay_tool, "stable-unread-turn")

    first = await first_tool.execute(action="look", reason="first wording")
    replay = await replay_tool.execute(action="look", reason="later wording")

    assert replay == first
    assert session.calls == [("look", {})]

    next_turn_tool = LifeEngineMinecraftTool(plugin=plugin)
    _bind_turn(next_turn_tool, "next-unread-turn")
    await next_turn_tool.execute(action="look")
    assert session.calls == [("look", {}), ("look", {})]


async def test_concurrent_same_turn_operation_shares_one_execution() -> None:
    """Concurrent duplicate deliveries await one in-flight embodied operation."""

    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingSession(_Session):
        async def look(self) -> dict[str, Any]:
            self.calls.append(("look", {}))
            entered.set()
            await release.wait()
            return {"success": True, "observation": {"sequence": 7}}

    session = BlockingSession()
    plugin = SimpleNamespace(service=SimpleNamespace(minecraft_session=session))
    first_tool = LifeEngineMinecraftTool(plugin=plugin)
    replay_tool = LifeEngineMinecraftTool(plugin=plugin)
    _bind_turn(first_tool, "concurrent-unread-turn")
    _bind_turn(replay_tool, "concurrent-unread-turn")

    first_task = asyncio.create_task(first_tool.execute(action="look"))
    await entered.wait()
    replay_task = asyncio.create_task(replay_tool.execute(action="look"))
    await asyncio.sleep(0)
    release.set()

    first, replay = await asyncio.gather(first_task, replay_task)
    assert replay == first
    assert session.calls == [("look", {})]


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
