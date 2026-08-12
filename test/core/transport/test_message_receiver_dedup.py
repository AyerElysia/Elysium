from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.models.message import Message
from src.core.transport.message_receive.receiver import MessageReceiver


async def test_unresolved_platform_identity_does_not_overwrite_person_data() -> None:
    receiver = MessageReceiver()
    message = Message(
        message_id="msg-unresolved",
        content="hello",
        processed_plain_text="hello",
        sender_id="ou_known",
        sender_name="身份未解析的飞书用户（账号…nown）",
        platform="feishu",
        chat_type="private",
        stream_id="stream-identity",
        extra={
            "identity_resolution_status": "unresolved",
            "canonical_person_key": "",
        },
    )
    helper = MagicMock()
    helper.update_person_info = AsyncMock(return_value=True)

    with patch(
        "src.core.utils.user_query_helper.get_user_query_helper",
        return_value=helper,
    ):
        await receiver._update_person_info(message)

    helper.update_person_info.assert_awaited_once_with(
        platform="feishu",
        user_id="ou_known",
        nickname=None,
        cardname=None,
        canonical_person_key=None,
    )


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


async def test_receive_standardized_message_uses_full_receive_pipeline() -> None:
    """已标准化消息仍需统一去重、用户更新和事件发布。"""
    message = Message(
        message_id="msg-inject-1",
        content="hello from ayla",
        processed_plain_text="hello from ayla",
        sender_id="app-user-1",
        sender_name="汐汐",
        platform="ayla",
        chat_type="private",
        stream_id="explicit-ayla-stream",
    )
    receiver = MessageReceiver()
    receiver._update_person_info = AsyncMock()  # type: ignore[method-assign]
    event_manager = MagicMock()
    event_manager.publish_event = AsyncMock(
        return_value={"decision": "SUCCESS", "params": {}}
    )
    receiver._event_manager = event_manager

    first = await receiver.receive_message(
        message,
        "ayla_adapter:adapter:ayla_adapter",
        envelope={},
    )
    duplicate = await receiver.receive_message(
        message,
        "ayla_adapter:adapter:ayla_adapter",
        envelope={},
    )

    assert first is True
    assert duplicate is False
    receiver._update_person_info.assert_awaited_once_with(message)
    event_manager.publish_event.assert_awaited_once()
    kwargs = event_manager.publish_event.await_args.args[1]
    assert kwargs["message"] is message
    assert kwargs["message"].stream_id == "explicit-ayla-stream"
    assert kwargs["adapter_signature"] == "ayla_adapter:adapter:ayla_adapter"


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
