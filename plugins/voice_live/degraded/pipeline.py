"""降级管线编排器。

将 VAD、LLM、TTS 三个子系统编排为完整的降级语音交互管线：
VAD 检测语音 -> 累积音频 -> MiMo 音频理解 -> 流式文本 -> 切句 -> TTS -> 流式音频

支持打断（barge-in）：用户开始说话时立即取消当前 TTS 播放。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import Enum, auto
from typing import Any

from src.kernel.logger import get_logger

from .llm_client import DegradedLLMClient
from .server_vad import ServerVAD, VadConfig
from .tts_streamer import TTSStreamer

logger = get_logger("voice_live.pipeline", display="Degraded Pipeline")


class PipelineState(Enum):
    """降级管线状态。"""

    IDLE = auto()
    LISTENING = auto()
    THINKING = auto()
    SPEAKING = auto()
    ERROR = auto()


# 回调类型
AudioOutputCallback = Callable[[bytes, str], Awaitable[None]]  # (audio_bytes, format)
StateChangeCallback = Callable[[PipelineState], Awaitable[None]]
TranscriptCallback = Callable[[str, str], Awaitable[None]]  # (role, text)


class DegradedPipeline:
    """降级管线编排器。

    当全双工 Provider 不可用时，使用级联管线实现语音交互：
    1. ServerVAD 检测用户说话起止
    2. 将完整语音段发送给 MiMo-V2.5 进行音频理解
    3. 流式获取文本回复
    4. TTSStreamer 按句合成语音并流式返回

    支持 barge-in：用户打断时取消当前 TTS。
    """

    def __init__(
        self,
        vad_config: VadConfig,
        llm_client: DegradedLLMClient,
        tts_streamer: TTSStreamer,
    ) -> None:
        self._vad = ServerVAD(vad_config)
        self._llm = llm_client
        self._tts = tts_streamer

        self._state = PipelineState.IDLE
        self._running = False
        self._processing_task: asyncio.Task[None] | None = None

        # 回调
        self._audio_output_cb: AudioOutputCallback | None = None
        self._state_change_cb: StateChangeCallback | None = None
        self._transcript_cb: TranscriptCallback | None = None

        # 注册 VAD 回调
        self._vad.on_speech_start(self._handle_speech_start)
        self._vad.on_speech_end(self._handle_speech_end)

        # 注册 TTS 输出回调
        self._tts.on_audio_output(self._handle_tts_output)

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def state(self) -> PipelineState:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # 回调注册
    # ------------------------------------------------------------------

    def on_audio_output(self, callback: AudioOutputCallback) -> None:
        """注册音频输出回调（发送给客户端）。"""
        self._audio_output_cb = callback

    def on_state_change(self, callback: StateChangeCallback) -> None:
        """注册状态变更回调。"""
        self._state_change_cb = callback

    def on_transcript(self, callback: TranscriptCallback) -> None:
        """注册转写回调 (role, text)。"""
        self._transcript_cb = callback

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self, system_prompt: str = "") -> None:
        """启动降级管线。"""
        if self._running:
            return
        self._running = True
        if system_prompt:
            self._llm.set_system_prompt(system_prompt)
        await self._set_state(PipelineState.LISTENING)
        logger.info("降级管线已启动")

    async def stop(self) -> None:
        """停止降级管线。"""
        if not self._running:
            return
        self._running = False

        # 取消正在进行的处理
        if self._processing_task and not self._processing_task.done():
            self._processing_task.cancel()
            try:
                await self._processing_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._processing_task = None

        await self._tts.cancel()
        self._vad.reset()
        await self._set_state(PipelineState.IDLE)
        logger.info("降级管线已停止")

    # ------------------------------------------------------------------
    # 音频输入
    # ------------------------------------------------------------------

    async def feed_audio(self, pcm_chunk: bytes) -> None:
        """输入音频数据。

        Args:
            pcm_chunk: PCM16 原始音频字节（16kHz mono）
        """
        if not self._running:
            return
        await self._vad.process_audio(pcm_chunk)

    async def interrupt(self) -> None:
        """打断当前生成（barge-in）。"""
        if self._state == PipelineState.SPEAKING:
            await self._tts.cancel()
            if self._processing_task and not self._processing_task.done():
                self._processing_task.cancel()
                self._processing_task = None
            await self._set_state(PipelineState.LISTENING)
            logger.debug("用户打断，已取消当前生成")

    # ------------------------------------------------------------------
    # VAD 回调处理
    # ------------------------------------------------------------------

    async def _handle_speech_start(self) -> None:
        """VAD 检测到语音开始。"""
        # 如果正在播放 TTS，触发打断
        if self._state == PipelineState.SPEAKING:
            await self.interrupt()

    async def _handle_speech_end(self, audio_buffer: bytes) -> None:
        """VAD 检测到语音结束，携带完整音频。"""
        if not self._running:
            return

        # 启动异步处理任务
        self._processing_task = asyncio.create_task(
            self._process_utterance(audio_buffer)
        )

    # ------------------------------------------------------------------
    # 核心处理流程
    # ------------------------------------------------------------------

    async def _process_utterance(self, audio_bytes: bytes) -> None:
        """处理一段完整的用户语音。

        流程：LLM 流式理解 -> 切句 -> TTS 流式合成
        """
        try:
            # 1. 进入思考状态
            await self._set_state(PipelineState.THINKING)

            # 2. 流式获取 LLM 回复
            full_text = ""
            async for text_delta in self._llm.stream_response(audio_bytes):
                if not self._running:
                    return
                full_text += text_delta
                # 将文本增量喂给 TTS 切句器
                await self._tts.feed_text(text_delta)

            # 3. 发送用户转写（音频理解结果通过 LLM 回复推断）
            if self._transcript_cb and full_text:
                await self._transcript_cb("assistant", full_text)

            # 4. 刷新 TTS 剩余文本
            await self._tts.flush()

            # 5. 回到监听状态
            if self._running:
                await self._set_state(PipelineState.LISTENING)

        except asyncio.CancelledError:
            logger.debug("语音处理被取消（打断）")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"降级管线处理异常: {exc}")
            if self._running:
                await self._set_state(PipelineState.ERROR)
                # 短暂等待后恢复监听
                await asyncio.sleep(1.0)
                if self._running:
                    await self._set_state(PipelineState.LISTENING)

    # ------------------------------------------------------------------
    # TTS 输出处理
    # ------------------------------------------------------------------

    async def _handle_tts_output(self, audio_bytes: bytes, fmt: str) -> None:
        """TTS 合成输出回调。"""
        if not self._running:
            return

        # 首次收到音频时切换到说话状态
        if self._state != PipelineState.SPEAKING:
            await self._set_state(PipelineState.SPEAKING)

        # 转发给客户端
        if self._audio_output_cb:
            await self._audio_output_cb(audio_bytes, fmt)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    async def _set_state(self, new_state: PipelineState) -> None:
        """设置管线状态并触发回调。"""
        if self._state == new_state:
            return
        self._state = new_state
        if self._state_change_cb:
            try:
                await self._state_change_cb(new_state)
            except Exception:  # noqa: BLE001
                pass
