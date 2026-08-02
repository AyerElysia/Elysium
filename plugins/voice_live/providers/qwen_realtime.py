"""Alibaba Cloud Qwen3.5-Omni Realtime WebSocket provider."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp

from ..protocol import ProviderState
from .base import (
    AudioDelta,
    BaseRealtimeProvider,
    InterruptionEvent,
    ToolCallEvent,
    TranscriptEvent,
)


def _url_with_model(url: str, model: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("model", model)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class QwenRealtimeProvider(BaseRealtimeProvider):
    provider_name = "qwen_realtime"
    input_sample_rate = 16000
    output_sample_rate = 24000

    def __init__(
        self,
        upstream_url: str,
        api_key: str,
        *,
        model: str,
        voice: str = "Tina",
        connect_timeout: float = 20.0,
        event_timeout: float = 45.0,
    ) -> None:
        super().__init__()
        if not api_key:
            raise RuntimeError("Qwen Realtime API key environment variable is empty")
        self._url = _url_with_model(upstream_url, model)
        self._api_key = api_key
        self._voice = voice or "Tina"
        self._connect_timeout = connect_timeout
        self._event_timeout = event_timeout
        self._http: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._updated: asyncio.Future[dict[str, Any]] | None = None
        self._closed = False
        self._active_response_id = ""
        self._active_item_id = ""

    async def connect(self, session_config: dict[str, Any]) -> None:
        self._session_config = dict(session_config)
        await self._emit_state(ProviderState.CONNECTING)
        self._http = aiohttp.ClientSession()
        try:
            self._ws = await asyncio.wait_for(
                self._http.ws_connect(
                    self._url,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    heartbeat=20,
                ),
                timeout=self._connect_timeout,
            )
            self._updated = asyncio.get_running_loop().create_future()
            self._receive_task = asyncio.create_task(self._receive_loop(), name="voice-qwen-receive")
            session = {
                "modalities": ["text", "audio"],
                "voice": self._voice,
                "instructions": str(session_config.get("instructions") or ""),
                "input_audio_format": "pcm",
                "output_audio_format": "pcm",
                "input_audio_transcription": {"model": "qwen3-asr-flash-realtime"},
                "turn_detection": {"type": "semantic_vad"},
            }
            tools = list(session_config.get("tools") or [])
            if tools:
                session["tools"] = tools
                session["tool_choice"] = "auto"
            await self._send(
                {
                    "type": "session.update",
                    "session": session,
                }
            )
            await asyncio.wait_for(asyncio.shield(self._updated), timeout=self._event_timeout)
        except Exception:
            await self.disconnect()
            raise
        await self._emit_state(ProviderState.LISTENING)

    async def disconnect(self) -> None:
        self._closed = True
        current = asyncio.current_task()
        if self._receive_task and self._receive_task is not current and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._http and not self._http.closed:
            await self._http.close()
        await self._emit_state(ProviderState.CLOSED)

    async def send_audio(self, pcm16: bytes) -> None:
        await self._send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm16).decode("ascii"),
            }
        )

    async def interrupt(self, *, played_audio_ms: int | None = None) -> None:
        await self._send({"type": "response.cancel"})
        if played_audio_ms is not None and self._active_item_id:
            await self._send(
                {
                    "type": "conversation.item.truncate",
                    "item_id": self._active_item_id,
                    "content_index": 0,
                    "audio_end_ms": max(0, played_audio_ms),
                }
            )
        await self._emit_state(ProviderState.INTERRUPTED)
        await self._emit_interruption(
            InterruptionEvent("client", self._active_response_id, self._active_item_id)
        )

    async def send_text(self, text: str) -> None:
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        await self._send({"type": "response.create"})

    async def submit_tool_result(self, call_id: str, result: Any) -> None:
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result,
                },
            }
        )
        await self._send({"type": "response.create"})

    async def _send(self, event: dict[str, Any]) -> None:
        if not self._ws or self._ws.closed:
            raise RuntimeError("Qwen Realtime websocket is closed")
        event.setdefault("event_id", f"voice_{uuid.uuid4().hex}")
        await self._ws.send_str(json.dumps(event, ensure_ascii=False, separators=(",", ":")))

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            async for message in self._ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_event(json.loads(message.data))
                elif message.type == aiohttp.WSMsgType.ERROR:
                    raise RuntimeError(str(self._ws.exception() or "Qwen websocket error"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._updated is not None and not self._updated.done():
                self._updated.set_exception(exc)
            if not self._closed:
                await self._emit_error(str(exc))
                await self._emit_state(ProviderState.ERROR)

    async def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        event_id = str(event.get("event_id") or "")
        if event_type == "session.updated":
            if self._updated is not None and not self._updated.done():
                self._updated.set_result(event)
            return
        if event_type == "input_audio_buffer.speech_started":
            await self._emit_interruption(
                InterruptionEvent("server_vad", self._active_response_id, self._active_item_id)
            )
            await self._emit_state(ProviderState.LISTENING)
            return
        if event_type == "input_audio_buffer.speech_stopped":
            await self._emit_state(ProviderState.THINKING)
            return
        if event_type == "response.created":
            response = event.get("response") or {}
            self._active_response_id = str(response.get("id") or event.get("response_id") or "")
            await self._emit_state(ProviderState.THINKING)
            return
        if event_type in {"response.output_item.added", "conversation.item.created"}:
            item = event.get("item") or {}
            if item.get("role") == "assistant":
                self._active_item_id = str(item.get("id") or "")
            return
        if event_type == "response.audio.delta" and event.get("delta"):
            await self._emit_state(ProviderState.SPEAKING)
            await self._emit_audio(
                AudioDelta(base64.b64decode(event["delta"]), self.output_sample_rate, response_id=self._active_response_id)
            )
            return
        if event_type == "response.audio_transcript.delta" and event.get("delta"):
            await self._emit_transcript(
                TranscriptEvent("assistant", str(event["delta"]), False, event_id)
            )
            return
        if event_type == "response.audio_transcript.done" and event.get("transcript"):
            await self._emit_transcript(
                TranscriptEvent("assistant", str(event["transcript"]), True, event_id)
            )
            return
        if event_type == "conversation.item.input_audio_transcription.completed":
            text = str(event.get("transcript") or "")
            if text:
                await self._emit_transcript(TranscriptEvent("user", text, True, event_id))
            return
        if event_type == "response.function_call_arguments.done":
            await self._emit_tool_call(
                ToolCallEvent(
                    str(event.get("call_id") or ""),
                    str(event.get("name") or ""),
                    str(event.get("arguments") or "{}"),
                )
            )
            return
        if event_type == "response.done":
            response = event.get("response") or {}
            if response.get("usage"):
                await self._emit_metrics(dict(response["usage"]))
            await self._emit_state(ProviderState.LISTENING)
            return
        if event_type == "error":
            error = event.get("error") or {}
            message = str(error.get("message") or event.get("message") or "Qwen Realtime error")
            await self._emit_error(message)
