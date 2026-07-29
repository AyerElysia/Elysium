"""Neo message-plane adapter for the N.E.K.O presentation surface."""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import os
from pathlib import Path
import time
import uuid
from typing import Any, Mapping

from mofox_wire import CoreSink, MessageEnvelope

from src.core.components.base.adapter import BaseAdapter
from src.core.managers.event_manager import get_event_manager
from src.kernel.logger import get_logger

from .protocol import (
    CLIENT_EVENT_TYPES,
    SurfaceEvent,
    SurfaceProtocolError,
    parse_input_audio_payload,
    parse_input_image_payload,
)
from .service import NekoSurfaceGateway

logger = get_logger("NekoSurfaceAdapter", color="#F5A6C8")

PLATFORM = "neko.surface"
_SURFACE_TTS_TIMEOUT_SECONDS = 30.0
_SURFACE_TTS_CONCURRENCY = 2
# Higgs returns a complete audio object for each request.  Keep requests
# short enough that the first utterance can start while later sentences are
# still being synthesized, without splitting ordinary short replies.
_SURFACE_TTS_SEGMENT_MAX_CHARS = 180
_SURFACE_TTS_SEGMENT_MIN_CHARS = 4
_SURFACE_TTS_SENTENCE_END = frozenset("。！？!?；;\n")
_ADAPTER_INSTANCE: "NekoSurfaceAdapter | None" = None


def get_neko_surface_adapter() -> "NekoSurfaceAdapter | None":
    return _ADAPTER_INSTANCE


def set_neko_surface_adapter(adapter: "NekoSurfaceAdapter | None") -> None:
    global _ADAPTER_INSTANCE
    _ADAPTER_INSTANCE = adapter


