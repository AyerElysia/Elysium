"""Acknowledged, idempotent transport for an OBS/browser stage client."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Protocol

from src.kernel.logger import get_logger

from .domain import LIVESTREAM_PROTOCOL_VERSION, PlaybackReceipt, StageMessage
from .performance import AudioPacket

logger = get_logger("livestream.stage", display="直播舞台")


class StageUnavailableError(RuntimeError):
    """Raised when no primary output stage is connected."""


class StageProtocolError(RuntimeError):
    """Raised when a stage client violates the versioned protocol."""


class WebSocketLike(Protocol):
    """Narrow socket boundary used by the stage hub and unit tests."""

    async def send_json(self, data: Any) -> None: ...

    async def send_bytes(self, data: bytes) -> None: ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


@dataclass(slots=True)
class _StageClient:
    client_id: str
    socket: WebSocketLike
    connected_at: float = field(default_factory=time.time)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(slots=True)
class _PendingPlayback:
    client_id: str
    utterance_id: str
    chunk_id: str
    future: asyncio.Future[PlaybackReceipt]


class StageHub:
    """Own the primary stage and correlate real playback acknowledgements."""

    def __init__(
        self,
        *,
        receipt_cache_size: int = 1000,
        send_timeout_seconds: float = 5.0,
        max_clients: int = 4,
    ) -> None:
        if receipt_cache_size <= 0:
            raise ValueError("receipt_cache_size must be positive")
        if send_timeout_seconds <= 0:
            raise ValueError("send_timeout_seconds must be positive")
        if max_clients <= 0:
            raise ValueError("max_clients must be positive")
        self._clients: dict[str, _StageClient] = {}
        self._primary_client_id: str | None = None
        self._pending: dict[str, _PendingPlayback] = {}
        self._receipts: OrderedDict[str, PlaybackReceipt] = OrderedDict()
        self._receipt_cache_size = receipt_cache_size
        self._send_timeout_seconds = send_timeout_seconds
        self._max_clients = max_clients
        self._lock = asyncio.Lock()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def primary_client_id(self) -> str | None:
        return self._primary_client_id

    async def attach(
        self,
        client_id: str,
        socket: WebSocketLike,
        *,
        request_primary: bool = False,
    ) -> bool:
        """Attach an authenticated stage socket; return whether it is primary."""

        if not client_id.strip():
            raise ValueError("client_id must not be empty")
        async with self._lock:
            if client_id in self._clients:
                raise StageProtocolError(f"duplicate stage client id: {client_id}")
            if len(self._clients) >= self._max_clients:
                raise StageProtocolError("livestream stage capacity reached")
            if request_primary and self._primary_client_id is not None:
                raise StageProtocolError("a primary stage is already connected")
            self._clients[client_id] = _StageClient(client_id, socket)
            if request_primary and self._primary_client_id is None:
                self._primary_client_id = client_id
            return self._primary_client_id == client_id

    async def detach(self, client_id: str) -> None:
        """Detach one client and fail its unresolved output acknowledgements."""

        async with self._lock:
            client = self._clients.pop(client_id, None)
            if client is None:
                return
            if self._primary_client_id == client_id:
                self._primary_client_id = None
            affected = [
                (playback_id, pending)
                for playback_id, pending in self._pending.items()
                if pending.client_id == client_id
            ]
            for playback_id, pending in affected:
                if not pending.future.done():
                    pending.future.set_result(
                        PlaybackReceipt(
                            playback_id=playback_id,
                            utterance_id=pending.utterance_id,
                            chunk_id=pending.chunk_id,
                            outcome="failed",
                            detail="primary stage disconnected before acknowledgement",
                        )
                    )

    async def close(self) -> None:
        """Close all owned sockets and release pending callers."""

        clients = list(self._clients.values())
        for client in clients:
            await self.detach(client.client_id)
            try:
                await asyncio.wait_for(
                    client.socket.close(code=1001, reason="livestream stage stopped"),
                    timeout=self._send_timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    f"关闭直播舞台连接失败: client={client.client_id} error={exc}"
                )

    async def play(
        self,
        *,
        playback_id: str,
        utterance_id: str,
        chunk_id: str,
        text: str,
        audio: AudioPacket,
        cues: dict[str, str],
        timeout_seconds: float,
    ) -> PlaybackReceipt:
        """Offer one audio artifact and wait for the primary client's receipt."""

        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        async with self._lock:
            cached = self._receipts.get(playback_id)
            if cached is not None:
                self._receipts.move_to_end(playback_id)
                return cached
            client_id = self._primary_client_id
            client = self._clients.get(client_id or "")
            if client is None:
                raise StageUnavailableError("no primary livestream stage is connected")
            if playback_id in self._pending:
                raise StageProtocolError(
                    f"playback is already in progress: {playback_id}"
                )
            future = asyncio.get_running_loop().create_future()
            pending = _PendingPlayback(
                client_id=client.client_id,
                utterance_id=utterance_id,
                chunk_id=chunk_id,
                future=future,
            )
            self._pending[playback_id] = pending

        message = StageMessage(
            type="audio.offer",
            payload={
                "playback_id": playback_id,
                "utterance_id": utterance_id,
                "chunk_id": chunk_id,
                "text": text,
                "mime_type": audio.mime_type,
                "size_bytes": len(audio.content),
                "audio_sha256": audio.sha256,
                "cues": cues,
            },
        )
        phase = "send"
        try:
            await asyncio.wait_for(
                self._send_offer(client, message, audio.content),
                timeout=self._send_timeout_seconds,
            )
            phase = "acknowledgement"
            receipt = await asyncio.wait_for(
                asyncio.shield(pending.future),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            receipt = PlaybackReceipt(
                playback_id=playback_id,
                utterance_id=utterance_id,
                chunk_id=chunk_id,
                outcome="timed_out",
                detail=(
                    f"stage send exceeded {self._send_timeout_seconds:.1f}s"
                    if phase == "send"
                    else f"no playback acknowledgement within {timeout_seconds:.1f}s"
                ),
            )
            await self._send_interrupt(client, utterance_id, "playback acknowledgement timeout")
        finally:
            self._pending.pop(playback_id, None)

        self._cache_receipt(receipt)
        return receipt

    async def _send_offer(
        self,
        client: _StageClient,
        message: StageMessage,
        audio: bytes,
    ) -> None:
        async with client.send_lock:
            await client.socket.send_json(message.model_dump(mode="json"))
            await client.socket.send_bytes(audio)

    async def handle_message(self, client_id: str, data: dict[str, Any]) -> None:
        """Validate one stage acknowledgement from the socket receive loop."""

        if int(data.get("version", 0)) != LIVESTREAM_PROTOCOL_VERSION:
            raise StageProtocolError("unsupported stage protocol version")
        message_type = str(data.get("type", ""))
        if message_type in {"hello", "pong"}:
            return
        if message_type != "playback.receipt":
            raise StageProtocolError(f"unsupported stage message: {message_type}")
        if client_id != self._primary_client_id:
            raise StageProtocolError("only the primary stage may acknowledge playback")

        payload = data.get("payload")
        if not isinstance(payload, dict):
            raise StageProtocolError("playback receipt payload must be an object")
        try:
            receipt = PlaybackReceipt.model_validate(payload)
        except Exception as exc:
            raise StageProtocolError(f"invalid playback receipt: {exc}") from exc
        pending = self._pending.get(receipt.playback_id)
        if pending is None:
            cached = self._receipts.get(receipt.playback_id)
            if cached is not None and cached != receipt:
                raise StageProtocolError("conflicting duplicate playback receipt")
            return
        if (
            pending.client_id != client_id
            or pending.utterance_id != receipt.utterance_id
            or pending.chunk_id != receipt.chunk_id
        ):
            raise StageProtocolError("playback receipt correlation mismatch")
        if not pending.future.done():
            pending.future.set_result(receipt)

    async def interrupt(self, utterance_id: str, reason: str) -> None:
        client = self._clients.get(self._primary_client_id or "")
        if client is not None:
            await self._send_interrupt(client, utterance_id, reason)

    async def send_control(self, message_type: str, payload: dict[str, Any]) -> None:
        """Send one versioned non-audio cue to the primary stage."""

        client = self._clients.get(self._primary_client_id or "")
        if client is None:
            raise StageUnavailableError("no primary livestream stage is connected")
        message = StageMessage(type=message_type, payload=payload)
        await asyncio.wait_for(
            self._send_control(client, message),
            timeout=self._send_timeout_seconds,
        )

    @staticmethod
    async def _send_control(client: _StageClient, message: StageMessage) -> None:
        async with client.send_lock:
            await client.socket.send_json(message.model_dump(mode="json"))

    async def _send_interrupt(
        self,
        client: _StageClient,
        utterance_id: str,
        reason: str,
    ) -> None:
        message = StageMessage(
            type="playback.interrupt",
            payload={"utterance_id": utterance_id, "reason": reason},
        )
        try:
            await asyncio.wait_for(
                self._send_control(client, message),
                timeout=self._send_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                f"发送直播中断指令失败: client={client.client_id} error={exc}"
            )

    def _cache_receipt(self, receipt: PlaybackReceipt) -> None:
        existing = self._receipts.get(receipt.playback_id)
        if existing is not None and existing != receipt:
            raise StageProtocolError("playback identity received conflicting outcomes")
        self._receipts[receipt.playback_id] = receipt
        self._receipts.move_to_end(receipt.playback_id)
        while len(self._receipts) > self._receipt_cache_size:
            self._receipts.popitem(last=False)
