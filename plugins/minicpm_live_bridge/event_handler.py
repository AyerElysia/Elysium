"""把 Neo 核心消息事件实时桥接给 MiniCPM live session。"""

from __future__ import annotations

import time
from typing import Any

from src.core.components.base.event_handler import BaseEventHandler
from src.core.components.types import EventType
from src.kernel.event import EventDecision
from src.kernel.logger import get_logger

from .config import MiniCPMLiveBridgeConfig
from .debug_log import live_terminal_log
from .router import MiniCPMLiveRouter

logger = get_logger("MiniCPM_Live_Events", display="MiniCPM Live Events", color="#93C5FD")


def _preview(value: Any, *, limit: int = 360) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


class MiniCPMLiveUnifiedEventHandler(BaseEventHandler):
    """将 QQ/其它通道的消息事件推入 live 的实时统一事件流。"""

    handler_name = "minicpm_live_unified_event_bridge"
    handler_description = "把核心消息事件同步到 MiniCPM live 连接"
    weight = 5
    intercept_message = False
    init_subscribe: list[EventType | str] = [
        EventType.ON_MESSAGE_RECEIVED,
        EventType.ON_MESSAGE_SENT,
    ]

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        config = getattr(self.plugin, "config", None)
        if not isinstance(config, MiniCPMLiveBridgeConfig):
            return EventDecision.PASS, params

        if not config.plugin.enabled or not config.unified_event_stream.sync_core_events_to_live:
            return EventDecision.PASS, params

        message = params.get("message")
        if message is None:
            return EventDecision.PASS, params

        stream_id = str(getattr(message, "stream_id", "") or "")
        if (
            config.unified_event_stream.ignore_live_echo_to_live
            and stream_id == config.session.stream_id
        ):
            return EventDecision.PASS, params

        direction = "sent" if event_name == EventType.ON_MESSAGE_SENT.value else "received"
        content = (
            getattr(message, "processed_plain_text", None)
            or (getattr(message, "content", "") if isinstance(getattr(message, "content", ""), str) else "")
            or str(getattr(message, "content", "") or "")
        )
        unified_event = {
            "origin": "neo_core",
            "channel": "chat",
            "source": str(getattr(message, "platform", "") or "unknown"),
            "event_type": event_name,
            "direction": direction,
            "stream_id": stream_id,
            "chat_type": str(getattr(message, "chat_type", "") or ""),
            "message_id": str(getattr(message, "message_id", "") or ""),
            "sender_id": str(getattr(message, "sender_id", "") or ""),
            "sender_name": str(getattr(message, "sender_name", "") or ""),
            "sender_role": str(getattr(message, "sender_role", "") or ""),
            "message_type": str(getattr(getattr(message, "message_type", ""), "value", "") or ""),
            "text": str(content or ""),
            "time": float(getattr(message, "time", 0.0) or time.time()),
            "adapter_signature": str(params.get("adapter_signature") or ""),
        }

        try:
            await MiniCPMLiveRouter.publish_unified_event(
                unified_event,
                max_backlog=int(config.unified_event_stream.max_backlog_events),
            )
            debug_cfg = getattr(config, "debug", None)
            if bool(getattr(debug_cfg, "terminal_log_enabled", True)) and bool(
                getattr(debug_cfg, "log_core_events", True)
            ):
                try:
                    limit = max(80, int(getattr(debug_cfg, "preview_chars", 360) or 360))
                except (TypeError, ValueError):
                    limit = 360
                live_terminal_log(
                    logger,
                    config,
                    "MiniCPM Live core event synced: "
                    f"source={unified_event['source']} "
                    f"direction={direction} "
                    f"stream={stream_id[:8]}… "
                    f"sender={unified_event['sender_name'] or unified_event['sender_id'] or '-'} "
                    f"text={_preview(content, limit=limit)}"
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"同步核心事件到 MiniCPM live 失败: {exc}")

        return EventDecision.PASS, params
