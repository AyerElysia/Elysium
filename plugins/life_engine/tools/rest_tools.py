"""life_engine 主动休息工具。"""

from __future__ import annotations

from typing import Annotated

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool

from ..service.registry import get_life_engine_service


logger = log_api.get_logger("life_engine.rest_tools")


class LifeEngineRestHeartbeatTool(BaseTool):
    """让生命中枢主动暂停 LLM 心跳一段时间。"""

    tool_name: str = "nucleus_rest_heartbeat"
    tool_description: str = (
        "主动休息一段时间，暂停普通 LLM 心跳。"
        "\n\n"
        "当你感觉自己只是在惯性地心跳、需要安静、整理、沉淀，或者暂时没有真正想推进的事时使用。"
        "调用后，life_engine 的普通心跳模型调用会暂停到指定时间；这不是消失，只是休息。"
        "外界一旦有新消息，系统会自动解除休息锁并恢复。"
        "\n\n"
        "参数：\n"
        "- `duration_minutes`: 想休息多少分钟。系统会限制在 5 到 480 分钟之间。\n"
        "- `reason`: 简短说明为什么此刻想停下来。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        duration_minutes: Annotated[int, "想主动休息的分钟数，系统会限制在 5 到 480 分钟之间"],
        reason: Annotated[str, "为什么此刻想停下来，简短说明即可"] = "",
    ) -> tuple[bool, str | dict]:
        service = get_life_engine_service()
        if service is None:
            return False, "life_engine 服务不可用，无法设置主动休息"

        try:
            minutes = int(duration_minutes)
        except (TypeError, ValueError):
            return False, "duration_minutes 必须是整数分钟数"

        result = await service.request_self_pause(
            duration_minutes=minutes,
            reason=reason,
        )
        logger.info(
            "[nucleus_rest_heartbeat] 主动休息已设置: "
            f"duration={result.get('duration_minutes')}min "
            f"until={result.get('paused_until')} reason={result.get('reason') or '-'}"
        )
        return True, result


REST_TOOLS = [
    LifeEngineRestHeartbeatTool,
]


__all__ = [
    "LifeEngineRestHeartbeatTool",
    "REST_TOOLS",
]
