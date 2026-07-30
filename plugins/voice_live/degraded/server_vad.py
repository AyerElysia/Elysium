"""服务端语音活动检测（VAD）。

实现基于能量和可选 Silero 模型的语音活动检测，
用于降级管线中判断用户说话起止。
"""

from __future__ import annotations

import asyncio
import struct
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from src.kernel.logger import get_logger

logger = get_logger("voice_live.vad", display="Server VAD")


@dataclass(slots=True)
class VadConfig:
    """VAD 配置。"""

    threshold: float = 0.012  # RMS 能量阈值
    silence_ms: int = 800  # 静音触发时间
    min_speech_ms: int = 300  # 最短语音时长
    max_ms: int = 15000  # 最大语音时长
    pre_speech_ms: int = 200  # 预录缓冲
    sample_rate: int = 16000  # 采样率


# VAD 事件回调
SpeechStartCallback = Callable[[], Awaitable[None]]
SpeechEndCallback = Callable[[bytes], Awaitable[None]]  # 参数为完整音频 buffer


class ServerVAD:
    """服务端语音活动检测器。

    基于 RMS 能量阈值检测语音活动，
    支持预录缓冲和静音触发。
    """

    def __init__(self, config: VadConfig) -> None:
        self._config = config
        self._is_speaking = False
        self._audio_buffer: bytearray = bytearray()
        self._pre_buffer: deque[bytes] = deque(
            maxlen=int(config.pre_speech_ms * config.sample_rate / 1000 / 320)  # 每帧 320 bytes (10ms@16kHz)
        )
        self._silence_frames = 0
        self._speech_frames = 0

        # 回调
        self._on_speech_start: SpeechStartCallback | None = None
        self._on_speech_end: SpeechEndCallback | None = None

        # 计算帧参数
        self._frame_size = int(config.sample_rate * 2 / 100)  # 10ms 帧，16-bit = 2 bytes
        self._silence_threshold_frames = int(config.silence_ms / 10)
        self._min_speech_frames = int(config.min_speech_ms / 10)
        self._max_speech_frames = int(config.max_ms / 10)

    # ------------------------------------------------------------------
    # 回调注册
    # ------------------------------------------------------------------

    def on_speech_start(self, callback: SpeechStartCallback) -> None:
        """注册语音开始回调。"""
        self._on_speech_start = callback

    def on_speech_end(self, callback: SpeechEndCallback) -> None:
        """注册语音结束回调（携带完整音频 buffer）。"""
        self._on_speech_end = callback

    # ------------------------------------------------------------------
    # 核心处理
    # ------------------------------------------------------------------

    async def process_audio(self, pcm_chunk: bytes) -> None:
        """处理音频数据。

        Args:
            pcm_chunk: PCM16 原始音频字节
        """
        # 计算 RMS 能量
        rms = self._calculate_rms(pcm_chunk)
        is_voice = rms > self._config.threshold

        if not self._is_speaking:
            # 未在说话状态
            if is_voice:
                # 检测到语音开始
                self._is_speaking = True
                self._speech_frames = 1
                self._silence_frames = 0
                # 将预录缓冲加入正式 buffer
                self._audio_buffer = bytearray()
                for pre_chunk in self._pre_buffer:
                    self._audio_buffer.extend(pre_chunk)
                self._audio_buffer.extend(pcm_chunk)
                self._pre_buffer.clear()

                if self._on_speech_start:
                    await self._on_speech_start()
                logger.debug(f"语音开始 (RMS={rms:.4f})")
            else:
                # 继续积累预录缓冲
                self._pre_buffer.append(pcm_chunk)
        else:
            # 正在说话状态
            self._audio_buffer.extend(pcm_chunk)

            if is_voice:
                self._speech_frames += 1
                self._silence_frames = 0
            else:
                self._silence_frames += 1

            # 检查是否结束
            should_end = False
            if self._silence_frames >= self._silence_threshold_frames:
                # 静音超过阈值
                if self._speech_frames >= self._min_speech_frames:
                    should_end = True
                else:
                    # 语音太短，丢弃
                    self._reset()
                    return

            if self._speech_frames >= self._max_speech_frames:
                # 超过最大时长
                should_end = True

            if should_end:
                await self._finish_speech()

    def reset(self) -> None:
        """重置 VAD 状态。"""
        self._reset()

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _reset(self) -> None:
        """内部重置。"""
        self._is_speaking = False
        self._audio_buffer = bytearray()
        self._silence_frames = 0
        self._speech_frames = 0

    async def _finish_speech(self) -> None:
        """完成一段语音。"""
        audio_bytes = bytes(self._audio_buffer)
        duration_ms = len(audio_bytes) / (self._config.sample_rate * 2) * 1000
        logger.debug(f"语音结束 (时长={duration_ms:.0f}ms)")
        self._reset()

        if self._on_speech_end:
            await self._on_speech_end(audio_bytes)

    @staticmethod
    def _calculate_rms(pcm_data: bytes) -> float:
        """计算 PCM16 音频的 RMS 能量。"""
        if len(pcm_data) < 2:
            return 0.0
        # 解析 16-bit 采样
        n_samples = len(pcm_data) // 2
        if n_samples == 0:
            return 0.0
        samples = struct.unpack(f"<{n_samples}h", pcm_data[:n_samples * 2])
        # 计算 RMS（归一化到 0-1）
        sum_squares = sum(s * s for s in samples)
        rms = (sum_squares / n_samples) ** 0.5 / 32768.0
        return rms
