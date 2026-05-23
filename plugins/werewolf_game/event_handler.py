"""Message event interception for Werewolf commands."""

from __future__ import annotations

import shlex
from typing import Any

from src.app.plugin_system.base import BaseEventHandler
from src.core.components.types import EventType
from src.kernel.event import EventDecision

from .service import WerewolfGameService


class WerewolfCommandEventHandler(BaseEventHandler):
    """Handle Werewolf commands before they reach life chatter."""

    handler_name = "werewolf_command_dispatch"
    handler_description = "拦截狼人杀命令，避免夜晚私聊行动进入统一意识"
    weight = 2300
    intercept_message = True
    init_subscribe = [EventType.ON_MESSAGE_RECEIVED]

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        del event_name
        message = params.get("message")
        if message is None:
            return EventDecision.SUCCESS, params

        text = str(
            getattr(message, "processed_plain_text", "")
            or getattr(message, "content", "")
            or ""
        ).strip()
        args = self._parse_args(text)
        if args is None:
            return EventDecision.SUCCESS, params

        service = WerewolfGameService(plugin=self.plugin)
        if str(getattr(message, "chat_type", "")) == "group":
            reply = await service.handle_group_command(message, args)
            await self._send_public_reply(message, reply)
        else:
            reply = await service.handle_private_command(message, args)
            await service._send_private_referee_message(
                str(getattr(message, "platform", "") or "qq"),
                str(getattr(message, "sender_id", "")),
                reply,
            )
        return EventDecision.STOP, params

    def _parse_args(self, text: str) -> list[str] | None:
        if not text.startswith("/"):
            return None
        try:
            parts = shlex.split(text[1:])
        except ValueError:
            parts = text[1:].split()
        if not parts:
            return None
        if parts[0].lower() not in {"狼人杀", "werewolf", "ww"}:
            return None
        return parts[1:]

    async def _send_public_reply(self, message: Any, reply: str) -> None:
        from src.app.plugin_system.api.send_api import send_text

        if not reply:
            return
        await send_text(
            reply,
            stream_id=str(getattr(message, "stream_id", "")),
            platform=str(getattr(message, "platform", "") or "") or None,
            reply_to=str(getattr(message, "message_id", "") or "") or None,
        )

