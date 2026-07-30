"""全双工 Provider 基类。

定义所有实时语音 Provider 的统一接口，包括：
- 连接/断开生命周期
- 音频流收发
- 打断（barge-in）支持
- 状态与转写回调
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


class ProviderState(Enum):
    """Provider 运行状态。"""

    IDLE = auto()
    CONNECTING = auto()
    LISTENING = auto()
    SPEAKING = auto()
    ERROR = auto()
    CLOSED = auto()


@dataclass(slots=True)
class TranscriptEvent:
    """转写事件。"""

    role: str  # "user" | "assistant"
    text: str
    is_final: bool = True
    timestamp_ms: int = 0


@dataclass(slots=True)
class AudioDelta:
    """音频增量数据。"""

    data: bytes  # PCM16 raw bytes
    sample_rate: int = 24000
    format: str = "pcm16"


# 回调类型定义
AudioDeltaCallback = Callable[[AudioDelta], Awaitable[None]]
StateChangeCallback = Callable[[ProviderState], Awaitable[None]]
TranscriptCallback = Callable[[TranscriptEvent], Awaitable[None]]
ErrorCallback = Callable[[str], Awaitable[None]]


class BaseRealtimeProvider(ABC):
    """全双工实时语音 Provider 抽象基类。

    所有 Provider（OpenAI Realtime、Moshi 等）都继承此类，
    实现统一的连接、音频流、打断和回调接口。
    """

    provider_name: str = "base"

    def __init__(self) -> None:
        self._state = ProviderState.IDLE
        self._audio_delta_callbacks: list[AudioDeltaCallback] = []
        self._state_change_callbacks: list[StateChangeCallback] = []
        self._transcript_callbacks: list[TranscriptCallback] = []
        self._error_callbacks: list[ErrorCallback] = []
        self._session_config: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 状态属性
    # ------------------------------------------------------------------

    @property
    def state(self) -> ProviderState:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state in (ProviderState.LISTENING, ProviderState.SPEAKING)

    # ------------------------------------------------------------------
    # 回调注册
    # ------------------------------------------------------------------

    def on_audio_delta(self, callback: AudioDeltaCallback) -> None:
        """注册音频输出回调。"""
        self._audio_delta_callbacks.append(callback)

    def on_state_change(self, callback: StateChangeCallback) -> None:
        """注册状态变更回调。"""
        self._state_change_callbacks.append(callback)

    def on_transcript(self, callback: TranscriptCallback) -> None:
        """注册转写事件回调。"""
        self._transcript_callbacks.append(callback)

    def on_error(self, callback: ErrorCallback) -> None:
        """注册错误回调。"""
        self._error_callbacks.append(callback)

    # ------------------------------------------------------------------
    # 内部回调触发
    # ------------------------------------------------------------------

    async def _emit_audio_delta(self, delta: AudioDelta) -> None:
        for cb in self._audio_delta_callbacks:
            try:
                await cb(delta)
            except Exception:  # noqa: BLE001
                pass

    async def _emit_state_change(self, new_state: ProviderState) -> None:
        old_state = self._state
        self._state = new_state
        if old_state != new_state:
            for cb in self._state_change_callbacks:
                try:
                    await cb(new_state)
                except Exception:  # noqa: BLE001
                    pass

    async def _emit_transcript(self, event: TranscriptEvent) -> None:
        for cb in self._transcript_callbacks:
            try:
                await cb(event)
            except Exception:  # noqa: BLE001
                pass

    async def _emit_error(self, message: str) -> None:
        for cb in self._error_callbacks:
            try:
                await cb(message)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # 抽象接口
    # ------------------------------------------------------------------

    @abstractmethod
    async def connect(self, session_config: dict[str, Any]) -> None:
        """连接到上游全双工模型。

        Args:
            session_config: 会话配置，包含 instructions、voice 等参数
        """
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """断开连接并清理资源。"""
        ...

    @abstractmethod
    async def send_audio(self, pcm_chunk: bytes) -> None:
        """发送音频数据到上游模型。

        Args:
            pcm_chunk: PCM16 原始音频字节
        """
        ...

    @abstractmethod
    async def interrupt(self) -> None:
        """打断当前生成（barge-in）。

        当用户开始说话时调用，停止当前的音频输出。
        """
        ...

    async def send_text(self, text: str) -> None:
        """发送文本消息到上游模型（可选）。

        某些 Provider 支持文本注入，默认不实现。
        """
        del text  # 默认不支持

    async def update_instructions(self, instructions: str) -> None:
        """动态更新系统指令（可选）。

        某些 Provider 支持运行时更新 instructions。
        """
        del instructions  # 默认不支持
