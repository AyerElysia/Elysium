from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugins.life_engine.service.event_handler import LifeEngineMessageCollectorHandler
from src.core.components.types import EventType
from src.kernel.event import EventDecision


def _assert_delivery_subscription(handler: object) -> None:
    subscribed = set(handler.get_subscribed_events())
    assert EventType.ON_MESSAGE_RECEIVED in subscribed
    assert EventType.ON_MESSAGE_DELIVERED in subscribed
    assert EventType.ON_MESSAGE_DELIVERY_FAILED in subscribed
    assert EventType.ON_MESSAGE_DELIVERY_UNKNOWN in subscribed
    assert EventType.ON_RECEIVED_OTHER_MESSAGE in subscribed
    assert EventType.ON_MESSAGE_SENT in subscribed


async def test_life_engine_collects_only_confirmed_outbound_messages() -> None:
    service = SimpleNamespace(
        record_message=AsyncMock(),
        record_provider_notice=AsyncMock(),
        record_delivery_status=AsyncMock(),
        record_send_requested=AsyncMock(),
    )
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
    service.record_message.assert_awaited_once_with(
        message,
        direction="sent",
        envelope=None,
        adapter_signature="",
    )

    service.record_message.reset_mock()
    decision, returned_params = await handler.execute(
        EventType.ON_MESSAGE_SENT.value,
        params,
    )

    assert decision is EventDecision.PASS
    assert returned_params is params
    service.record_send_requested.assert_awaited_once_with(
        message,
        envelope=None,
        adapter_signature="",
    )
    service.record_message.assert_not_awaited()


async def test_life_engine_records_other_transport_facts_without_processing() -> None:
    service = SimpleNamespace(
        record_message=AsyncMock(),
        record_provider_notice=AsyncMock(),
        record_delivery_status=AsyncMock(),
        record_send_requested=AsyncMock(),
    )
    handler = LifeEngineMessageCollectorHandler(
        SimpleNamespace(plugin_name="life_engine", service=service)
    )
    raw = {"message_info": {"message_type": "notice"}}
    params = {
        "raw": raw,
        "processed": "",
        "adapter_signature": "napcat_adapter:adapter:qq",
    }

    decision, returned_params = await handler.execute(
        EventType.ON_RECEIVED_OTHER_MESSAGE.value,
        params,
    )

    assert decision is EventDecision.PASS
    assert returned_params is params
    service.record_provider_notice.assert_awaited_once_with(
        raw,
        adapter_signature="napcat_adapter:adapter:qq",
    )
    service.record_message.assert_not_awaited()


async def test_life_engine_records_unknown_delivery_without_confirming() -> None:
    service = SimpleNamespace(
        record_message=AsyncMock(),
        record_provider_notice=AsyncMock(),
        record_delivery_status=AsyncMock(),
        record_send_requested=AsyncMock(),
    )
    handler = LifeEngineMessageCollectorHandler(
        SimpleNamespace(plugin_name="life_engine", service=service)
    )
    message = SimpleNamespace(message_id="unknown-1")
    params = {
        "message": message,
        "delivery_status": "unknown",
        "adapter_signature": "napcat_adapter:adapter:qq",
    }

    decision, returned_params = await handler.execute(
        EventType.ON_MESSAGE_DELIVERY_UNKNOWN.value,
        params,
    )

    assert decision is EventDecision.SUCCESS
    assert returned_params is params
    service.record_delivery_status.assert_awaited_once_with(
        message,
        status="unknown",
        adapter_signature="napcat_adapter:adapter:qq",
    )
    service.record_message.assert_not_awaited()
