"""Show 爱莉's text reply on the danmaku page."""

from __future__ import annotations

from typing import Any

from src.core.components.base.event_handler import BaseEventHandler
from src.core.components.types import EventType
from src.kernel.event import EventDecision

from .bridge import PLATFORM, STREAM_ID
from .listen import get_feed


class LiveDanmakuReplyHandler(BaseEventHandler):
    handler_name = "live_danmaku_reply"
    handler_description = "把直播文字回复显示到弹幕页"
    weight = 10
    intercept_message = False
    init_subscribe = [EventType.ON_MESSAGE_DELIVERED]

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        if event_name != EventType.ON_MESSAGE_DELIVERED.value:
            return EventDecision.PASS, params
        message = params.get("message")
        if message is None:
            return EventDecision.PASS, params
        if str(getattr(message, "platform", "") or "") != PLATFORM:
            return EventDecision.PASS, params
        if str(getattr(message, "stream_id", "") or "") != STREAM_ID:
            return EventDecision.PASS, params
        text = getattr(message, "processed_plain_text", None) or getattr(message, "content", "")
        if isinstance(text, str) and text.strip():
            get_feed().note_reply(text.strip())
        return EventDecision.PASS, params
