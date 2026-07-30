"""TTS 播放队列。

按句接收 LLM 输出，调用本地 TTS 服务合成音频，
通过 WebSocket 推送到前端播放。

特性：
- 按句分割（根据配置的分句符）
- FIFO 播放队列，支持优先级插入
- 输出格式：PCM16/OGG → WebSocket 二进制帧
- 口型时间戳数据生成
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import httpx

if TYPE_CHECKING:
    from ..config import LivestreamConfig

logger = logging.getLogger(__name__)

# 音频帧回调：(audio_bytes, metadata)
AudioFrameCallback = Callable[[bytes, dict[str, Any]], Awaitable[None]]
# 播放完成回调
PlaybackCompleteCallback = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class TTSSentence:
    """待合成的句子。"""

    text: str
    priority: int = 5  # 默认优先级
    enqueue_time: float = field(default_factory=time.time)


class TTSQueue:
    """TTS 播放队列。"""

    def __init__(self, config: "LivestreamConfig") -> None:
        tts_cfg = config.tts
        self._endpoint = tts_cfg.tts_endpoint
        self._speed = tts_cfg.speed
        self._volume = tts_cfg.volume
        self._delimiters = tts_cfg.sentence_delimiters
        self._max_sentence_length = tts_cfg.max_sentence_length

        # 播放队列
        self._queue: asyncio.Queue[TTSSentence | None] = asyncio.Queue(maxsize=100)
        self._playing = False
        self._task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None

        # 回调
        self._audio_callback: AudioFrameCallback | None = None
        self._complete_callback: PlaybackCompleteCallback | None = None

        # 构建分句正则
        escaped = re.escape(self._delimiters.replace("\\n", "\n"))
        self._split_pattern = re.compile(f"([{escaped}])")

    def on_audio_frame(self, callback: AudioFrameCallback) -> None:
        """注册音频帧回调（推送到 WebSocket）。"""
        self._audio_callback = callback

    def on_playback_complete(self, callback: PlaybackCompleteCallback) -> None:
        """注册播放完成回调。"""
        self._complete_callback = callback

    async def start(self) -> None:
        """启动 TTS 播放循环。"""
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._playing = True
        self._task = asyncio.create_task(self._playback_loop())
        logger.info(f"TTS 队列已启动: endpoint={self._endpoint}")

    async def stop(self) -> None:
        """停止 TTS 播放。"""
        self._playing = False
        await self._queue.put(None)  # 哨兵
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        if self._client:
            await self._client.aclose()
            self._client = None
        logger.info("TTS 队列已停止")

    async def speak(self, text: str, priority: int = 5) -> None:
        """将文本分句后加入播放队列。

        Args:
            text: 要合成的完整文本。
            priority: 优先级（数值越小越优先）。
        """
        sentences = self._split_sentences(text)
        for sentence in sentences:
            if sentence.strip():
                await self._queue.put(
                    TTSSentence(text=sentence.strip(), priority=priority)
                )

    async def clear(self) -> None:
        """清空播放队列（打断当前播放）。"""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        logger.debug("TTS 队列已清空")

    @property
    def pending_count(self) -> int:
        """待播放句子数。"""
        return self._queue.qsize()

    def _split_sentences(self, text: str) -> list[str]:
        """将文本按分句符切分。"""
        # 先按分句符切分
        parts = self._split_pattern.split(text)
        sentences: list[str] = []
        current = ""

        for part in parts:
            current += part
            if self._split_pattern.match(part):
                # 这是一个分隔符
                if current.strip():
                    sentences.append(current.strip())
                current = ""

        # 处理最后一段
        if current.strip():
            sentences.append(current.strip())

        # 强制切分过长的句子
        result: list[str] = []
        for s in sentences:
            if len(s) > self._max_sentence_length:
                # 按逗号切分
                sub_parts = s.split("，")
                for sp in sub_parts:
                    if sp.strip():
                        result.append(sp.strip())
            else:
                result.append(s)

        return result

    async def _playback_loop(self) -> None:
        """播放主循环。"""
        while self._playing:
            try:
                sentence = await self._queue.get()
                if sentence is None:
                    break

                # 合成音频
                audio_bytes = await self._synthesize(sentence.text)
                if audio_bytes and self._audio_callback:
                    # 估算时长（PCM16 16kHz mono: 32000 bytes/s）
                    duration = len(audio_bytes) / 32000
                    metadata = {
                        "text": sentence.text,
                        "duration": duration,
                        "timestamp": time.time(),
                    }
                    await self._audio_callback(audio_bytes, metadata)

                    # 等待播放完成（基于时长估算）
                    await asyncio.sleep(duration)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"TTS 播放异常: {exc}", exc_info=True)

        # 播放完成回调
        if self._complete_callback:
            try:
                await self._complete_callback()
            except Exception:
                pass

    async def _synthesize(self, text: str) -> bytes | None:
        """调用本地 TTS 服务合成音频。

        Returns:
            PCM16 音频字节，失败返回 None。
        """
        if not self._client:
            return None

        try:
            # 调用 TTS 服务（兼容 Player2 协议）
            response = await self._client.post(
                self._endpoint,
                json={
                    "text": text,
                    "speed": self._speed,
                    "volume": self._volume,
                },
            )
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            if "audio" in content_type or "octet-stream" in content_type:
                return response.content
            else:
                # 某些 TTS 服务返回 JSON 包含 base64 音频
                import base64
                data = response.json()
                audio_b64 = data.get("audio", data.get("data", ""))
                if audio_b64:
                    return base64.b64decode(audio_b64)
                return None

        except Exception as exc:
            logger.warning(f"TTS 合成失败: {text[:20]}... -> {exc}")
            return None
