"""life_engine 消息收集事件处理器。"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseEventHandler
from src.app.plugin_system.types import EventType
from src.kernel.event import EventDecision

from .audit import log_error

logger = get_logger("life_engine", display="life_engine")


class LifeEngineMessageCollectorHandler(BaseEventHandler):
    """收集聊天流入站/出站消息，供 life_engine 中枢在心跳时统一处理。"""

    plugin_name = "life_engine"
    handler_name = "message_collector"
    handler_description = "收集收发消息并堆积到 life_engine 队列"
    weight = 50
    intercept_message = False
    init_subscribe: list[EventType | str] = [
        EventType.ON_MESSAGE_RECEIVED,
        EventType.ON_MESSAGE_SENT,
    ]

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """把收发消息事件中的 message 记录到 life_engine 服务队列。"""
        if event_name not in {
            EventType.ON_MESSAGE_RECEIVED.value,
            EventType.ON_MESSAGE_SENT.value,
        }:
            return EventDecision.PASS, params

        plugin = self.plugin
        if getattr(plugin, "plugin_name", "") != "life_engine":
            return EventDecision.PASS, params

        try:
            message = params.get("message")
            if message is None:
                return EventDecision.SUCCESS, params

            service = getattr(plugin, "service", None)
            if service is None:
                return EventDecision.SUCCESS, params

            direction = "received"
            if event_name == EventType.ON_MESSAGE_SENT.value:
                direction = "sent"

            await service.record_message(message, direction=direction)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"life_engine 收集消息失败: {exc}")
            log_error(
                "message_collect_failed",
                str(exc),
                event_name=event_name,
            )

        return EventDecision.SUCCESS, params
