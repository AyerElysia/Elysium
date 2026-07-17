from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

from src.core.components.types import EventType
from src.core.models.message import Message, MessageType
from src.core.transport.message_send.message_sender import MessageSender


def _envelope(
    *,
    platform: str,
    target_user_id: str,
    segments: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "message_info": {
            "platform": platform,
            "user_info": {"user_id": target_user_id},
        },
        "message_segment": segments,
    }


def _make_stream_manager(order: list[str] | None = None) -> SimpleNamespace:
    async def get_or_create_stream(**_kwargs: object) -> SimpleNamespace:
        if order is not None:
            order.append("stream")
        return SimpleNamespace()

    async def add_sent_message_to_history(_message: Message) -> None:
        if order is not None:
            order.append("history")

    return SimpleNamespace(
        get_or_create_stream=AsyncMock(side_effect=get_or_create_stream),
        add_sent_message_to_history=AsyncMock(side_effect=add_sent_message_to_history),
    )


def _patch_stream_manager(
    monkeypatch: pytest.MonkeyPatch,
    stream_manager: SimpleNamespace,
) -> None:
    monkeypatch.setattr(
        "src.core.managers.stream_manager.get_stream_manager",
        lambda: stream_manager,
    )


def _patch_event_manager(
    monkeypatch: pytest.MonkeyPatch,
    event_manager: SimpleNamespace,
) -> None:
    monkeypatch.setattr(
        "src.core.managers.event_manager.get_event_manager",
        lambda: event_manager,
    )


def _message(
    message_id: str,
    *,
    content: str,
    platform: str = "qq",
    stream_id: str = "stream-1",
    target_user_id: str = "user-123",
    message_type: MessageType = MessageType.TEXT,
) -> Message:
    return Message(
        message_id=message_id,
        content=content,
        processed_plain_text=content if message_type == MessageType.TEXT else None,
        message_type=message_type,
        platform=platform,
        chat_type="private",
        stream_id=stream_id,
        target_user_id=target_user_id,
    )


def test_virtual_send_includes_operator_platforms() -> None:
    assert MessageSender._should_use_virtual_send(Message(platform="live"))
    assert MessageSender._should_use_virtual_send(Message(platform="game.sts2.operator"))
    assert MessageSender._should_use_virtual_send(Message(platform="game.minecraft.operator"))
    assert not MessageSender._should_use_virtual_send(Message(platform="qq"))


def test_timeout_detection_follows_httpx_exception_chain() -> None:
    read_timeout_type = type(
        "ReadTimeout",
        (Exception,),
        {"__module__": "httpx"},
    )
    wrapped = RuntimeError("wrapped transport error")
    wrapped.__cause__ = read_timeout_type("read timed out")

    assert MessageSender._is_timeout_exception(wrapped) is True
    assert MessageSender._is_timeout_exception(RuntimeError("send failed")) is False


@pytest.mark.asyncio
async def test_send_message_overrides_sender_with_bot_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = MessageSender()
    adapter = SimpleNamespace(
        get_bot_info=AsyncMock(return_value={"bot_id": "bot-001", "bot_name": "NeoBot"}),
        _send_platform_message=AsyncMock(return_value=None),
    )
    sender.set_adapter_manager(SimpleNamespace(get_adapter=lambda _sig: adapter))
    sender._converter = SimpleNamespace(  # type: ignore[assignment]
        message_to_envelope=AsyncMock(
            return_value=_envelope(
                platform="qq",
                target_user_id="user-123",
                segments=[{"type": "text", "data": "hello"}],
            )
        )
    )
    stream_manager = _make_stream_manager()
    event_manager = SimpleNamespace(publish_event=AsyncMock(return_value={"params": {}}))
    _patch_stream_manager(monkeypatch, stream_manager)
    _patch_event_manager(monkeypatch, event_manager)

    message = _message("m1", content="hello")
    message.sender_id = "user-123"
    message.sender_name = "User"

    assert await sender.send_message(message, adapter_signature="mock:adapter:qq") is True

    assert message.sender_id == "bot-001"
    assert message.sender_name == "NeoBot"
    assert message.sender_cardname == "NeoBot"
    adapter.get_bot_info.assert_awaited_once()
    adapter._send_platform_message.assert_awaited_once()
    stream_manager.get_or_create_stream.assert_awaited_once()
    stream_manager.add_sent_message_to_history.assert_awaited_once_with(message)
    assert [args.args[0] for args in event_manager.publish_event.await_args_list] == [
        EventType.ON_MESSAGE_SENT,
        EventType.ON_MESSAGE_DELIVERED,
    ]


