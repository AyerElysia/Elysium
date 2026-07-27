"""聊天命令分流事件处理器。"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.api.command_api import execute_command, match_command
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_text
from src.core.components.base import BaseEventHandler
from src.core.components.types import EventType
from src.kernel.event import EventDecision


logger = get_logger("commands_plugin")


class CommandDispatchEventHandler(BaseEventHandler):
    """在消息入站时先处理聊天命令。"""

    handler_name = "command_dispatch"
    handler_description = "拦截并执行聊天命令"
    weight = 2000
    intercept_message = True
    init_subscribe = [EventType.ON_MESSAGE_RECEIVED]

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        del event_name

        plugin = self.plugin
        if str(getattr(plugin, "plugin_name", "")) != "commands_plugin":
            return EventDecision.SUCCESS, params

        message = params.get("message")
        if message is None:
            return EventDecision.SUCCESS, params

        if str(getattr(message, "sender_role", "") or "").lower() == "bot":
            return EventDecision.SUCCESS, params

        text = str(
            getattr(message, "processed_plain_text", "")
            or getattr(message, "content", "")
            or ""
        ).strip()
        if not text:
            return EventDecision.SUCCESS, params

        command_path, command_cls, _ = match_command(text)
        if command_cls is None:
            if command_path.startswith("/"):
                await send_text(
                    f"未知命令: {command_path}",
                    stream_id=str(getattr(message, "stream_id", "")),
                    platform=str(getattr(message, "platform", "") or "") or None,
                    reply_to=str(getattr(message, "message_id", "") or "") or None,
                )
                logger.debug(f"未知命令已拦截: {command_path}")
                return EventDecision.STOP, params
            return EventDecision.SUCCESS, params

        ok, result = await execute_command(message, text)

        auto_reply = getattr(command_cls, "auto_reply", True)
        if auto_reply:
            reply_text = str(result or "").strip()
            if reply_text:
                await send_text(
                    reply_text,
                    stream_id=str(getattr(message, "stream_id", "")),
                    platform=str(getattr(message, "platform", "") or "") or None,
                    reply_to=str(getattr(message, "message_id", "") or "") or None,
                )

        logger.debug(
            f"命令已执行: {command_path}, success={ok}, auto_reply={auto_reply}"
        )
        return EventDecision.STOP, params
