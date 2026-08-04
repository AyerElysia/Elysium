"""Authenticated, correlated WebSocket client for Minecraft body bridges."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from websockets.asyncio.client import ClientConnection, connect
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .embodiment_contracts import (
    ActionCommand,
    ActionReceipt,
    WorldObservation,
)

BRIDGE_PROTOCOL = "elysium.minecraft.bridge/1"


class BridgeProtocolError(RuntimeError):
    """Raised when a peer violates the bridge protocol."""


class BridgeDisconnectedError(ConnectionError):
    """Raised when an operation requires a connected bridge."""


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    """Explicit connection and transport lifetime settings."""

    uri: str
    token: str
    listen_uri: str | None = None
    expected_instance_id: str | None = None
    open_timeout_seconds: float = 10.0
    acknowledgement_timeout_seconds: float = 2.0
    observation_timeout_seconds: float = 10.0
    max_in_flight: int = 8

    def __post_init__(self) -> None:
        """Validate transport configuration without exposing the token."""

        if not self.uri.strip():
            raise ValueError("bridge uri must not be empty")
        if self.listen_uri is not None:
            parsed = urlsplit(self.listen_uri)
            if parsed.scheme != "ws" or not parsed.hostname or parsed.port is None:
                raise ValueError("listen_uri must be an explicit ws://host:port URI")
        if not self.token:
            raise ValueError("bridge token must not be empty")
        if self.max_in_flight < 1:
            raise ValueError("max_in_flight must be positive")
        for value in (
            self.open_timeout_seconds,
            self.acknowledgement_timeout_seconds,
            self.observation_timeout_seconds,
        ):
            if value <= 0:
                raise ValueError("bridge timeouts must be positive")


@dataclass(slots=True)
class _PendingCommand:
    """Futures associated with one correlated bridge command."""

    acknowledged: asyncio.Future[ActionReceipt]
    terminal: asyncio.Future[ActionReceipt]


class MinecraftBridgeClient:
    """Persistent bridge connection with authentication and backpressure."""

    def __init__(self, config: BridgeConfig) -> None:
        """Create a disconnected bridge client."""

        self._config = config
        self._socket: ClientConnection | ServerConnection | None = None
        self._server: Server | None = None
        self._accept_ready: (
            asyncio.Future[
                tuple[ServerConnection, str, tuple[str, ...], dict[str, Any]]
            ]
            | None
        ) = None
        self._receiver: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._observation_changed = asyncio.Condition()
        self._in_flight = asyncio.Semaphore(config.max_in_flight)
        self._pending: dict[str, _PendingCommand] = {}
        self._latest_observation: WorldObservation | None = None
        self._instance_id: str | None = None
        self._capabilities: tuple[str, ...] = ()
        self._hello_metadata: dict[str, Any] = {}
        self._failure: BaseException | None = None

    @property
    def connected(self) -> bool:
        """Return whether an authenticated receiver is active."""

        return (
            self._socket is not None
            and self._receiver is not None
            and not self._receiver.done()
        )

    @property
    def instance_id(self) -> str | None:
        """Return the authenticated game instance identifier."""

        return self._instance_id

    @property
    def capabilities(self) -> tuple[str, ...]:
        """Return operations advertised by the connected body."""

        return self._capabilities

    @property
    def hello_metadata(self) -> dict[str, Any]:
        """Return non-secret authenticated body metadata from the hello frame."""

        return dict(self._hello_metadata)

    async def open(self) -> None:
        """Connect, authenticate a nonce, and start the receive loop."""

        if self.connected:
            return
        self._failure = None
        if self._config.listen_uri is not None:
            await self._open_listener()
            return
        async with asyncio.timeout(self._config.open_timeout_seconds):
            socket = await connect(
                self._config.uri,
                max_size=16 * 1024 * 1024,
                ping_interval=10,
                ping_timeout=10,
            )
            try:
                instance_id, capabilities, metadata = await self._authenticate(socket)
            except BaseException:
                await socket.close()
                raise

        self._socket = socket
        self._instance_id = instance_id
        self._capabilities = capabilities
        self._hello_metadata = metadata
        self._receiver = asyncio.create_task(
            self._receive_loop(socket),
            name=f"minecraft_bridge_receive:{instance_id}",
        )

    async def _open_listener(self) -> None:
        """Accept a reverse-connected Windows relay without a firewall rule."""

        listen_uri = self._config.listen_uri
        if listen_uri is None:
            raise RuntimeError("reverse listener URI is absent")
        parsed = urlsplit(listen_uri)
        expected_path = parsed.path or "/"
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[
            tuple[ServerConnection, str, tuple[str, ...], dict[str, Any]]
        ] = loop.create_future()
        self._accept_ready = ready

        async def handler(socket: ServerConnection) -> None:
            """Authenticate one reverse relay and hold its server handler open."""

            request_path = socket.request.path.split("?", maxsplit=1)[0]
            if request_path != expected_path:
                await socket.close(code=1008, reason="bridge path mismatch")
                return
            if ready.done() or self._socket is not None:
                await socket.close(code=1013, reason="controller lease is occupied")
                return
            try:
                instance_id, capabilities, metadata = await self._authenticate(socket)
            except asyncio.CancelledError:
                raise
            except Exception as exception:  # noqa: BLE001 - auth boundary rejects all failures
                if not ready.done():
                    ready.set_exception(exception)
                await socket.close(code=1008, reason="bridge authentication failed")
                return
            if not ready.done():
                ready.set_result((socket, instance_id, capabilities, metadata))
            await socket.wait_closed()

        self._server = await serve(
            handler,
            parsed.hostname,
            parsed.port,
            max_size=16 * 1024 * 1024,
            ping_interval=10,
            ping_timeout=10,
        )
        try:
            async with asyncio.timeout(self._config.open_timeout_seconds):
                socket, instance_id, capabilities, metadata = await ready
        except BaseException:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            self._accept_ready = None
            raise
        self._socket = socket
        self._instance_id = instance_id
        self._capabilities = capabilities
        self._hello_metadata = metadata
        self._receiver = asyncio.create_task(
            self._receive_loop(socket),
            name=f"minecraft_reverse_bridge_receive:{instance_id}",
        )

    async def _authenticate(
        self,
        socket: ClientConnection | ServerConnection,
    ) -> tuple[str, tuple[str, ...], dict[str, Any]]:
        """Authenticate a body peer regardless of WebSocket connection direction."""

        hello = self._decode(await socket.recv())
        self._validate_hello(hello)
        nonce = str(hello["nonce"])
        instance_id = str(hello["instance_id"])
        digest = hmac.new(
            self._config.token.encode("utf-8"),
            nonce.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        await socket.send(
            self._encode(
                {
                    "type": "authenticate",
                    "protocol": BRIDGE_PROTOCOL,
                    "digest": digest,
                }
            )
        )
        response = self._decode(await socket.recv())
        if response.get("type") != "authentication" or not response.get("accepted"):
            raise BridgeProtocolError("bridge authentication was rejected")
        capabilities = tuple(str(item) for item in hello.get("capabilities", []))
        metadata = {
            key: hello[key]
            for key in (
                "body_type",
                "bridge_version",
                "minecraft_version",
                "neoforge_version",
            )
            if key in hello
        }
        return instance_id, capabilities, metadata

    async def close(self) -> None:
        """Request control release, close the socket, and fail pending work."""

        socket = self._socket
        receiver = self._receiver
        server = self._server
        self._socket = None
        self._receiver = None
        self._server = None
        self._accept_ready = None
        close_errors: list[Exception] = []
        if socket is not None:
            try:
                await self._send_on(
                    socket, {"type": "release_all", "reason": "client closing"}
                )
            except ConnectionClosed:
                # A crashed or normally exiting game may close the peer first. At
                # that point it can no longer retain controls, so cleanup remains
                # successful and, importantly, retry-safe.
                pass
            except Exception as exc:  # noqa: BLE001 - finish all cleanup first
                close_errors.append(exc)
            try:
                await socket.close()
            except ConnectionClosed:
                pass
            except Exception as exc:  # noqa: BLE001 - finish all cleanup first
                close_errors.append(exc)
        if receiver is not None and receiver is not asyncio.current_task():
            receiver.cancel()
            try:
                await receiver
            except asyncio.CancelledError:
                pass
        if server is not None:
            server.close()
            await server.wait_closed()
        self._fail_pending(BridgeDisconnectedError("bridge closed"))
        if close_errors:
            raise ExceptionGroup("Minecraft bridge cleanup failed", close_errors)

    async def observe(self, after_sequence: int | None = None) -> WorldObservation:
        """Wait for a complete observation newer than the requested sequence."""

        self._require_connected()

        def available() -> bool:
            observation = self._latest_observation
            if observation is None:
                return False
            return after_sequence is None or observation.sequence > after_sequence

        async with asyncio.timeout(self._config.observation_timeout_seconds):
            async with self._observation_changed:
                await self._observation_changed.wait_for(
                    lambda: available() or self._failure is not None
                )
        if self._failure is not None:
            raise BridgeDisconnectedError(
                f"bridge receiver failed: {self._failure}"
            ) from self._failure
        observation = self._latest_observation
        if observation is None:
            raise BridgeProtocolError("observation notification had no payload")
        return observation

    async def act(self, command: ActionCommand) -> ActionReceipt:
        """Send one command, require a quick acknowledgement, then await completion."""

        self._require_connected()
        loop = asyncio.get_running_loop()
        pending = _PendingCommand(
            acknowledged=loop.create_future(),
            terminal=loop.create_future(),
        )
        async with self._in_flight:
            if command.command_id in self._pending:
                raise ValueError(f"command already pending: {command.command_id}")
            self._pending[command.command_id] = pending
            try:
                await self._send(
                    {
                        "type": "command",
                        "protocol": BRIDGE_PROTOCOL,
                        "command": command.to_wire(),
                    }
                )
                async with asyncio.timeout(
                    self._config.acknowledgement_timeout_seconds
                ):
                    acknowledgement = await pending.acknowledged
                if not acknowledgement.accepted or acknowledgement.terminal:
                    return acknowledgement
                if command.timeout_seconds is None:
                    return await pending.terminal
                async with asyncio.timeout(command.timeout_seconds):
                    return await pending.terminal
            finally:
                self._pending.pop(command.command_id, None)

    async def interrupt(self, intent_id: str, reason: str) -> None:
        """Ask the body to stop one intention and release every held control."""

        self._require_connected()
        await self._send(
            {
                "type": "interrupt",
                "protocol": BRIDGE_PROTOCOL,
                "intent_id": intent_id,
                "reason": reason,
            }
        )

    async def _receive_loop(
        self,
        socket: ClientConnection | ServerConnection,
    ) -> None:
        """Route all server messages to observations or command futures."""
        try:
            async for raw in socket:
                message = self._decode(raw)
                message_type = message.get("type")
                if message_type == "observation":
                    await self._accept_observation(message)
                elif message_type == "receipt":
                    self._accept_receipt(message)
                elif message_type == "heartbeat":
                    continue
                else:
                    raise BridgeProtocolError(
                        f"unexpected bridge message type: {message_type!r}"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport failure must wake all waiters
            self._failure = exc
            self._fail_pending(exc)
            async with self._observation_changed:
                self._observation_changed.notify_all()
        else:
            self._failure = BridgeDisconnectedError("bridge peer disconnected")
            self._fail_pending(self._failure)
            async with self._observation_changed:
                self._observation_changed.notify_all()

    async def _accept_observation(self, message: Mapping[str, Any]) -> None:
        """Validate instance identity and monotonically increasing sequence."""

        observation = WorldObservation.from_wire(dict(message["observation"]))
        if observation.instance_id != self._instance_id:
            raise BridgeProtocolError("observation came from a different game instance")
        previous = self._latest_observation
        if previous is not None and observation.sequence != previous.sequence + 1:
            raise BridgeProtocolError(
                "observation sequence is not contiguous: "
                f"{previous.sequence} -> {observation.sequence}"
            )
        async with self._observation_changed:
            self._latest_observation = observation
            self._observation_changed.notify_all()

    def _accept_receipt(self, message: Mapping[str, Any]) -> None:
        """Correlate an acknowledgement or terminal receipt."""

        receipt = ActionReceipt.from_wire(dict(message["receipt"]))
        pending = self._pending.get(receipt.command_id)
        if pending is None:
            raise BridgeProtocolError(
                f"receipt has no pending command: {receipt.command_id}"
            )
        if not pending.acknowledged.done():
            pending.acknowledged.set_result(receipt)
        if receipt.terminal and not pending.terminal.done():
            pending.terminal.set_result(receipt)

    def _validate_hello(self, hello: Mapping[str, Any]) -> None:
        """Validate protocol and configured instance binding."""

        if hello.get("type") != "hello":
            raise BridgeProtocolError("first bridge message was not hello")
        if hello.get("protocol") != BRIDGE_PROTOCOL:
            raise BridgeProtocolError("bridge protocol version mismatch")
        for field_name in ("nonce", "instance_id"):
            if not str(hello.get(field_name) or "").strip():
                raise BridgeProtocolError(f"hello is missing {field_name}")
        expected = self._config.expected_instance_id
        if expected is not None and str(hello["instance_id"]) != expected:
            raise BridgeProtocolError(
                "connected game instance was not the configured one"
            )

    async def _send(self, message: Mapping[str, Any]) -> None:
        """Serialize one message on the live socket."""

        socket = self._socket
        if socket is None:
            raise BridgeDisconnectedError("bridge is not connected")
        await self._send_on(socket, message)

    async def _send_on(
        self,
        socket: ClientConnection | ServerConnection,
        message: Mapping[str, Any],
    ) -> None:
        """Serialize writes so frames cannot interleave."""

        async with self._send_lock:
            await socket.send(self._encode(message))

    def _require_connected(self) -> None:
        """Raise with the receiver failure as the causal exception."""

        if not self.connected:
            raise BridgeDisconnectedError("bridge is not connected") from self._failure

    def _fail_pending(self, exception: BaseException) -> None:
        """Wake every command waiter after a transport failure."""

        for pending in tuple(self._pending.values()):
            for future in (pending.acknowledged, pending.terminal):
                if not future.done():
                    future.set_exception(exception)

    @staticmethod
    def _encode(message: Mapping[str, Any]) -> str:
        """Encode one compact UTF-8 JSON text frame."""

        return json.dumps(message, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _decode(raw: str | bytes) -> dict[str, Any]:
        """Decode one JSON object and reject non-object payloads."""

        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise BridgeProtocolError("bridge message must be a JSON object")
        return payload
