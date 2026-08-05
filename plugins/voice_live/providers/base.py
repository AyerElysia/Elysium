"""Strict provider contract for realtime speech-to-speech backends."""

from __future__ import annotations

import asyncio
import hashlib
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..protocol import ProviderState


@dataclass(slots=True, frozen=True)
class TranscriptEvent:
    role: str
    text: str
    is_final: bool = True
    event_id: str = ""


@dataclass(slots=True, frozen=True)
class AudioDelta:
    data: bytes
    sample_rate: int
    format: str = "pcm16"
    response_id: str = ""


@dataclass(slots=True, frozen=True)
class InterruptionEvent:
    source: str
    response_id: str = ""
    item_id: str = ""


@dataclass(slots=True, frozen=True)
class ToolCallEvent:
    call_id: str
    name: str
    arguments_json: str


@dataclass(slots=True, frozen=True)
class ProviderMetrics:
    values: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class RealtimeContextDeliveryReceipt:
    """Content-free proof that upstream stored one exact transient context."""

    item_ids: tuple[str, ...]
    exact: bool
    expected_utf8_bytes: int
    expected_sha256: str
    accepted_utf8_bytes: int | None
    accepted_sha256: str | None
    transport_event_ids: tuple[str, ...] = ()


@dataclass(slots=True)
class _PendingContextItemAck:
    expected_text: str = field(repr=False)
    future: asyncio.Future[tuple[str, str | None]] = field(repr=False)


AudioCallback = Callable[[AudioDelta], Awaitable[None]]
StateCallback = Callable[[ProviderState], Awaitable[None]]
TranscriptCallback = Callable[[TranscriptEvent], Awaitable[None]]
ErrorCallback = Callable[[str], Awaitable[None]]
InterruptionCallback = Callable[[InterruptionEvent], Awaitable[None]]
MetricsCallback = Callable[[ProviderMetrics], Awaitable[None]]
ToolCallCallback = Callable[[ToolCallEvent], Awaitable[None]]
ResponseDoneCallback = Callable[[bool], Awaitable[None]]


