"""Optional ambient mirroring into N.E.K.O without rewriting chat history."""

from __future__ import annotations

from typing import Any

from src.core.components.base.event_handler import BaseEventHandler
from src.core.components.types import EventType
from src.kernel.event import EventDecision

from .adapter import PLATFORM


class NekoSurfaceDeliveredMirror(BaseEventHandler):
    handler_name = "neko_surface_delivered_mirror"
    handler_description = "Optionally mirror delivered Neo messages to N.E.K.O"
    weight = -20
    intercept_message = False
    init_subscribe = [EventType.ON_MESSAGE_DELIVERED]

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        gateway = self.plugin.gateway
        if not gateway.config.mirror_all:
            return EventDecision.PASS, params

        message = params.get("message")
        if message is None or str(getattr(message, "platform", "") or "") == PLATFORM:
            return EventDecision.PASS, params

        text = str(
            getattr(message, "processed_plain_text", "")
            or getattr(message, "content", "")
            or ""
        ).strip()
        if not text:
            return EventDecision.PASS, params

        turn_id = str(getattr(message, "message_id", "") or "")
        await gateway.publish(
            "assistant.text",
            payload={
                "text": text,
                "is_final": True,
                "metadata": {
                    "mirrored": True,
                    "source_platform": str(getattr(message, "platform", "") or ""),
                    "stream_id": str(getattr(message, "stream_id", "") or ""),
                },
            },
            turn_id=turn_id,
            priority=3,
        )
        await gateway.publish(
            "turn.end",
            payload={"reason": "ambient_mirror"},
            turn_id=turn_id,
            priority=8,
        )
        return EventDecision.SUCCESS, params
