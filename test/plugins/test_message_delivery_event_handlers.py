from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.life_engine.service.event_handler import LifeEngineMessageCollectorHandler
from plugins.minicpm_live_bridge.config import MiniCPMLiveBridgeConfig
from plugins.minicpm_live_bridge.event_handler import MiniCPMLiveUnifiedEventHandler
from plugins.minicpm_live_bridge.router import MiniCPMLiveRouter
from plugins.webui_backend.backend.event_handler.live_chat_event_handler import (
    LiveChatEventHandler,
)
from plugins.webui_backend.backend.router.live_chat_router import LiveChatRouter
from src.core.components.types import EventType
from src.kernel.event import EventDecision


def _assert_delivery_subscription(handler: object) -> None:
    subscribed = set(handler.get_subscribed_events())
    assert EventType.ON_MESSAGE_RECEIVED in subscribed
    assert EventType.ON_MESSAGE_DELIVERED in subscribed
    assert EventType.ON_MESSAGE_SENT not in subscribed


async def test_life_engine_collects_only_confirmed_outbound_messages() -> None:
    service = SimpleNamespace(record_message=AsyncMock())
    handler = LifeEngineMessageCollectorHandler(
        SimpleNamespace(plugin_name="life_engine", service=service)
    )
    message = SimpleNamespace(message_id="delivered-1")
    params = {"message": message}

    _assert_delivery_subscription(handler)

    decision, returned_params = await handler.execute(
        EventType.ON_MESSAGE_DELIVERED.value,
        params,
    )

    assert decision is EventDecision.SUCCESS
    assert returned_params is params
    service.record_message.assert_awaited_once_with(message, direction="sent")

    service.record_message.reset_mock()
    decision, returned_params = await handler.execute(
        EventType.ON_MESSAGE_SENT.value,
        params,
    )

    assert decision is EventDecision.PASS
    assert returned_params is params
    service.record_message.assert_not_awaited()


async def test_webui_marks_delivered_messages_as_sent() -> None:
    handler = LiveChatEventHandler(SimpleNamespace())
    message = SimpleNamespace(message_id="delivered-2")
    params = {"message": message}
    message_data = {"message_id": message.message_id, "is_sent": True}
    handler._build_message_data = AsyncMock(return_value=message_data)
    handler._broadcast_message = AsyncMock()

    _assert_delivery_subscription(handler)

    decision, returned_params = await handler.execute(
        EventType.ON_MESSAGE_DELIVERED.value,
        params,
    )

    assert decision is EventDecision.SUCCESS
    assert returned_params is params
    handler._build_message_data.assert_awaited_once_with(message, True)
    handler._broadcast_message.assert_awaited_once_with(message_data)


async def test_webui_ignores_neko_surface_messages() -> None:
    handler = LiveChatEventHandler(SimpleNamespace())
    message = SimpleNamespace(message_id="surface-1", platform="neko.surface")
    params = {"message": message}
    handler._build_message_data = AsyncMock()
    handler._broadcast_message = AsyncMock()

    decision, returned_params = await handler.execute(
        EventType.ON_MESSAGE_RECEIVED.value,
        params,
    )

    assert decision is EventDecision.PASS
    assert returned_params is params
    handler._build_message_data.assert_not_awaited()
    handler._broadcast_message.assert_not_awaited()


async def test_webui_broadcast_drops_neko_surface_messages() -> None:
    class _WebSocketProbe:
        pass

    websocket = _WebSocketProbe()
    websocket.send_json = AsyncMock()
    previous_connections = LiveChatRouter.active_connections
    LiveChatRouter.active_connections = {"surface-stream": {websocket}}
    try:
        await LiveChatRouter.broadcast_message(
            {
                "message_id": "surface-2",
                "stream_id": "surface-stream",
                "platform": "neko.surface",
            }
        )
    finally:
        LiveChatRouter.active_connections = previous_connections

    websocket.send_json.assert_not_awaited()


async def test_minicpm_maps_delivered_messages_to_sent_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = MiniCPMLiveBridgeConfig()
    config.debug.terminal_log_enabled = False
    handler = MiniCPMLiveUnifiedEventHandler(SimpleNamespace(config=config))
    message = SimpleNamespace(
        message_id="delivered-3",
        stream_id="qq-stream",
        platform="qq",
        chat_type="private",
        sender_id="bot-1",
        sender_name="NeoBot",
        sender_role="assistant",
        message_type=SimpleNamespace(value="text"),
        processed_plain_text="delivered",
        content="delivered",
        time=1.0,
    )
    params = {
        "message": message,
        "adapter_signature": "napcat_adapter:adapter:napcat_adapter",
    }
    publish_event = AsyncMock(return_value={})
    monkeypatch.setattr(MiniCPMLiveRouter, "publish_unified_event", publish_event)

    _assert_delivery_subscription(handler)

    decision, returned_params = await handler.execute(
        EventType.ON_MESSAGE_DELIVERED.value,
        params,
    )

    assert decision is EventDecision.PASS
    assert returned_params is params
    publish_event.assert_awaited_once()
    unified_event = publish_event.await_args.args[0]
    assert unified_event["event_type"] == EventType.ON_MESSAGE_DELIVERED.value
    assert unified_event["direction"] == "sent"
    assert unified_event["message_id"] == "delivered-3"
