"""Elysium-owned message envelope and adapter transport contracts.

This module keeps the platform-facing message plane inside Elysium.  It is
deliberately small: adapters share one typed envelope, one builder, and the
HTTP/WebSocket lifecycle required by the current platform plugins.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Literal,
    NotRequired,
    Protocol,
    Required,
    TypedDict,
)
from urllib.parse import urlparse

import orjson
from aiohttp import web as aiohttp_web
from websockets.legacy import client as ws_client
from websockets.legacy import server as ws_server

logger = logging.getLogger("elysium.wire")

MessageDirection = Literal["incoming", "outgoing"]


class UserRole(Enum):
    OWNER = "owner"
    OPERATOR = "operator"
    BOT = "bot"
    MEMBER = "member"
    OTHER = "other"


class SegPayload(TypedDict, total=False):
    type: Required[str]
    data: Required[str | list["SegPayload"] | dict[str, Any]]
    translated_data: NotRequired[str | list["SegPayload"]]


class UserInfoPayload(TypedDict, total=False):
    platform: Required[str]
    role: Required[UserRole]
    user_id: Required[str]
    user_nickname: NotRequired[str]
    user_cardname: NotRequired[str]
    user_avatar: NotRequired[str]


class GroupInfoPayload(TypedDict, total=False):
    platform: Required[str]
    group_id: Required[str]
    group_name: Required[str]


class FormatInfoPayload(TypedDict, total=False):
    content_format: NotRequired[list[str]]
    accept_format: NotRequired[list[str]]


class TemplateInfoPayload(TypedDict, total=False):
    template_items: NotRequired[dict[str, str]]
    template_name: NotRequired[dict[str, str]]
    template_default: NotRequired[bool]


class MessageInfoPayload(TypedDict, total=False):
    platform: Required[str]
    message_id: Required[str]
    time: NotRequired[float]
    group_info: NotRequired[GroupInfoPayload]
    user_info: NotRequired[UserInfoPayload]
    format_info: NotRequired[FormatInfoPayload]
    template_info: NotRequired[TemplateInfoPayload]
    additional_config: NotRequired[dict[str, Any]]


class MessageEnvelope(TypedDict, total=False):
    direction: MessageDirection
    message_info: Required[MessageInfoPayload]
    message_segment: Required[SegPayload | list[SegPayload]]
    raw_message: NotRequired[Any]
    raw_bytes: NotRequired[bytes]
    message_chain: NotRequired[list[SegPayload]]
    platform: NotRequired[str]
    message_id: NotRequired[str]
    timestamp_ms: NotRequired[int]
    correlation_id: NotRequired[str]
    schema_version: NotRequired[int]
    metadata: NotRequired[dict[str, Any]]


class CoreSink(Protocol):
    async def send(self, message: MessageEnvelope) -> None: ...

    async def send_many(self, messages: list[MessageEnvelope]) -> None: ...

    def set_outgoing_handler(
        self,
        handler: Callable[[MessageEnvelope], Awaitable[None]] | None,
    ) -> None: ...

    def remove_outgoing_handler(
        self,
        handler: Callable[[MessageEnvelope], Awaitable[None]],
    ) -> None: ...

    async def push_outgoing(self, envelope: MessageEnvelope) -> None: ...

    async def close(self) -> None: ...


class WebSocketLike(Protocol):
    def __aiter__(self) -> AsyncIterator[str | bytes]: ...

    @property
    def closed(self) -> bool: ...

    async def send(self, data: str | bytes) -> None: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class WebSocketAdapterOptions:
    url: str
    headers: dict[str, str] | None = None
    incoming_parser: Callable[[str | bytes], Any] | None = None
    outgoing_encoder: Callable[[MessageEnvelope], str | bytes] | None = None
    mode: Literal["client", "server"] = "client"
    allowed_paths: list[str] | None = None
    reconnect_interval: float = 5.0
    max_reconnect_attempts: int | None = None
    max_message_size: int | None = 32 * 1024 * 1024


@dataclass(slots=True)
class HttpAdapterOptions:
    host: str = "0.0.0.0"
    port: int = 8089
    path: str = "/adapter/messages"
    app: aiohttp_web.Application | None = None


AdapterTransportOptions = WebSocketAdapterOptions | HttpAdapterOptions | None


async def _send_many(sink: CoreSink, messages: list[MessageEnvelope]) -> None:
    sender = getattr(sink, "send_many", None)
    if callable(sender):
        await sender(messages)
        return
    for message in messages:
        await sink.send(message)


class AdapterBase:
    """Transport lifecycle shared by Elysium platform adapters."""

    platform = "unknown"

    def __init__(
        self,
        core_sink: CoreSink,
        transport: AdapterTransportOptions = None,
    ) -> None:
        self.core_sink = core_sink
        self._transport_config = transport
        self._ws: WebSocketLike | None = None
        self._ws_task: asyncio.Task[None] | None = None
        self._ws_server: Any | None = None
        self._http_runner: aiohttp_web.AppRunner | None = None
        self._http_site: aiohttp_web.BaseSite | None = None
        self._closed = False
        self._reconnect_attempts = 0
        self._ws_handler_tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self._closed = False
        self._reconnect_attempts = 0
        register = getattr(self.core_sink, "set_outgoing_handler", None)
        if callable(register):
            try:
                register(self._on_outgoing_from_core)
            except Exception:
                logger.exception("Failed to register the adapter outgoing handler")
        if isinstance(self._transport_config, WebSocketAdapterOptions):
            if self._transport_config.mode == "server":
                await self._start_ws_server(self._transport_config)
            else:
                self._ws_task = asyncio.create_task(
                    self._ws_connect_loop(self._transport_config),
                    name=f"elysium_wire:{self.platform}",
                )
        elif isinstance(self._transport_config, HttpAdapterOptions):
            await self._start_http_transport(self._transport_config)

    async def stop(self) -> None:
        self._closed = True
        remove = getattr(self.core_sink, "remove_outgoing_handler", None)
        if callable(remove):
            try:
                remove(self._on_outgoing_from_core)
            except Exception:
                logger.exception("Failed to detach the adapter outgoing handler")
        else:
            register = getattr(self.core_sink, "set_outgoing_handler", None)
            if callable(register):
                try:
                    register(None)
                except Exception:
                    logger.exception("Failed to clear the adapter outgoing handler")

        if self._ws_task is not None:
            self._ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ws_task
            self._ws_task = None

        handlers = tuple(self._ws_handler_tasks)
        for task in handlers:
            task.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)
        self._ws_handler_tasks.clear()

        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        if self._ws_server is not None:
            self._ws_server.close()
            await self._ws_server.wait_closed()
            self._ws_server = None
        if self._http_site is not None:
            await self._http_site.stop()
            self._http_site = None
        if self._http_runner is not None:
            await self._http_runner.cleanup()
            self._http_runner = None

    def is_connected(self) -> bool:
        if isinstance(self._transport_config, WebSocketAdapterOptions):
            return self._ws is not None and not self._ws.closed
        if isinstance(self._transport_config, HttpAdapterOptions):
            return self._http_site is not None
        return False

    async def wait_connected(self, timeout: float = 10.0) -> bool:
        if not isinstance(self._transport_config, WebSocketAdapterOptions):
            return True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not self._closed:
            if self.is_connected():
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.1)
        return False

    async def on_platform_message(self, raw: Any) -> None:
        envelope = await self.from_platform_message(raw)
        if not envelope:
            return
        await self.core_sink.send(envelope)

    async def on_platform_messages(self, raw_messages: list[Any]) -> None:
        envelopes: list[MessageEnvelope] = []
        for raw in raw_messages:
            envelope = await self.from_platform_message(raw)
            if envelope:
                envelopes.append(envelope)
        if envelopes:
            await _send_many(self.core_sink, envelopes)

    async def send_to_platform(self, envelope: MessageEnvelope) -> None:
        await self._send_platform_message(envelope)

    async def send_batch_to_platform(self, envelopes: list[MessageEnvelope]) -> None:
        for envelope in envelopes:
            await self._send_platform_message(envelope)

    async def _on_outgoing_from_core(self, envelope: MessageEnvelope) -> None:
        platform = envelope.get("platform") or envelope.get("message_info", {}).get(
            "platform"
        )
        if platform and platform != self.platform:
            return
        await self._send_platform_message(envelope)

    async def from_platform_message(self, raw: Any) -> MessageEnvelope | None:
        raise NotImplementedError

    async def _send_platform_message(self, envelope: MessageEnvelope) -> None:
        if isinstance(self._transport_config, WebSocketAdapterOptions):
            await self._send_via_ws(envelope)
            return
        raise NotImplementedError

    async def _ws_connect_loop(self, options: WebSocketAdapterOptions) -> None:
        while not self._closed:
            try:
                self._ws = await ws_client.connect(
                    options.url,
                    extra_headers=options.headers,
                    max_size=options.max_message_size,
                )
                self._reconnect_attempts = 0
                await self._ws_listen_loop(options)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._closed:
                    return
                self._reconnect_attempts += 1
                maximum = options.max_reconnect_attempts
                if maximum is not None and self._reconnect_attempts > maximum:
                    logger.error("WebSocket reconnect attempts exhausted")
                    return
                logger.warning(
                    "WebSocket connection failed; retrying in %.1fs: %s",
                    options.reconnect_interval,
                    type(exc).__name__,
                )
                await asyncio.sleep(options.reconnect_interval)
            finally:
                if self._ws is not None and not self._ws.closed:
                    with contextlib.suppress(Exception):
                        await self._ws.close()
                self._ws = None

    async def _start_ws_server(self, options: WebSocketAdapterOptions) -> None:
        parsed = urlparse(options.url)
        host = parsed.hostname or "0.0.0.0"
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        expected_path = parsed.path or "/"

        async def handler(websocket: Any) -> None:
            if options.allowed_paths and websocket.path not in options.allowed_paths:
                await websocket.close(code=4000, reason="Path not allowed")
                return
            if websocket.path != expected_path:
                await websocket.close(code=4000, reason="Path mismatch")
                return
            self._ws = websocket
            await self._ws_listen_loop(options)

        self._ws_server = await ws_server.serve(
            handler,
            host,
            port,
            extra_headers=options.headers,
            max_size=options.max_message_size,
        )

    async def _ws_listen_loop(self, options: WebSocketAdapterOptions) -> None:
        websocket = self._ws
        if websocket is None:
            raise RuntimeError("WebSocket listener started without a connection")
        parser = options.incoming_parser or self._default_ws_parser
        try:
            async for raw in websocket:
                if self._closed:
                    return
                try:
                    payload = parser(raw)
                    task = asyncio.create_task(self.on_platform_message(payload))
                    self._ws_handler_tasks.add(task)
                    task.add_done_callback(self._ws_handler_tasks.discard)
                    task.add_done_callback(self._log_ws_handler_failure)
                except Exception:
                    logger.exception("Failed to decode a WebSocket message")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("WebSocket listen loop failed")
        finally:
            if self._ws is websocket:
                self._ws = None

    @staticmethod
    def _log_ws_handler_failure(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "WebSocket message handler failed: %s",
                type(error).__name__,
                exc_info=error,
            )

    async def _send_via_ws(self, envelope: MessageEnvelope) -> None:
        websocket = self._ws
        if websocket is None or websocket.closed:
            raise RuntimeError("WebSocket transport is not active")
        options = self._transport_config
        encoder = options.outgoing_encoder if isinstance(
            options, WebSocketAdapterOptions
        ) else None
        payload = encoder(envelope) if encoder else self._default_ws_encoder(envelope)
        await websocket.send(payload)

    async def _start_http_transport(self, options: HttpAdapterOptions) -> None:
        app = options.app or aiohttp_web.Application()
        app.add_routes([aiohttp_web.post(options.path, self._handle_http_request)])
        self._http_runner = aiohttp_web.AppRunner(app)
        await self._http_runner.setup()
        self._http_site = aiohttp_web.TCPSite(
            self._http_runner,
            options.host,
            options.port,
        )
        await self._http_site.start()

    async def _handle_http_request(
        self,
        request: aiohttp_web.Request,
    ) -> aiohttp_web.Response:
        raw = await request.read()
        payload = orjson.loads(raw) if raw else {}
        if isinstance(payload, list):
            await self.on_platform_messages(payload)
        else:
            await self.on_platform_message(payload)
        return aiohttp_web.json_response({"status": "ok"})

    @staticmethod
    def _default_ws_parser(raw: str | bytes) -> Any:
        payload = orjson.loads(raw)
        if (
            isinstance(payload, dict)
            and payload.get("type") == "message"
            and "payload" in payload
        ):
            return payload["payload"]
        return payload

    @staticmethod
    def _default_ws_encoder(envelope: MessageEnvelope) -> bytes:
        return orjson.dumps({"type": "send", "payload": envelope})


class MessageBuilder:
    """Fluent builder for the Elysium message envelope."""

    def __init__(self) -> None:
        self._direction: MessageDirection = "outgoing"
        self._message_info: MessageInfoPayload = {}
        self._segments: list[SegPayload] = []
        self._metadata: dict[str, Any] | None = None
        self._timestamp_ms: int | None = None
        self._message_id: str | None = None

    def direction(self, value: MessageDirection) -> "MessageBuilder":
        self._direction = value
        return self

    def message_id(self, value: str) -> "MessageBuilder":
        self._message_id = value
        return self

    def timestamp_ms(self, value: int | None = None) -> "MessageBuilder":
        self._timestamp_ms = value if value is not None else int(time.time() * 1000)
        return self

    def metadata(self, value: dict[str, Any]) -> "MessageBuilder":
        self._metadata = value
        return self

    def platform(self, value: str) -> "MessageBuilder":
        self._message_info["platform"] = value
        return self

    def from_user(
        self,
        user_id: str,
        *,
        platform: str | None = None,
        nickname: str | None = None,
        cardname: str | None = None,
        user_avatar: str | None = None,
        role: UserRole | None = UserRole.MEMBER,
    ) -> "MessageBuilder":
        if platform:
            self.platform(platform)
        info: UserInfoPayload = {"user_id": user_id}  # type: ignore[typeddict-item]
        if nickname:
            info["user_nickname"] = nickname
        if cardname:
            info["user_cardname"] = cardname
        if user_avatar:
            info["user_avatar"] = user_avatar
        if role is not None:
            info["role"] = role
        self._message_info["user_info"] = info
        return self

    def from_group(
        self,
        group_id: str,
        *,
        platform: str | None = None,
        name: str | None = None,
    ) -> "MessageBuilder":
        if platform:
            self.platform(platform)
        info: GroupInfoPayload = {"group_id": group_id}  # type: ignore[typeddict-item]
        if name:
            info["group_name"] = name
        self._message_info["group_info"] = info
        return self

    def seg(self, type_: str, data: Any) -> "MessageBuilder":
        self._segments.append({"type": type_, "data": data})
        return self

    def text(self, content: str) -> "MessageBuilder":
        return self.seg("text", content)

    def image(self, url: str) -> "MessageBuilder":
        return self.seg("image", url)

    def reply(self, target_message_id: str) -> "MessageBuilder":
        return self.seg("reply", target_message_id)

    def raw_segment(self, segment: SegPayload) -> "MessageBuilder":
        self._segments.append(segment)
        return self

    def format_info(
        self,
        content_format: list[str],
        accept_format: list[str],
    ) -> "MessageBuilder":
        self._message_info["format_info"] = {
            "content_format": content_format,
            "accept_format": accept_format,
        }
        return self

    def seg_list(self, segments: list[SegPayload]) -> "MessageBuilder":
        self._segments.extend(segments)
        return self

    def build(self) -> MessageEnvelope:
        if not self._segments:
            raise ValueError("at least one message segment is required")
        message_id = self._message_id or str(uuid.uuid4())
        info = dict(self._message_info)
        info.setdefault("message_id", message_id)
        info.setdefault("time", time.time())
        platform = info.get("platform")
        for nested_key in ("group_info", "user_info"):
            nested = info.get(nested_key)
            if platform and isinstance(nested, dict):
                nested.setdefault("platform", platform)
        segments = [dict(segment) for segment in self._segments]
        envelope: MessageEnvelope = {
            "direction": self._direction,
            "message_info": info,  # type: ignore[typeddict-item]
            "message_segment": segments[0] if len(segments) == 1 else segments,
        }
        if self._metadata is not None:
            envelope["metadata"] = dict(self._metadata)
        if self._timestamp_ms is not None:
            envelope["timestamp_ms"] = self._timestamp_ms
        return envelope


__all__ = [
    "AdapterBase",
    "AdapterTransportOptions",
    "CoreSink",
    "FormatInfoPayload",
    "GroupInfoPayload",
    "HttpAdapterOptions",
    "MessageBuilder",
    "MessageDirection",
    "MessageEnvelope",
    "MessageInfoPayload",
    "SegPayload",
    "TemplateInfoPayload",
    "UserInfoPayload",
    "UserRole",
    "WebSocketAdapterOptions",
]