@pytest.mark.asyncio
async def test_normal_duplicate_messages_are_both_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = MessageSender()
    adapter = SimpleNamespace(
        get_bot_info=AsyncMock(return_value={}),
        _send_platform_message=AsyncMock(return_value=None),
    )
    sender.set_adapter_manager(SimpleNamespace(get_adapter=lambda _sig: adapter))
    sender._converter = SimpleNamespace(  # type: ignore[assignment]
        message_to_envelope=AsyncMock(
            return_value=_envelope(
                platform="qq",
                target_user_id="user-123",
                segments=[{"type": "text", "data": "same payload"}],
            )
        )
    )
    stream_manager = _make_stream_manager()
    _patch_stream_manager(monkeypatch, stream_manager)
    _patch_event_manager(
        monkeypatch,
        SimpleNamespace(publish_event=AsyncMock(return_value={"params": {}})),
    )

    first = _message("normal-1", content="same payload")
    second = _message("normal-2", content="same payload")

    assert await sender.send_message(first, adapter_signature="mock:adapter:qq") is True
    assert await sender.send_message(second, adapter_signature="mock:adapter:qq") is True

    assert adapter._send_platform_message.await_count == 2
    assert stream_manager.add_sent_message_to_history.await_args_list == [
        call(first),
        call(second),
    ]


@pytest.mark.asyncio
async def test_send_timeout_suppresses_only_matching_immediate_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = MessageSender()
    adapter = SimpleNamespace(
        get_bot_info=AsyncMock(return_value={}),
        _send_platform_message=AsyncMock(side_effect=TimeoutError("read timed out")),
    )
    sender.set_adapter_manager(SimpleNamespace(get_adapter=lambda _sig: adapter))
    sender._converter = SimpleNamespace(  # type: ignore[assignment]
        message_to_envelope=AsyncMock(
            return_value=_envelope(
                platform="feishu",
                target_user_id="user-123",
                segments=[{"type": "text", "data": "same payload"}],
            )
        )
    )
    stream_manager = _make_stream_manager()
    event_manager = SimpleNamespace(publish_event=AsyncMock(return_value={"params": {}}))
    _patch_stream_manager(monkeypatch, stream_manager)
    _patch_event_manager(monkeypatch, event_manager)

    first = _message(
        "timeout-1",
        content="same payload",
        platform="feishu",
        stream_id="stream-timeout",
    )
    retry = _message(
        "timeout-2",
        content="same payload",
        platform="feishu",
        stream_id="stream-timeout",
    )

    assert await sender.send_message(first, adapter_signature="mock:adapter:feishu") is False
    assert await sender.send_message(retry, adapter_signature="mock:adapter:feishu") is True

    adapter._send_platform_message.assert_awaited_once()
    stream_manager.add_sent_message_to_history.assert_not_awaited()
    assert [args.args[0] for args in event_manager.publish_event.await_args_list] == [
        EventType.ON_MESSAGE_SENT,
        EventType.ON_MESSAGE_SENT,
    ]


