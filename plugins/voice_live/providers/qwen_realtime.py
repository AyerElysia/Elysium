"""Alibaba Cloud Qwen Audio/Omni Realtime WebSocket provider."""

from __future__ import annotations

import asyncio
import base64
import json
import re
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

_QWEN_MAX_FRAME_BYTES = 256 * 1024
_QWEN_CONTEXT_CHUNK_BYTES = 192 * 1024
# Qwen Audio does not promise WebSocket PONG replies.  ``None`` keeps aiohttp's
# automatic reply to server PINGs but prevents client-originated heartbeat PINGs
# from falsely terminating an otherwise healthy realtime session.
_QWEN_WEBSOCKET_HEARTBEAT: None = None


def _split_utf8_text(text: str, *, max_bytes: int) -> list[str]:
    """Split text without breaking UTF-8 code points or dropping content."""

    if max_bytes < 4:
        raise ValueError("max_bytes must accommodate one UTF-8 code point")
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return [text]

    chunks: list[str] = []
    offset = 0
    while offset < len(encoded):
        end = min(offset + max_bytes, len(encoded))
        if end < len(encoded):
            while end > offset and encoded[end] & 0xC0 == 0x80:
                end -= 1
        if end == offset:
            raise ValueError("unable to split UTF-8 text at the requested byte limit")
        chunks.append(encoded[offset:end].decode("utf-8"))
        offset = end
    return chunks


