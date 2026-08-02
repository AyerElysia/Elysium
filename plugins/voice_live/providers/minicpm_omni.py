"""MiniCPM-o 4.5 provider for llama.cpp-omni's native ``/backend`` WS."""

from __future__ import annotations

import asyncio
import base64
import json
import wave
from pathlib import Path
from typing import Any

import aiohttp

from ..audio import float32_b64_to_pcm16, pcm16_to_float32_b64, resample_pcm16_mono
from ..protocol import ProviderState
from .base import AudioDelta, BaseRealtimeProvider, InterruptionEvent, TranscriptEvent


def _reference_audio_b64(path_value: str) -> str:
    if not path_value:
        return ""
    path = Path(path_value).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"reference audio not found: {path}")
    if path.suffix.lower() != ".wav":
        return base64.b64encode(path.read_bytes()).decode("ascii")
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError("reference WAV must be mono PCM16")
        pcm16 = handle.readframes(handle.getnframes())
        pcm16 = resample_pcm16_mono(pcm16, handle.getframerate(), 16000)
    return pcm16_to_float32_b64(pcm16)


class MiniCPMOmniProvider(BaseRealtimeProvider):
    provider_name = "minicpm_omni"
    input_sample_rate = 16000
    output_sample_rate = 24000

    def __init__(
        self,
        upstream_url: str,
        *,
        mode: str = "full_duplex",
        reference_audio_path: str = "",
        tts_reference_audio_path: str = "",
        input_chunk_ms: int = 1000,
        connect_timeout: float = 20.0,
        event_timeout: float = 45.0,
    ) -> None:
        super().__init__()
        self._url = upstream_url
        self._mode = mode
        self._reference_audio_path = reference_audio_path
        self._tts_reference_audio_path = tts_reference_audio_path
        self._chunk_bytes = self.input_sample_rate * 2 * input_chunk_ms // 1000
        self._connect_timeout = connect_timeout
        self._event_timeout = event_timeout
        self._http: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[dict[str, Any]] | None = None
        self._input = bytearray()
        self._closed = False
        self._response_text: dict[str, str] = {}

    async def connect(self, session_config: dict[str, Any]) -> None:
        self._session_config = dict(session_config)
        await self._emit_state(ProviderState.CONNECTING)
        self._closed = False
        self._http = aiohttp.ClientSession()
        try:
            self._ws = await asyncio.wait_for(
                # Omni initialization and decode are intentionally heavyweight.
                # aiohttp's heartbeat closes the socket if the native server is
                # busy for half the heartbeat interval, even though the model is
                # still making progress.  Business-event and session deadlines
                # provide the liveness boundary for this provider instead.
                self._http.ws_connect(self._url, heartbeat=None),
                timeout=self._connect_timeout,
            )
            self._ready = asyncio.get_running_loop().create_future()
            self._receive_task = asyncio.create_task(self._receive_loop(), name="voice-minicpm-receive")
            reference = _reference_audio_b64(self._reference_audio_path)
            tts_reference = _reference_audio_b64(self._tts_reference_audio_path)
            payload: dict[str, Any] = {
                "mode": self._mode,
                "use_tts": True,
                "system_prompt": str(session_config.get("instructions") or ""),
                "config": dict(session_config.get("provider_config") or {}),
            }
            if reference or tts_reference:
                payload["voice"] = {
                    "ref_audio": reference,
                    "tts_ref_audio": tts_reference or reference,
                }
            await self._send({"type": "session.init", "payload": payload})
            await asyncio.wait_for(asyncio.shield(self._ready), timeout=self._event_timeout)
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
        self._input.clear()
        await self._emit_state(ProviderState.CLOSED)

    async def send_audio(self, pcm16: bytes) -> None:
        if not self._ws or self._ws.closed:
            raise RuntimeError("MiniCPM-o provider is not connected")
        if self._mode != "full_duplex":
            raise RuntimeError("turn-based MiniCPM-o audio must use send_turn_audio()")
        self._input.extend(pcm16)
        while len(self._input) >= self._chunk_bytes:
            chunk = bytes(self._input[: self._chunk_bytes])
            del self._input[: self._chunk_bytes]
            await self._send_audio_chunk(chunk)

    async def send_turn_audio(self, pcm16: bytes) -> None:
        """Submit one complete audio turn for deterministic native acceptance."""

        if not self._ws or self._ws.closed:
            raise RuntimeError("MiniCPM-o provider is not connected")
        if self._mode != "turn_based":
            raise RuntimeError("send_turn_audio() requires turn_based mode")
        provider_config = dict(self._session_config.get("provider_config") or {})
        generation = dict(provider_config.get("generation") or {})
        await self._send(
            {
                "type": "input.append",
                "input": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "audio",
                                    "data": pcm16_to_float32_b64(pcm16),
                                }
                            ],
                        }
                    ],
                    "streaming": True,
                    "use_tts_template": True,
                    "generation": generation,
                },
            }
        )

    async def inject_context(self, text: str) -> None:
        """Append text context to the native conversation without a response."""

        if not self._ws or self._ws.closed:
            raise RuntimeError("MiniCPM-o provider is not connected")
        await self._send(
            {
                "type": "input.append",
                "input": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": text}],
                        }
                    ],
                    "streaming": False,
                    "context_only": True,
                },
            }
        )

    async def _send_audio_chunk(self, chunk: bytes, *, force_listen: bool = False) -> None:
        await self._send(
            {
                "type": "input.append",
                "input": {
                    "audio_base64": pcm16_to_float32_b64(chunk) if chunk else "",
                    "force_listen": force_listen,
                },
            }
        )

    async def interrupt(self, *, played_audio_ms: int | None = None) -> None:
        del played_audio_ms
        await self._send_audio_chunk(b"", force_listen=True)
        await self._emit_state(ProviderState.INTERRUPTED)
        await self._emit_interruption(InterruptionEvent(source="client"))
        await self._emit_state(ProviderState.LISTENING)

    async def _send(self, value: dict[str, Any]) -> None:
        if not self._ws or self._ws.closed:
            raise RuntimeError("MiniCPM-o websocket is closed")
        await self._ws.send_str(json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            async for message in self._ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_event(json.loads(message.data))
                elif message.type == aiohttp.WSMsgType.ERROR:
                    raise RuntimeError(str(self._ws.exception() or "MiniCPM-o websocket error"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(exc)
            if not self._closed:
                await self._emit_error(str(exc))
                await self._emit_state(ProviderState.ERROR)

    async def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if event.get("metrics"):
            await self._emit_metrics(dict(event["metrics"]))
        if event_type == "session.created":
            if self._ready is not None and not self._ready.done():
                self._ready.set_result(event)
            return
        if event_type == "response.output.delta":
            kind = event.get("kind")
            response_id = str(event.get("response_id") or "")
            if kind == "audio" and event.get("audio"):
                await self._emit_state(ProviderState.SPEAKING)
                await self._emit_audio(
                    AudioDelta(
                        float32_b64_to_pcm16(str(event["audio"])),
                        self.output_sample_rate,
                        response_id=response_id,
                    )
                )
            elif kind == "text" and event.get("text"):
                text = str(event["text"])
                self._response_text[response_id] = self._response_text.get(response_id, "") + text
                await self._emit_transcript(
                    TranscriptEvent("assistant", text, False, response_id)
                )
            elif kind == "listen":
                await self._emit_state(ProviderState.LISTENING)
            return
        if event_type == "response.done":
            response_id = str(event.get("response_id") or "")
            text = str(event.get("text") or self._response_text.pop(response_id, ""))
            if event.get("audio"):
                await self._emit_audio(
                    AudioDelta(
                        float32_b64_to_pcm16(str(event["audio"])),
                        self.output_sample_rate,
                        response_id=response_id,
                    )
                )
            if text:
                await self._emit_transcript(TranscriptEvent("assistant", text, True, response_id))
            status = str(event.get("status") or "completed").lower()
            await self._emit_response_done(
                status not in {"cancelled", "failed", "incomplete", "error"}
            )
            await self._emit_state(ProviderState.LISTENING)
            return
        if event_type == "session.closed":
            reason = str(event.get("reason") or "server_closed")
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(
                    RuntimeError(f"MiniCPM-o session closed during initialization: {reason}")
                )
            if not self._closed:
                await self._emit_error(f"MiniCPM-o session closed: {reason}")
            await self._emit_state(ProviderState.CLOSED)
            return
        if event_type == "error":
            message = str(event.get("message") or event.get("error") or "MiniCPM-o error")
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(RuntimeError(message))
            await self._emit_error(message)
            await self._emit_state(ProviderState.ERROR)