class BaseRealtimeProvider(ABC):
    """A provider must expose one deterministic model path and lifecycle."""

    provider_name = "base"
    input_sample_rate = 16000
    output_sample_rate = 24000

    def __init__(self) -> None:
        self._state = ProviderState.IDLE
        self._session_config: dict[str, Any] = {}
        self._audio_callbacks: list[AudioCallback] = []
        self._state_callbacks: list[StateCallback] = []
        self._transcript_callbacks: list[TranscriptCallback] = []
        self._error_callbacks: list[ErrorCallback] = []
        self._interruption_callbacks: list[InterruptionCallback] = []
        self._metrics_callbacks: list[MetricsCallback] = []
        self._tool_callbacks: list[ToolCallCallback] = []
        self._response_done_callbacks: list[ResponseDoneCallback] = []
        self._pending_context_item_acks: dict[str, _PendingContextItemAck] = {}

    @property
    def state(self) -> ProviderState:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state not in {ProviderState.IDLE, ProviderState.ERROR, ProviderState.CLOSED}

    def on_audio_delta(self, callback: AudioCallback) -> None:
        self._audio_callbacks.append(callback)

    def on_state_change(self, callback: StateCallback) -> None:
        self._state_callbacks.append(callback)

    def on_transcript(self, callback: TranscriptCallback) -> None:
        self._transcript_callbacks.append(callback)

    def on_error(self, callback: ErrorCallback) -> None:
        self._error_callbacks.append(callback)

    def on_interruption(self, callback: InterruptionCallback) -> None:
        self._interruption_callbacks.append(callback)

    def on_metrics(self, callback: MetricsCallback) -> None:
        self._metrics_callbacks.append(callback)

    def on_tool_call(self, callback: ToolCallCallback) -> None:
        self._tool_callbacks.append(callback)

    def on_response_done(self, callback: ResponseDoneCallback) -> None:
        """Register a model-turn completion callback."""

        self._response_done_callbacks.append(callback)

    async def _emit_audio(self, event: AudioDelta) -> None:
        for callback in self._audio_callbacks:
            await callback(event)

    async def _emit_state(self, state: ProviderState) -> None:
        if state == self._state:
            return
        self._state = state
        for callback in self._state_callbacks:
            await callback(state)

    async def _emit_transcript(self, event: TranscriptEvent) -> None:
        for callback in self._transcript_callbacks:
            await callback(event)

    async def _emit_error(self, message: str) -> None:
        for callback in self._error_callbacks:
            await callback(message)

    async def _emit_interruption(self, event: InterruptionEvent) -> None:
        for callback in self._interruption_callbacks:
            await callback(event)

    async def _emit_metrics(self, values: dict[str, Any]) -> None:
        event = ProviderMetrics(dict(values))
        for callback in self._metrics_callbacks:
            await callback(event)

    async def _emit_tool_call(self, event: ToolCallEvent) -> None:
        for callback in self._tool_callbacks:
            await callback(event)

    async def _emit_response_done(self, success: bool) -> None:
        """Report whether one model turn completed successfully."""

        for callback in self._response_done_callbacks:
            await callback(bool(success))

    def _begin_context_item_ack(
        self,
        item_id: str,
        expected_text: str,
    ) -> asyncio.Future[tuple[str, str | None]]:
        """Register an acknowledgement before sending to avoid a fast-echo race."""

        identity = str(item_id or "").strip()
        if not identity or identity in self._pending_context_item_acks:
            raise ValueError("realtime context item id must be unique and non-empty")
        future: asyncio.Future[tuple[str, str | None]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending_context_item_acks[identity] = _PendingContextItemAck(
            expected_text=str(expected_text),
            future=future,
        )
        return future

    def _acknowledge_context_item(self, event: dict[str, Any]) -> bool:
        """Resolve a pending create only from the server's full echoed item."""

        item = event.get("item")
        if not isinstance(item, dict):
            return False
        item_id = str(item.get("id") or "").strip()
        pending = self._pending_context_item_acks.get(item_id)
        if pending is None or pending.future.done():
            return False

        accepted_text: str | None = None
        content = item.get("content")
        if (
            item.get("type") == "message"
            and item.get("role") == "user"
            and isinstance(content, list)
            and len(content) == 1
            and isinstance(content[0], dict)
            and content[0].get("type") == "input_text"
            and isinstance(content[0].get("text"), str)
        ):
            accepted_text = content[0]["text"]
        pending.future.set_result(
            (str(event.get("event_id") or ""), accepted_text)
        )
        return True

    async def _await_context_item_acks(
        self,
        expected_text: str,
        registrations: list[tuple[str, asyncio.Future[tuple[str, str | None]]]],
        *,
        timeout: float,
    ) -> RealtimeContextDeliveryReceipt:
        """Build one aggregate receipt without retaining or returning prompt text."""

        results: list[tuple[str, str | None]] = []
        futures = [future for _, future in registrations]
        try:
            gathered = asyncio.gather(*(asyncio.shield(future) for future in futures))
            results = list(
                await asyncio.wait_for(gathered, timeout=max(0.1, float(timeout)))
            )
        except TimeoutError:
            results = [
                future.result()
                if future.done() and not future.cancelled() and future.exception() is None
                else ("", None)
                for future in futures
            ]
        finally:
            for item_id, future in registrations:
                pending = self._pending_context_item_acks.get(item_id)
                if pending is not None and pending.future is future:
                    self._pending_context_item_acks.pop(item_id, None)
                if not future.done():
                    future.cancel()

        expected = str(expected_text)
        expected_bytes = len(expected.encode("utf-8"))
        expected_sha256 = hashlib.sha256(expected.encode("utf-8")).hexdigest()
        accepted_parts = [accepted for _, accepted in results]
        accepted_text = (
            "".join(accepted_parts)
            if accepted_parts and all(isinstance(part, str) for part in accepted_parts)
            else None
        )
        accepted_bytes = (
            len(accepted_text.encode("utf-8")) if accepted_text is not None else None
        )
        accepted_sha256 = (
            hashlib.sha256(accepted_text.encode("utf-8")).hexdigest()
            if accepted_text is not None
            else None
        )
        return RealtimeContextDeliveryReceipt(
            item_ids=tuple(item_id for item_id, _ in registrations),
            exact=bool(
                accepted_text == expected
                and accepted_bytes == expected_bytes
                and accepted_sha256 == expected_sha256
            ),
            expected_utf8_bytes=expected_bytes,
            expected_sha256=expected_sha256,
            accepted_utf8_bytes=accepted_bytes,
            accepted_sha256=accepted_sha256,
            transport_event_ids=tuple(
                event_id for event_id, _ in results if event_id
            ),
        )

    def _cancel_pending_context_item_acks(self) -> None:
        """Release waiters when a provider connection is closing."""

        pending = tuple(self._pending_context_item_acks.values())
        self._pending_context_item_acks.clear()
        for item in pending:
            if not item.future.done():
                item.future.cancel()

    def _discard_context_item_acks(
        self,
        registrations: list[
            tuple[str, asyncio.Future[tuple[str, str | None]]]
        ],
    ) -> None:
        """Discard only one failed injection's pending acknowledgements."""

        for item_id, future in registrations:
            pending = self._pending_context_item_acks.get(item_id)
            if pending is not None and pending.future is future:
                self._pending_context_item_acks.pop(item_id, None)
            if not future.done():
                future.cancel()

    @abstractmethod
    async def connect(self, session_config: dict[str, Any]) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def send_audio(self, pcm16: bytes) -> None: ...

    @abstractmethod
    async def interrupt(self, *, played_audio_ms: int | None = None) -> None: ...

    async def send_text(self, text: str) -> None:
        raise NotImplementedError(f"{self.provider_name} does not support text injection")

    async def inject_context(
        self,
        text: str,
    ) -> RealtimeContextDeliveryReceipt | None:
        """Inject transient turn context without requesting an extra response."""

        raise NotImplementedError(
            f"{self.provider_name} does not support transient context injection"
        )

    async def submit_tool_result(self, call_id: str, result: Any) -> None:
        raise NotImplementedError(f"{self.provider_name} does not support tool results")
