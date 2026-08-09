"""Connection lifecycle, reliability, and backpressure for N.E.K.O surfaces."""

from __future__ import annotations

import asyncio
import hmac
import os
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect

from src.core.components.base.service import BaseService
from src.kernel.logger import get_logger

from .protocol import (
    CLIENT_EVENT_TYPES,
    SCHEMA_VERSION,
    SurfaceEvent,
    SurfaceProtocolError,
)

logger = get_logger("NekoSurfaceGateway", color="#F5A6C8")

SurfaceInputHandler = Callable[[SurfaceEvent], Awaitable[None]]


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def _env_float(name: str, default: float, *, minimum: float = 0.1) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}


@dataclass(frozen=True, slots=True)
class SurfaceGatewayConfig:
    token: str
    queue_size: int = 128
    handshake_timeout: float = 10.0
    max_clients: int = 8
    dedupe_capacity: int = 4096
    dedupe_ttl: float = 600.0
    mirror_all: bool = False

    @classmethod
    def from_env(cls) -> SurfaceGatewayConfig:
        return cls(
            token=os.environ.get("NEKO_SURFACE_TOKEN", "").strip(),
            queue_size=_env_int("NEKO_SURFACE_QUEUE_SIZE", 128),
            handshake_timeout=_env_float("NEKO_SURFACE_HANDSHAKE_TIMEOUT", 10.0),
            max_clients=_env_int("NEKO_SURFACE_MAX_CLIENTS", 8),
            dedupe_capacity=_env_int("NEKO_SURFACE_DEDUPE_CAPACITY", 4096),
            dedupe_ttl=_env_float("NEKO_SURFACE_DEDUPE_TTL", 600.0),
            mirror_all=_env_flag("NEKO_SURFACE_MIRROR_ALL", False),
        )


@dataclass(frozen=True, slots=True)
class QueuePutResult:
    enqueued: bool
    dropped_event_id: str = ""


class QueueClosed(RuntimeError):
    pass


class BoundedEventQueue:
    """FIFO queue that sheds the oldest lowest-priority event when full."""

    def __init__(self, maxsize: int) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self.maxsize = maxsize
        self._items: list[SurfaceEvent] = []
        self._condition = asyncio.Condition()
        self._closed = False

    def __len__(self) -> int:
        return len(self._items)

    async def put(self, event: SurfaceEvent) -> QueuePutResult:
        async with self._condition:
            if self._closed:
                return QueuePutResult(False, event.event_id)

            dropped_event_id = ""
            if len(self._items) >= self.maxsize:
                lowest_priority = min(item.priority for item in self._items)
                if event.priority <= lowest_priority:
                    return QueuePutResult(False, event.event_id)
                drop_index = next(
                    index
                    for index, item in enumerate(self._items)
                    if item.priority == lowest_priority
                )
                dropped_event_id = self._items.pop(drop_index).event_id

            self._items.append(event)
            self._condition.notify(1)
            return QueuePutResult(True, dropped_event_id)

    async def get(self) -> SurfaceEvent:
        async with self._condition:
            while not self._items and not self._closed:
                await self._condition.wait()
            if not self._items:
                raise QueueClosed()
            return self._items.pop(0)

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()


