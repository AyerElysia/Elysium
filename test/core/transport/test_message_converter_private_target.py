from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.models.message import Message, MessageType
from src.core.transport.message_receive.converter import MessageConverter


async def test_message_to_envelope_private_target_prefers_stream_person(monkeypatch: pytest.MonkeyPatch) -> None:
    """私聊发送时，当未显式提供 target_user_id，应优先使用 stream 的 person_id，而不是 sender(bot)。"""
    converter = MessageConverter()

    fake_stream_manager = SimpleNamespace(
        get_stream_info=AsyncMock(return_value={
            "person_id": "hash_person_888",
            "group_id": None,
            "group_name": None,
        })
    )

    helper = SimpleNamespace(
        person_crud=SimpleNamespace(
            get_by=AsyncMock(
                return_value=SimpleNamespace(
                    person_id="hash_person_888",
                    user_id="user-888",
                    nickname="Alice",
                    cardname="",
                )
            )
        )
    )

    monkeypatch.setattr(
        "src.core.managers.stream_manager.get_stream_manager",
        lambda: fake_stream_manager,
    )
    monkeypatch.setattr(
        "src.core.utils.user_query_helper.get_user_query_helper",
        lambda: helper,
    )

    message = Message(
        message_id="m2",
        content="hello",
        message_type=MessageType.TEXT,
        sender_id="bot-001",
        sender_name="NeoBot",
        platform="qq",
        chat_type="private",
        stream_id="stream-private-1",
    )

    envelope = await converter.message_to_envelope(message)

    message_info = envelope.get("message_info")
    assert isinstance(message_info, dict)
    user_info = message_info.get("user_info")
    assert isinstance(user_info, dict)
    assert user_info.get("user_id") == "user-888"
    assert user_info.get("user_nickname") == "NeoBot"
    fake_stream_manager.get_stream_info.assert_awaited_once_with("stream-private-1")
    helper.person_crud.get_by.assert_awaited_once_with(person_id="hash_person_888")


async def test_message_to_envelope_private_target_drops_internal_route_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内部 p-/g- 路由 key 不能作为平台私聊 user_id 传给适配器。"""
    converter = MessageConverter()

    fake_stream_manager = SimpleNamespace(get_stream_info=AsyncMock(return_value=None))
    monkeypatch.setattr(
        "src.core.managers.stream_manager.get_stream_manager",
        lambda: fake_stream_manager,
    )

    message = Message(
        message_id="m3",
        content="hello",
        message_type=MessageType.TEXT,
        sender_id="p-5750ede8",
        sender_name="NeoBot",
        platform="qq",
        chat_type="private",
        stream_id="stream-private-2",
        target_user_id="p-5750ede8",
    )

    envelope = await converter.message_to_envelope(message)

    message_info = envelope.get("message_info")
    assert isinstance(message_info, dict)
    user_info = message_info.get("user_info")
    assert isinstance(user_info, dict)
    assert user_info.get("user_id") is None


async def test_message_to_envelope_drops_bot_placeholder_sender_as_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bot 占位 id（feishu_bot）绝不能被兜底为私聊发送目标。

    回归：主动流（如 s-proactive）无真实目标时，若 sender_id 是
    feishu_bot 占位符，converter 兜底必须跳过它，否则会把消息
    发给 bot 自己（飞书 99992351 invalid open_id）。
    """
    converter = MessageConverter()
    fake_stream_manager = SimpleNamespace(get_stream_info=AsyncMock(return_value=None))
    monkeypatch.setattr(
        "src.core.managers.stream_manager.get_stream_manager",
        lambda: fake_stream_manager,
    )

    message = Message(
        message_id="m-bot-placeholder",
        content="hello",
        message_type=MessageType.TEXT,
        sender_id="feishu_bot",
        sender_name="爱莉",
        platform="feishu",
        chat_type="private",
        stream_id="s-proactive",
    )

    envelope = await converter.message_to_envelope(message)

    message_info = envelope.get("message_info")
    assert isinstance(message_info, dict)
    user_info = message_info.get("user_info")
    assert isinstance(user_info, dict)
    assert user_info.get("user_id") is None


async def test_message_to_envelope_keeps_real_platform_id_as_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实平台用户 id（非 bot 占位）仍可作私聊兜底目标。"""
    converter = MessageConverter()
    fake_stream_manager = SimpleNamespace(get_stream_info=AsyncMock(return_value=None))
    monkeypatch.setattr(
        "src.core.managers.stream_manager.get_stream_manager",
        lambda: fake_stream_manager,
    )

    message = Message(
        message_id="m-real-user",
        content="hello",
        message_type=MessageType.TEXT,
        sender_id="ou_da5c1234567890",
        sender_name="Alice",
        platform="feishu",
        chat_type="private",
        stream_id="stream-private-3",
    )

    envelope = await converter.message_to_envelope(message)

    message_info = envelope.get("message_info")
    assert isinstance(message_info, dict)
    user_info = message_info.get("user_info")
    assert isinstance(user_info, dict)
    assert user_info.get("user_id") == "ou_da5c1234567890"
