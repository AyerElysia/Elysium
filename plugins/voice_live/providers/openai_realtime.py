"""OpenAI Realtime API Provider。

实现 OpenAI Realtime API 的 WebSocket 协议，支持：
- 服务端 VAD（语音活动检测）
- 音频流式输入/输出
- 打断（barge-in）
- 转写事件

协议参考：https://platform.openai.com/docs/guides/realtime
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

import aiohttp

from src.kernel.logger import get_logger

from .base import (
    AudioDelta,
    BaseRealtimeProvider,
    ProviderState,
    TranscriptEvent,
)

logger = get_logger("voice_live.openai_realtime", display="OpenAI Realtime")


class OpenAIRealtimeProvider(BaseRealtimeProvider):
    """OpenAI Realtime API 全双工 Provider。

    通过 WebSocket 连接到 OpenAI Realtime API，实现：
    - 持续音频流输入（input_audio_buffer.append）
    - 服务端 VAD 自动检测说话起止
    - 流式音频输出（response.audio.delta）
    - 打断支持（response.cancel）
    """

    provider_name = "openai_realtime"

    def __init__(
        self,
        upstream_url: str,
        api_key: str,
        model: str = "gpt-4o-realtime-preview",
        voice: str = "alloy",
        connect_timeout: float = 10.0,
    ) -> None:
        super().__init__()
        self._upstream_url = upstream_url
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._connect_timeout = connect_timeout

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._closed = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def connect(self, session_config: dict[str, Any]) -> None:
        """连接到 OpenAI Realtime API。"""
        self._session_config = session_config
        await self._emit_state_change(ProviderState.CONNECTING)

        # 构建 WebSocket URL
        url = self._upstream_url.rstrip("/")
        if "?" not in url:
            url = f"{url}?model={self._model}"
        elif "model=" not in url:
            url = f"{url}&model={self._model}"

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "OpenAI-Beta": "realtime=v1",
        }

        try:
            self._session = aiohttp.ClientSession()
            self._ws = await asyncio.wait_for(
                self._session.ws_connect(url, headers=headers),
                timeout=self._connect_timeout,
            )
        except Exception as exc:
            await self._emit_error(f"连接失败: {exc}")
            await self._emit_state_change(ProviderState.ERROR)
            raise

        # 发送 session.update 配置
        instructions = session_config.get("instructions", "")
        await self._send_event({
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "instructions": instructions,
                "voice": self._voice,
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "input_audio_transcription": {"model": "whisper-1"},
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 500,
                },
            },
        })

        # 启动接收循环
        self._receive_task = asyncio.create_task(self._receive_loop())
        await self._emit_state_change(ProviderState.LISTENING)
        logger.info(f"已连接到 OpenAI Realtime API: {self._model}")

    async def disconnect(self) -> None:
        """断开连接。"""
        self._closed = True
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        await self._emit_state_change(ProviderState.CLOSED)
        logger.info("已断开 OpenAI Realtime API 连接")

    # ------------------------------------------------------------------
    # 音频流
    # ------------------------------------------------------------------

    async def send_audio(self, pcm_chunk: bytes) -> None:
        """发送音频数据。"""
        if not self._ws or self._ws.closed:
            return
        audio_b64 = base64.b64encode(pcm_chunk).decode("ascii")
        await self._send_event({
            "type": "input_audio_buffer.append",
            "audio": audio_b64,
        })

    async def interrupt(self) -> None:
        """打断当前生成。"""
        if not self._ws or self._ws.closed:
            return
        await self._send_event({"type": "response.cancel"})
        logger.debug("已发送打断信号")

    async def send_text(self, text: str) -> None:
        """发送文本消息。"""
        if not self._ws or self._ws.closed:
            return
        await self._send_event({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        })
        await self._send_event({"type": "response.create"})

    async def update_instructions(self, instructions: str) -> None:
        """更新系统指令。"""
        if not self._ws or self._ws.closed:
            return
        await self._send_event({
            "type": "session.update",
            "session": {"instructions": instructions},
        })

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    async def _send_event(self, event: dict[str, Any]) -> None:
        """发送 JSON 事件到 WebSocket。"""
        if self._ws and not self._ws.closed:
            await self._ws.send_json(event)

    async def _receive_loop(self) -> None:
        """接收并处理上游事件循环。"""
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if self._closed:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_event(json.loads(msg.data))
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    await self._emit_error(f"WebSocket 错误: {self._ws.exception()}")
                    break
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                    break
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            if not self._closed:
                await self._emit_error(f"接收循环异常: {exc}")
                await self._emit_state_change(ProviderState.ERROR)

    async def _handle_event(self, event: dict[str, Any]) -> None:
        """处理上游事件。"""
        event_type = event.get("type", "")

        match event_type:
            # 音频输出
            case "response.audio.delta":
                audio_b64 = event.get("delta", "")
                if audio_b64:
                    audio_bytes = base64.b64decode(audio_b64)
                    await self._emit_audio_delta(AudioDelta(
                        data=audio_bytes,
                        sample_rate=24000,
                        format="pcm16",
                    ))

            # 音频转写
            case "response.audio_transcript.delta":
                delta_text = event.get("delta", "")
                if delta_text:
                    await self._emit_transcript(TranscriptEvent(
                        role="assistant",
                        text=delta_text,
                        is_final=False,
                        timestamp_ms=int(time.time() * 1000),
                    ))

            case "response.audio_transcript.done":
                transcript = event.get("transcript", "")
                if transcript:
                    await self._emit_transcript(TranscriptEvent(
                        role="assistant",
                        text=transcript,
                        is_final=True,
                        timestamp_ms=int(time.time() * 1000),
                    ))

            # 用户语音转写
            case "conversation.item.input_audio_transcription.completed":
                transcript = event.get("transcript", "")
                if transcript:
                    await self._emit_transcript(TranscriptEvent(
                        role="user",
                        text=transcript,
                        is_final=True,
                        timestamp_ms=int(time.time() * 1000),
                    ))

            # VAD 事件
            case "input_audio_buffer.speech_started":
                # 用户开始说话 -> 打断当前输出
                await self._emit_state_change(ProviderState.LISTENING)

            case "input_audio_buffer.speech_stopped":
                # 用户停止说话 -> 等待响应
                pass

            # 响应状态
            case "response.created":
                await self._emit_state_change(ProviderState.SPEAKING)

            case "response.done":
                await self._emit_state_change(ProviderState.LISTENING)

            # 错误
            case "error":
                error_msg = event.get("error", {}).get("message", "未知错误")
                await self._emit_error(error_msg)
                logger.error(f"OpenAI Realtime 错误: {error_msg}")

            case _:
                # 忽略其他事件
                pass
