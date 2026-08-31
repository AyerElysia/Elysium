"""P3-05 stable chat fact builder contracts."""

from datetime import UTC, datetime

from plugins.life_engine.service.chat_events import (
    build_chat_message_event,
    build_chat_provider_notice_event,
)
from src.core.models.media import MediaAttachment, MediaSegmentType
from src.core.models.message import Message, MessageType
from src.kernel.llm.payload.media import MediaKind, MediaRef


def _message(*, notice: bool = False) -> Message:
    attachment = MediaAttachment(
        MediaSegmentType.IMAGE,
        MediaRef.from_bytes(
            b"\x89PNG\r\n\x1a\nfixture",
            kind=MediaKind.IMAGE,
            source_message_id="provider-msg-1",
        ),
        filename="picture.png",
        resource_id="resource-1",
    )
    return Message(
        message_id="msg-1",
        time=datetime(2026, 8, 4, 8, 0, tzinfo=UTC),
        reply_to="msg-parent",
        content="hello",
        processed_plain_text="hello",
        message_type=MessageType.UNKNOWN if notice else MessageType.IMAGE,
        sender_id="user-1",
        sender_name="User One",
        platform="feishu",
        chat_type="private",
        stream_id="feishu:private:user-1",
        attachments=[attachment],
        extra={
            "is_notice": notice,
            "notice_type": "group_recall" if notice else None,
            "feishu_event_id": "evt-provider-1",
            "feishu_message_id": "provider-msg-1",
            "provider_receipt": {"message_id": "provider-msg-1"},
        },
    )


def test_received_chat_fact_keeps_reply_media_and_provider_identity() -> None:
    event = build_chat_message_event(
        _message(),
        direction="received",
        adapter_signature="feishu_adapter:adapter:feishu",
    )

    assert event.event_type == "chat.message.received"
    assert event.stream_id == "feishu:private:user-1"
    assert event.metadata["chat"]["reply_to"] == "msg-parent"
    assert event.metadata["provider_identity"]["feishu_event_id"] == "evt-provider-1"
    descriptor = event.metadata["chat"]["attachments"][0]
    assert descriptor["metadata"]["resource_id"] == "resource-1"
    assert "path" not in str(descriptor).lower()
    assert "base64" not in str(descriptor).lower()


def test_voice_message_keeps_safe_attachment_for_history_projection() -> None:
    voice = MediaAttachment(
        MediaSegmentType.VOICE,
        MediaRef.from_bytes(
            b"OggSfixture",
            kind=MediaKind.AUDIO,
            source_message_id="provider-voice-1",
        ),
        filename="voice.ogg",
        resource_id="resource-voice-1",
    )
    message = Message(
        message_id="voice-1",
        time=datetime(2026, 8, 4, 8, 1, tzinfo=UTC),
        content="",
        message_type=MessageType.VOICE,
        sender_id="user-1",
        platform="feishu",
        chat_type="private",
        stream_id="feishu:private:user-1",
        attachments=[voice],
    )

    event = build_chat_message_event(message, direction="received")

    assert event.event_type == "chat.message.received"
    assert event.metadata["chat"]["parts"][0]["type"] == "voice"
    descriptor = event.metadata["chat"]["attachments"][0]
    assert descriptor["metadata"]["resource_id"] == "resource-voice-1"
    assert "base64" not in str(descriptor).lower()
    assert "path" not in str(descriptor).lower()


def test_send_requested_fact_never_claims_delivery_success() -> None:
    event = build_chat_message_event(_message(), direction="requested")

    assert event.event_type == "chat.message.send_requested"
    assert event.event_type != "chat.message.delivery_confirmed"


