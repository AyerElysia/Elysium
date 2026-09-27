"""KOOK gateway lifecycle tests."""

from __future__ import annotations

import asyncio

from plugins.kook_adapter.config import KookAdapterConfig
from plugins.kook_adapter.gateway import KookGateway
from plugins.kook_adapter.plugin import KookAdapter, KookAdapterPlugin


def test_new_kook_install_registers_no_adapter_until_explicitly_enabled() -> None:
    config = KookAdapterConfig()
    assert config.plugin.enabled is False
    assert KookAdapterPlugin(config=config).get_components() == []
    assert KookAdapterPlugin(config=None).get_components() == []

    enabled_config = KookAdapterConfig.from_dict({"plugin": {"enabled": True}})
    assert KookAdapterPlugin(config=enabled_config).get_components() == [KookAdapter]


async def test_gateway_start_is_idempotent_and_stop_awaits_listener() -> None:
    """Concurrent starts must own one managed listener that stop fully joins."""

    entered = asyncio.Event()

    async def get_url() -> str:
        return "ws://example.invalid"

    async def on_event(_event) -> None:
        return None

    gateway = KookGateway("token", get_url, on_event)
    connect_calls = 0

    async def blocked_connect_loop() -> None:
        nonlocal connect_calls
        connect_calls += 1
        entered.set()
        await asyncio.Event().wait()

    gateway._connect_loop = blocked_connect_loop  # type: ignore[method-assign]

    await asyncio.gather(gateway.start(), gateway.start())
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    listener = gateway._listen_task_info.task

    assert connect_calls == 1
    assert listener.done() is False
    assert gateway.alive is True

    await gateway.stop()

    assert listener.done() is True
    assert gateway._listen_task_info is None
    assert gateway.connected is False
    assert gateway.alive is False


def test_rest_direct_message_keeps_create_at_as_msg_timestamp() -> None:
    from plugins.kook_adapter.events import (
        _kook_event_unix_seconds,
        rest_direct_message_to_person_event,
    )

    event = rest_direct_message_to_person_event(
        {
            "id": "msg-1",
            "type": 9,
            "author_id": "1370110560",
            "content": "hi",
            "create_at": 1789562229000,
            "author": {"id": "1370110560", "username": "user"},
        },
        target_id="1370110560",
    )
    assert event["msg_id"] == "msg-1"
    assert event["msg_timestamp"] == 1789562229000
    assert _kook_event_unix_seconds(event) == 1789562229.0
