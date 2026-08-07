"""life_engine 消息收集事件处理器。"""

from __future__ import annotations

import traceback
from typing import Any, ClassVar

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
    init_subscribe: ClassVar[list[EventType | str]] = [
        EventType.ON_MESSAGE_RECEIVED,
        EventType.ON_MESSAGE_SENT,
        EventType.ON_MESSAGE_DELIVERED,
        EventType.ON_MESSAGE_DELIVERY_FAILED,
        EventType.ON_MESSAGE_DELIVERY_UNKNOWN,
        EventType.ON_RECEIVED_OTHER_MESSAGE,
    ]

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """把已接收或已确认投递的 message 记录到 life_engine 服务队列。"""
        if event_name not in {
            EventType.ON_MESSAGE_RECEIVED.value,
            EventType.ON_MESSAGE_SENT.value,
            EventType.ON_MESSAGE_DELIVERED.value,
            EventType.ON_MESSAGE_DELIVERY_FAILED.value,
            EventType.ON_MESSAGE_DELIVERY_UNKNOWN.value,
            EventType.ON_RECEIVED_OTHER_MESSAGE.value,
        }:
            return EventDecision.PASS, params

        plugin = self.plugin
        if getattr(plugin, "plugin_name", "") != "life_engine":
            return EventDecision.PASS, params

        try:
            service = getattr(plugin, "service", None)
            if service is None:
                return EventDecision.SUCCESS, params

            if event_name == EventType.ON_RECEIVED_OTHER_MESSAGE.value:
                raw = params.get("raw")
                if isinstance(raw, dict):
                    await service.record_provider_notice(
                        raw,
                        adapter_signature=str(params.get("adapter_signature") or ""),
                    )
                return EventDecision.PASS, params

            message = params.get("message")
            if message is None:
                return EventDecision.SUCCESS, params

            if event_name == EventType.ON_MESSAGE_SENT.value:
                await service.record_send_requested(
                    message,
                    envelope=params.get("envelope"),
                    adapter_signature=str(params.get("adapter_signature") or ""),
                )
                return EventDecision.PASS, params

            if event_name in {
                EventType.ON_MESSAGE_DELIVERY_FAILED.value,
                EventType.ON_MESSAGE_DELIVERY_UNKNOWN.value,
            }:
                await service.record_delivery_status(
                    message,
                    status=str(params.get("delivery_status") or "unknown"),
                    adapter_signature=str(params.get("adapter_signature") or ""),
                )
                return EventDecision.SUCCESS, params

            direction = "received"
            if event_name == EventType.ON_MESSAGE_DELIVERED.value:
                direction = "sent"

            await service.record_message(
                message,
                direction=direction,
                envelope=params.get("envelope"),
                adapter_signature=str(params.get("adapter_signature") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            # 附带异常类型与 traceback，便于定位 "session already committed" 等
            # 事务状态错误究竟发生在哪个阶段（事实提交/EventBus/World catch-up）。
            tb_text = traceback.format_exc(limit=12)
            logger.error(
                f"life_engine 收集消息失败: {type(exc).__name__}: {exc}\n{tb_text}"
            )
            log_error(
                "message_collect_failed",
                str(exc),
                event_name=event_name,
                error_type=type(exc).__name__,
                traceback=tb_text,
            )

        return EventDecision.SUCCESS, params
