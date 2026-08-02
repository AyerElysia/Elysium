from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.models.message import Message
from src.core.transport.message_receive.receiver import MessageReceiver


async def test_message_claim_is_atomic_under_concurrency() -> None:
    receiver = MessageReceiver()

    results = await asyncio.gather(
        *(
            receiver._claim_message(
                adapter_signature="plugin:adapter:qq",
                platform="qq",
                message_id="msg-concurrent",
            )
            for _ in range(20)
        )
    )

    assert results.count(True) == 1
    assert results.count(False) == 19


async def test_message_claim_expires_after_dedup_window() -> None:
    receiver = MessageReceiver()

    with patch(
        "src.core.transport.message_receive.receiver.time.monotonic",
        side_effect=[100.0, 100.0, 221.0],
    ):
        first = await receiver._claim_message(
            adapter_signature="plugin:adapter:qq",
            platform="qq",
            message_id="msg-expiring",
        )
        await receiver._complete_message(
            adapter_signature="plugin:adapter:qq",
            platform="qq",
            message_id="msg-expiring",
        )
        after_window = await receiver._claim_message(
            adapter_signature="plugin:adapter:qq",
            platform="qq",
            message_id="msg-expiring",
        )

    assert first is True
    assert after_window is True


async def test_receive_envelope_dedups_same_message_in_window() -> None:
    """同一条入站消息在去重窗口内只应分发一次。"""
    message = Message(
        message_id="msg-001",
        content="hello",
        processed_plain_text="hello",
        sender_id="u1",
        sender_name="Alice",
        platform="qq",
        chat_type="group",
        stream_id="stream-1",
    )

    converter = MagicMock()
    converter.envelope_to_message = AsyncMock(return_value=message)

    receiver = MessageReceiver(converter=converter)
    receiver._update_person_info = AsyncMock()  # type: ignore[method-assign]

    event_manager = MagicMock()
    event_manager.publish_event = AsyncMock(return_value={"decision": "SUCCESS", "params": {}})
    receiver._event_manager = event_manager

    envelope = {
        "direction": "incoming",
        "message_info": {
            "message_id": "msg-001",
            "platform": "qq",
            "user_info": {"user_id": "u1", "user_nickname": "Alice"},
            "group_info": {"group_id": "g1", "group_name": "TestGroup"},
        },
        "message_segment": [{"type": "text", "data": {"text": "hello"}}],
    }

    await receiver.receive_envelope(envelope, "plugin:adapter:qq")
    await receiver.receive_envelope(envelope, "plugin:adapter:qq")

    assert converter.envelope_to_message.await_count == 1
    assert event_manager.publish_event.await_count == 1


async def test_receive_envelope_different_message_ids_not_deduped() -> None:
    """不同 message_id 的入站消息应正常分别分发。"""
    message1 = Message(
        message_id="msg-101",
        content="m1",
        processed_plain_text="m1",
        sender_id="u1",
        sender_name="Alice",
        platform="qq",
        chat_type="group",
        stream_id="stream-1",
    )
    message2 = Message(
        message_id="msg-102",
        content="m2",
        processed_plain_text="m2",
        sender_id="u1",
        sender_name="Alice",
        platform="qq",
        chat_type="group",
        stream_id="stream-1",
    )

    converter = MagicMock()
    converter.envelope_to_message = AsyncMock(side_effect=[message1, message2])

    receiver = MessageReceiver(converter=converter)
    receiver._update_person_info = AsyncMock()  # type: ignore[method-assign]

    event_manager = MagicMock()
    event_manager.publish_event = AsyncMock(return_value={"decision": "SUCCESS", "params": {}})
    receiver._event_manager = event_manager

    envelope1 = {
        "direction": "incoming",
        "message_info": {
            "message_id": "msg-101",
            "platform": "qq",
            "user_info": {"user_id": "u1", "user_nickname": "Alice"},
        },
        "message_segment": [{"type": "text", "data": {"text": "m1"}}],
    }
    envelope2 = {
        "direction": "incoming",
        "message_info": {
            "message_id": "msg-102",
            "platform": "qq",
            "user_info": {"user_id": "u1", "user_nickname": "Alice"},
        },
        "message_segment": [{"type": "text", "data": {"text": "m2"}}],
    }

    await receiver.receive_envelope(envelope1, "plugin:adapter:qq")
    await receiver.receive_envelope(envelope2, "plugin:adapter:qq")

    assert converter.envelope_to_message.await_count == 2
    assert event_manager.publish_event.await_count == 2


async def test_conversion_failure_releases_claim_for_redelivery() -> None:
    """A transient conversion failure must not permanently suppress redelivery."""

    message = Message(
        message_id="msg-retry",
        content="hello",
        processed_plain_text="hello",
        sender_id="u1",
        sender_name="Alice",
        platform="qq",
        chat_type="private",
        stream_id="stream-1",
    )
    converter = MagicMock()
    converter.envelope_to_message = AsyncMock(
        side_effect=[ValueError("temporary decode failure"), message]
    )
    receiver = MessageReceiver(converter=converter)
    receiver._update_person_info = AsyncMock()  # type: ignore[method-assign]
    event_manager = MagicMock()
    event_manager.publish_event = AsyncMock(
        return_value={"decision": "SUCCESS", "params": {}}
    )
    receiver._event_manager = event_manager
    envelope = {
        "direction": "incoming",
        "message_info": {
            "message_id": "msg-retry",
            "platform": "qq",
            "message_type": "private",
            "user_info": {"user_id": "u1", "user_nickname": "Alice"},
        },
        "message_segment": [{"type": "text", "data": {"text": "hello"}}],
    }

    await receiver.receive_envelope(envelope, "plugin:adapter:qq")
    await receiver.receive_envelope(envelope, "plugin:adapter:qq")

    assert converter.envelope_to_message.await_count == 2
    assert event_manager.publish_event.await_count == 1