class NekoSurfaceAdapter(BaseAdapter):
    """Treat N.E.K.O as a body/channel while keeping cognition in Neo."""

    adapter_name = "neko_surface_adapter"
    adapter_version = "1.0.0"
    adapter_description = "elysia.surface.v1 N.E.K.O surface adapter"
    platform = PLATFORM
    run_in_subprocess = False

    def __init__(
        self,
        core_sink: CoreSink | None,
        plugin: Any | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(core_sink, plugin=plugin, transport=None, **kwargs)
        self.gateway: NekoSurfaceGateway = plugin.gateway
        self._tts_generation = 0
        self._tts_slots = asyncio.Semaphore(_SURFACE_TTS_CONCURRENCY)
        self._tts_synthesis_tasks: set[asyncio.Task[tuple[str, str] | None]] = set()
        self._tts_delivery_tasks: set[asyncio.Task[None]] = set()
        self._tts_tail: asyncio.Task[None] | None = None
        self.gateway.bind_input_handler(self.handle_surface_event)
        set_neko_surface_adapter(self)

    async def on_adapter_loaded(self) -> None:
        logger.info("N.E.K.O Surface adapter ready")

    async def on_adapter_unloaded(self) -> None:
        pending_tts = self._invalidate_surface_tts("adapter unloaded")
        if pending_tts:
            await asyncio.gather(*pending_tts, return_exceptions=True)
        if self.gateway is not None:
            self.gateway.bind_input_handler(None)
        set_neko_surface_adapter(None)

    async def health_check(self) -> bool:
        return True

    def is_connected(self) -> bool:  # type: ignore[override]
        return bool(getattr(self.gateway, "_clients", {}))

    @staticmethod
    def _auto_tts_enabled() -> bool:
        """Return whether Surface text should be voiced by Neo's TTS service."""
        raw = os.environ.get("NEKO_SURFACE_AUTO_TTS")
        if raw is None:
            return True
        return raw.strip().lower() in {"1", "true", "yes", "on", "enabled"}

    @staticmethod
    def _get_tts_service() -> Any | None:
        """Resolve the loaded TTS service without making Surface depend on it."""
        try:
            from src.core.managers import get_plugin_manager

            plugin = get_plugin_manager().get_plugin("tts_voice_plugin")
            service = getattr(plugin, "tts_service", None) if plugin is not None else None
            if service is not None and callable(getattr(service, "generate_voice", None)):
                return service
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Surface TTS plugin lookup failed: {exc}")

        try:
            from src.app.plugin_system.api.service_api import get_service

            service = get_service("tts_voice_plugin:service:tts")
            if service is not None and callable(getattr(service, "generate_voice", None)):
                return service
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Surface TTS service lookup failed: {exc}")
        return None

    @staticmethod
    def _tts_mime_type(service: Any) -> str:
        """Map the configured TTS response format to a browser/decoder MIME."""
        config = getattr(service, "_config", None)
        format_config = getattr(config, "tts_advanced", None)
        response_format = str(
            getattr(format_config, "media_type", "wav") or "wav"
        ).strip().lower()
        return {
            "mp3": "audio/mpeg",
            "mpeg": "audio/mpeg",
            "opus": "audio/opus",
            "ogg": "audio/ogg",
            "wav": "audio/wav",
            "aac": "audio/aac",
            "flac": "audio/flac",
            "pcm": "audio/pcm",
        }.get(response_format, "audio/wav")

    def _invalidate_surface_tts(self, reason: str) -> list[asyncio.Task[Any]]:
        """Cancel all queued voices when the active user turn is superseded."""
        self._tts_generation += 1
        tasks: list[asyncio.Task[Any]] = [
            *self._tts_synthesis_tasks,
            *self._tts_delivery_tasks,
        ]
        self._tts_synthesis_tasks.clear()
        self._tts_delivery_tasks.clear()
        self._tts_tail = None
        active = [task for task in tasks if not task.done()]
        if active:
            logger.info(f"Surface auto TTS queue cancelled ({len(active)} tasks): {reason}")
        for task in active:
            task.cancel()
        return tasks

    def _on_surface_tts_synthesis_done(
        self,
        task: asyncio.Task[tuple[str, str] | None],
    ) -> None:
        self._tts_synthesis_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Surface auto TTS synthesis task failed: {exc}")

    def _on_surface_tts_delivery_done(self, task: asyncio.Task[None]) -> None:
        self._tts_delivery_tasks.discard(task)
        if self._tts_tail is task:
            self._tts_tail = None
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Surface auto TTS delivery task failed: {exc}")

    def _schedule_surface_voice(
        self,
        text: str,
        *,
        turn_id: str,
        metadata: Mapping[str, Any],
        generation: int,
    ) -> None:
        """Synthesize concurrently while preserving voice publication order."""
        if (
            not self._auto_tts_enabled()
            or not text.strip()
            or generation != self._tts_generation
        ):
            return

        service = self._get_tts_service()
        if service is None:
            logger.warning("Surface auto TTS skipped: tts_voice_plugin service is unavailable")
            return

        predecessor = self._tts_tail
        synthesis_task = asyncio.create_task(
            self._generate_surface_voice(
                text,
                generation=generation,
                service=service,
            ),
            name=f"neko_surface_tts_synthesis:{turn_id}",
        )
        delivery_task = asyncio.create_task(
            self._deliver_surface_voice(
                synthesis_task,
                predecessor=predecessor,
                turn_id=turn_id,
                metadata=dict(metadata),
                generation=generation,
            ),
            name=f"neko_surface_tts_delivery:{turn_id}",
        )
        self._tts_synthesis_tasks.add(synthesis_task)
        self._tts_delivery_tasks.add(delivery_task)
        self._tts_tail = delivery_task
        synthesis_task.add_done_callback(self._on_surface_tts_synthesis_done)
        delivery_task.add_done_callback(self._on_surface_tts_delivery_done)

    @staticmethod
    def _split_surface_tts_text(text: str) -> list[str]:
        """Split a completed reply into speakable units for application streaming.

        The model currently hands the message sender a completed string rather
        than token deltas.  Sentence-sized Higgs requests still improve
        time-to-first-audio and preserve natural prosody better than character
        chunks.  A long punctuation-free run is capped as a final fallback.
        """
        source = str(text or "").strip()
        if not source:
            return []

        segments: list[str] = []
        current: list[str] = []

        def flush() -> None:
            value = "".join(current).strip()
            if value:
                segments.append(value)
            current.clear()

        for char in source:
            current.append(char)
            joined = "".join(current)
            if char in _SURFACE_TTS_SENTENCE_END and len(joined) >= _SURFACE_TTS_SEGMENT_MIN_CHARS:
                flush()
                continue
            if len(joined) >= _SURFACE_TTS_SEGMENT_MAX_CHARS:
                # Prefer a nearby comma/space when a provider emits a very
                # long sentence; otherwise cut at the configured hard cap.
                cut = max(
                    joined.rfind(mark, 0, _SURFACE_TTS_SEGMENT_MAX_CHARS)
                    for mark in ("，", ",", "、", " ")
                )
                if cut >= _SURFACE_TTS_SEGMENT_MIN_CHARS:
                    prefix = joined[: cut + 1].strip()
                    suffix = joined[cut + 1 :]
                    segments.append(prefix)
                    current.clear()
                    current.extend(suffix)
                else:
                    flush()
        flush()
        return segments

    async def _generate_surface_voice(
        self,
        text: str,
        *,
        generation: int,
        service: Any,
    ) -> tuple[str, str] | None:
        """Generate one automatic Higgs voice for a Surface text reply."""
        if not text.strip() or generation != self._tts_generation:
            return None

        try:
            async with self._tts_slots:
                if generation != self._tts_generation:
                    return None
                audio_b64 = await asyncio.wait_for(
                    service.generate_voice(text, "default"),
                    timeout=_SURFACE_TTS_TIMEOUT_SECONDS,
                )
        except asyncio.TimeoutError:
            logger.warning(
                f"Surface auto TTS timed out after {_SURFACE_TTS_TIMEOUT_SECONDS:.1f}s"
            )
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Surface auto TTS failed: {exc}")
            return None
        if not isinstance(audio_b64, str) or not audio_b64.strip():
            logger.warning("Surface auto TTS returned no audio")
            return None
        if generation != self._tts_generation:
            logger.info("Surface auto TTS result discarded: a newer reply superseded it")
            return None
        return audio_b64, self._tts_mime_type(service)

    async def _deliver_surface_voice(
        self,
        synthesis_task: asyncio.Task[tuple[str, str] | None],
        *,
        predecessor: asyncio.Task[None] | None,
        turn_id: str,
        metadata: Mapping[str, Any],
        generation: int,
    ) -> None:
        """Publish completed synthesis after every earlier voice job settles."""
        try:
            generated_voice = await synthesis_task
            if predecessor is not None:
                await asyncio.gather(predecessor, return_exceptions=True)
            if generated_voice is None or generation != self._tts_generation:
                return
            audio_b64, mime_type = generated_voice
            await self.gateway.publish(
                "assistant.voice",
                payload={
                    "data": audio_b64,
                    "mime_type": mime_type,
                    "speech_id": turn_id,
                    "metadata": dict(metadata),
                },
                turn_id=turn_id,
                priority=7,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Surface auto TTS delivery failed: {exc}")

    async def get_bot_info(self) -> dict[str, Any]:  # type: ignore[override]
        return {
            "bot_id": "elysia_surface",
            "bot_name": "Elysia Surface",
            "platform": PLATFORM,
        }

    async def handle_surface_event(self, event: SurfaceEvent) -> None:
        """Dispatch an authenticated client event into the canonical message plane."""
        if event.type in {"user.text", "user.transcript.final", "user.audio", "user.screen"}:
            # A new user turn invalidates any Higgs request still synthesizing
            # the previous reply, so late audio cannot be played out of order.
            self._invalidate_surface_tts("new user turn")
            envelope = await self.from_platform_message(event)
            if self.core_sink is None:
                raise RuntimeError("NekoSurfaceAdapter CoreSink is not ready")
            await self.core_sink.send(envelope)
            return

        if event.type == "user.interaction":
            # Gestures are useful to expression/action plugins, but should not
            # silently become a second chat turn unless they include text.
            await get_event_manager().publish_event(
                "neko_surface:user_interaction",
                {
                    "event": event,
                    "surface_id": event.surface_id,
                    "character": event.character,
                },
            )
            interaction_text = str(event.payload.get("text") or "").strip()
            if interaction_text:
                self._invalidate_surface_tts("new user interaction")
                envelope = await self.from_platform_message(event)
                if self.core_sink is None:
                    raise RuntimeError("NekoSurfaceAdapter CoreSink is not ready")
                await self.core_sink.send(envelope)
            return

        # playback/state events are consumed by the gateway itself. Unknown
        # events are rejected by protocol validation before reaching here.
        if event.type not in {"playback.started", "playback.ended", "state", "ack"}:
            raise SurfaceProtocolError("unsupported_event", event.type)

    async def from_platform_message(  # type: ignore[override]
        self,
        raw: SurfaceEvent | Mapping[str, Any],
    ) -> MessageEnvelope:
        event = raw if isinstance(raw, SurfaceEvent) else SurfaceEvent.from_dict(
            raw,
            allowed_types=CLIENT_EVENT_TYPES,
        )
        if event.type not in {
            "user.text",
            "user.transcript.final",
            "user.audio",
            "user.screen",
            "user.interaction",
        }:
            raise SurfaceProtocolError("not_user_event", f"cannot convert {event.type}")

        text = ""
        if event.type not in {"user.audio", "user.screen"}:
            text = str(event.payload.get("text") or "").strip()
            if not text:
                raise SurfaceProtocolError("missing_text", "surface user event has no text")

        user_id = str(event.payload.get("user_id") or "neko_owner").strip() or "neko_owner"
        nickname = str(event.payload.get("user_name") or "主人").strip() or "主人"
        timestamp = event.payload.get("timestamp", time.time())
        try:
            timestamp_value = float(timestamp)
        except (TypeError, ValueError):
            timestamp_value = time.time()

        message_id = event.event_id or f"surface_{uuid.uuid4().hex}"
        metadata = {
            "source": "neko_surface",
            "schema_version": event.schema_version,
            "surface_id": event.surface_id,
            "session_id": event.session_id,
            "character": event.character,
            "turn_id": event.turn_id,
            "event_type": event.type,
            "origin": event.origin,
            # This is an authenticated one-to-one surface. Do not hold a
            # direct user turn in the global QQ/group message coalescing window.
            "bypass_message_buffer": True,
        }
        if event.type == "user.audio":
            audio_base64, mime_type = parse_input_audio_payload(event.payload)
            duration = event.payload.get(
                "duration_seconds",
                event.payload.get("duration"),
            )
            audio_data: dict[str, Any] = {
                "base64": audio_base64,
                "mime_type": mime_type,
            }
            if duration is not None:
                audio_data["duration_seconds"] = float(duration)
            message_segments: list[dict[str, Any]] = [
                {
                    "type": "voice",
                    "data": audio_data,
                    "mime_type": mime_type,
                }
            ]
        elif event.type == "user.screen":
            image_base64, mime_type = parse_input_image_payload(event.payload)
            image_data: dict[str, Any] = {
                "base64": image_base64,
                "mime_type": mime_type,
            }
            payload_metadata = event.payload.get("metadata")
            screen_title = event.payload.get("window_title")
            if screen_title is None and isinstance(payload_metadata, Mapping):
                screen_title = payload_metadata.get("window_title")
            screen_capture_type = event.payload.get("capture_type")
            if screen_capture_type is None and isinstance(payload_metadata, Mapping):
                screen_capture_type = payload_metadata.get("capture_type")
            context_parts = ["[N.E.K.O 主动屏幕观察]"]
            if screen_title:
                context_parts.append(f"当前窗口：{str(screen_title).strip()[:256]}")
            if screen_capture_type:
                context_parts.append(f"采集类型：{str(screen_capture_type).strip()[:64]}")
            message_segments = [
                {"type": "text", "data": " ".join(context_parts)},
                {
                    "type": "image",
                    "data": image_data,
                    "mime_type": mime_type,
                }
            ]
            # Active screen observations are opportunities for the unified
            # Life Chatter brain, rather than direct user turns. Keep only the
            # bounded metadata needed to reason about the observed window.
            metadata.update(
                {
                    "is_proactive_opportunity_trigger": True,
                    "is_proactive_vision": True,
                    "proactive": True,
                }
            )
            for key in ("window_title", "capture_type"):
                value = event.payload.get(key)
                if value is None and isinstance(payload_metadata, Mapping):
                    value = payload_metadata.get(key)
                if value is not None:
                    metadata[key] = str(value).strip()[:256]
            enabled_modes = event.payload.get("enabled_modes")
            if enabled_modes is None and isinstance(payload_metadata, Mapping):
                enabled_modes = payload_metadata.get("enabled_modes")
            if isinstance(enabled_modes, (list, tuple)):
                metadata["enabled_modes"] = [
                    str(item).strip()[:64]
                    for item in enabled_modes
                    if str(item).strip()
                ][:16]
        else:
            message_segments = [{"type": "text", "data": text}]

        return {
            "direction": "incoming",
            "message_info": {
                "platform": PLATFORM,
                "message_id": message_id,
                "time": timestamp_value,
                "user_info": {
                    "platform": PLATFORM,
                    "user_id": user_id,
                    "user_nickname": nickname,
                },
                "extra": metadata,
            },
            "message_segment": message_segments,
            "metadata": metadata,
        }

    async def _send_platform_message(  # type: ignore[override]
        self,
        envelope: MessageEnvelope,
    ) -> None:
        """Translate canonical outgoing segments into presentation events."""
        message_info = envelope.get("message_info") or {}
        segments = envelope.get("message_segment") or envelope.get("message_chain") or []
        if isinstance(segments, dict):
            segments = [segments]
        if not isinstance(segments, (list, tuple)):
            segments = []

        turn_id = str(
            message_info.get("turn_id")
            or message_info.get("request_id")
            or message_info.get("message_id")
            or uuid.uuid4().hex
        )
        metadata = message_info.get("extra")
        if not isinstance(metadata, dict):
            metadata = {}

        text_parts: list[str] = []
        media: list[dict[str, Any]] = []
        voices: list[dict[str, Any]] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            segment_type = str(segment.get("type") or "").strip().lower()
            data = segment.get("data")
            if segment_type == "text":
                value = str(data or "")
                if value:
                    text_parts.append(value)
            elif segment_type in {"voice", "audio"}:
                voice_data, voice_mime = _serialise_media_data(data, segment.get("mime_type"))
                voices.append(
                    {
                        "data": voice_data,
                        "mime_type": voice_mime,
                        "speech_id": str(segment.get("speech_id") or turn_id),
                    }
                )
            elif segment_type in {"image", "file", "video"}:
                media_data, _ = _serialise_media_data(data, segment.get("mime_type"))
                media.append({"type": segment_type, "data": media_data, "name": segment.get("name")})

        if voices:
            self._invalidate_surface_tts("explicit voice segment")
        tts_generation = self._tts_generation

        text_deliveries = 0
        if text_parts:
            text_deliveries = await self.gateway.publish(
                "assistant.text",
                payload={
                    "text": "".join(text_parts),
                    "is_final": True,
                    "metadata": metadata,
                },
                turn_id=turn_id,
                priority=6,
            )

        for voice in voices:
            await self.gateway.publish(
                "assistant.voice",
                payload={**voice, "metadata": metadata},
                turn_id=turn_id,
                priority=7,
            )
        if media:
            await self.gateway.publish(
                "assistant.media",
                payload={"items": media, "metadata": metadata},
                turn_id=turn_id,
                priority=4,
            )
        if text_parts or voices or media:
            await self.gateway.publish(
                "turn.end",
                payload={"reason": "message_delivered", "message_id": message_info.get("message_id")},
                turn_id=turn_id,
                priority=9,
            )

        if text_parts and not voices and text_deliveries:
            # Higgs is request/response rather than PCM-delta streaming.  Send
            # sentence-sized requests concurrently and chain publication so
            # N.E.K.O receives one continuous, ordered speech stream.
            tts_text = "".join(text_parts)
            for segment in self._split_surface_tts_text(tts_text):
                self._schedule_surface_voice(
                    segment,
                    turn_id=turn_id,
                    metadata=metadata,
                    generation=tts_generation,
                )


def _serialise_media_data(data: Any, mime_type: Any = None) -> tuple[Any, str]:
    """Make local media JSON-safe; N.E.K.O cannot read Neo's filesystem paths."""
    resolved_mime = str(mime_type or "").strip().lower()
    if isinstance(data, (bytes, bytearray)):
        return base64.b64encode(bytes(data)).decode("ascii"), resolved_mime or "application/octet-stream"
    if isinstance(data, dict):
        nested_mime = data.get("mime_type") or data.get("content_type") or resolved_mime
        nested = data.get("data")
        if nested is None:
            nested = data.get("url") or data.get("path") or ""
        return _serialise_media_data(nested, nested_mime)
    if isinstance(data, Path):
        data = str(data)
    if isinstance(data, str):
        candidate = data.strip()
        if candidate and not candidate.startswith(("http://", "https://", "data:", "base64|")):
            try:
                path = Path(candidate)
                if path.is_file() and path.stat().st_size <= 16 * 1024 * 1024:
                    raw = path.read_bytes()
                    guessed = mimetypes.guess_type(path.name)[0]
                    return base64.b64encode(raw).decode("ascii"), resolved_mime or guessed or "application/octet-stream"
            except OSError:
                pass
        if candidate.startswith("base64|"):
            return candidate[7:], resolved_mime or "application/octet-stream"
        return candidate, resolved_mime or "application/octet-stream"
    return str(data or ""), resolved_mime or "application/octet-stream"