class EventDeduplicator:
    """Bounded TTL cache used across reconnects to suppress repeated input."""

    def __init__(self, capacity: int, ttl: float) -> None:
        self.capacity = max(1, capacity)
        self.ttl = max(0.1, ttl)
        self._seen: OrderedDict[str, float] = OrderedDict()

    def remember(self, key: str, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        cutoff = current - self.ttl
        while self._seen:
            first_key, first_seen = next(iter(self._seen.items()))
            if first_seen >= cutoff:
                break
            self._seen.pop(first_key, None)

        if key in self._seen:
            self._seen.move_to_end(key)
            self._seen[key] = current
            return True

        self._seen[key] = current
        while len(self._seen) > self.capacity:
            self._seen.popitem(last=False)
        return False


def extract_access_token(websocket: WebSocket) -> str:
    authorization = str(websocket.headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    header_token = str(websocket.headers.get("x-neko-surface-token") or "").strip()
    if header_token:
        return header_token
    return str(websocket.query_params.get("token") or "").strip()


def token_is_valid(provided: str, expected: str) -> bool:
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


@dataclass(slots=True)
class SurfaceClientState:
    connection_id: str
    websocket: WebSocket
    surface_id: str
    character: str
    session_id: str
    queue: BoundedEventQueue
    connected_at: float = field(default_factory=time.time)
    next_sequence: int = 1
    last_received_sequence: int = 0
    last_acknowledged_sequence: int = 0
    dropped_events: int = 0
    pending_acks: dict[str, int] = field(default_factory=dict)
    client_state: dict[str, Any] = field(default_factory=dict)

    def allocate_sequence(self) -> int:
        value = self.next_sequence
        self.next_sequence += 1
        return value


class NekoSurfaceGateway:
    """Owns authenticated surface sessions without owning Neo's cognition."""

    def __init__(self, config: SurfaceGatewayConfig | None = None) -> None:
        self.config = config or SurfaceGatewayConfig.from_env()
        self._clients: dict[str, SurfaceClientState] = {}
        self._clients_lock = asyncio.Lock()
        self._input_handler: SurfaceInputHandler | None = None
        self._dedupe = EventDeduplicator(
            self.config.dedupe_capacity,
            self.config.dedupe_ttl,
        )

    def bind_input_handler(self, handler: SurfaceInputHandler | None) -> None:
        self._input_handler = handler

    @property
    def token_configured(self) -> bool:
        return bool(self.config.token)

    async def snapshot(self) -> dict[str, Any]:
        async with self._clients_lock:
            clients = list(self._clients.values())
        return {
            "schema_version": SCHEMA_VERSION,
            "token_configured": self.token_configured,
            "connected_clients": len(clients),
            "clients": [
                {
                    "surface_id": item.surface_id,
                    "character": item.character,
                    "session_id": item.session_id,
                    "queued_events": len(item.queue),
                    "dropped_events": item.dropped_events,
                    "connected_at": item.connected_at,
                }
                for item in clients
            ],
        }

    async def shutdown(self) -> None:
        async with self._clients_lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for state in clients:
            await state.queue.close()
            try:
                await state.websocket.close(code=1001)
            except (RuntimeError, ConnectionError) as exc:
                logger.debug(f"Surface shutdown close failed: {exc}")

    async def serve_authorized(
        self,
        websocket: WebSocket,
        *,
        expected_surface_id: str,
        input_enabled: bool,
        actor_id: str,
    ) -> None:
        """Serve a v1 connection after API ticket authentication.

        The existing gateway remains the owner of Surface queues and protocol
        lifecycle.  This entry point only binds the already-authenticated API
        grant to the first hello and temporarily restricts input dispatch.
        """
        del actor_id
        state: SurfaceClientState | None = None
        writer_task: asyncio.Task[None] | None = None
        try:
            raw_hello = await asyncio.wait_for(
                websocket.receive_json(), timeout=self.config.handshake_timeout
            )
            hello = SurfaceEvent.from_dict(raw_hello, allowed_types=CLIENT_EVENT_TYPES)
            if hello.type != "hello" or hello.surface_id != expected_surface_id:
                raise SurfaceProtocolError("surface_mismatch", "hello surface_id is not authorized")
            state = await self._register(websocket, hello)
            writer_task = asyncio.create_task(
                self._writer(state), name=f"neko_surface_authorized_writer:{expected_surface_id}"
            )
            await self._send_to(
                state,
                "ready",
                payload={
                    "accepted_schema": SCHEMA_VERSION,
                    "accepted_events": sorted(CLIENT_EVENT_TYPES - {"hello"})
                    if input_enabled
                    else ["ack", "playback.started", "playback.ended", "state"],
                    "queue_size": self.config.queue_size,
                },
                priority=9,
            )
            while True:
                raw = await websocket.receive_json()
                if not input_enabled:
                    event_type = str(raw.get("type") or "") if isinstance(raw, dict) else ""
                    if event_type in {"user.text", "user.transcript.final", "user.audio", "user.screen", "user.interaction"}:
                        await self._send_error(state, "surface_input_forbidden", "Surface ticket is observer-only")
                        continue
                await self._handle_client_event(state, raw)
        except TimeoutError:
            await websocket.close(code=4408)
        except (SurfaceProtocolError, WebSocketDisconnect):
            if state is not None:
                await self._send_error(state, "surface_protocol_error", "Surface protocol rejected the event")
        finally:
            if writer_task is not None:
                writer_task.cancel()
                try:
                    await writer_task
                except asyncio.CancelledError:
                    pass
            if state is not None:
                await self._unregister(state)

    async def connection_summaries(self, surface_id: str) -> list[dict[str, Any]]:
        async with self._clients_lock:
            clients = [item for item in self._clients.values() if item.surface_id == surface_id]
        return [
            {
                "connection_id": item.connection_id,
                "surface_id": item.surface_id,
                "character": item.character,
                "session_id": item.session_id,
                "connected_at": item.connected_at,
                "queued_events": len(item.queue),
                "dropped_events": item.dropped_events,
                "last_received_sequence": item.last_received_sequence,
                "last_acknowledged_sequence": item.last_acknowledged_sequence,
            }
            for item in clients
        ]

    async def disconnect_connection(self, surface_id: str, connection_id: str, *, reason: str) -> bool:
        del reason
        async with self._clients_lock:
            state = self._clients.get(connection_id)
            if state is None or state.surface_id != surface_id:
                return False
            self._clients.pop(connection_id, None)
        await state.queue.close()
        try:
            await state.websocket.close(code=1000)
        except (RuntimeError, ConnectionError) as exc:
            logger.debug(f"Surface disconnect close failed: {exc}")
        return True

    async def serve(self, websocket: WebSocket) -> None:
        provided_token = extract_access_token(websocket)
        await websocket.accept()
        if not token_is_valid(provided_token, self.config.token):
            await websocket.send_json(
                self._standalone_error("unauthorized", "invalid or missing surface token")
            )
            await websocket.close(code=4401)
            return

        state: SurfaceClientState | None = None
        writer_task: asyncio.Task[None] | None = None
        try:
            raw_hello = await asyncio.wait_for(
                websocket.receive_json(),
                timeout=self.config.handshake_timeout,
            )
            hello = SurfaceEvent.from_dict(raw_hello, allowed_types=CLIENT_EVENT_TYPES)
            if hello.type != "hello":
                raise SurfaceProtocolError("hello_required", "the first event must be hello")

            state = await self._register(websocket, hello)
            writer_task = asyncio.create_task(
                self._writer(state),
                name=f"neko_surface_writer:{state.surface_id}",
            )
            await self._send_to(
                state,
                "ready",
                payload={
                    "accepted_schema": SCHEMA_VERSION,
                    "accepted_events": sorted(CLIENT_EVENT_TYPES - {"hello"}),
                    "queue_size": self.config.queue_size,
                },
                priority=9,
            )

            while True:
                raw = await websocket.receive_json()
                await self._handle_client_event(state, raw)
        except TimeoutError:
            await websocket.send_json(
                self._standalone_error("handshake_timeout", "hello was not received in time")
            )
            await websocket.close(code=4408)
        except SurfaceProtocolError as exc:
            if state is None:
                await websocket.send_json(self._standalone_error(exc.code, exc.detail))
            else:
                await self._send_error(state, exc.code, exc.detail)
            await websocket.close(code=4400)
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"Surface connection failed: {exc}", exc_info=True)
            if state is not None:
                await self._send_error(state, "internal_error", "surface connection failed")
        finally:
            if writer_task is not None:
                writer_task.cancel()
                try:
                    await writer_task
                except asyncio.CancelledError:
                    pass
            if state is not None:
                await self._unregister(state)

    async def publish(
        self,
        event_type: str,
        *,
        payload: dict[str, Any] | None = None,
        turn_id: str = "",
        character: str = "",
        target_surface_id: str = "",
        priority: int = 5,
        origin: str = "neo",
    ) -> int:
        async with self._clients_lock:
            clients = list(self._clients.values())

        delivered = 0
        for state in clients:
            if target_surface_id and state.surface_id != target_surface_id:
                continue
            if character and state.character != character:
                continue
            result = await self._enqueue(
                state,
                event_type,
                payload=payload,
                turn_id=turn_id,
                priority=priority,
                origin=origin,
            )
            if result.enqueued:
                delivered += 1
        return delivered

    async def _register(self, websocket: WebSocket, hello: SurfaceEvent) -> SurfaceClientState:
        session_id = hello.session_id or str(uuid4())
        state = SurfaceClientState(
            connection_id=str(uuid4()),
            websocket=websocket,
            surface_id=hello.surface_id,
            character=hello.character,
            session_id=session_id,
            queue=BoundedEventQueue(self.config.queue_size),
            last_received_sequence=hello.sequence,
        )

        async with self._clients_lock:
            existing = [
                item
                for item in self._clients.values()
                if item.surface_id == state.surface_id
            ]
            effective_count = len(self._clients) - len(existing)
            if effective_count >= self.config.max_clients:
                raise SurfaceProtocolError("capacity", "surface client limit reached")
            for old in existing:
                self._clients.pop(old.connection_id, None)
            self._clients[state.connection_id] = state

        for old in existing:
            await old.queue.close()
            try:
                await old.websocket.close(code=1012)
            except RuntimeError as exc:
                logger.debug(f"Surface replacement close was already complete: {exc}")
        logger.info(f"Surface connected: id={state.surface_id} character={state.character}")
        return state

    async def _unregister(self, state: SurfaceClientState) -> None:
        async with self._clients_lock:
            self._clients.pop(state.connection_id, None)
        await state.queue.close()
        logger.info(f"Surface disconnected: id={state.surface_id}")

    async def _writer(self, state: SurfaceClientState) -> None:
        while True:
            try:
                event = await state.queue.get()
            except QueueClosed:
                return
            await state.websocket.send_json(event.to_dict())
            if event.type not in {"ack", "error", "ready", "state"}:
                state.pending_acks[event.event_id] = event.sequence

    async def _handle_client_event(
        self,
        state: SurfaceClientState,
        raw: dict[str, Any],
    ) -> None:
        try:
            event = SurfaceEvent.from_dict(raw, allowed_types=CLIENT_EVENT_TYPES)
            if event.type == "hello":
                raise SurfaceProtocolError("duplicate_hello", "hello is only valid once")
            if event.surface_id and event.surface_id != state.surface_id:
                raise SurfaceProtocolError("surface_mismatch", "surface_id changed mid-session")
            if event.session_id and event.session_id != state.session_id:
                raise SurfaceProtocolError("session_mismatch", "session_id changed mid-session")

            dedupe_key = f"{state.surface_id}:{event.event_id}"
            if self._dedupe.remember(dedupe_key):
                await self._send_ack(state, event, status="duplicate")
                return
            if event.sequence <= state.last_received_sequence:
                raise SurfaceProtocolError(
                    "out_of_order",
                    f"sequence {event.sequence} is not newer than {state.last_received_sequence}",
                )
            state.last_received_sequence = event.sequence

            if event.type == "ack":
                acknowledged_id = str(event.payload.get("event_id") or "")
                acknowledged_sequence = state.pending_acks.pop(acknowledged_id, 0)
                state.last_acknowledged_sequence = max(
                    state.last_acknowledged_sequence,
                    acknowledged_sequence,
                )
                return

            if event.type == "state":
                state.client_state.update(event.payload)
                await self._send_ack(state, event)
                return

            if event.type in {"playback.started", "playback.ended"}:
                state.client_state["playback"] = dict(event.payload)
                state.client_state["playback"]["event"] = event.type
                await self._send_ack(state, event)
                return

            if self._input_handler is None:
                raise SurfaceProtocolError("adapter_unavailable", "surface adapter is not ready")
            await self._input_handler(event)
            await self._send_ack(state, event)
        except SurfaceProtocolError as exc:
            await self._send_error(state, exc.code, exc.detail)
        except Exception:
            logger.exception("Surface input dispatch failed")
            await self._send_error(state, "dispatch_failed", "surface input was not accepted")

    async def _enqueue(
        self,
        state: SurfaceClientState,
        event_type: str,
        *,
        payload: dict[str, Any] | None = None,
        turn_id: str = "",
        priority: int = 5,
        origin: str = "neo",
    ) -> QueuePutResult:
        event = SurfaceEvent.create(
            event_type,
            sequence=state.allocate_sequence(),
            session_id=state.session_id,
            turn_id=turn_id,
            surface_id=state.surface_id,
            character=state.character,
            origin=origin,
            payload=payload,
            priority=priority,
        )
        result = await state.queue.put(event)
        if result.dropped_event_id:
            state.dropped_events += 1
            state.pending_acks.pop(result.dropped_event_id, None)
        if not result.enqueued:
            state.dropped_events += 1
        return result

    async def _send_to(
        self,
        state: SurfaceClientState,
        event_type: str,
        *,
        payload: dict[str, Any] | None = None,
        turn_id: str = "",
        priority: int = 5,
    ) -> None:
        await self._enqueue(
            state,
            event_type,
            payload=payload,
            turn_id=turn_id,
            priority=priority,
        )

    async def _send_ack(
        self,
        state: SurfaceClientState,
        event: SurfaceEvent,
        *,
        status: str = "accepted",
    ) -> None:
        await self._send_to(
            state,
            "ack",
            payload={"event_id": event.event_id, "sequence": event.sequence, "status": status},
            turn_id=event.turn_id,
            priority=9,
        )

    async def _send_error(
        self,
        state: SurfaceClientState,
        code: str,
        detail: str,
    ) -> None:
        await self._send_to(
            state,
            "error",
            payload={"code": code, "detail": detail},
            priority=9,
        )

    @staticmethod
    def _standalone_error(code: str, detail: str) -> dict[str, Any]:
        return SurfaceEvent.create(
            "error",
            sequence=0,
            origin="neo",
            payload={"code": code, "detail": detail},
            priority=9,
        ).to_dict()


class NekoSurfaceService(BaseService):
    """Public service API for expression, motion, and other presentation events."""

    service_name = "neko_surface_gateway"
    service_description = "Publish presentation events to connected N.E.K.O surfaces"
    version = "1.0.0"

    def __init__(self, plugin: Any) -> None:
        super().__init__(plugin)
        self.gateway: NekoSurfaceGateway = plugin.gateway

    async def publish(self, event_type: str, **kwargs: Any) -> int:
        return await self.gateway.publish(event_type, **kwargs)

    async def expression(
        self,
        name: str,
        *,
        intensity: float = 1.0,
        turn_id: str = "",
        character: str = "",
    ) -> int:
        return await self.gateway.publish(
            "presentation.expression",
            payload={"name": name, "intensity": float(intensity)},
            turn_id=turn_id,
            character=character,
            priority=7,
        )

    async def motion(
        self,
        name: str,
        *,
        loop: bool = False,
        turn_id: str = "",
        character: str = "",
    ) -> int:
        return await self.gateway.publish(
            "presentation.motion",
            payload={"name": name, "loop": bool(loop)},
            turn_id=turn_id,
            character=character,
            priority=7,
        )
