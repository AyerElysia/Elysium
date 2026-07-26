"""nucleus_minecraft 工具。

让爱莉通过工具调用控制 Minecraft 会话。
这是她与 MC 世界的接口。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Any

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool

from .prompts import TOOL_DESCRIPTION

logger = log_api.get_logger("life_engine.minecraft.tools")


def _get_session(plugin: Any) -> Any:
    """从 plugin 获取 MinecraftSession。"""
    service = getattr(plugin, "service", None)
    if service is None:
        return None
    scheduler = getattr(service, "_learning_scheduler", None)
    if scheduler is None:
        return None
    return getattr(scheduler, "minecraft_session", None)


class LifeEngineMinecraftTool(BaseTool):
    """Minecraft 具身体验工具。"""

    tool_name: str = "nucleus_minecraft"
    description: str = TOOL_DESCRIPTION
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["start", "stop", "do", "look", "status"],
                "description": "操作类型",
            },
            "intent": {
                "type": "string",
                "description": "意图描述（action=do 时必填），如 '砍那棵树'、'向下挖'",
            },
            "goal": {
                "type": "string",
                "description": "会话目标（action=start 时可选），如 '收集木材做工具'",
            },
        },
        "required": ["action"],
    }

    async def execute(
        self,
        action: Annotated[str, "操作类型"],
        intent: Annotated[str, "意图描述"] = "",
        goal: Annotated[str, "会话目标"] = "",
        **kwargs: Any,
    ) -> str:
        """执行 Minecraft 操作。"""
        plugin = kwargs.get("plugin")
        if plugin is None:
            return json.dumps({"success": False, "error": "plugin 未提供"}, ensure_ascii=False)

        session = _get_session(plugin)
        if session is None:
            return json.dumps(
                {"success": False, "error": "Minecraft 未启用或会话未初始化"},
                ensure_ascii=False,
            )

        match action:
            case "start":
                result = await session.start(goal=goal)
            case "stop":
                result = await session.stop()
            case "do":
                if not intent:
                    result = {"success": False, "error": "请提供 intent 参数"}
                else:
                    result = await session.do_intent(intent)
            case "look":
                result = await session.look()
            case "status":
                result = await session.get_status()
            case _:
                result = {"success": False, "error": f"未知操作: {action}"}

        return json.dumps(result, ensure_ascii=False, default=str)


MINECRAFT_TOOLS = [LifeEngineMinecraftTool]

__all__ = ["LifeEngineMinecraftTool", "MINECRAFT_TOOLS"]
