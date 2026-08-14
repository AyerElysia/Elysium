from __future__ import annotations

import asyncio
from typing import Any

import orjson
import pytest

from src.core.transport.wire import (
    AdapterBase,
    MessageBuilder,
    MessageEnvelope,
    UserRole,
    WebSocketAdapterOptions,
)


class _Sink:
    def __init__(self) -> None:
        self.incoming: list[MessageEnvelope] = []
        self.outgoing_handler: Any = None

    async def send(self, message: MessageEnvelope) -> None:
        self.incoming.append(message)

    async def send_many(self, messages: list[MessageEnvelope]) -> None:
        self.incoming.extend(messages)

    def set_outgoing_handler(self, handler: Any) -> None:
        self.outgoing_handler = handler

    def remove_outgoing_handler(self, handler: Any) -> None:
        if self.outgoing_handler == handler:
            self.outgoing_handler = None

    async def push_outgoing(self, envelope: MessageEnvelope) -> None:
        if self.outgoing_handler is not None:
            await self.outgoing_handler(envelope)

    async def close(self) -> None:
        return None


class _Adapter(AdapterBase):
    platform = "test"

    async def from_platform_message(self, raw: Any) -> MessageEnvelope | None:
        return raw


class _WebSocket:
    def __init__(self, incoming: list[bytes] | None = None) -> None:
        self.incoming = incoming or []
        self.sent: list[str | bytes] = []
        self.closed = False

    def __aiter__(self) -> _WebSocket:
        return self

    async def __anext__(self) -> bytes:
        if not self.incoming:
            raise StopAsyncIteration
        return self.incoming.pop(0)

    async def send(self, data: str | bytes) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


def test_message_builder_preserves_user_role_and_platform() -> None:
    envelope = (
        MessageBuilder()
        .direction("incoming")
        .platform("test")
        .from_user("user-1", role=UserRole.OWNER)
        .text("hello")
        .build()
    )

    assert envelope["message_info"]["platform"] == "test"
    assert envelope["message_info"]["user_info"] == {
        "platform": "test",
        "role": UserRole.OWNER,
        "user_id": "user-1",
    }
    assert envelope["message_segment"] == {"type": "text", "data": "hello"}


def test_default_websocket_codec_keeps_legacy_frame_shape() -> None:
    envelope = (
        MessageBuilder()
        .platform("test")
        .from_user("user-1", role=UserRole.MEMBER)
        .text("hello")
        .build()
    )

    encoded = AdapterBase._default_ws_encoder(envelope)
    decoded = orjson.loads(encoded)

    assert isinstance(encoded, bytes)
    assert decoded["type"] == "send"
    assert decoded["payload"]["message_info"]["user_info"]["role"] == "member"
    incoming = orjson.dumps({"type": "message", "payload": decoded["payload"]})
    assert AdapterBase._default_ws_parser(incoming) == decoded["payload"]


@pytest.mark.asyncio
async def test_outgoing_websocket_uses_configured_encoder() -> None:
    sink = _Sink()
    adapter = _Adapter(
        sink,
        WebSocketAdapterOptions(
            url="ws://127.0.0.1:1/messages",
            outgoing_encoder=lambda _envelope: b"encoded",
        ),
    )
    websocket = _WebSocket()
    adapter._ws = websocket

    await adapter.send_to_platform(
        MessageBuilder().platform("test").text("hello").build()
    )

    assert websocket.sent == [b"encoded"]


@pytest.mark.asyncio
async def test_handler_failure_is_observed_without_leaking_task(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _FailingAdapter(_Adapter):
        async def from_platform_message(self, raw: Any) -> MessageEnvelope | None:
            raise RuntimeError("boom")

    sink = _Sink()
    adapter = _FailingAdapter(sink)
    websocket = _WebSocket([orjson.dumps({"message_info": {}, "message_segment": {}})])
    adapter._ws = websocket

    await adapter._ws_listen_loop(
        WebSocketAdapterOptions(url="ws://127.0.0.1:1/messages")
    )
    await asyncio.gather(*tuple(adapter._ws_handler_tasks), return_exceptions=True)
    await asyncio.sleep(0)

    assert not adapter._ws_handler_tasks
    assert "WebSocket message handler failed" in caplog.text