def _url_with_model(url: str, model: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("model", model)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def _qwen_tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
    """Translate the flattened Realtime schema to DashScope's nested schema."""
    if isinstance(tool.get("function"), dict):
        return dict(tool)
    function = {
        key: tool[key] for key in ("name", "description", "parameters") if key in tool
    }
    return {"type": "function", "function": function}


def _qwen_safe_tool_name(name: str) -> str:
    """Map Elysium's namespaced tool names to Qwen's function-name alphabet."""
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def _is_stale_cancel_error(error: dict[str, Any]) -> bool:
    """Recognize Qwen's harmless response.cancel completion race."""

    return (
        str(error.get("type") or "") == "invalid_request_error"
        and str(error.get("code") or "") == "invalid_value"
        and "no active response" in str(error.get("message") or "").lower()
    )


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
        self._model = model
        self._api_key = api_key
        self._voice = voice or "Tina"
        self._connect_timeout = connect_timeout
        self._event_timeout = event_timeout
        self._http: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._updated: asyncio.Future[dict[str, Any]] | None = None
        self._closed = False
        self._terminal_error: Exception | None = None
        self._response_active = False
        self._active_response_id = ""
        self._active_item_id = ""
        self._interrupting_response_id = ""
        self._interrupt_lock = asyncio.Lock()
        self._response_generation = 0
        self._transient_context_expiry: dict[str, int] = {}
        self._tool_name_map: dict[str, str] = {}

    async def connect(self, session_config: dict[str, Any]) -> None:
        self._session_config = dict(session_config)
        await self._emit_state(ProviderState.CONNECTING)
        self._http = aiohttp.ClientSession()
        try:
            self._ws = await asyncio.wait_for(
                self._http.ws_connect(
                    self._url,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    heartbeat=_QWEN_WEBSOCKET_HEARTBEAT,
                ),
                timeout=self._connect_timeout,
            )
            self._receive_task = asyncio.create_task(
                self._receive_loop(), name="voice-qwen-receive"
            )
            if self._model.startswith("qwen-audio-"):
                max_history_turns = int(
                    session_config.get("qwen_max_history_turns") or 12
                )
                if not 1 <= max_history_turns <= 50:
                    raise ValueError("Qwen max_history_turns must be between 1 and 50")
                session = {
                    "modalities": ["text", "audio"],
                    "voice": self._voice,
                    "input_audio_format": "pcm",
                    "output_audio_format": "pcm",
                    "max_history_turns": max_history_turns,
                    "turn_detection": {"type": "smart_turn"},
                }
            else:
                session = {
                    "modalities": ["text", "audio"],
                    "voice": self._voice,
                    "input_audio_format": "pcm",
                    "output_audio_format": "pcm",
                    "input_audio_transcription": {"model": "qwen3-asr-flash-realtime"},
                    "turn_detection": {"type": "semantic_vad"},
                }
            await self._update_session(session)

            instructions = str(session_config.get("instructions") or "")
            if instructions:
                await self._update_session({"instructions": instructions})

            tools = list(session_config.get("tools") or [])
            if tools:
                translated_tools: list[dict[str, Any]] = []
                for tool in tools:
                    translated = _qwen_tool_schema(tool)
                    function = translated.get("function") or {}
                    internal_name = str(function.get("name") or "")
                    provider_name = _qwen_safe_tool_name(internal_name)
                    previous = self._tool_name_map.get(provider_name)
                    if previous is not None and previous != internal_name:
                        raise ValueError(
                            "Qwen tool-name mapping collision: "
                            f"{previous!r} and {internal_name!r}"
                        )
                    self._tool_name_map[provider_name] = internal_name
                    function["name"] = provider_name
                    translated_tools.append(translated)
                tool_session: dict[str, Any] = {"tools": translated_tools}
                if not self._model.startswith("qwen-audio-"):
                    tool_session["tool_choice"] = "auto"
                acknowledgement = await self._update_session(tool_session)
                acknowledged_session = acknowledgement.get("session") or {}
                acknowledged_tools = (
                    acknowledged_session.get("tools")
                    if isinstance(acknowledged_session, dict)
                    else None
                )
                acknowledged_names = []
                if isinstance(acknowledged_tools, list):
                    for acknowledged_tool in acknowledged_tools:
                        acknowledged_function = (
                            acknowledged_tool.get("function")
                            if isinstance(acknowledged_tool, dict)
                            else None
                        )
                        if isinstance(acknowledged_function, dict):
                            acknowledged_names.append(
                                str(acknowledged_function.get("name") or "")
                            )
                await self._emit_metrics(
                    {
                        "configuration": {
                            "requested_tool_count": len(tools),
                            "requested_tool_names": sorted(self._tool_name_map),
                            "acknowledged_tool_count": (
                                len(acknowledged_tools)
                                if isinstance(acknowledged_tools, list)
                                else None
                            ),
                            "acknowledged_tool_names": sorted(acknowledged_names),
                        }
                    }
                )
        except Exception:
            await self.disconnect()
            raise
        await self._emit_state(ProviderState.LISTENING)

    async def disconnect(self) -> None:
        self._closed = True
        current = asyncio.current_task()
        if (
            self._receive_task
            and self._receive_task is not current
            and not self._receive_task.done()
        ):
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
        async with self._interrupt_lock:
            if not self._response_active:
                return
            response_id = self._active_response_id
            item_id = self._active_item_id
            # Claim the response before the first await so overlapping local and
            # server barge-in signals cannot emit duplicate response.cancel events.
            self._response_active = False
            self._interrupting_response_id = response_id
            await self._send({"type": "response.cancel"})
            if (
                not self._model.startswith("qwen-audio-")
                and played_audio_ms is not None
                and item_id
            ):
                await self._send(
                    {
                        "type": "conversation.item.truncate",
                        "item_id": item_id,
                        "content_index": 0,
                        "audio_end_ms": max(0, played_audio_ms),
                    }
                )
            await self._emit_state(ProviderState.INTERRUPTED)
            await self._emit_interruption(
                InterruptionEvent("client", response_id, item_id)
            )

    async def send_text(self, text: str) -> None:
        await self.inject_context(text)
        await self._send({"type": "response.create"})

    async def inject_context(self, text: str) -> None:
        """Append one turn context item without independently triggering speech."""

        chunks = _split_utf8_text(text, max_bytes=_QWEN_CONTEXT_CHUNK_BYTES)
        for chunk in chunks:
            item_id = f"voice_context_{uuid.uuid4().hex}"
            await self._send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "id": item_id,
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": chunk}],
                    },
                }
            )
            self._track_transient_context(item_id, response_ttl=1)
        if len(chunks) > 1:
            await self._emit_metrics(
                {
                    "context_delivery": {
                        "bytes": len(text.encode("utf-8")),
                        "chunks": len(chunks),
                        "chunk_limit_bytes": _QWEN_CONTEXT_CHUNK_BYTES,
                    }
                }
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
            await self._send({"type": "conversation.item.delete", "item_id": item_id})
            self._transient_context_expiry.pop(item_id, None)

    def _track_transient_context(self, item_id: str, *, response_ttl: int) -> None:
        self._transient_context_expiry[item_id] = (
            self._response_generation + max(1, int(response_ttl))
        )

    async def submit_tool_result(self, call_id: str, result: Any) -> None:
        item_id = f"voice_tool_result_{uuid.uuid4().hex}"
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "id": item_id,
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result, ensure_ascii=False)
                    if not isinstance(result, str)
                    else result,
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
            if self._terminal_error is not None:
                raise self._terminal_error
            raise RuntimeError("Qwen Realtime websocket is closed")
        event.setdefault("event_id", f"voice_{uuid.uuid4().hex}")
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        payload_bytes = len(payload.encode("utf-8"))
        if payload_bytes > _QWEN_MAX_FRAME_BYTES:
            event_type = str(event.get("type") or "unknown")
            raise RuntimeError(
                "Qwen outbound websocket event exceeds the 256 KiB frame limit: "
                f"type={event_type}, bytes={payload_bytes}"
            )
        await self._ws.send_str(payload)

    async def _update_session(self, session: dict[str, Any]) -> dict[str, Any]:
        """Apply one semantic configuration unit and await its acknowledgement."""
        self._updated = asyncio.get_running_loop().create_future()
        await self._send({"type": "session.update", "session": session})
        try:
            return await asyncio.wait_for(
                asyncio.shield(self._updated), timeout=self._event_timeout
            )
        except TimeoutError as exc:
            fields = ",".join(sorted(session))
            raise TimeoutError(
                f"Qwen session.update was not acknowledged; fields={fields}"
            ) from exc

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            while True:
                message = await self._ws.receive()
                if message.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_event(json.loads(message.data))
                elif message.type == aiohttp.WSMsgType.ERROR:
                    raise RuntimeError(
                        str(self._ws.exception() or "Qwen websocket error")
                    )
                elif message.type in {
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                }:
                    code = self._ws.close_code or message.data or "unknown"
                    reason = str(message.extra or "").strip()
                    suffix = f", reason={reason}" if reason else ""
                    raise RuntimeError(
                        f"Qwen websocket closed by server: code={code}{suffix}"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - upstream transport boundary
            self._terminal_error = exc
            if self._updated is not None and not self._updated.done():
                self._updated.set_exception(exc)
            if not self._closed:
                await self._emit_error(str(exc))
                await self._emit_state(ProviderState.ERROR)

    async def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        event_id = str(event.get("event_id") or "")
        if event_type == "session.created":
            return
        if event_type == "session.updated":
            if self._updated is not None and not self._updated.done():
                self._updated.set_result(event)
            return
        if event_type == "input_audio_buffer.speech_started":
            # Qwen emits speech_started for every ordinary user turn.  It is an
            # interruption only while an assistant response is actually active;
            # treating an idle turn start as barge-in clears an empty playback
            # queue and resets SeedVC immediately before the new response.
            if self._response_active:
                await self._emit_interruption(
                    InterruptionEvent(
                        "server_vad", self._active_response_id, self._active_item_id
                    )
                )
            await self._emit_state(ProviderState.LISTENING)
            return
        if event_type == "input_audio_buffer.speech_stopped":
            await self._emit_state(ProviderState.THINKING)
            return
        if event_type == "response.created":
            response = event.get("response") or {}
            self._response_active = True
            self._active_response_id = str(
                response.get("id") or event.get("response_id") or ""
            )
            self._interrupting_response_id = ""
            await self._emit_state(ProviderState.THINKING)
            return
        if event_type in {"response.output_item.added", "conversation.item.created"}:
            item = event.get("item") or {}
            if item.get("role") == "assistant":
                self._active_item_id = str(item.get("id") or "")
            return
        if event_type == "response.audio.delta" and event.get("delta"):
            if self._interrupting_response_id == self._active_response_id:
                return
            await self._emit_state(ProviderState.SPEAKING)
            await self._emit_audio(
                AudioDelta(
                    base64.b64decode(event["delta"]),
                    self.output_sample_rate,
                    response_id=self._active_response_id,
                )
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
                await self._emit_transcript(
                    TranscriptEvent("user", text, True, event_id)
                )
            return
        if event_type == "response.function_call_arguments.done":
            provider_name = str(event.get("name") or "")
            await self._emit_tool_call(
                ToolCallEvent(
                    str(event.get("call_id") or ""),
                    self._tool_name_map.get(provider_name, provider_name),
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
            self._active_response_id = ""
            self._active_item_id = ""
            await self._emit_state(ProviderState.LISTENING)
            return
        if event_type == "error":
            error = event.get("error") or {}
            if self._interrupting_response_id and _is_stale_cancel_error(error):
                response_id = self._interrupting_response_id
                self._response_active = False
                self._active_response_id = ""
                self._active_item_id = ""
                self._interrupting_response_id = ""
                await self._emit_metrics(
                    {
                        "interruption": {
                            "stale_cancel_ignored": 1,
                            "response_id": response_id,
                        }
                    }
                )
                await self._emit_state(ProviderState.LISTENING)
                return
            message = str(
                error.get("message") or event.get("message") or "Qwen Realtime error"
            )
            code = str(error.get("code") or "").strip()
            error_type = str(error.get("type") or "").strip()
            details = ", ".join(value for value in (error_type, code) if value)
            if details:
                message = f"{message} ({details})"
            if self._updated is not None and not self._updated.done():
                self._updated.set_exception(RuntimeError(message))
            await self._emit_error(message)
