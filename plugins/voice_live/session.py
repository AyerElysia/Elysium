"""One deterministic realtime voice session and its owned resources."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
import uuid
from collections.abc import Callable
from typing import Any

from src.kernel.logger import get_logger

from .audio import resample_pcm16_mono
from .audio_archive import AudioTrackSpec, VoiceAudioArchive
from .config import VoiceLiveConfig
from .consciousness import VoiceLiveConsciousnessManager
from .context_bridge import ContextBridge, VoicePromptBundle
from .protocol import (
    AUDIO_MAGIC,
    ProviderState,
    SessionState,
    pack_audio_frame,
    unpack_audio_frame,
)
from .providers.base import (
    AudioDelta,
    BaseRealtimeProvider,
    InterruptionEvent,
    ProviderMetrics,
    ToolCallEvent,
    TranscriptEvent,
)
from .providers.factory import create_provider
from .runtime_store import VoiceEpisodeStore
from .tool_broker import VoiceToolBroker
from .voice_conversion import (
    ConvertedAudio,
    VoiceConverter,
    create_voice_converter,
)

logger = get_logger("voice_live.session", display="Voice Call")
ProviderFactory = Callable[[VoiceLiveConfig], BaseRealtimeProvider]
VoiceConverterFactory = Callable[[VoiceLiveConfig], VoiceConverter | None]


class CallSession:
    """Own one provider, consciousness instance and durable voice episode."""

    def __init__(
        self,
        config: VoiceLiveConfig,
        session_id: str | None = None,
        *,
        provider_factory: ProviderFactory = create_provider,
        voice_converter_factory: VoiceConverterFactory = create_voice_converter,
        store: VoiceEpisodeStore | None = None,
        consciousness: VoiceLiveConsciousnessManager | None = None,
        bridge: ContextBridge | None = None,
        tool_broker: VoiceToolBroker | None = None,
    ) -> None:
        self._config = config
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.episode_id = self.session_id
        self._provider_factory = provider_factory
        self._voice_converter_factory = voice_converter_factory
        self._runtime_injected = any(
            value is not None for value in (store, consciousness, bridge, tool_broker)
        )
        instance_id = f"{config.session.instance_id_prefix}_{self.episode_id}"
        self._store = store or VoiceEpisodeStore(
            config.observability.trace_root, instance_id, self.episode_id
        )
        self._consciousness = consciousness or VoiceLiveConsciousnessManager(
            config, self.episode_id, self._store
        )
        self._bridge = bridge or ContextBridge(config, self._consciousness, self._store)
        self._tool_broker = tool_broker or VoiceToolBroker(
            self._consciousness, config, self._store
        )
        self._provider: BaseRealtimeProvider | None = None
        self._voice_converter: VoiceConverter | None = None
        self._conversion_queue: (
            asyncio.Queue[tuple[str, int, AudioDelta | None]] | None
        ) = None
        self._conversion_task: asyncio.Task[None] | None = None
        self._conversion_generation = 0
        self._converted_audio_bytes = 0
        self._conversion_blocks = 0
        self._conversion_inference_ms = 0.0
        self._state = SessionState.CREATED
        self._send_json: Any = None
        self._send_bytes: Any = None
        self._last_input_sequence = -1
        self._output_sequence = 0
        self._input_audio_bytes = 0
        self._output_audio_bytes = 0
        self._interruptions = 0
        self._created_monotonic = time.monotonic()
        self._failure_reason = ""
        self._dynamic_context_refresh_lock = asyncio.Lock()
        self._dynamic_context_frontier = 0
        self._dynamic_context_delivered_frontier: int | None = None
        self._last_unverified_dynamic_context_signature = ""
        self._dynamic_context_refresh_task: asyncio.Task[None] | None = None
        self._provider_starting = False
        self._state_report_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        self._state_report_task: asyncio.Task[None] | None = None
        self._state_reports_enabled = False
        self._subject_context_audit: dict[str, Any] = {}
        self._audio_archive: VoiceAudioArchive | None = None
        self._audio_archive_summary: dict[str, Any] = {}
        self._audio_capture_error = ""

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def mode(self) -> str:
        return "full_duplex" if self._state is SessionState.ACTIVE else ""

    @property
    def is_active(self) -> bool:
        return self._state is SessionState.ACTIVE

    @property
    def provider_name(self) -> str:
        return self._provider.provider_name if self._provider is not None else ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "episode_id": self.episode_id,
            "state": self._state.value,
            "provider": self.provider_name,
            "voice_conversion": (
                self._config.voice_conversion.profile_id
                if self._voice_converter is not None
                else ""
            ),
            "voice_conversion_revision": (
                str(getattr(self._voice_converter, "profile_revision", ""))
                if self._voice_converter is not None
                else ""
            ),
            "voice_conversion_queue": (
                self._conversion_queue.qsize() if self._conversion_queue else 0
            ),
            "voice_conversion_blocks": self._conversion_blocks,
            "voice_conversion_inference_ms": round(self._conversion_inference_ms, 3),
            "subject_context_revision": self._subject_context_audit.get("revision", ""),
            "subject_context_source_digest": self._subject_context_audit.get(
                "source_digest", ""
            ),
            "input_audio_bytes": self._input_audio_bytes,
            "output_audio_bytes": self._output_audio_bytes,
            "interruptions": self._interruptions,
            "age_seconds": round(time.monotonic() - self._created_monotonic, 3),
            "failure_reason": self._failure_reason,
            "audio_archive": self._audio_archive_snapshot(),
        }

    def set_send_callbacks(self, send_json: Any, send_bytes: Any) -> None:
        self._send_json = send_json
        self._send_bytes = send_bytes

    def _resume_runtime(self, episode_id: str) -> None:
        if not episode_id or episode_id == self.episode_id:
            return
        if self._runtime_injected:
            raise RuntimeError("cannot replace an injected test runtime")
        self.episode_id = episode_id
        instance_id = f"{self._config.session.instance_id_prefix}_{episode_id}"
        self._store = VoiceEpisodeStore(
            self._config.observability.trace_root, instance_id, episode_id
        )
        self._consciousness = VoiceLiveConsciousnessManager(
            self._config, episode_id, self._store
        )
        self._bridge = ContextBridge(self._config, self._consciousness, self._store)
        self._tool_broker = VoiceToolBroker(
            self._consciousness, self._config, self._store
        )
        self._audio_archive = None
        self._audio_archive_summary = {}
        self._audio_capture_error = ""

    async def start(self, mode: str = "auto", *, resume_episode_id: str = "") -> bool:
        """Start the explicit provider; never switch models implicitly."""
        if self._state is not SessionState.CREATED:
            await self._send_error("会话已经启动或结束")
            return False
        if mode not in {"auto", "full_duplex"}:
            await self._fail(f"不支持的模式: {mode}；Voice Live 不会隐式切换模型")
            return False

        try:
            self._resume_runtime(resume_episode_id)
        except Exception as exc:  # noqa: BLE001 - persisted runtime boundary
            await self._fail(f"恢复会话失败: {exc}")
            return False
        self._state = SessionState.CONNECTING
        await self._store.append_async(
            "session.connecting",
            {"session_id": self.session_id, "resume": bool(resume_episode_id)},
        )
        await self._store.checkpoint_async("connecting", session_id=self.session_id)
        try:
            provider = self._provider_factory(self._config)
            self._provider = provider
            await self._start_audio_archive(provider)
            self._register_provider_callbacks(provider)
            await self._consciousness.activate(provider.provider_name)
            self._state_reports_enabled = True
            converter = self._voice_converter_factory(self._config)
            if converter is not None:
                self._voice_converter = converter
                conversion_info = await converter.connect()
                self._conversion_queue = asyncio.Queue(
                    maxsize=self._config.voice_conversion.queue_max_chunks
                )
                self._conversion_task = asyncio.create_task(
                    self._conversion_loop(),
                    name=f"voice-conversion-{self.session_id}",
                )
                await self._store.append_async(
                    "voice_conversion.ready",
                    {
                        "profile_id": self._config.voice_conversion.profile_id,
                        "input_sample_rate": converter.input_sample_rate,
                        "output_sample_rate": converter.output_sample_rate,
                        "service": conversion_info.get("health", {}),
                    },
                )
                if self._audio_archive is not None:
                    self._audio_archive.update_metadata(
                        voice_conversion_active=True,
                        voice_conversion_profile=(
                            self._config.voice_conversion.profile_id
                        ),
                        voice_conversion_revision=str(
                            getattr(converter, "profile_revision", "")
                        ),
                        playback_track="assistant_converted",
                    )
            schemas = self._tool_broker.schemas()
            prompt_result = self._bridge.build_system_prompt()
            if inspect.isawaitable(prompt_result):
                prompt_result = await prompt_result
            if isinstance(prompt_result, VoicePromptBundle):
                instructions = prompt_result.text
                self._subject_context_audit = dict(prompt_result.subject_context)
                context_layers = prompt_result.layers
            else:
                # Isolated test bridges may still expose the legacy string contract.
                instructions = str(prompt_result)
                context_layers = {}
            if self._audio_archive is not None:
                self._audio_archive.update_metadata(
                    subject_context_revision=self._subject_context_audit.get(
                        "revision", ""
                    ),
                    subject_context_source_digest=self._subject_context_audit.get(
                        "source_digest", ""
                    ),
                    subject_context_projection_sha256=self._subject_context_audit.get(
                        "projection_sha256", ""
                    ),
                )
            await self._store.append_async(
                "provider.configuration",
                {
                    "provider": provider.provider_name,
                    "instruction_chars": len(instructions),
                    "instruction_bytes": len(instructions.encode("utf-8")),
                    "tool_count": len(schemas),
                    "tool_schema_bytes": len(
                        json.dumps(
                            schemas,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ),
                    "context_layers": context_layers,
                },
            )
            dynamic_context_task: asyncio.Task[tuple[str, Any | None]] | None = None
            if self._config.session.cross_scene_awareness:
                builder = getattr(self._bridge, "build_llm_context_prefix", None)
                if callable(builder):
                    dynamic_context_task = asyncio.create_task(
                        builder(),
                        name=f"voice-subconscious-prepare-{self.session_id}",
                    )
            self._provider_starting = True
            try:
                try:
                    await provider.connect(
                        {
                            "instructions": instructions,
                            "model": self._config.full_duplex.model_name,
                            "voice": self._config.full_duplex.voice,
                            "qwen_max_history_turns": (
                                self._config.full_duplex.qwen_max_history_turns
                            ),
                            "qwen_turn_detection": (
                                self._config.full_duplex.qwen_turn_detection
                            ),
                            "qwen_vad_threshold": (
                                self._config.full_duplex.qwen_vad_threshold
                            ),
                            "qwen_vad_silence_duration_ms": (
                                self._config.full_duplex.qwen_vad_silence_duration_ms
                            ),
                            "tools": schemas,
                            "provider_config": {
                                "tools_available": [
                                    schema["name"] for schema in schemas
                                ]
                            },
                        }
                    )
                except BaseException:
                    if (
                        dynamic_context_task is not None
                        and not dynamic_context_task.done()
                    ):
                        dynamic_context_task.cancel()
                    if dynamic_context_task is not None:
                        await asyncio.gather(
                            dynamic_context_task,
                            return_exceptions=True,
                        )
                    raise
                if dynamic_context_task is not None:
                    await self._deliver_dynamic_context(
                        *(await dynamic_context_task)
                    )
            finally:
                self._provider_starting = False
        except Exception as exc:
            failure = str(exc).strip() or type(exc).__name__
            logger.error(  # noqa: G201 - project Logger has no exception() method
                f"实时语音会话启动失败: {failure}",
                exc_info=True,
            )
            provider = self._provider
            self._provider = None
            if provider is not None:
                try:
                    await provider.disconnect()
                except Exception as cleanup_exc:  # noqa: BLE001
                    logger.debug(f"Provider startup cleanup failed: {cleanup_exc}")
            await self._stop_state_reports()
            await self._stop_dynamic_context_refresh()
            await self._stop_voice_conversion()
            await self._stop_audio_archive(reason="startup_failed")
            if self._consciousness.is_active:
                await self._consciousness.suspend(reason="startup_failed")
            await self._fail(f"启动失败: {failure}")
            return False

        self._state = SessionState.ACTIVE
        self._queue_state_report("实时通话已连接，正在倾听")
        await self._store.append_async(
            "session.ready",
            {"session_id": self.session_id, "provider": provider.provider_name},
        )
        await self._store.checkpoint_async(
            "active",
            session_id=self.session_id,
            provider=provider.provider_name,
            subject_context=self._subject_context_audit,
        )
        await self._send_json_safe(
            {
                "type": "ready",
                "mode": "full_duplex",
                "provider": provider.provider_name,
                "session_id": self.session_id,
                "episode_id": self.episode_id,
                "protocol": 1,
                "input_sample_rate": self._config.audio.input_sample_rate,
                "output_sample_rate": self._config.audio.output_sample_rate,
                "voice_profile": (
                    self._config.voice_conversion.profile_id
                    if self._voice_converter is not None
                    else ""
                ),
                "audio_capture": self._audio_archive_snapshot(),
            }
        )
        return True

    async def stop(self, *, reason: str = "normal") -> None:
        if self._state in {SessionState.STOPPING, SessionState.ENDED}:
            return
        was_failed = self._state is SessionState.FAILED
        self._state = SessionState.STOPPING
        errors: list[Exception] = []
        await self._stop_state_reports()
        await self._stop_dynamic_context_refresh()
        provider = self._provider
        self._provider = None
        if provider is not None:
            try:
                await provider.disconnect()
            except Exception as exc:  # noqa: BLE001 - lifecycle cleanup boundary
                errors.append(exc)
        try:
            await self._stop_voice_conversion()
        except Exception as exc:  # noqa: BLE001 - lifecycle cleanup boundary
            errors.append(exc)
        try:
            await self._stop_audio_archive(reason=reason)
        except Exception as exc:  # noqa: BLE001 - lifecycle cleanup boundary
            errors.append(exc)
        if self._consciousness.is_active:
            try:
                await self._consciousness.suspend(reason=reason)
            except Exception as exc:  # noqa: BLE001 - lifecycle cleanup boundary
                errors.append(exc)
        final_state = SessionState.FAILED if was_failed else SessionState.ENDED
        try:
            await self._store.append_async(
                "session.ended",
                {
                    "reason": reason,
                    "failed": was_failed,
                    "cleanup_errors": [str(exc) for exc in errors],
                    "audio_archive": self._audio_archive_snapshot(),
                },
            )
            await self._store.checkpoint_async(
                final_state.value,
                reason=reason,
                cleanup_errors=[str(exc) for exc in errors],
                metrics=self.snapshot(),
            )
        except Exception as exc:  # noqa: BLE001 - checkpoint boundary
            errors.append(exc)
        self._state = final_state
        await self._send_json_safe(
            {"type": "ended", "reason": reason, "state": final_state.value}
        )
        if errors:
            logger.warning(
                "Voice Live 会话结束时有 %d 个清理错误: %s",
                len(errors),
                "; ".join(str(exc) for exc in errors),
            )

    async def handle_message(self, data: dict[str, Any]) -> None:
        message_type = str(data.get("type") or "")
        if message_type == "start":
            await self.start(
                str(data.get("mode") or "auto"),
                resume_episode_id=str(data.get("resume_episode_id") or ""),
            )
        elif message_type == "interrupt":
            if self._provider is not None and self.is_active:
                self._interruptions += 1
                await self._reset_voice_conversion()
                await self._send_json_safe(
                    {"type": "playback.clear", "reason": "client_barge_in"}
                )
                played_ms = data.get("played_audio_ms")
                await self._provider.interrupt(
                    played_audio_ms=int(played_ms) if played_ms is not None else None
                )
        elif message_type == "text":
            text = str(data.get("text") or "").strip()
            if text and self._provider is not None and self.is_active:
                await self._provider.send_text(text)
        elif message_type == "stop":
            await self.stop(reason="client_stop")
        elif message_type == "ping":
            await self._send_json_safe(
                {
                    "type": "pong",
                    "server_monotonic_ms": time.monotonic_ns() // 1_000_000,
                }
            )
        else:
            await self._send_error(f"未知消息类型: {message_type}")

    async def handle_audio(self, data: bytes) -> None:
        if not self.is_active or self._provider is None or not data:
            return
        if not data.startswith(AUDIO_MAGIC):
            raise ValueError("audio frame must use Voice Live protocol v1")
        frame = unpack_audio_frame(data)
        if frame.sequence <= self._last_input_sequence:
            raise ValueError(
                f"audio sequence must increase: {frame.sequence} <= {self._last_input_sequence}"
            )
        self._last_input_sequence = frame.sequence
        pcm16 = resample_pcm16_mono(
            frame.pcm16, frame.sample_rate, self._provider.input_sample_rate
        )
        self._input_audio_bytes += len(pcm16)
        self._archive_audio("user_input", pcm16, self._provider.input_sample_rate)
        await self._provider.send_audio(pcm16)

    def _register_provider_callbacks(self, provider: BaseRealtimeProvider) -> None:
        provider.on_audio_delta(self._on_audio)
        provider.on_state_change(self._on_provider_state)
        provider.on_transcript(self._on_transcript)
        provider.on_error(self._on_error)
        provider.on_interruption(self._on_interruption)
        provider.on_metrics(self._on_metrics)
        provider.on_tool_call(self._on_tool_call)
        provider.on_response_done(self._on_response_done)

    async def _on_audio(self, event: AudioDelta) -> None:
        self._archive_audio("assistant_source", event.data, event.sample_rate)
        if self._voice_converter is not None:
            queue = self._conversion_queue
            if queue is None or self._conversion_task is None:
                await self._fail("爱莉实时音色转换管线未运行")
                return
            try:
                queue.put_nowait(("audio", self._conversion_generation, event))
            except asyncio.QueueFull:
                await self._fail(
                    "爱莉实时音色转换队列已满；为避免声音延迟持续增长，会话已停止"
                )
            return
        await self._emit_audio(event)

    async def _emit_audio(self, event: AudioDelta) -> None:
        if self._send_bytes is None or not event.data:
            return
        self._output_sequence += 1
        self._output_audio_bytes += len(event.data)
        await self._send_bytes(
            pack_audio_frame(self._output_sequence, event.sample_rate, event.data)
        )

    async def _on_provider_state(self, state: ProviderState) -> None:
        if state is ProviderState.LISTENING:
            await self._queue_conversion_control("flush")
            if not self._provider_starting:
                self._schedule_dynamic_context_refresh()
        await self._send_json_safe({"type": "state", "state": state.value})
        await self._store.append_async("provider.state", {"state": state.value})
        if self._consciousness.is_active:
            self._queue_state_report(f"实时通话状态：{state.value}")

    def _queue_state_report(self, summary: str) -> None:
        """Coalesce slow World state writes outside the realtime callback path."""

        if not self._state_reports_enabled or not summary:
            return
        if self._state_report_queue.full():
            try:
                self._state_report_queue.get_nowait()
                self._state_report_queue.task_done()
            except asyncio.QueueEmpty:
                pass
        self._state_report_queue.put_nowait(summary)
        if self._state_report_task is None or self._state_report_task.done():
            self._state_report_task = asyncio.create_task(
                self._state_report_loop(),
                name=f"voice-state-report-{self.session_id}",
            )

    async def _state_report_loop(self) -> None:
        while self._state_reports_enabled:
            summary = await self._state_report_queue.get()
            try:
                await self._consciousness.report_state(summary)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - non-critical World projection
                logger.warning(
                    "Voice state projection failed outside the media path: "
                    f"{type(exc).__name__}"
                )
                await self._store.append_async(
                    "consciousness.state_report_failed",
                    {"error_type": type(exc).__name__},
                )
            finally:
                self._state_report_queue.task_done()

    async def _stop_state_reports(self) -> None:
        self._state_reports_enabled = False
        task = self._state_report_task
        self._state_report_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        while True:
            try:
                self._state_report_queue.get_nowait()
                self._state_report_queue.task_done()
            except asyncio.QueueEmpty:
                break

    async def _refresh_dynamic_context(self) -> None:
        """Refresh the read-only subconscious prefix for the current frontier."""

        if not self._config.session.cross_scene_awareness:
            return
        provider = self._provider
        builder = getattr(self._bridge, "build_llm_context_prefix", None)
        if provider is None or not callable(builder):
            return
        try:
            async with self._dynamic_context_refresh_lock:
                if (
                    self._dynamic_context_delivered_frontier
                    == self._dynamic_context_frontier
                ):
                    return
                prefix, projection = await builder()
                await self._deliver_dynamic_context_locked(prefix, projection)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - transient provider boundary
            await self._store.append_async(
                "subconscious_context.refresh_failed",
                {
                    "provider": self.provider_name,
                    "frontier": self._dynamic_context_frontier,
                    "error_type": type(exc).__name__,
                },
            )
            logger.warning(
                "Voice recent subconscious context refresh failed: %s",
                type(exc).__name__,
            )

    def _schedule_dynamic_context_refresh(self) -> None:
        """Refresh outside provider callbacks so receipt events can be consumed."""

        if self._state in {
            SessionState.STOPPING,
            SessionState.ENDED,
            SessionState.FAILED,
        }:
            return
        task = self._dynamic_context_refresh_task
        if task is not None and not task.done():
            return
        self._dynamic_context_refresh_task = asyncio.create_task(
            self._refresh_dynamic_context(),
            name=f"voice-subconscious-refresh-{self.session_id}",
        )

    async def _stop_dynamic_context_refresh(self) -> None:
        """Cancel the session-owned refresh task during failure or shutdown."""

        task = self._dynamic_context_refresh_task
        self._dynamic_context_refresh_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - task cleanup boundary
            logger.debug(
                "Voice subconscious refresh cleanup observed: %s",
                type(exc).__name__,
            )

    async def _deliver_dynamic_context(
        self,
        prefix: str,
        projection: Any | None,
    ) -> None:
        """Strictly deliver one startup projection before exposing readiness."""

        async with self._dynamic_context_refresh_lock:
            await self._deliver_dynamic_context_locked(prefix, projection)

    async def _deliver_dynamic_context_locked(
        self,
        prefix: str,
        projection: Any | None,
    ) -> None:
        """Inject one projection without mutating any Life Engine cursor."""

        if not prefix or projection is None:
            return
        if self._dynamic_context_delivered_frontier == self._dynamic_context_frontier:
            return
        provider = self._provider
        if provider is None:
            return

        provider_receipt = await provider.inject_context(prefix)
        prefix_encoded = prefix.encode("utf-8")
        prefix_bytes = len(prefix_encoded)
        prefix_sha256 = hashlib.sha256(prefix_encoded).hexdigest()
        projection_sha256 = str(
            getattr(projection, "projection_sha256", "") or ""
        )
        exact = bool(
            provider_receipt is not None
            and provider_receipt.exact
            and provider_receipt.expected_utf8_bytes == prefix_bytes
            and provider_receipt.accepted_utf8_bytes == prefix_bytes
            and provider_receipt.expected_sha256 == prefix_sha256
            and provider_receipt.accepted_sha256 == prefix_sha256
        )
        self._dynamic_context_delivered_frontier = self._dynamic_context_frontier

        stats_builder = getattr(
            self._bridge,
            "dynamic_context_projection_stats",
            None,
        )
        if callable(stats_builder):
            stats = stats_builder()
            if stats:
                await self._store.append_async(
                    "subconscious_context.projected",
                    {**stats, "frontier": self._dynamic_context_frontier},
                )

        audit = {
            "provider": self.provider_name,
            "frontier": self._dynamic_context_frontier,
            "projection_sha256": projection_sha256,
            "source_delivered_bytes": int(
                getattr(projection, "delivered_bytes", 0) or 0
            ),
            "transport_bytes": prefix_bytes,
            "through_sequence": int(
                getattr(projection, "through_sequence", 0) or 0
            ),
        }
        if exact:
            self._last_unverified_dynamic_context_signature = ""
            await self._store.append_async(
                "subconscious_context.delivered",
                {
                    **audit,
                    "transport_event_count": len(
                        provider_receipt.transport_event_ids
                    ),
                },
            )
            return

        await self._store.append_async(
            "subconscious_context.delivery_unverified",
            {
                **audit,
                "reason": "provider_exact_context_receipt_absent",
            },
        )
        if projection_sha256 != self._last_unverified_dynamic_context_signature:
            logger.warning(
                "Voice recent subconscious context was not proven exactly accepted"
            )
        self._last_unverified_dynamic_context_signature = projection_sha256

    async def _on_response_done(self, success: bool) -> None:
        """Open the next turn frontier after transient context has expired."""

        record = await self._store.append_async(
            "provider.response_done",
            {"success": bool(success)},
        )
        # Open and refresh the next provider-context frontier before the durable
        # Life activity write.  The audit write must not delay turn readiness.
        self._dynamic_context_frontier += 1
        self._schedule_dynamic_context_refresh()
        record_state = getattr(self._bridge, "record_activity_state", None)
        if callable(record_state):
            await record_state(
                occurrence_id=(
                    f"voice:{self.episode_id}:response-done:{record.sequence}"
                ),
                state_kind=(
                    "response_completed" if success else "response_cancelled"
                ),
                payload={"success": bool(success)},
            )

    async def _on_transcript(self, event: TranscriptEvent) -> None:
        await self._send_json_safe(
            {
                "type": "transcript",
                "role": event.role,
                "text": event.text,
                "is_final": event.is_final,
                "event_id": event.event_id,
            }
        )
        if event.is_final:
            if self._audio_archive is not None:
                await self._store.append_async(
                    "audio.transcript_anchor",
                    {
                        "role": event.role,
                        "provider_event_id": event.event_id,
                        "cursors": self._audio_archive.cursor_snapshot(),
                    },
                )
            await self._bridge.record_transcript(
                event.role, event.text, provider_event_id=event.event_id
            )

    async def _on_error(self, message: str) -> None:
        await self._store.append_async("provider.error", {"message": message})
        if self._state not in {
            SessionState.STOPPING,
            SessionState.ENDED,
            SessionState.FAILED,
        }:
            await self._fail(f"上游实时模型异常: {message}")

    async def _on_interruption(self, event: InterruptionEvent) -> None:
        record = await self._store.append_async(
            "provider.interruption",
            {
                "source": event.source,
                "response_id": event.response_id,
                "item_id": event.item_id,
                "audio_cursors": (
                    self._audio_archive.cursor_snapshot()
                    if self._audio_archive is not None
                    else {}
                ),
            },
        )
        # Browser-originated barge-in already cleared playback, reset conversion,
        # and incremented the counter in handle_message().  The provider callback
        # is an acknowledgement, not a second interruption.
        if event.source != "client":
            # Playback safety is latency-sensitive.  Clear it before the durable
            # Life activity write so storage cannot hold audible stale output.
            self._interruptions += 1
            await self._reset_voice_conversion()
            await self._send_json_safe(
                {"type": "playback.clear", "reason": event.source}
            )
        record_state = getattr(self._bridge, "record_activity_state", None)
        if callable(record_state):
            await record_state(
                occurrence_id=(
                    f"voice:{self.episode_id}:interruption:{record.sequence}"
                ),
                state_kind="response_interrupted",
                payload={
                    "source": event.source,
                    "response_id": event.response_id,
                    "item_id": event.item_id,
                },
                causation_id=str(event.response_id or ""),
            )

    async def _on_metrics(self, event: ProviderMetrics) -> None:
        await self._store.append_async("provider.metrics", event.values)
        await self._send_json_safe(
            {"type": "metrics", "values": event.values, "session": self.snapshot()}
        )

    async def _on_tool_call(self, event: ToolCallEvent) -> None:
        await self._store.append_async(
            "tool.call",
            {
                "call_id": event.call_id,
                "name": event.name,
                "arguments_json": event.arguments_json,
            },
        )
        turn_occurrence_id = ""
        activity_ids: dict[str, str] = {}
        record_call = getattr(self._bridge, "record_tool_call_activity", None)
        if callable(record_call):
            turn_occurrence_id, activity_ids = await record_call(
                call_id=event.call_id,
                name=event.name,
                arguments_json=event.arguments_json,
            )
        try:
            result = await self._tool_broker.execute(event.name, event.arguments_json)
            tool_succeeded = bool(
                not isinstance(result, dict) or result.get("success", True)
            )
        except Exception as exc:  # noqa: BLE001 - tool adapter boundary
            result = {"success": False, "error": str(exc)}
            tool_succeeded = False
            await self._store.append_async(
                "tool.failed", {"name": event.name, "error": str(exc)}
            )
        await self._store.append_async(
            "tool.result",
            {
                "call_id": event.call_id,
                "name": event.name,
                "success": tool_succeeded,
                "result": result,
            },
        )
        record_result = getattr(self._bridge, "record_tool_result_activity", None)
        if callable(record_result) and activity_ids:
            await record_result(
                call_id=event.call_id,
                name=event.name,
                result=result,
                success=tool_succeeded,
                turn_occurrence_id=turn_occurrence_id,
                activity_ids=activity_ids,
            )
        projector = getattr(self._bridge, "project_tool_result", None)
        if callable(projector):
            result, projection_stats = projector(result)
            await self._store.append_async(
                "tool.projected",
                {
                    "name": event.name,
                    "call_id": event.call_id,
                    **projection_stats,
                },
            )
        if self._provider is not None:
            await self._provider.submit_tool_result(event.call_id, result)

    async def _fail(self, message: str) -> None:
        if self._state is SessionState.FAILED:
            return
        self._failure_reason = message
        self._state = SessionState.FAILED
        await self._store.append_async("session.failed", {"error": message})
        await self._store.checkpoint_async("failed", error=message)
        await self._send_error(message, fatal=True)
        provider = self._provider
        self._provider = None
        if provider is not None:
            try:
                await provider.disconnect()
            except Exception as cleanup_exc:  # noqa: BLE001
                logger.debug(f"Provider failure cleanup failed: {cleanup_exc}")
        await self._stop_state_reports()
        await self._stop_voice_conversion()
        await self._stop_audio_archive(reason="failed")
        if self._consciousness.is_active:
            await self._consciousness.suspend(reason="abnormal_exit")
        await self._store.checkpoint_async(
            "suspended", reason="abnormal_exit", error=message
        )

    async def _send_json_safe(self, data: dict[str, Any]) -> None:
        if self._send_json is None:
            return
        try:
            await self._send_json(data)
        except Exception as exc:  # noqa: BLE001 - browser transport boundary
            logger.debug(f"Voice Live transport send failed: {exc}")

    async def _queue_conversion_control(self, operation: str) -> None:
        queue = self._conversion_queue
        if queue is None:
            return
        try:
            queue.put_nowait((operation, self._conversion_generation, None))
        except asyncio.QueueFull:
            await self._fail("爱莉实时音色转换控制队列已满；会话无法保持实时性")

    async def _reset_voice_conversion(self) -> None:
        queue = self._conversion_queue
        if self._voice_converter is None or queue is None:
            return
        self._conversion_generation += 1
        while True:
            try:
                queue.get_nowait()
                queue.task_done()
            except asyncio.QueueEmpty:
                break
        await self._queue_conversion_control("reset")

    async def _conversion_loop(self) -> None:
        queue = self._conversion_queue
        converter = self._voice_converter
        if queue is None or converter is None:
            return
        try:
            while True:
                operation, generation, event = await queue.get()
                try:
                    converted: ConvertedAudio | None = None
                    if operation == "audio" and event is not None:
                        converted = await converter.process(
                            event.data, event.sample_rate
                        )
                    elif operation == "flush":
                        converted = await converter.flush()
                    elif operation == "reset":
                        await converter.reset()
                    elif operation == "stop":
                        return
                    if converted is not None:
                        await self._record_conversion_metrics(converted)
                        if (
                            converted.data
                            and generation == self._conversion_generation
                            and self._state
                            not in {
                                SessionState.STOPPING,
                                SessionState.ENDED,
                                SessionState.FAILED,
                            }
                        ):
                            output_rate = self._config.audio.output_sample_rate
                            pcm16 = resample_pcm16_mono(
                                converted.data,
                                converted.sample_rate,
                                output_rate,
                            )
                            self._converted_audio_bytes += len(pcm16)
                            self._archive_audio(
                                "assistant_converted",
                                pcm16,
                                output_rate,
                            )
                            await self._emit_audio(
                                AudioDelta(
                                    pcm16,
                                    output_rate,
                                    response_id=(
                                        event.response_id if event is not None else ""
                                    ),
                                )
                            )
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - converter process boundary
            message = str(exc).strip() or type(exc).__name__
            await self._store.append_async("voice_conversion.error", {"error": message})
            if self._state not in {
                SessionState.STOPPING,
                SessionState.ENDED,
                SessionState.FAILED,
            }:
                await self._fail(f"爱莉实时音色转换失败: {message}")

    async def _record_conversion_metrics(self, converted: ConvertedAudio) -> None:
        blocks = int(converted.metrics.get("block_count", 0))
        inference_ms = float(converted.metrics.get("inference_ms", 0.0))
        if blocks <= 0 and not converted.data:
            return
        self._conversion_blocks += blocks
        self._conversion_inference_ms += inference_ms
        values = {
            "profile_id": self._config.voice_conversion.profile_id,
            "block_count": blocks,
            "inference_ms": inference_ms,
            "pending_samples": int(converted.metrics.get("pending_samples", 0)),
            "output_bytes": len(converted.data),
        }
        await self._store.append_async("voice_conversion.metrics", values)
        await self._send_json_safe(
            {"type": "metrics", "values": {"voice_conversion": values}}
        )

    async def _stop_voice_conversion(self) -> None:
        converter = self._voice_converter
        task = self._conversion_task
        self._voice_converter = None
        self._conversion_task = None
        self._conversion_queue = None
        current = asyncio.current_task()
        if task is not None and task is not current and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if converter is not None:
            await converter.close()

    async def _start_audio_archive(self, provider: BaseRealtimeProvider) -> None:
        if not self._config.observability.persist_audio:
            return
        specs = [
            AudioTrackSpec(
                "user_input",
                provider.input_sample_rate,
                role="user",
                stage="provider_input",
            ),
            AudioTrackSpec(
                "assistant_source",
                provider.output_sample_rate,
                role="assistant",
                stage="provider_output_before_voice_conversion",
            ),
        ]
        if self._config.voice_conversion.enabled:
            specs.append(
                AudioTrackSpec(
                    "assistant_converted",
                    self._config.audio.output_sample_rate,
                    role="assistant",
                    stage="browser_playback_after_voice_conversion",
                )
            )
        archive = VoiceAudioArchive(self._store)
        try:
            await archive.start(
                specs,
                metadata={
                    "archive_layer": "L0_episode_source",
                    "canonicalization": "not_applied",
                    "training_eligibility": "unreviewed",
                    "provider": provider.provider_name,
                    "model": self._config.full_duplex.model_name,
                    "voice": self._config.full_duplex.voice,
                    "user_id": self._config.session.user_id,
                    "record_to_life": self._config.session.record_to_life,
                    "voice_conversion_configured": (
                        self._config.voice_conversion.enabled
                    ),
                    "voice_conversion_profile": (
                        self._config.voice_conversion.profile_id
                        if self._config.voice_conversion.enabled
                        else ""
                    ),
                    "playback_track": (
                        "assistant_converted"
                        if self._config.voice_conversion.enabled
                        else "assistant_source"
                    ),
                    "retention": "until_explicitly_removed",
                },
            )
        except Exception:
            await archive.close(reason="startup_failed")
            raise
        self._audio_archive = archive
        await self._store.append_async(
            "audio.archive.started",
            {
                "schema_version": 1,
                "directory": "audio",
                "tracks": [spec.name for spec in specs],
            },
        )

    def _archive_audio(self, track: str, pcm16: bytes, sample_rate: int) -> None:
        archive = self._audio_archive
        if archive is None or not pcm16:
            return
        try:
            accepted = archive.append(track, pcm16, sample_rate)
        except Exception as exc:  # noqa: BLE001 - recorder must not break Voice
            self._audio_capture_error = f"{type(exc).__name__}: {exc}"
            return
        if not accepted and not self._audio_capture_error:
            snapshot = archive.snapshot()
            self._audio_capture_error = str(
                snapshot.get("writer_error") or "audio archive queue overflow"
            )

    def _audio_archive_snapshot(self) -> dict[str, Any]:
        if self._audio_archive is not None:
            snapshot = self._audio_archive.snapshot()
        elif self._audio_archive_summary:
            tracks = dict(self._audio_archive_summary.get("tracks") or {})
            snapshot = {
                "enabled": True,
                "state": self._audio_archive_summary.get("state", "closed"),
                "writer_error": self._audio_archive_summary.get("writer_error", ""),
                "tracks": {
                    name: {
                        "sample_rate": item.get("sample_rate"),
                        "written_bytes": item.get("pcm_bytes", 0),
                        "unwritten_bytes": item.get("unwritten_bytes", 0),
                        "dropped_bytes": item.get("dropped_bytes", 0),
                    }
                    for name, item in tracks.items()
                },
            }
        else:
            enabled = bool(self._config.observability.persist_audio)
            snapshot = {
                "enabled": enabled,
                "state": "configured" if enabled else "disabled",
                "writer_error": "",
                "tracks": {},
            }
        if self._audio_capture_error:
            snapshot["capture_error"] = self._audio_capture_error
        return snapshot

    async def _stop_audio_archive(self, *, reason: str) -> None:
        archive = self._audio_archive
        if archive is None:
            return
        try:
            summary = await archive.close(reason=reason)
            self._audio_archive_summary = summary
            await self._store.append_async(
                "audio.archive.closed",
                {
                    "state": summary.get("state", ""),
                    "reason": summary.get("reason", reason),
                    "writer_error": summary.get("writer_error", ""),
                    "tracks": {
                        name: {
                            "pcm_bytes": item.get("pcm_bytes", 0),
                            "unwritten_bytes": item.get("unwritten_bytes", 0),
                            "dropped_bytes": item.get("dropped_bytes", 0),
                            "sha256_pcm": item.get("sha256_pcm", ""),
                        }
                        for name, item in dict(summary.get("tracks") or {}).items()
                    },
                },
            )
        finally:
            self._audio_archive = None

    async def _send_error(self, message: str, *, fatal: bool = False) -> None:
        await self._send_json_safe(
            {"type": "error", "message": message, "fatal": fatal}
        )


__all__ = ["CallSession", "SessionState"]
