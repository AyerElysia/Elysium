"""Strict provider contract for realtime speech-to-speech backends."""

from __future__ import annotations

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

    async def inject_context(self, text: str) -> None:
        """Inject transient turn context without requesting an extra response."""

        raise NotImplementedError(
            f"{self.provider_name} does not support transient context injection"
        )

    async def submit_tool_result(self, call_id: str, result: Any) -> None:
        raise NotImplementedError(f"{self.provider_name} does not support tool results")
