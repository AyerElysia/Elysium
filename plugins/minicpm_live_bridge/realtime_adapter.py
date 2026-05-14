"""Live realtime transport adapters.

前端始终说 Neo 自己的 live 协议；具体要不要直通、怎么接上游实时模型，
都收敛到这里。
"""

from __future__ import annotations

import base64
import io
import json
import sys
import time
import urllib.parse
import wave
from collections import deque
from dataclasses import dataclass, field
from typing import Any

try:
    import audioop
except Exception:  # pragma: no cover - Python may remove audioop in a future runtime.
    audioop = None  # type: ignore[assignment]


@dataclass(slots=True)
class RealtimeAdapterResult:
    """单次消息处理结果。"""

    upstream_messages: list[str | bytes] = field(default_factory=list)
    client_messages: list[str | bytes | dict[str, Any]] = field(default_factory=list)


class BaseRealtimeAdapter:
    """Neo live 协议 <-> 上游实时服务 之间的适配层基类。"""

    adapter_name = "passthrough"

    def __init__(
        self,
        *,
        session_id: str,
        upstream_url: str,
        upstream_headers: dict[str, str] | None = None,
    ) -> None:
        self.session_id = str(session_id or "")
        self._upstream_url = str(upstream_url or "")
        self._upstream_headers = dict(upstream_headers or {})
        self.session_start_payload: dict[str, Any] = {}
        self.context_snapshot: dict[str, Any] = {}
        self.recent_unified_events: deque[dict[str, Any]] = deque(maxlen=200)
        self.last_screen_frame: dict[str, Any] = {}
        self.last_audio_input: dict[str, Any] = {}

    def upstream_connect_url(self) -> str:
        return self._upstream_url

    def upstream_connect_headers(self) -> dict[str, str]:
        return dict(self._upstream_headers)

    async def upstream_pre_connect(self) -> None:
        """Optional async hook called before the WebSocket connection is opened.

        Override to perform any handshake/reset that must happen over HTTP
        before the persistent WS connection is established.
        """

    def upstream_sse_url(self) -> str | None:
        """Optional HTTP URL for the server-sent-events response stream.

        When non-None the proxy will open a parallel SSE subscription and
        forward the events to the client WebSocket via on_sse_event().
        """
        return None

    def upstream_sse_headers(self) -> dict[str, str]:
        """Extra headers for the SSE subscription request."""
        return {}

    async def on_sse_event(self, data: dict[str, Any]) -> RealtimeAdapterResult:
        """Called for each parsed SSE data event from the upstream SSE stream."""
        return RealtimeAdapterResult(client_messages=[data])

    async def on_client_message(self, payload: Any) -> RealtimeAdapterResult:
        self._remember_client_payload(payload)
        wire = self._to_wire_message(payload)
        if wire is None:
            return RealtimeAdapterResult()
        return RealtimeAdapterResult(upstream_messages=[wire])

    async def on_upstream_message(self, payload: Any) -> RealtimeAdapterResult:
        return RealtimeAdapterResult(client_messages=[payload])

    def describe_state(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter_name,
            "session_id": self.session_id,
            "has_session_start": bool(self.session_start_payload),
            "has_context_snapshot": bool(self.context_snapshot),
            "buffered_unified_events": len(self.recent_unified_events),
            "has_screen_frame": bool(self.last_screen_frame),
            "has_audio_input": bool(self.last_audio_input),
        }

    def _remember_client_payload(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return

        packet_type = str(payload.get("type") or "").strip().lower()
        if packet_type == "session.start":
            self.session_start_payload = dict(payload)
            return

        if packet_type == "context.snapshot":
            context = payload.get("context")
            self.context_snapshot = dict(context) if isinstance(context, dict) else {}
            return

        if packet_type == "unified.event":
            event = payload.get("event")
            if isinstance(event, dict):
                self.recent_unified_events.append(dict(event))
            return

        if packet_type == "screen.frame":
            self.last_screen_frame = {
                "timestamp": payload.get("timestamp"),
                "width": payload.get("width"),
                "height": payload.get("height"),
                "image_present": bool(payload.get("image")),
            }
            return

        if packet_type in {"audio.chunk", "audio.turn"}:
            self.last_audio_input = {
                "type": packet_type,
                "timestamp": payload.get("timestamp"),
                "mime_type": payload.get("mime_type"),
                "audio_present": bool(payload.get("data") or payload.get("audio_base64")),
            }

    @staticmethod
    def _to_wire_message(payload: Any) -> str | bytes | None:
        if payload is None:
            return None
        if isinstance(payload, (bytes, bytearray)):
            return bytes(payload)
        if isinstance(payload, str):
            return payload
        return json.dumps(payload, ensure_ascii=False)


class PassthroughRealtimeAdapter(BaseRealtimeAdapter):
    """直通适配器：客户端和上游都使用同一套协议。"""

    adapter_name = "passthrough"


class MiniCPMRealtimeAdapter(BaseRealtimeAdapter):
    """Neo live v0 <-> MiniCPM-o Realtime API 适配器。

    MiniCPM-o 4.5 的实时协议是 OpenAI Realtime 风格：
    `session.update` 初始化，`input_audio_buffer.append` 持续输入
    16kHz float32 PCM，`response.output_audio.delta` 持续返回 24kHz
    float32 PCM。前端不直接依赖这套 provider 协议。
    """

    adapter_name = "minicpm_realtime_v0"
    _CONTEXT_REFRESH_MIN_INTERVAL_SECONDS = 2.0
    _MAX_INSTRUCTION_CHARS = 30000
    _MAX_CONTEXT_TEXT_CHARS = 12000
    _MAX_UNIFIED_EVENTS_IN_INSTRUCTIONS = 40
    _MAX_SESSION_EVENTS_IN_INSTRUCTIONS = 24

    def __init__(
        self,
        *,
        session_id: str,
        upstream_url: str,
        upstream_headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            session_id=session_id,
            upstream_url=upstream_url,
            upstream_headers=upstream_headers,
        )
        self._created_at = time.monotonic()
        self._session_update_sent = False
        self._last_session_update_monotonic = 0.0
        self._last_video_frame_base64 = ""
        self._assistant_text_parts: list[str] = []
        self._upstream_session_id = ""
        self._upstream_ready = False
        self._chunks_sent = 0
        self._realtime_mode = self._detect_realtime_mode(upstream_url)
        # UID error throttling: suppress repeated "UID changed in stream" spam
        self._uid_error_count = 0
        self._uid_error_last_forwarded = 0.0

    def upstream_connect_url(self) -> str:
        """Append stable uid= param so the upstream server can track this session."""
        base_url = self._upstream_url
        try:
            parsed = urllib.parse.urlparse(base_url)
            qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            if "uid" not in qs:
                uid_value = str(self.session_id or "neo_live")[:32]
                qs["uid"] = [uid_value]
                new_query = urllib.parse.urlencode(
                    {k: v[0] for k, v in qs.items()}, safe=""
                )
                base_url = parsed._replace(query=new_query).geturl()
        except Exception:
            pass
        return base_url

    async def upstream_pre_connect(self) -> None:
        """POST to /api/v1/completions before the WS connection.

        The model server only resets stream_manager.uid when POST /api/v1/completions
        is called with the correct uid header.  Without this the server will reject
        the first few WS messages with {"error":"UID changed in stream"} because
        stream_manager.uid still holds the previous session's uid.
        """
        import aiohttp

        uid_value = str(self.session_id or "neo_live")[:32]
        try:
            parsed = urllib.parse.urlparse(self._upstream_url)
            scheme = "https" if parsed.scheme == "wss" else "http"
            reset_url = urllib.parse.urlunparse(
                (scheme, parsed.netloc, "/api/v1/completions", "", "", "")
            )
            headers = {"uid": uid_value, "Content-Type": "application/json"}
            # Send a minimal payload so the endpoint doesn't 422 on missing body
            body = {"messages": [], "reset_only": True}
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(reset_url, headers=headers, json=body) as resp:
                    # We don't care about the response body — the important side
                    # effect is that stream_manager.uid is now set to our uid.
                    pass
        except Exception:
            # Best-effort; if the reset call fails we still proceed with WS.
            pass

    def upstream_sse_url(self) -> str | None:
        """Derive the SSE response URL from the WebSocket upstream URL.

        ws://host:port/ws/api/v1/stream  →  http://host:port/api/v1/stream
        ws://host:port/ws/stream         →  http://host:port/api/v1/stream
        """
        try:
            parsed = urllib.parse.urlparse(self._upstream_url)
            scheme = "https" if parsed.scheme == "wss" else "http"
            path = parsed.path.rstrip("/")
            # Strip leading /ws prefix if present
            if path.startswith("/ws"):
                path = path[3:] or "/"
            # Map to /api/v1/stream regardless of original suffix
            if path in {"", "/"}:
                path = "/api/v1/stream"
            elif not path.startswith("/api"):
                path = "/api/v1/stream"
            return urllib.parse.urlunparse((scheme, parsed.netloc, path, "", "", ""))
        except Exception:
            return None

    def upstream_sse_headers(self) -> dict[str, str]:
        uid_value = str(self.session_id or "neo_live")[:32]
        headers: dict[str, str] = {"uid": uid_value}
        headers.update(self._upstream_headers)
        return headers

    async def on_sse_event(self, data: dict[str, Any]) -> RealtimeAdapterResult:
        """Map an SSE response chunk from the model server to client messages.

        The model server sends chunks like:
        {
          "id": uid,
          "response_id": N,
          "choices": [{"role": "assistant", "audio": "<b64>", "text": "...",
                        "finish_reason": "processing"}]
        }
        """
        messages: list[Any] = []
        choices = data.get("choices") or []
        if isinstance(choices, dict):
            choices = [choices]
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            finish = str(choice.get("finish_reason") or "")
            if finish == "stop":
                messages.extend(self._finalize_assistant_text(reason="sse.stop"))
                messages.append({"type": "listen", "role": "assistant", "upstream_type": "sse.stop"})
                continue
            text_fragment = str(choice.get("text") or "")
            audio_b64 = str(choice.get("audio") or "")
            if text_fragment:
                self._assistant_text_parts.append(text_fragment)
                messages.append({"type": "text_delta", "role": "assistant", "delta": text_fragment})
            if audio_b64:
                messages.append({
                    "type": "audio_delta",
                    "role": "assistant",
                    "audio": audio_b64,
                    "sample_rate": 24000,
                    "encoding": "pcm_f32le",
                })
        return RealtimeAdapterResult(client_messages=messages)

    async def on_client_message(self, payload: Any) -> RealtimeAdapterResult:
        self._remember_client_payload(payload)
        if not isinstance(payload, dict):
            return RealtimeAdapterResult()

        packet_type = str(payload.get("type") or "").strip().lower()

        if packet_type == "session.start":
            return RealtimeAdapterResult(
                client_messages=[
                    {
                        "type": "status",
                        "status": "session.start",
                        "text": "MiniCPM realtime session starting",
                    }
                ]
            )

        if packet_type == "context.snapshot":
            return RealtimeAdapterResult(
                upstream_messages=[self._build_session_update(reason="context.snapshot")]
            )

        if packet_type == "unified.event":
            return self._handle_unified_event_refresh()

        if packet_type == "screen.frame":
            self._last_video_frame_base64 = self._extract_media_base64(
                payload.get("image"),
                expected_prefix="image/",
            )[0]
            return RealtimeAdapterResult()

        if packet_type in {"audio.chunk", "audio.turn"}:
            return self._handle_audio_input(payload)

        if packet_type == "session.interrupt":
            messages = self._ensure_session_update_messages(reason="session.interrupt")
            messages.append(
                self._build_audio_append(
                    self._silence_pcm_base64(samples=4000),
                    force_listen=True,
                )
            )
            return RealtimeAdapterResult(upstream_messages=messages)

        if packet_type == "session.stop":
            return RealtimeAdapterResult(
                upstream_messages=[
                    json.dumps(
                        {"type": "session.close", "reason": "user_stop"},
                        ensure_ascii=False,
                    )
                ]
            )

        if packet_type == "text.input":
            text = str(payload.get("text") or "").strip()
            if text:
                self.recent_unified_events.append(
                    {
                        "origin": "minicpm_live_bridge",
                        "source": "live",
                        "event_type": "text.input",
                        "role": "user",
                        "text": text,
                        "time": time.time(),
                    }
                )
                messages = self._maybe_build_context_refresh(reason="text.input", force=True)
                return RealtimeAdapterResult(
                    upstream_messages=messages,
                    client_messages=[
                        {
                            "type": "status",
                            "status": "text.context_refreshed" if messages else "text.buffered",
                            "text": "text input added to realtime context",
                        }
                    ],
                )
            return RealtimeAdapterResult()

        return RealtimeAdapterResult(
            client_messages=[
                {
                    "type": "status",
                    "status": "ignored",
                    "text": f"MiniCPM realtime adapter ignored unsupported packet: {packet_type}",
                }
            ]
        )

    async def on_upstream_message(self, payload: Any) -> RealtimeAdapterResult:
        if not isinstance(payload, dict):
            return RealtimeAdapterResult(client_messages=[payload])

        packet_type = str(payload.get("type") or "").strip()
        if packet_type in {"session.queued", "session.queue_update", "session.queue_done"}:
            if packet_type == "session.queue_done":
                self._upstream_ready = True
            return RealtimeAdapterResult(client_messages=[self._map_session_status(payload)])

        if packet_type == "session.created":
            self._upstream_ready = True
            self._upstream_session_id = str(payload.get("session_id") or "")
            return RealtimeAdapterResult(client_messages=[self._map_session_status(payload)])

        if packet_type == "response.listen":
            messages = self._finalize_assistant_text(
                reason="listen",
                kv_cache_length=payload.get("kv_cache_length"),
            )
            messages.append(
                {
                    "type": "listen",
                    "role": "assistant",
                    "kv_cache_length": payload.get("kv_cache_length"),
                    "upstream_type": packet_type,
                }
            )
            return RealtimeAdapterResult(client_messages=messages)

        if packet_type == "response.output_audio.delta":
            return RealtimeAdapterResult(client_messages=self._map_audio_delta(payload))

        if packet_type == "session.closed":
            messages = self._finalize_assistant_text(reason="session.closed")
            messages.append(
                {
                    "type": "status",
                    "status": "session.closed",
                    "text": f"MiniCPM realtime session closed: {payload.get('reason') or '-'}",
                    "reason": payload.get("reason"),
                }
            )
            return RealtimeAdapterResult(client_messages=messages)

        if packet_type == "error" or "error" in payload:
            error_val = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error_val, str) and "uid" in error_val.lower():
                # Throttle "UID changed in stream" spam — forward first occurrence
                # then only once every 30 s; the upstream server keeps re-sending
                # this message for each audio frame when it has a session-uid mismatch.
                self._uid_error_count += 1
                now = time.monotonic()
                if self._uid_error_count == 1 or (now - self._uid_error_last_forwarded) > 30.0:
                    self._uid_error_last_forwarded = now
                    return RealtimeAdapterResult(client_messages=[self._map_error(payload)])
                return RealtimeAdapterResult()
            return RealtimeAdapterResult(client_messages=[self._map_error(payload)])

        if packet_type == "result":
            return RealtimeAdapterResult(client_messages=self._map_legacy_duplex_result(payload))

        if "choices" in payload:
            return RealtimeAdapterResult(
                client_messages=[
                    {
                        "type": "status",
                        "status": "upstream.ack",
                        "text": "MiniCPM upstream acknowledged request",
                        "payload": payload,
                    }
                ]
            )

        return RealtimeAdapterResult(client_messages=[payload])

    def describe_state(self) -> dict[str, Any]:
        state = super().describe_state()
        state.update(
            {
                "bootstrap_ready": bool(self.session_start_payload and self.context_snapshot),
                "realtime_mode": self._realtime_mode,
                "session_update_sent": self._session_update_sent,
                "upstream_ready": self._upstream_ready,
                "upstream_session_id": self._upstream_session_id,
                "chunks_sent": self._chunks_sent,
                "has_video_frame_base64": bool(self._last_video_frame_base64),
                "assistant_text_buffer_chars": len("".join(self._assistant_text_parts)),
            }
        )
        return state

    @staticmethod
    def _detect_realtime_mode(upstream_url: str) -> str:
        try:
            parsed = urllib.parse.urlparse(str(upstream_url or ""))
            mode = urllib.parse.parse_qs(parsed.query).get("mode", ["video"])[0]
        except Exception:
            mode = "video"
        mode = str(mode or "video").strip().lower()
        return "audio" if mode == "audio" else "video"

    def _handle_unified_event_refresh(self) -> RealtimeAdapterResult:
        # Model server protocol doesn't use session.update; nothing to send
        return RealtimeAdapterResult()

    def _handle_audio_input(self, payload: dict[str, Any]) -> RealtimeAdapterResult:
        audio_base64, error = self._payload_to_minicpm_pcm_base64(payload)
        if error:
            return RealtimeAdapterResult(
                client_messages=[
                    {
                        "type": "error",
                        "message": error,
                        "source": "minicpm_realtime_adapter",
                    }
                ]
            )

        self._chunks_sent += 1
        return RealtimeAdapterResult(
            upstream_messages=[
                self._build_audio_append(
                    audio_base64,
                    force_listen=bool(payload.get("force_listen")),
                )
            ]
        )

    def _ensure_session_update_messages(self, *, reason: str) -> list[str]:
        # Model server protocol doesn't need a session.update handshake
        self._session_update_sent = True
        return []

    def _maybe_build_context_refresh(self, *, reason: str, force: bool) -> list[str]:
        # No-op for model server protocol
        return []
        return [self._build_session_update(reason=reason)]

    def _build_session_update(self, *, reason: str) -> str:
        self._session_update_sent = True
        self._last_session_update_monotonic = time.monotonic()

        session: dict[str, Any] = {
            "instructions": self._build_instructions(reason=reason),
        }
        if self._realtime_mode == "video":
            session["max_slice_nums"] = self._max_slice_nums()

        payload = {"type": "session.update", "session": session}
        return json.dumps(payload, ensure_ascii=False)

    def _build_audio_append(self, audio_base64: str, *, force_listen: bool = False) -> str:
        """Build an audio input message for the MiniCPM-o model server.

        Protocol: /ws/api/v1/stream expects
          {"messages": [{"role": "user", "content": [
              {"type": "input_audio", "input_audio": {"data": "<b64>"}},
              {"type": "image_data", "image_data": {"data": "<b64>"}}  // optional
          ]}]}
        """
        content: list[dict[str, Any]] = [
            {"type": "input_audio", "input_audio": {"data": audio_base64, "timestamp": ""}}
        ]
        if self._last_video_frame_base64:
            content.append(
                {"type": "image_data", "image_data": {"data": self._last_video_frame_base64}}
            )
        payload: dict[str, Any] = {
            "messages": [{"role": "user", "content": content}]
        }
        if force_listen:
            payload["force_listen"] = True
        return json.dumps(payload, ensure_ascii=False)

    def _max_slice_nums(self) -> int:
        value = self.session_start_payload.get("max_slice_nums")
        capture = self.session_start_payload.get("capture")
        if value is None and isinstance(capture, dict):
            value = capture.get("max_slice_nums")
        try:
            return max(1, min(9, int(value or 1)))
        except (TypeError, ValueError):
            return 1

    def _build_instructions(self, *, reason: str) -> str:
        context = self.context_snapshot if isinstance(self.context_snapshot, dict) else {}
        life_prompt = context.get("life_chatter_prompt")
        if not isinstance(life_prompt, dict):
            life_prompt = {}

        system_prompt = str(life_prompt.get("system_prompt") or "").strip()
        user_prompt = str(life_prompt.get("user_prompt") or "").strip()
        dynamic_context = (
            str(life_prompt.get("dynamic_context") or "").strip()
            or str(context.get("life_runtime_context") or "").strip()
        )

        parts = [
            "你运行在 Neo-MoFox 的 Live 全双工语音通道中。",
            "你不是新的主意识；你是接入统一 session 和统一事件流的实时语音/读屏通道。",
            "你需要延续 life_chatter 的人格、记忆、边界和历史格式。"
            "输出只给用户听和看，不要输出 tool call JSON、action 名称、thought/reason 元信息。",
            "如果需要工具或复杂后台操作，用自然语言说明需要转到正式聊天通道，不要假装已经执行。",
        ]
        if system_prompt:
            parts.append("<life_chatter_system_prompt>\n" + system_prompt + "\n</life_chatter_system_prompt>")
        if user_prompt:
            parts.append(
                "<life_chatter_prompt_context>\n"
                + self._clip_text(user_prompt, self._MAX_CONTEXT_TEXT_CHARS)
                + "\n</life_chatter_prompt_context>"
            )
        if dynamic_context:
            parts.append(
                "<life_runtime_context>\n"
                + self._clip_text(dynamic_context, self._MAX_CONTEXT_TEXT_CHARS)
                + "\n</life_runtime_context>"
            )

        unified_events = self._events_for_instructions(
            list(context.get("unified_events") or []),
            limit=self._MAX_UNIFIED_EVENTS_IN_INSTRUCTIONS,
        )
        if self.recent_unified_events:
            unified_events.extend(
                self._events_for_instructions(
                    list(self.recent_unified_events),
                    limit=self._MAX_UNIFIED_EVENTS_IN_INSTRUCTIONS,
                )
            )
            unified_events = unified_events[-self._MAX_UNIFIED_EVENTS_IN_INSTRUCTIONS :]
        if unified_events:
            parts.append(
                "<realtime_unified_events>\n"
                + json.dumps(unified_events, ensure_ascii=False)
                + "\n</realtime_unified_events>"
            )

        session_events = self._events_for_instructions(
            list(context.get("session_events") or []),
            limit=self._MAX_SESSION_EVENTS_IN_INSTRUCTIONS,
        )
        if session_events:
            parts.append(
                "<live_session_events>\n"
                + json.dumps(session_events, ensure_ascii=False)
                + "\n</live_session_events>"
            )

        parts.append(f"<realtime_adapter_state reason=\"{reason}\" session_id=\"{self.session_id}\" />")
        return self._clip_text("\n\n".join(parts), self._MAX_INSTRUCTION_CHARS)

    @classmethod
    def _events_for_instructions(cls, events: list[Any], *, limit: int) -> list[dict[str, Any]]:
        compacted: list[dict[str, Any]] = []
        for event in events[-max(1, int(limit or 1)) :]:
            if not isinstance(event, dict):
                continue
            compact: dict[str, Any] = {}
            for key in (
                "sequence",
                "origin",
                "source",
                "event_type",
                "direction",
                "stream_id",
                "sender_name",
                "role",
                "text",
                "time",
            ):
                value = event.get(key)
                if value is None or value == "":
                    continue
                if key == "text":
                    value = cls._clip_text(str(value), 800)
                compact[key] = value
            if compact:
                compacted.append(compact)
        return compacted

    @staticmethod
    def _clip_text(text: str, limit: int) -> str:
        value = str(text or "")
        max_chars = max(200, int(limit or 200))
        if len(value) <= max_chars:
            return value
        return value[: max_chars - 16] + "\n...[truncated]"

    def _payload_to_minicpm_pcm_base64(self, payload: dict[str, Any]) -> tuple[str, str]:
        data_value = payload.get("audio_base64") or payload.get("data") or payload.get("audio") or ""
        audio_base64, mime_type = self._extract_media_base64(data_value, expected_prefix="audio/")
        mime_type = str(payload.get("mime_type") or payload.get("audio_mime_type") or mime_type or "").lower()
        encoding = str(payload.get("encoding") or "").strip().lower()
        sample_rate = self._safe_int(payload.get("sample_rate"), default=0)
        channels = self._safe_int(payload.get("channels"), default=1)

        if not audio_base64:
            return "", "audio packet has no audio data"

        if encoding in {"pcm_float32", "float32"} and sample_rate == 16000 and channels == 1:
            return audio_base64, ""

        if mime_type.startswith("audio/wav") or mime_type.startswith("audio/x-wav"):
            try:
                return self._wav_base64_to_float32_16k_base64(audio_base64), ""
            except Exception as exc:  # noqa: BLE001
                return "", f"failed to convert WAV audio for MiniCPM realtime: {exc}"

        if encoding in {"pcm_float32", "float32"} and sample_rate and sample_rate != 16000:
            return "", (
                "pcm_float32 audio must be 16kHz before reaching MiniCPM realtime; "
                f"got {sample_rate}Hz"
            )

        return "", (
            "MiniCPM realtime requires 16kHz mono float32 PCM base64. "
            f"Unsupported audio mime/encoding: mime={mime_type or '-'} encoding={encoding or '-'}"
        )

    @staticmethod
    def _extract_media_base64(value: Any, *, expected_prefix: str = "") -> tuple[str, str]:
        text = str(value or "").strip()
        if not text:
            return "", ""
        if text.startswith("data:"):
            header, sep, body = text.partition(",")
            if not sep:
                return "", ""
            mime_type = header[5:].split(";", 1)[0].strip()
            if expected_prefix and not mime_type.startswith(expected_prefix):
                return "", mime_type
            return body.strip(), mime_type
        return text, ""

    @staticmethod
    def _safe_int(value: Any, *, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @classmethod
    def _wav_base64_to_float32_16k_base64(cls, audio_base64: str) -> str:
        if audioop is None:
            raise RuntimeError("audioop is unavailable; cannot resample WAV audio")

        raw = base64.b64decode(audio_base64)
        with wave.open(io.BytesIO(raw), "rb") as reader:
            channels = int(reader.getnchannels())
            sample_rate = int(reader.getframerate())
            sample_width = int(reader.getsampwidth())
            frames = reader.readframes(reader.getnframes())

        if channels > 1:
            frames = audioop.tomono(frames, sample_width, 1.0 / channels, 1.0 / channels)
            channels = 1
        if sample_width != 2:
            frames = audioop.lin2lin(frames, sample_width, 2)
            sample_width = 2
        if sample_rate != 16000:
            frames, _ = audioop.ratecv(frames, sample_width, channels, sample_rate, 16000, None)

        import array

        samples_i16 = array.array("h")
        samples_i16.frombytes(frames)
        if sys.byteorder != "little":
            samples_i16.byteswap()
        samples_f32 = array.array("f", (max(-1.0, min(1.0, sample / 32768.0)) for sample in samples_i16))
        if sys.byteorder != "little":
            samples_f32.byteswap()
        return base64.b64encode(samples_f32.tobytes()).decode("ascii")

    @staticmethod
    def _silence_pcm_base64(*, samples: int) -> str:
        return base64.b64encode(b"\x00" * max(1, int(samples or 1)) * 4).decode("ascii")

    @staticmethod
    def _map_session_status(payload: dict[str, Any]) -> dict[str, Any]:
        packet_type = str(payload.get("type") or "")
        text = packet_type
        if packet_type in {"session.queued", "session.queue_update"}:
            text = (
                f"MiniCPM realtime queued: position={payload.get('position') or '-'} "
                f"wait={payload.get('estimated_wait_s') or '-'}s"
            )
        elif packet_type == "session.queue_done":
            text = "MiniCPM realtime worker assigned"
        elif packet_type == "session.created":
            text = f"MiniCPM realtime session created: {payload.get('session_id') or '-'}"
        return {
            "type": "status",
            "status": packet_type,
            "text": text,
            "payload": payload,
        }

    def _map_audio_delta(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        text_delta = str(payload.get("text") or "")
        if text_delta:
            self._assistant_text_parts.append(text_delta)
            messages.append(
                {
                    "type": "partial",
                    "role": "assistant",
                    "text": "".join(self._assistant_text_parts),
                    "delta": text_delta,
                    "kv_cache_length": payload.get("kv_cache_length"),
                    "upstream_type": payload.get("type"),
                }
            )

        audio = str(payload.get("audio") or "")
        if audio:
            messages.append(
                {
                    "type": "audio",
                    "role": "assistant",
                    "audio_base64": audio,
                    "encoding": "pcm_float32",
                    "sample_rate": 24000,
                    "channels": 1,
                    "end_of_turn": bool(payload.get("end_of_turn")),
                    "kv_cache_length": payload.get("kv_cache_length"),
                    "upstream_type": payload.get("type"),
                }
            )

        if bool(payload.get("end_of_turn")):
            messages.extend(
                self._finalize_assistant_text(
                    reason="end_of_turn",
                    kv_cache_length=payload.get("kv_cache_length"),
                )
            )
        return messages

    def _finalize_assistant_text(
        self,
        *,
        reason: str,
        kv_cache_length: Any = None,
    ) -> list[dict[str, Any]]:
        text = "".join(self._assistant_text_parts).strip()
        self._assistant_text_parts = []
        if not text:
            return []
        return [
            {
                "type": "final",
                "role": "assistant",
                "text": text,
                "finish_reason": reason,
                "kv_cache_length": kv_cache_length,
            }
        ]

    def _map_legacy_duplex_result(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        if payload.get("is_listen"):
            return [
                {
                    "type": "listen",
                    "role": "assistant",
                    "kv_cache_length": payload.get("kv_cache_length"),
                    "upstream_type": "result",
                }
            ]

        messages: list[dict[str, Any]] = []
        text = str(payload.get("text") or "")
        if text:
            messages.append({"type": "partial", "role": "assistant", "text": text, "upstream_type": "result"})
        audio = str(payload.get("audio_data") or "")
        if audio:
            messages.append(
                {
                    "type": "audio",
                    "role": "assistant",
                    "audio_base64": audio,
                    "encoding": "pcm_float32",
                    "sample_rate": 24000,
                    "channels": 1,
                    "end_of_turn": bool(payload.get("end_of_turn")),
                    "upstream_type": "result",
                }
            )
        if bool(payload.get("end_of_turn")) and text:
            messages.append(
                {
                    "type": "final",
                    "role": "assistant",
                    "text": text,
                    "finish_reason": "end_of_turn",
                    "kv_cache_length": payload.get("kv_cache_length"),
                }
            )
        return messages

    @staticmethod
    def _map_error(payload: dict[str, Any]) -> dict[str, Any]:
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or error.get("code") or "MiniCPM realtime error")
            code = error.get("code")
        else:
            message = str(error or payload.get("message") or "MiniCPM realtime error")
            code = None
        return {
            "type": "error",
            "message": message,
            "code": code,
            "payload": payload,
        }


def build_realtime_adapter(
    *,
    adapter_name: str,
    session_id: str,
    upstream_url: str,
    upstream_headers: dict[str, str] | None = None,
) -> BaseRealtimeAdapter:
    normalized = str(adapter_name or "passthrough").strip().lower()
    if normalized == "minicpm_realtime_v0":
        return MiniCPMRealtimeAdapter(
            session_id=session_id,
            upstream_url=upstream_url,
            upstream_headers=upstream_headers,
        )
    return PassthroughRealtimeAdapter(
        session_id=session_id,
        upstream_url=upstream_url,
        upstream_headers=upstream_headers,
    )