@pytest.mark.asyncio
async def test_timeout_fingerprint_keeps_media_and_target_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = MessageSender()
    adapter = SimpleNamespace(
        get_bot_info=AsyncMock(return_value={}),
        _send_platform_message=AsyncMock(
            side_effect=[TimeoutError("read timed out"), None, None]
        ),
    )
    sender.set_adapter_manager(SimpleNamespace(get_adapter=lambda _sig: adapter))

    async def convert_media(message: Message) -> dict[str, object]:
        return _envelope(
            platform=message.platform,
            target_user_id=str(message.extra["target_user_id"]),
            segments=[{"type": "image", "data": message.content}],
        )

    sender._converter = SimpleNamespace(  # type: ignore[assignment]
        message_to_envelope=AsyncMock(side_effect=convert_media)
    )
    stream_manager = _make_stream_manager()
    _patch_stream_manager(monkeypatch, stream_manager)
    _patch_event_manager(
        monkeypatch,
        SimpleNamespace(publish_event=AsyncMock(return_value={"params": {}})),
    )

    first = _message(
        "image-timeout-1",
        content="image-a",
        platform="feishu",
        stream_id="stream-media",
        target_user_id="user-123",
        message_type=MessageType.IMAGE,
    )
    same_media_retry = _message(
        "image-timeout-2",
        content="image-a",
        platform="feishu",
        stream_id="stream-media",
        target_user_id="user-123",
        message_type=MessageType.IMAGE,
    )
    different_target = _message(
        "image-target-2",
        content="image-a",
        platform="feishu",
        stream_id="stream-media",
        target_user_id="user-456",
        message_type=MessageType.IMAGE,
    )
    different_media = _message(
        "image-media-2",
        content="image-b",
        platform="feishu",
        stream_id="stream-media",
        target_user_id="user-123",
        message_type=MessageType.IMAGE,
    )

    assert await sender.send_message(first, adapter_signature="mock:adapter:feishu") is False
    assert await sender.send_message(
        same_media_retry,
        adapter_signature="mock:adapter:feishu",
    ) is True
    assert await sender.send_message(
        different_target,
        adapter_signature="mock:adapter:feishu",
    ) is True
    assert await sender.send_message(
        different_media,
        adapter_signature="mock:adapter:feishu",
    ) is True

    assert adapter._send_platform_message.await_count == 3
    assert stream_manager.add_sent_message_to_history.await_args_list == [
        call(different_target),
        call(different_media),
    ]


@pytest.mark.asyncio
async def test_non_timeout_failure_does_not_suppress_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = MessageSender()
    adapter = SimpleNamespace(
        get_bot_info=AsyncMock(return_value={}),
        _send_platform_message=AsyncMock(side_effect=[RuntimeError("send failed"), None]),
    )
    sender.set_adapter_manager(SimpleNamespace(get_adapter=lambda _sig: adapter))
    sender._converter = SimpleNamespace(  # type: ignore[assignment]
        message_to_envelope=AsyncMock(
            return_value=_envelope(
                platform="qq",
                target_user_id="user-123",
                segments=[{"type": "text", "data": "retry payload"}],
            )
        )
    )
    stream_manager = _make_stream_manager()
    _patch_stream_manager(monkeypatch, stream_manager)
    _patch_event_manager(
        monkeypatch,
        SimpleNamespace(publish_event=AsyncMock(return_value={"params": {}})),
    )

    first = _message("failure-1", content="retry payload")
    retry = _message("failure-2", content="retry payload")

    assert await sender.send_message(first, adapter_signature="mock:adapter:qq") is False
    assert await sender.send_message(retry, adapter_signature="mock:adapter:qq") is True

    assert adapter._send_platform_message.await_count == 2
    stream_manager.add_sent_message_to_history.assert_awaited_once_with(retry)


