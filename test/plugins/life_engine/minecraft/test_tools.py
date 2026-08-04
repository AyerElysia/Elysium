"""Consumer contracts for the public Minecraft tool."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from plugins.life_engine.minecraft.tools import LifeEngineMinecraftTool


class _Session:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def preflight(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("preflight", kwargs))
        return {"success": True, "ready_to_start": True}


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

    result = json.loads(
        await tool.execute(action="preflight", body_name="agent", plugin=plugin)
    )

    assert result == {"success": True, "ready_to_start": True}
    assert session.calls == [("preflight", {"body_name": "agent"})]


async def test_tool_reports_disabled_session_without_private_scheduler_fallback() -> (
    None
):
    """Missing public session ownership is a diagnosable disabled state."""

    plugin = SimpleNamespace(service=SimpleNamespace(minecraft_session=None))
    tool = LifeEngineMinecraftTool(plugin=plugin)

    result = json.loads(await tool.execute(action="status", plugin=plugin))

    assert result["success"] is False
