"""通话会话管理。

每个 WebSocket 连接对应一个 CallSession，
管理全双工/降级路径的选择、生命周期和消息协议。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from enum import Enum, auto
from typing import Any

from src.kernel.logger import get_logger

from .config import VoiceLiveConfig
from .degraded.llm_client import DegradedLLMClient
from .degraded.pipeline import DegradedPipeline, PipelineState
from .degraded.server_vad import VadConfig
from .degraded.tts_streamer import TTSStreamer
from .providers.base import (
    AudioDelta,
    BaseRealtimeProvider,
    ProviderState,
    TranscriptEvent,
)
from .providers.factory import create_provider

logger = get_logger("voice_live.session", display="Call Session")


class SessionState(Enum):
    """会话状态。"""

    IDLE = auto()
    CONNECTING = auto()
    ACTIVE_FULL_DUPLEX = auto()
    ACTIVE_DEGRADED = auto()
    ENDED = auto()


class CallSession:
    """单次语音通话会话。

    状态机：idle -> connecting -> active (full_duplex | degraded) -> ended

    根据配置选择路径：
    - 全双工 Provider 已配置且可用 -> 全双工路径
    - 否则 -> 降级管线
    """

    def __init__(
        self,
        config: VoiceLiveConfig,
        session_id: str | None = None,
    ) -> None:
        self._config = config
        self.session_id = session_id or str(uuid.uuid4())[:8]
        self._state = SessionState.IDLE
        self._mode: str = ""  # "full_duplex" | "degraded"
        self._created_at = time.time()

        # 全双工路径
        self._provider: BaseRealtimeProvider | None = None

        # 降级路径
        self._pipeline: DegradedPipeline | None = None

        # 消息发送回调（由 Router 设置）
        self._send_json: Any = None  # async (dict) -> None
        self._send_bytes: Any = None  # async (bytes) -> None

        # 上下文桥接（由外部注入）
        self._system_prompt: str = ""

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def is_active(self) -> bool:
        return self._state in (SessionState.ACTIVE_FULL_DUPLEX, SessionState.ACTIVE_DEGRADED)

    # ------------------------------------------------------------------
    # 回调设置
    # ------------------------------------------------------------------

    def set_send_callbacks(self, send_json: Any, send_bytes: Any) -> None:
        """设置消息发送回调。"""
        self._send_json = send_json
        self._send_bytes = send_bytes

    def set_system_prompt(self, prompt: str) -> None:
        """设置系统提示词（来自 ContextBridge）。"""
        self._system_prompt = prompt

    # ------------------------------------------------------------------
    # 会话生命周期
    # ------------------------------------------------------------------

    async def start(self, mode: str = "auto") -> None:
        """启动会话。

        Args:
            mode: "auto" | "full_duplex" | "degraded"
        """
        if self._state != SessionState.IDLE:
            await self._send_error("会话已在运行中")
            return

        await self._set_state(SessionState.CONNECTING)

        try:
            # 路径选择
            if mode == "full_duplex" or (
                mode == "auto" and self._should_use_full_duplex()
            ):
                await self._start_full_duplex()
            elif mode == "degraded" or mode == "auto":
                await self._start_degraded()
            else:
                await self._send_error(f"不支持的模式: {mode}")
                await self._set_state(SessionState.ENDED)
                return

            # 发送就绪消息
            await self._send_json_safe({
                "type": "ready",
                "mode": self._mode,
                "provider": self._get_provider_name(),
                "session_id": self.session_id,
            })

        except Exception as exc:  # noqa: BLE001
            logger.error(f"会话启动失败: {exc}")
            await self._send_error(f"启动失败: {exc}")
            await self._set_state(SessionState.ENDED)

    async def stop(self) -> None:
        """停止会话。"""
        if self._state == SessionState.ENDED:
            return

        # 清理全双工路径
        if self._provider:
            try:
                await self._provider.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._provider = None

        # 清理降级路径
        if self._pipeline:
            await self._pipeline.stop()
            self._pipeline = None

        await self._set_state(SessionState.ENDED)
        await self._send_json_safe({"type": "ended"})
        logger.info(f"会话结束: {self.session_id}")

    # ------------------------------------------------------------------
    # 客户端消息处理
    # ------------------------------------------------------------------

    async def handle_message(self, data: dict[str, Any]) -> None:
        """处理客户端 JSON 控制消息。"""
        msg_type = data.get("type", "")

        match msg_type:
            case "start":
                mode = data.get("mode", "auto")
                await self.start(mode)
            case "interrupt":
                await self._handle_interrupt()
            case "stop":
                await self.stop()
            case "ping":
                await self._send_json_safe({"type": "pong"})
            case _:
                logger.debug(f"未知消息类型: {msg_type}")

    async def handle_audio(self, audio_bytes: bytes) -> None:
        """处理客户端二进制音频帧。"""
        if not self.is_active:
            return

        if self._state == SessionState.ACTIVE_FULL_DUPLEX and self._provider:
            await self._provider.send_audio(audio_bytes)
        elif self._state == SessionState.ACTIVE_DEGRADED and self._pipeline:
            await self._pipeline.feed_audio(audio_bytes)

    # ------------------------------------------------------------------
    # 全双工路径
    # ------------------------------------------------------------------

    async def _start_full_duplex(self) -> None:
        """启动全双工路径。"""
        fd_config = self._config.full_duplex
        self._provider = create_provider(self._config)

        if self._provider is None:
            raise RuntimeError("无法创建全双工 Provider")

        # 注册回调
        self._provider.on_audio_delta(self._on_provider_audio)
        self._provider.on_state_change(self._on_provider_state)
        self._provider.on_transcript(self._on_provider_transcript)
        self._provider.on_error(self._on_provider_error)

        # 构建会话配置
        session_config = {
            "instructions": self._system_prompt or fd_config.instructions,
            "voice": fd_config.voice,
            "model": fd_config.model_name,
        }

        await self._provider.connect(session_config)
        self._mode = "full_duplex"
        await self._set_state(SessionState.ACTIVE_FULL_DUPLEX)
        logger.info(f"全双工会话已启动: {self.session_id}")

    # ------------------------------------------------------------------
    # 降级路径
    # ------------------------------------------------------------------

    async def _start_degraded(self) -> None:
        """启动降级管线。"""
        deg_config = self._config.degraded
        vad_section = self._config.vad

        # 构建 VAD 配置
        vad_config = VadConfig(
            threshold=vad_section.threshold,
            silence_ms=vad_section.silence_ms,
            min_speech_ms=vad_section.min_speech_ms,
            max_ms=vad_section.max_ms,
            pre_speech_ms=vad_section.pre_speech_ms,
            sample_rate=self._config.audio.input_sample_rate,
        )

        # 构建 LLM 客户端
        llm_client = self._build_llm_client(deg_config)

        # 构建 TTS 流式合成器
        tts_streamer = TTSStreamer(
            tts_style=deg_config.tts_style,
            sentence_min_chars=deg_config.sentence_min_chars,
        )

        # 组装管线
        self._pipeline = DegradedPipeline(vad_config, llm_client, tts_streamer)
        self._pipeline.on_audio_output(self._on_pipeline_audio)
        self._pipeline.on_state_change(self._on_pipeline_state)
        self._pipeline.on_transcript(self._on_pipeline_transcript)

        await self._pipeline.start(self._system_prompt)
        self._mode = "degraded"
        await self._set_state(SessionState.ACTIVE_DEGRADED)
        logger.info(f"降级会话已启动: {self.session_id}")

    def _build_llm_client(self, deg_config: Any) -> DegradedLLMClient:
        """构建降级 LLM 客户端。"""
        # 从 model.toml 的 [model_tasks.live] 获取模型信息
        # 默认使用 NexusAI 中转站 + MiMo-V2.5
        return DegradedLLMClient(
            base_url="http://localhost:3000/v1",
            api_key="sk-V2o9Ut2rBHFgkH4hCy53snYbQA5uAlkc25jlRzmtT9P3wapo",
            model="mimo-v2.5",
            timeout=deg_config.llm_timeout,
            max_context_turns=deg_config.max_context_turns,
        )

    # ------------------------------------------------------------------
    # 打断处理
    # ------------------------------------------------------------------

    async def _handle_interrupt(self) -> None:
        """处理打断请求。"""
        if self._state == SessionState.ACTIVE_FULL_DUPLEX and self._provider:
            await self._provider.interrupt()
        elif self._state == SessionState.ACTIVE_DEGRADED and self._pipeline:
            await self._pipeline.interrupt()

    # ------------------------------------------------------------------
    # Provider 回调
    # ------------------------------------------------------------------

    async def _on_provider_audio(self, delta: AudioDelta) -> None:
        """全双工 Provider 音频输出。"""
        if self._send_bytes:
            # 先发 JSON 头（可选），再发二进制帧
            await self._send_json_safe({
                "type": "audio_delta",
                "format": delta.format,
                "sample_rate": delta.sample_rate,
            })
            await self._send_bytes(delta.data)


    async def _on_provider_state(self, state: ProviderState) -> None:
        """全双工 Provider 状态变更。"""
        state_map = {
            ProviderState.LISTENING: "listening",
            ProviderState.SPEAKING: "speaking",
            ProviderState.CONNECTING: "connecting",
            ProviderState.ERROR: "error",
        }
        mapped = state_map.get(state, "idle")
        await self._send_json_safe({"type": "state", "state": mapped})

    async def _on_provider_transcript(self, event: TranscriptEvent) -> None:
        """全双工 Provider 转写事件。"""
        await self._send_json_safe({
            "type": "transcript",
            "role": event.role,
            "text": event.text,
            "is_final": event.is_final,
        })

    async def _on_provider_error(self, message: str) -> None:
        """全双工 Provider 错误。"""
        await self._send_error(message)

    # ------------------------------------------------------------------
    # Pipeline 回调
    # ------------------------------------------------------------------

    async def _on_pipeline_audio(self, audio_bytes: bytes, fmt: str) -> None:
        """降级管线音频输出。"""
        if self._send_bytes:
            await self._send_json_safe({
                "type": "audio_delta",
                "format": fmt,
                "sample_rate": self._config.audio.output_sample_rate,
            })
            await self._send_bytes(audio_bytes)

    async def _on_pipeline_state(self, state: PipelineState) -> None:
        """降级管线状态变更。"""
        state_map = {
            PipelineState.LISTENING: "listening",
            PipelineState.THINKING: "thinking",
            PipelineState.SPEAKING: "speaking",
            PipelineState.ERROR: "error",
        }
        mapped = state_map.get(state, "idle")
        await self._send_json_safe({"type": "state", "state": mapped})

    async def _on_pipeline_transcript(self, role: str, text: str) -> None:
        """降级管线转写。"""
        await self._send_json_safe({
            "type": "transcript",
            "role": role,
            "text": text,
            "is_final": True,
        })

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _should_use_full_duplex(self) -> bool:
        """判断是否应使用全双工路径。"""
        fd_config = self._config.full_duplex
        return fd_config.provider_type != "disabled" and bool(fd_config.upstream_url)

    def _get_provider_name(self) -> str:
        """获取当前 Provider 名称。"""
        if self._mode == "full_duplex" and self._provider:
            return self._provider.provider_name
        return "degraded_pipeline"

    async def _set_state(self, new_state: SessionState) -> None:
        """设置会话状态。"""
        self._state = new_state

    async def _send_json_safe(self, data: dict[str, Any]) -> None:
        """安全发送 JSON 消息。"""
        if self._send_json:
            try:
                await self._send_json(data)
            except Exception:  # noqa: BLE001
                pass

    async def _send_error(self, message: str) -> None:
        """发送错误消息。"""
        await self._send_json_safe({"type": "error", "message": message})