@pytest.mark.asyncio
async def test_send_and_delivery_events_wrap_adapter_and_history_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    sender = MessageSender()

    async def send_to_adapter(_envelope: object) -> None:
        order.append("adapter")

    async def publish_event(
        event: EventType,
        params: dict[str, object],
    ) -> dict[str, object]:
        order.append(
            "sent" if event is EventType.ON_MESSAGE_SENT else "delivered"
        )
        return {"params": params}

    adapter = SimpleNamespace(
        get_bot_info=AsyncMock(return_value={}),
        _send_platform_message=AsyncMock(side_effect=send_to_adapter),
    )
    sender.set_adapter_manager(SimpleNamespace(get_adapter=lambda _sig: adapter))
    sender._converter = SimpleNamespace(  # type: ignore[assignment]
        message_to_envelope=AsyncMock(
            return_value=_envelope(
                platform="qq",
                target_user_id="user-123",
                segments=[{"type": "text", "data": "ordered"}],
            )
        )
    )
    stream_manager = _make_stream_manager(order)
    event_manager = SimpleNamespace(publish_event=AsyncMock(side_effect=publish_event))
    _patch_stream_manager(monkeypatch, stream_manager)
    _patch_event_manager(monkeypatch, event_manager)

    message = _message("ordered-1", content="ordered")

    assert await sender.send_message(message, adapter_signature="mock:adapter:qq") is True

    assert order == ["sent", "adapter", "stream", "history", "delivered"]
    assert [args.args[0] for args in event_manager.publish_event.await_args_list] == [
        EventType.ON_MESSAGE_SENT,
        EventType.ON_MESSAGE_DELIVERED,
    ]
    sent_params = event_manager.publish_event.await_args_list[0].args[1]
    delivered_params = event_manager.publish_event.await_args_list[1].args[1]
    assert sent_params["message"] is message
    assert sent_params["adapter_signature"] == "mock:adapter:qq"
    assert sent_params["continue_send"] is True
    assert delivered_params["message"] is message
    assert delivered_params["adapter_signature"] == "mock:adapter:qq"
    assert "continue_send" not in delivered_params


@pytest.mark.asyncio
async def test_failed_adapter_send_emits_only_pre_send_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = MessageSender()
    adapter = SimpleNamespace(
        get_bot_info=AsyncMock(return_value={}),
        _send_platform_message=AsyncMock(side_effect=RuntimeError("send failed")),
    )
    sender.set_adapter_manager(SimpleNamespace(get_adapter=lambda _sig: adapter))
    sender._converter = SimpleNamespace(  # type: ignore[assignment]
        message_to_envelope=AsyncMock(
            return_value=_envelope(
                platform="qq",
                target_user_id="user-123",
                segments=[{"type": "text", "data": "failed"}],
            )
        )
    )
    stream_manager = _make_stream_manager()
    event_manager = SimpleNamespace(publish_event=AsyncMock(return_value={"params": {}}))
    _patch_stream_manager(monkeypatch, stream_manager)
    _patch_event_manager(monkeypatch, event_manager)

    assert await sender.send_message(
        _message("failed-1", content="failed"),
        adapter_signature="mock:adapter:qq",
    ) is False

    stream_manager.add_sent_message_to_history.assert_not_awaited()
    event_manager.publish_event.assert_awaited_once()
    assert event_manager.publish_event.await_args.args[0] is EventType.ON_MESSAGE_SENT


