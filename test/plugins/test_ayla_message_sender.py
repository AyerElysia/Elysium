"""Ayla 出站 MessageSender 识别契约测试。

覆盖文档 `docs/architecture/Elysium接入Ayla平台模块.md` §3.5/§8.1：
- `_infer_adapter_signature` 在全局 registry 注册 `AylaAdapter` 后，
  对 `platform="ayla"` 的消息命中 `ayla_adapter:adapter:ayla_adapter`；
- ayla 不进入 virtual send 集合（走真实 Adapter 出站）；
- `send_message` 出站成功（虚拟确认），不再「未找到匹配 Adapter」。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from src.core.components.types import ComponentType
from src.core.models.message import Message, MessageType
from src.core.transport.message_send.message_sender import MessageSender
from plugins.ayla_adapter.plugin import AylaAdapter

_SIGNATURE = "ayla_adapter:adapter:ayla_adapter"


def _registry_with_ayla() -> Any:
    return SimpleNamespace(
        get_by_type=lambda component_type: (
            {_SIGNATURE: AylaAdapter}
            if component_type == ComponentType.ADAPTER
            else {}
        )
    )


def test_infer_adapter_signature_hits_ayla_when_registered() -> None:
    sender = MessageSender()
    message = Message(
        message_id="m1",
        content="hello",
        processed_plain_text="hello",
        message_type=MessageType.TEXT,
        platform="ayla",
        chat_type="private",
        stream_id="stream-ayla",
    )
    with patch(
        "src.core.components.registry.get_global_registry",
        return_value=_registry_with_ayla(),
    ):
        signature = sender._infer_adapter_signature(message)

    assert signature == _SIGNATURE


def test_infer_adapter_signature_finds_other_platforms_still() -> None:
    sender = MessageSender()
    message = Message(
        message_id="m1",
        content="hello",
        processed_plain_text="hello",
        message_type=MessageType.TEXT,
        platform="feishu",
        chat_type="private",
        stream_id="stream-f",
    )
    with patch(
        "src.core.components.registry.get_global_registry",
        return_value=SimpleNamespace(get_by_type=lambda _t: {}),
    ):
        signature = sender._infer_adapter_signature(message)

    assert signature is None


def test_ayla_platform_does_not_use_virtual_send() -> None:
    """ayla 走真实 Adapter 出站，不进入 virtual 平台集合。"""
    message = Message(
        message_id="m1",
        content="hello",
        processed_plain_text="hello",
        message_type=MessageType.TEXT,
        platform="ayla",
        chat_type="private",
        stream_id="stream-ayla",
    )
    assert MessageSender._should_use_virtual_send(message) is False


async def test_send_message_acknowledges_on_ayla_stream(
    monkeypatch,
) -> None:
    """ayla 流出站经真实 AylaAdapter 虚拟确认返回 True（不再 ConnectError）。"""
    adapter = SimpleNamespace(
        get_bot_info=AsyncMock(
            return_value={"bot_id": "elysia", "bot_name": "爱莉", "platform": "ayla"}
        ),
        _send_platform_message=AsyncMock(return_value=None),
    )
    manager = SimpleNamespace(get_adapter=lambda _sig: adapter)
    stream_manager = SimpleNamespace(
        get_or_create_stream=AsyncMock(return_value=SimpleNamespace()),
        add_sent_message_to_history=AsyncMock(),
        get_stream_info=AsyncMock(return_value={}),
    )
    event_manager = SimpleNamespace(
        publish_event=AsyncMock(
            return_value={"params": {"continue_send": True}, "decision": None}
        )
    )

    monkeypatch.setattr(
        "src.core.managers.adapter_manager.get_adapter_manager",
        lambda: manager,
    )
    monkeypatch.setattr(
        "src.core.managers.stream_manager.get_stream_manager",
        lambda: stream_manager,
    )
    monkeypatch.setattr(
        "src.core.managers.event_manager.get_event_manager",
        lambda: event_manager,
    )
    monkeypatch.setattr(
        "src.core.transport.multi_writer_hooks.invoke_outbox_intent_hook",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "src.core.transport.multi_writer_hooks.invoke_outbox_settle_hook",
        AsyncMock(return_value=None),
    )

    sender = MessageSender()
    sender.set_adapter_manager(manager)
    message = Message(
        message_id="m1",
        content="你好",
        processed_plain_text="你好",
        message_type=MessageType.TEXT,
        platform="ayla",
        chat_type="private",
        stream_id="stream-ayla",
    )

    with patch(
        "src.core.components.registry.get_global_registry",
        return_value=_registry_with_ayla(),
    ):
        result = await sender.send_message(message)

    assert result is True
    adapter._send_platform_message.assert_awaited_once()
    adapter.get_bot_info.assert_awaited_once()
    stream_manager.add_sent_message_to_history.assert_awaited_once()
