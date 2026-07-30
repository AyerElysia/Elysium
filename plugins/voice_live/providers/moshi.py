"""Moshi 全双工 Provider。

实现 Kyutai Moshi / NVIDIA PersonaPlex 的原生 WebSocket 协议。
Moshi 是真正的全双工语音对话模型，同时建模用户和系统的音频流，
无需显式的轮次管理。

协议特点：
- 二进制 WebSocket 帧传输原始 PCM 音频
- 24kHz 16-bit 单声道
- 同时收发音频流（真全双工）
- 文本 token 作为 JSON 消息发送
"""

from __future__ import annotations

import asyncio
import json
import struct
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

logger = get_logger("voice_live.moshi", display="Moshi")

# Moshi 音频参数
MOSHI_SAMPLE_RATE = 24000
MOSHI_CHANNELS = 1
MOSHI_SAMPLE_WIDTH = 2  # 16-bit


class MoshiProvider(BaseRealtimeProvider):
    """Moshi/PersonaPlex 全双工 Provider。

    Moshi 使用原生 WebSocket 协议进行真正的全双工语音对话：
    - 客户端持续发送用户音频帧（二进制）
    - 服务端持续返回模型音频帧（二进制）
    - 文本转写通过 JSON 消息传输
    """

    provider_name = "moshi"

    def __init__(
        self,
        upstream_url: str,
        connect_timeout: float = 10.0,
    ) -> None:
        super().__init__()
        self._upstream_url = upstream_url
        self._connect_timeout = connect_timeout

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._closed = False

        # Moshi 可能需要初始握手
        self._handshake_done = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def connect(self, session_config: dict[str, Any]) -> None:
        """连接到 Moshi 服务器。"""
        self._session_config = session_config
        await self._emit_state_change(ProviderState.CONNECTING)

        url = self._upstream_url.rstrip("/")
        # Moshi 默认端点
        if not url.endswith("/api/chat"):
            url = f"{url}/api/chat"

        try:
            self._session = aiohttp.ClientSession()
            self._ws = await asyncio.wait_for(
                self._session.ws_connect(url),
                timeout=self._connect_timeout,
            )
        except Exception as exc:
            await self._emit_error(f"连接失败: {exc}")
            await self._emit_state_change(ProviderState.ERROR)
            raise

        # 发送初始配置（如果 Moshi 需要）
        # 某些 Moshi 部署接受 JSON 配置作为第一条消息
        text_prompt = session_config.get("instructions", "")
        if text_prompt:
            await self._ws.send_json({
                "type": "config",
                "text_prompt": text_prompt,
                "sample_rate": MOSHI_SAMPLE_RATE,
            })

        # 启动接收循环
        self._receive_task = asyncio.create_task(self._receive_loop())
        self._handshake_done = True
        await self._emit_state_change(ProviderState.LISTENING)
        logger.info(f"已连接到 Moshi 服务器: {url}")

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
        logger.info("已断开 Moshi 连接")

    # ------------------------------------------------------------------
    # 音频流
    # ------------------------------------------------------------------

    async def send_audio(self, pcm_chunk: bytes) -> None:
        """发送音频数据（二进制帧）。

        Moshi 期望原始 PCM16 二进制数据。
        """
        if not self._ws or self._ws.closed:
            return
        # 直接发送二进制帧
        await self._ws.send_bytes(pcm_chunk)

    async def interrupt(self) -> None:
        """打断当前生成。

        Moshi 是真全双工，不需要显式打断。
        但可以发送一个控制消息来重置状态。
        """
        if not self._ws or self._ws.closed:
            return
        # Moshi 的全双工特性意味着它会自然处理重叠语音
        # 这里发送一个可选的控制信号
        try:
            await self._ws.send_json({"type": "interrupt"})
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    async def _receive_loop(self) -> None:
        """接收并处理上游消息循环。"""
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if self._closed:
                    break

                if msg.type == aiohttp.WSMsgType.BINARY:
                    # 二进制帧 = 音频数据
                    await self._emit_audio_delta(AudioDelta(
                        data=msg.data,
                        sample_rate=MOSHI_SAMPLE_RATE,
                        format="pcm16",
                    ))
                    # Moshi 正在说话时更新状态
                    if self._state != ProviderState.SPEAKING:
                        await self._emit_state_change(ProviderState.SPEAKING)

                elif msg.type == aiohttp.WSMsgType.TEXT:
                    # JSON 消息 = 控制/转写
                    try:
                        data = json.loads(msg.data)
                        await self._handle_json_message(data)
                    except json.JSONDecodeError:
                        pass

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

    async def _handle_json_message(self, data: dict[str, Any]) -> None:
        """处理 JSON 控制消息。"""
        msg_type = data.get("type", "")

        match msg_type:
            case "text" | "transcript":
                # 文本转写
                text = data.get("text", "")
                role = data.get("role", "assistant")
                if text:
                    await self._emit_transcript(TranscriptEvent(
                        role=role,
                        text=text,
                        is_final=data.get("is_final", True),
                        timestamp_ms=int(time.time() * 1000),
                    ))

            case "state":
                # 状态更新
                state = data.get("state", "")
                if state == "listening":
                    await self._emit_state_change(ProviderState.LISTENING)
                elif state == "speaking":
                    await self._emit_state_change(ProviderState.SPEAKING)

            case "error":
                await self._emit_error(data.get("message", "Moshi 错误"))

            case _:
                # 忽略未知消息
                pass
