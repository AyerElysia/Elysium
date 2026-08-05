"""Current OpenAI Realtime WebSocket provider (GA event schema)."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp

from ..audio import resample_pcm16_mono
from ..protocol import ProviderState
from .base import (
    AudioDelta,
    BaseRealtimeProvider,
    InterruptionEvent,
    RealtimeContextDeliveryReceipt,
    ToolCallEvent,
    TranscriptEvent,
)

_CONTEXT_ACK_TIMEOUT_SECONDS = 5.0


def _url_with_model(url: str, model: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("model", model)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class OpenAIRealtimeProvider(BaseRealtimeProvider):
    provider_name = "openai_realtime"
    input_sample_rate = 16000
    upstream_input_sample_rate = 24000
    output_sample_rate = 24000

    def __init__(
        self,
        upstream_url: str,
        api_key: str,
        *,
        model: str = "gpt-realtime",
        voice: str = "marin",
        connect_timeout: float = 20.0,
        event_timeout: float = 45.0,
    ) -> None:
        super().__init__()
        if not api_key:
            raise RuntimeError("OpenAI Realtime API key environment variable is empty")
        self._url = _url_with_model(upstream_url, model)
        self._api_key = api_key
        self._model = model
        self._voice = voice or "marin"
        self._connect_timeout = connect_timeout
        self._event_timeout = event_timeout
        self._http: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._updated: asyncio.Future[dict[str, Any]] | None = None
        self._closed = False
        self._response_active = False
        self._active_response_id = ""
        self._active_item_id = ""
        self._response_generation = 0
        self._transient_context_expiry: dict[str, int] = {}

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
            self._receive_task = asyncio.create_task(self._receive_loop(), name="voice-openai-receive")
            session = {
                        "type": "realtime",
                        "model": self._model,
                        "instructions": str(session_config.get("instructions") or ""),
                        "output_modalities": ["audio"],
                        "audio": {
                            "input": {
                                "format": {"type": "audio/pcm", "rate": self.upstream_input_sample_rate},
                                "transcription": {"model": "gpt-4o-mini-transcribe"},
                                "turn_detection": {
                                    "type": "semantic_vad",
                                    "create_response": True,
                                    "interrupt_response": True,
                                },
                            },
                            "output": {
                                "format": {"type": "audio/pcm", "rate": self.output_sample_rate},
                                "voice": self._voice,
                            },
                        },
                        "truncation": "disabled",
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
        self._cancel_pending_context_item_acks()
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
        audio = resample_pcm16_mono(pcm16, self.input_sample_rate, self.upstream_input_sample_rate)
        await self._send(
            {"type": "input_audio_buffer.append", "audio": base64.b64encode(audio).decode("ascii")}
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
        await self.inject_context(text)
        await self._send({"type": "response.create"})

    async def inject_context(
        self,
        text: str,
    ) -> RealtimeContextDeliveryReceipt:
        """Append context and prove the exact server-echoed UTF-8 content."""

        if not text:
            raise ValueError("realtime context must not be empty")
        item_id = f"voice_context_{uuid.uuid4().hex}"
        future = self._begin_context_item_ack(item_id, text)
        registrations = [(item_id, future)]
        try:
            await self._send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "id": item_id,
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": text}],
                    },
                }
            )
            self._track_transient_context(item_id, response_ttl=1)
        except BaseException:
            self._discard_context_item_acks(registrations)
            raise
        return await self._await_context_item_acks(
            text,
            registrations,
            timeout=min(_CONTEXT_ACK_TIMEOUT_SECONDS, self._event_timeout),
        )

    async def _delete_transient_context_items(self) -> None:
        """Expire turn context without deleting a just-produced tool result early."""

        self._response_generation += 1
        item_ids = [
            item_id
            for item_id, expiry in self._transient_context_expiry.items()
            if expiry <= self._response_generation
        ]
        for item_id in item_ids:
            await self._send(
                {"type": "conversation.item.delete", "item_id": item_id}
            )
            self._transient_context_expiry.pop(item_id, None)

    def _track_transient_context(self, item_id: str, *, response_ttl: int) -> None:
        self._transient_context_expiry[item_id] = (
            self._response_generation + max(1, int(response_ttl))
        )

    async def submit_tool_result(self, call_id: str, result: Any) -> None:
        output = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        item_id = f"voice_tool_result_{uuid.uuid4().hex}"
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "id": item_id,
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                },
            }
        )
        self._track_transient_context(
            item_id,
            response_ttl=2 if self._response_active else 1,
        )
        await self._send({"type": "response.create"})

    async def _send(self, event: dict[str, Any]) -> None:
        if not self._ws or self._ws.closed:
            raise RuntimeError("OpenAI Realtime websocket is closed")
        event.setdefault("event_id", f"voice_{uuid.uuid4().hex}")
        await self._ws.send_str(json.dumps(event, ensure_ascii=False, separators=(",", ":")))

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            async for message in self._ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_event(json.loads(message.data))
                elif message.type == aiohttp.WSMsgType.ERROR:
                    raise RuntimeError(str(self._ws.exception() or "OpenAI websocket error"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - upstream transport boundary
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
            self._response_active = True
            self._active_response_id = str(response.get("id") or event.get("response_id") or "")
            await self._emit_state(ProviderState.THINKING)
            return
        if event_type in {
            "response.output_item.added",
            "conversation.item.created",
            "conversation.item.added",
        }:
            self._acknowledge_context_item(event)
            item = event.get("item") or {}
            if item.get("role") == "assistant":
                self._active_item_id = str(item.get("id") or "")
            return
        if event_type in {"response.output_audio.delta", "response.audio.delta"} and event.get("delta"):
            await self._emit_state(ProviderState.SPEAKING)
            await self._emit_audio(
                AudioDelta(base64.b64decode(event["delta"]), self.output_sample_rate, response_id=self._active_response_id)
            )
            return
        if event_type in {"response.output_audio_transcript.delta", "response.audio_transcript.delta"} and event.get("delta"):
            await self._emit_transcript(TranscriptEvent("assistant", str(event["delta"]), False, event_id))
            return
        if event_type in {"response.output_audio_transcript.done", "response.audio_transcript.done"}:
            text = str(event.get("transcript") or "")
            if text:
                await self._emit_transcript(TranscriptEvent("assistant", text, True, event_id))
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
            status = str(response.get("status") or "completed").lower()
            success = status not in {"cancelled", "failed", "incomplete", "error"}
            await self._delete_transient_context_items()
            await self._emit_response_done(success)
            self._response_active = False
            await self._emit_state(ProviderState.LISTENING)
            return
        if event_type == "error":
            error = event.get("error") or {}
            await self._emit_error(str(error.get("message") or "OpenAI Realtime error"))