def test_delivery_fact_requires_delivered_direction_and_keeps_receipt() -> None:
    message = _message()
    message.extra["api_actor_id"] = "api-actor-1"
    message.extra.update(
        {
            "consciousness_instance_id": "chat-instance-1",
            "conscious_activity_id": "activity-1",
            "tool_call_id": "call-1",
            "origin_turn_key": "turn-1",
            "origin_stream_id": "feishu:private:user-1",
        }
    )
    event = build_chat_message_event(message, direction="delivered")

    assert event.event_type == "chat.message.delivery_confirmed"
    assert event.metadata["actor_id"] == "api-actor-1"
    assert event.metadata["provider_receipt"]["message_id"] == "provider-msg-1"
    assert event.source_instance_id == "chat-instance-1"
    assert event.causation_id == "activity-1"
    assert event.correlation_id == "turn-1"
    assert event.metadata["conscious_activity_lineage"] == {
        "conscious_activity_id": "activity-1",
        "tool_call_id": "call-1",
        "origin_turn_key": "turn-1",
        "origin_stream_id": "feishu:private:user-1",
    }


def test_failed_and_unknown_delivery_are_not_confirmed() -> None:
    failed = build_chat_message_event(
        _message(),
        direction="delivered",
        delivery_status="failed",
    )
    unknown = build_chat_message_event(
        _message(),
        direction="delivered",
        delivery_status="unknown",
    )

    assert failed.event_type == "chat.message.delivery_failed"
    assert unknown.event_type == "chat.message.delivery_unknown"


def test_notice_mapping_and_occurrence_are_stable() -> None:
    first = build_chat_message_event(_message(notice=True), direction="received")
    second = build_chat_message_event(_message(notice=True), direction="received")

    assert first.event_type == "chat.message.recalled"
    assert first.occurrence_id == second.occurrence_id


def test_napcat_notice_families_keep_distinct_durable_facts() -> None:
    expected = {
        ("notify", "poke"): "chat.poke.received",
        ("friend_recall", ""): "chat.message.recalled",
        ("group_msg_emoji_like", ""): "chat.reaction.added",
        ("group_increase", ""): "chat.member.joined",
        ("group_decrease", ""): "chat.member.left",
    }

    for (notice_type, sub_type), event_type in expected.items():
        raw = {
            "message_info": {
                "message_id": f"notice-{notice_type}-{sub_type}",
                "message_type": "notice",
                "time": datetime(2026, 8, 4, 8, 5, tzinfo=UTC).timestamp(),
                "platform": "qq",
                "user_info": {"user_id": "10001", "user_nickname": "QQ User"},
                "group_info": {"group_id": "20001"},
                "extra": {
                    "notice_type": notice_type,
                    "sub_type": sub_type,
                    "message_id": "provider-message-1",
                    "provider_raw_identity": {
                        "notice_type": notice_type,
                        "sub_type": sub_type,
                        "message_id": "provider-message-1",
                    },
                },
            }
        }

        event = build_chat_provider_notice_event(raw)

        assert event.event_type == event_type
        assert event.metadata["chat"]["reply_to"] == "provider-message-1"
        assert event.metadata["provider_identity"]["message_id"] == "provider-message-1"


def test_open_provider_notice_keeps_raw_identity_without_whole_payload() -> None:
    raw = {
        "message_info": {
            "message_id": "notice-1",
            "message_type": "notice",
            "time": datetime(2026, 8, 4, 8, 5, tzinfo=UTC).timestamp(),
            "platform": "qq",
            "user_info": {"user_id": "10001", "user_nickname": "QQ User"},
            "group_info": {"group_id": "20001"},
            "extra": {
                "notice_type": "notify",
                "sub_type": "input_status",
                "text_description": "用户输入状态发生变化",
                "provider_raw_identity": {
                    "notice_type": "notify",
                    "sub_type": "input_status",
                    "user_id": 10001,
                    "group_id": 20001,
                },
            },
        }
    }

    event = build_chat_provider_notice_event(
        raw,
        adapter_signature="napcat_adapter:adapter:qq",
    )

    assert event.event_type == "chat.provider_notice.received"
    assert event.stream_id == "qq:group:20001"
    assert event.metadata["provider_notice"]["kind"] == "notify"
    assert event.metadata["provider_notice"]["sub_type"] == "input_status"
    assert event.metadata["provider_identity"]["group_id"] == 20001