@pytest.mark.asyncio
async def test_continue_send_false_intercepts_before_adapter_and_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = MessageSender()
    adapter = SimpleNamespace(
        get_bot_info=AsyncMock(return_value={}),
        _send_platform_message=AsyncMock(return_value=None),
    )
    sender.set_adapter_manager(SimpleNamespace(get_adapter=lambda _sig: adapter))
    sender._converter = SimpleNamespace(  # type: ignore[assignment]
        message_to_envelope=AsyncMock(
            return_value=_envelope(
                platform="qq",
                target_user_id="user-123",
                segments=[{"type": "text", "data": "blocked"}],
            )
        )
    )
    stream_manager = _make_stream_manager()

    async def intercept(
        event: EventType,
        params: dict[str, object],
    ) -> dict[str, object]:
        assert event is EventType.ON_MESSAGE_SENT
        return {"params": {**params, "continue_send": False}}

    event_manager = SimpleNamespace(publish_event=AsyncMock(side_effect=intercept))
    _patch_stream_manager(monkeypatch, stream_manager)
    _patch_event_manager(monkeypatch, event_manager)

    message = _message("blocked-1", content="blocked")

    assert await sender.send_message(message, adapter_signature="mock:adapter:qq") is True
    adapter._send_platform_message.assert_not_awaited()
    stream_manager.get_or_create_stream.assert_not_awaited()
    stream_manager.add_sent_message_to_history.assert_not_awaited()
    event_manager.publish_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_virtual_send_events_wrap_history_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    sender = MessageSender()
    sender._converter = SimpleNamespace(  # type: ignore[assignment]
        message_to_envelope=AsyncMock(
            return_value=_envelope(
                platform="live",
                target_user_id="user-123",
                segments=[{"type": "text", "data": "virtual"}],
            )
        )
    )
    stream_manager = _make_stream_manager(order)

    async def publish_event(
        event: EventType,
        params: dict[str, object],
    ) -> dict[str, object]:
        order.append(
            "sent" if event is EventType.ON_MESSAGE_SENT else "delivered"
        )
        return {"params": params}

    event_manager = SimpleNamespace(publish_event=AsyncMock(side_effect=publish_event))
    _patch_stream_manager(monkeypatch, stream_manager)
    _patch_event_manager(monkeypatch, event_manager)

    message = _message("virtual-1", content="virtual", platform="live")

    assert await sender.send_message(message) is True

    assert order == ["sent", "stream", "history", "delivered"]
    assert [args.args[0] for args in event_manager.publish_event.await_args_list] == [
        EventType.ON_MESSAGE_SENT,
        EventType.ON_MESSAGE_DELIVERED,
    ]
    _, params = event_manager.publish_event.await_args_list[1].args
    assert params["adapter_signature"] == "live_bridge:adapter:virtual_live"


@pytest.mark.asyncio
async def test_virtual_send_can_be_intercepted_before_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = MessageSender()
    sender._converter = SimpleNamespace(  # type: ignore[assignment]
        message_to_envelope=AsyncMock(
            return_value=_envelope(
                platform="live",
                target_user_id="user-123",
                segments=[{"type": "text", "data": "blocked virtual"}],
            )
        )
    )
    stream_manager = _make_stream_manager()

    async def intercept(
        _event: EventType,
        params: dict[str, object],
    ) -> dict[str, object]:
        return {"params": {**params, "continue_send": False}}

    event_manager = SimpleNamespace(publish_event=AsyncMock(side_effect=intercept))
    _patch_stream_manager(monkeypatch, stream_manager)
    _patch_event_manager(monkeypatch, event_manager)

    message = _message("virtual-blocked", content="blocked virtual", platform="live")

    assert await sender.send_message(message) is True
    stream_manager.get_or_create_stream.assert_not_awaited()
    stream_manager.add_sent_message_to_history.assert_not_awaited()
    event_manager.publish_event.assert_awaited_once()
    assert event_manager.publish_event.await_args.args[0] is EventType.ON_MESSAGE_SENT


@pytest.mark.asyncio
async def test_missing_stream_id_does_not_emit_delivery_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = MessageSender()
    adapter = SimpleNamespace(
        get_bot_info=AsyncMock(return_value={}),
        _send_platform_message=AsyncMock(return_value=None),
    )
    sender.set_adapter_manager(SimpleNamespace(get_adapter=lambda _sig: adapter))
    sender._converter = SimpleNamespace(  # type: ignore[assignment]
        message_to_envelope=AsyncMock(
            return_value=_envelope(
                platform="qq",
                target_user_id="user-123",
                segments=[{"type": "text", "data": "no history"}],
            )
        )
    )
    event_manager = SimpleNamespace(publish_event=AsyncMock(return_value={"params": {}}))
    _patch_event_manager(monkeypatch, event_manager)

    message = _message("missing-stream", content="no history", stream_id="")

    assert await sender.send_message(message, adapter_signature="mock:adapter:qq") is True
    adapter._send_platform_message.assert_awaited_once()
    event_manager.publish_event.assert_awaited_once()
    assert event_manager.publish_event.await_args.args[0] is EventType.ON_MESSAGE_SENT
