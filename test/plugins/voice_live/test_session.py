from __future__ import annotations

import asyncio
import hashlib
import json
import os
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.voice_live.config import VoiceLiveConfig
from plugins.voice_live.context_bridge import VoicePromptBundle
from plugins.voice_live.protocol import (
    ProviderState,
    SessionState,
    pack_audio_frame,
    unpack_audio_frame,
)
from plugins.voice_live.providers.base import (
    AudioDelta,
    BaseRealtimeProvider,
    InterruptionEvent,
    RealtimeContextDeliveryReceipt,
    ToolCallEvent,
    TranscriptEvent,
)
from plugins.voice_live.runtime_store import VoiceEpisodeStore
from plugins.voice_live.session import CallSession
from plugins.voice_live.voice_conversion import ConvertedAudio


class FakeProvider(BaseRealtimeProvider):
    provider_name = "fake_realtime"

    def __init__(self) -> None:
        super().__init__()
        self.connected: dict[str, Any] | None = None
        self.audio: list[bytes] = []
        self.interrupt_values: list[int | None] = []
        self.tool_results: list[tuple[str, Any]] = []
        self.contexts: list[str] = []

    async def connect(self, session_config: dict[str, Any]) -> None:
        self.connected = session_config
        await self._emit_state(ProviderState.LISTENING)

    async def disconnect(self) -> None:
        await self._emit_state(ProviderState.CLOSED)

    async def send_audio(self, pcm16: bytes) -> None:
        self.audio.append(pcm16)

    async def interrupt(self, *, played_audio_ms: int | None = None) -> None:
        self.interrupt_values.append(played_audio_ms)
        await self._emit_interruption(InterruptionEvent("client"))

    async def submit_tool_result(self, call_id: str, result: Any) -> None:
        self.tool_results.append((call_id, result))

    async def inject_context(
        self,
        text: str,
    ) -> RealtimeContextDeliveryReceipt:
        self.contexts.append(text)
        encoded = text.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        return RealtimeContextDeliveryReceipt(
            item_ids=(f"fake-context-{len(self.contexts)}",),
            exact=True,
            expected_utf8_bytes=len(encoded),
            expected_sha256=digest,
            accepted_utf8_bytes=len(encoded),
            accepted_sha256=digest,
            transport_event_ids=(f"fake-event-{len(self.contexts)}",),
        )


class RejectingContextProvider(FakeProvider):
    """Reject transient delivery to exercise retry-safe cursor behavior."""

    async def inject_context(self, text: str) -> None:
        raise RuntimeError(f"context rejected: {text}")


class UnverifiedContextProvider(FakeProvider):
    """Accept transport writes without claiming exact upstream storage."""

    async def inject_context(
        self,
        text: str,
    ) -> RealtimeContextDeliveryReceipt:
        exact = await super().inject_context(text)
        return RealtimeContextDeliveryReceipt(
            item_ids=exact.item_ids,
            exact=False,
            expected_utf8_bytes=exact.expected_utf8_bytes,
            expected_sha256=exact.expected_sha256,
            accepted_utf8_bytes=None,
            accepted_sha256=None,
        )


class FakeConsciousness:
    instance_id = "voice_live_test"
    stream_id = "voice_live_test"

    def __init__(self) -> None:
        self.is_active = False
        self.reasons: list[str] = []
        self.committed_perceptions: list[tuple[Any, Any]] = []

    async def activate(self, provider_name: str) -> None:
        assert provider_name == "fake_realtime"
        self.is_active = True

    async def report_state(self, summary: str) -> None:
        assert summary

    async def suspend(self, *, reason: str) -> None:
        self.is_active = False
        self.reasons.append(reason)

    async def commit_perception(self, prepared: Any, receipt: Any) -> None:
        self.committed_perceptions.append((prepared, receipt))


class BlockingStateConsciousness(FakeConsciousness):
    def __init__(self) -> None:
        super().__init__()
        self.report_started = asyncio.Event()
        self.report_release = asyncio.Event()

    async def report_state(self, summary: str) -> None:
        assert summary
        self.report_started.set()
        await self.report_release.wait()


class FakeBridge:
    def __init__(self) -> None:
        self.transcripts: list[tuple[str, str, str]] = []

    def build_system_prompt(self) -> str:
        return "independent voice context"

    async def record_transcript(
        self, role: str, text: str, *, provider_event_id: str = ""
    ) -> None:
        self.transcripts.append((role, text, provider_event_id))


class BundleBridge(FakeBridge):
    async def build_system_prompt(self) -> VoicePromptBundle:
        return VoicePromptBundle(
            text="unified subject voice context",
            subject_context={
                "revision": "revision-1",
                "source_digest": "digest-1",
                "projection_sha256": "projection-1",
            },
            layers={
                "total": {
                    "algorithm": "voice-live-layered-v1",
                    "delivered_bytes": 29,
                    "max_bytes": 4096,
                }
            },
        )


class PerceptionBridge(FakeBridge):
    """Expose one transient delivery without putting it in the system prompt."""

    def __init__(self) -> None:
        super().__init__()
        content = 'world-perception:test-delivery\n{"presence":"active"}'
        encoded = content.encode("utf-8")
        self.prepared = SimpleNamespace(
            delivery_id="test-delivery",
            projection_sha256=hashlib.sha256(encoded).hexdigest(),
            delivered_bytes=len(encoded),
            content=content,
        )

    async def build_llm_context_prefix(self) -> tuple[str, object]:
        return "transient-presence", self.prepared


class CoordinatedPerceptionBridge(PerceptionBridge):
    def __init__(
        self,
        provider_started: asyncio.Event,
        perception_started: asyncio.Event,
    ) -> None:
        super().__init__()
        self._provider_started = provider_started
        self._perception_started = perception_started

    async def build_llm_context_prefix(self) -> tuple[str, object]:
        self._perception_started.set()
        await self._provider_started.wait()
        return await super().build_llm_context_prefix()


class CoordinatedProvider(FakeProvider):
    def __init__(
        self,
        provider_started: asyncio.Event,
        perception_started: asyncio.Event,
    ) -> None:
        super().__init__()
        self._provider_started = provider_started
        self._perception_started = perception_started

    async def connect(self, session_config: dict[str, Any]) -> None:
        self.connected = session_config
        self._provider_started.set()
        await self._perception_started.wait()
        await self._emit_state(ProviderState.LISTENING)


class FakeBroker:
    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": "action-think",
                "parameters": {"type": "object"},
            }
        ]

    async def execute(self, name: str, arguments_json: str) -> dict[str, Any]:
        return {"name": name, "arguments": arguments_json}


class FakeVoiceConverter:
    input_sample_rate = 24000
    output_sample_rate = 24000

    def __init__(self) -> None:
        self.processed: list[tuple[bytes, int]] = []
        self.flushes = 0
        self.resets = 0
        self.closed = False

    async def connect(self) -> dict[str, Any]:
        return {"health": {"status": "ok", "profile_id": "elysia"}}

    async def process(self, pcm16: bytes, sample_rate: int) -> ConvertedAudio:
        self.processed.append((pcm16, sample_rate))
        return ConvertedAudio(
            pcm16,
            24000,
            {"block_count": 1, "inference_ms": 5.0, "pending_samples": 0},
        )

    async def flush(self) -> ConvertedAudio:
        self.flushes += 1
        return ConvertedAudio(b"", 24000, {})

    async def reset(self) -> None:
        self.resets += 1

    async def close(self) -> None:
        self.closed = True


class BlockingVoiceConverter(FakeVoiceConverter):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def process(self, pcm16: bytes, sample_rate: int) -> ConvertedAudio:
        self.processed.append((pcm16, sample_rate))
        self.started.set()
        await self.release.wait()
        return ConvertedAudio(
            pcm16,
            24000,
            {"block_count": 1, "inference_ms": 5.0, "pending_samples": 0},
        )

    async def close(self) -> None:
        self.release.set()
        await super().close()


def make_config(tmp_path: Path) -> VoiceLiveConfig:
    config = VoiceLiveConfig()
    config.observability.trace_root = str(tmp_path)
    config.session.require_life_engine = False
    config.session.record_to_life = False
    return config


@pytest.mark.asyncio
async def test_slow_world_state_report_does_not_delay_session_ready(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    provider = FakeProvider()
    consciousness = BlockingStateConsciousness()
    session = CallSession(
        config,
        "nonblocking-state",
        provider_factory=lambda _: provider,
        store=VoiceEpisodeStore(
            tmp_path,
            "voice_nonblocking_state",
            "nonblocking-state",
        ),
        consciousness=consciousness,
        bridge=FakeBridge(),
        tool_broker=FakeBroker(),
    )

    assert await asyncio.wait_for(session.start(), timeout=2) is True
    await asyncio.wait_for(consciousness.report_started.wait(), timeout=1)
    assert session.state is SessionState.ACTIVE

    consciousness.report_release.set()
    await session.stop()


@pytest.mark.asyncio
async def test_provider_connect_and_world_prepare_overlap_during_startup(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    provider_started = asyncio.Event()
    perception_started = asyncio.Event()
    provider = CoordinatedProvider(provider_started, perception_started)
    bridge = CoordinatedPerceptionBridge(provider_started, perception_started)
    session = CallSession(
        config,
        "parallel-startup",
        provider_factory=lambda _: provider,
        store=VoiceEpisodeStore(
            tmp_path,
            "voice_parallel_startup",
            "parallel-startup",
        ),
        consciousness=FakeConsciousness(),
        bridge=bridge,
        tool_broker=FakeBroker(),
    )

    assert await asyncio.wait_for(session.start(), timeout=2) is True
    assert provider.contexts == ["transient-presence"]
    await session.stop()


@pytest.mark.asyncio
async def test_voice_perception_commits_only_after_completed_model_turn(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    provider = FakeProvider()
    consciousness = FakeConsciousness()
    bridge = PerceptionBridge()
    session = CallSession(
        config,
        "perception",
        provider_factory=lambda _: provider,
        store=VoiceEpisodeStore(tmp_path, "voice_perception", "perception"),
        consciousness=consciousness,
        bridge=bridge,
        tool_broker=FakeBroker(),
    )

    assert await session.start() is True
    assert provider.contexts == ["transient-presence"]
    assert consciousness.committed_perceptions == []
    await provider._emit_response_done(True)
    assert len(consciousness.committed_perceptions) == 1
    committed, receipt = consciousness.committed_perceptions[0]
    assert committed is bridge.prepared
    assert receipt.delivery_id == bridge.prepared.delivery_id
    assert receipt.projection_sha256 == bridge.prepared.projection_sha256
    assert receipt.delivered_bytes == bridge.prepared.delivered_bytes
    assert receipt.exact is True
    assert provider.contexts == ["transient-presence", "transient-presence"]
    await session.stop()


@pytest.mark.asyncio
async def test_voice_perception_rejection_does_not_commit(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    provider = RejectingContextProvider()
    consciousness = FakeConsciousness()
    bridge = PerceptionBridge()
    session = CallSession(
        config,
        "perception-rejected",
        provider_factory=lambda _: provider,
        store=VoiceEpisodeStore(
            tmp_path,
            "voice_perception_rejected",
            "perception-rejected",
        ),
        consciousness=consciousness,
        bridge=bridge,
        tool_broker=FakeBroker(),
    )

    assert await session.start() is False
    assert consciousness.committed_perceptions == []


@pytest.mark.asyncio
async def test_failed_voice_model_turn_keeps_perception_uncommitted(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    provider = FakeProvider()
    consciousness = FakeConsciousness()
    bridge = PerceptionBridge()
    session = CallSession(
        config,
        "perception-failed-turn",
        provider_factory=lambda _: provider,
        store=VoiceEpisodeStore(
            tmp_path,
            "voice_perception_failed_turn",
            "perception-failed-turn",
        ),
        consciousness=consciousness,
        bridge=bridge,
        tool_broker=FakeBroker(),
    )

    assert await session.start() is True
    await provider._emit_response_done(False)
    assert consciousness.committed_perceptions == []
    assert provider.contexts == ["transient-presence", "transient-presence"]
    await session.stop()


@pytest.mark.asyncio
async def test_voice_response_done_without_exact_provider_receipt_stays_pending(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    provider = UnverifiedContextProvider()
    consciousness = FakeConsciousness()
    bridge = PerceptionBridge()
    store = VoiceEpisodeStore(
        tmp_path,
        "voice_perception_unverified",
        "perception-unverified",
    )
    session = CallSession(
        config,
        "perception-unverified",
        provider_factory=lambda _: provider,
        store=store,
        consciousness=consciousness,
        bridge=bridge,
        tool_broker=FakeBroker(),
    )

    assert await session.start() is True
    await provider._emit_response_done(True)

    assert consciousness.committed_perceptions == []
    assert any(
        record.event == "perception.delivery_unverified"
        for record in store.read_all()
    )
    assert provider.contexts == ["transient-presence"]
    await session.stop()


@pytest.mark.asyncio
async def test_session_runs_audio_interrupt_transcript_tool_and_cleanup(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    provider = FakeProvider()
    consciousness = FakeConsciousness()
    bridge = FakeBridge()
    broker = FakeBroker()
    store = VoiceEpisodeStore(tmp_path, "voice_live_case", "case")
    session = CallSession(
        config,
        "case",
        provider_factory=lambda _: provider,
        store=store,
        consciousness=consciousness,
        bridge=bridge,
        tool_broker=broker,
    )
    json_events: list[dict[str, Any]] = []
    binary_events: list[bytes] = []

    async def send_json(value: dict[str, Any]) -> None:
        json_events.append(value)

    async def send_bytes(value: bytes) -> None:
        binary_events.append(value)

    session.set_send_callbacks(send_json, send_bytes)
    assert await session.start()
    assert session.state is SessionState.ACTIVE
    assert (
        provider.connected
        and provider.connected["instructions"] == "independent voice context"
    )
    assert json_events[-1]["type"] == "ready"

    pcm = b"\x00\x00" * 320
    await session.handle_audio(pack_audio_frame(1, 16000, pcm))
    assert provider.audio == [pcm]
    with pytest.raises(ValueError):
        await session.handle_audio(pack_audio_frame(1, 16000, pcm))

    await provider._emit_audio(AudioDelta(pcm, 24000, response_id="r1"))
    output = unpack_audio_frame(binary_events[-1])
    assert output.sequence == 1 and output.sample_rate == 24000 and output.pcm16 == pcm

    await provider._emit_transcript(TranscriptEvent("assistant", "你好", True, "evt"))
    assert bridge.transcripts == [("assistant", "你好", "evt")]
    await session.handle_message({"type": "interrupt", "played_audio_ms": 127})
    assert provider.interrupt_values == [127]
    clear_count = sum(event.get("type") == "playback.clear" for event in json_events)
    assert clear_count == 1
    await provider._emit_interruption(
        InterruptionEvent("client", "response-1", "item-1")
    )
    assert sum(event.get("type") == "playback.clear" for event in json_events) == 1

    await provider._emit_tool_call(ToolCallEvent("call", "action-think", '{"x":1}'))
    assert provider.tool_results == [
        ("call", {"name": "action-think", "arguments": '{"x":1}'})
    ]
    await session.stop(reason="test_complete")
    assert session.state is SessionState.ENDED
    assert consciousness.reasons == ["test_complete"]
    assert store.load_checkpoint()["state"] == "ended"


@pytest.mark.asyncio
async def test_session_persists_subject_revision_and_layer_audit(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    provider = FakeProvider()
    store = VoiceEpisodeStore(tmp_path, "voice_bundle", "bundle")
    session = CallSession(
        config,
        "bundle",
        provider_factory=lambda _: provider,
        store=store,
        consciousness=FakeConsciousness(),
        bridge=BundleBridge(),
        tool_broker=FakeBroker(),
    )

    assert await session.start() is True
    assert provider.connected is not None
    assert provider.connected["instructions"] == "unified subject voice context"
    assert provider.connected["qwen_max_history_turns"] == 12
    snapshot = session.snapshot()
    assert snapshot["subject_context_revision"] == "revision-1"
    assert snapshot["subject_context_source_digest"] == "digest-1"
    configuration = next(
        record
        for record in store.read_all()
        if record.event == "provider.configuration"
    )
    assert configuration.payload["context_layers"]["total"]["algorithm"] == (
        "voice-live-layered-v1"
    )
    assert store.load_checkpoint()["subject_context"]["projection_sha256"] == (
        "projection-1"
    )
    await session.stop()


@pytest.mark.asyncio
async def test_provider_error_is_fatal_and_suspends_consciousness(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    provider = FakeProvider()
    consciousness = FakeConsciousness()
    store = VoiceEpisodeStore(tmp_path, "voice_live_failure", "failure")
    session = CallSession(
        config,
        "failure",
        provider_factory=lambda _: provider,
        store=store,
        consciousness=consciousness,
        bridge=FakeBridge(),
        tool_broker=FakeBroker(),
    )
    events: list[dict[str, Any]] = []

    async def send_json(value: dict[str, Any]) -> None:
        events.append(value)

    async def send_bytes(value: bytes) -> None:
        del value

    session.set_send_callbacks(send_json, send_bytes)
    assert await session.start()
    await provider._emit_error("upstream gone")
    assert session.state is SessionState.FAILED
    assert consciousness.reasons == ["abnormal_exit"]
    assert any(event.get("fatal") is True for event in events)
    assert store.load_checkpoint()["state"] == "suspended"


@pytest.mark.asyncio
async def test_session_streams_provider_audio_through_voice_converter(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    config.voice_conversion.enabled = True
    provider = FakeProvider()
    converter = FakeVoiceConverter()
    session = CallSession(
        config,
        "converted",
        provider_factory=lambda _: provider,
        voice_converter_factory=lambda _: converter,
        store=VoiceEpisodeStore(tmp_path, "voice_live_converted", "converted"),
        consciousness=FakeConsciousness(),
        bridge=FakeBridge(),
        tool_broker=FakeBroker(),
    )
    json_events: list[dict[str, Any]] = []
    binary_events: list[bytes] = []

    async def send_json(value: dict[str, Any]) -> None:
        json_events.append(value)

    async def send_bytes(value: bytes) -> None:
        binary_events.append(value)

    session.set_send_callbacks(send_json, send_bytes)
    assert await session.start()
    pcm = b"\x01\x00" * 320
    await provider._emit_audio(AudioDelta(pcm, 24000, response_id="r-converted"))
    assert session._conversion_queue is not None
    await asyncio.wait_for(session._conversion_queue.join(), timeout=1)

    assert converter.processed == [(pcm, 24000)]
    frame = unpack_audio_frame(binary_events[-1])
    assert frame.sample_rate == 24000 and frame.pcm16 == pcm
    assert any(
        event.get("values", {}).get("voice_conversion", {}).get("block_count") == 1
        for event in json_events
    )

    await session.handle_message({"type": "interrupt"})
    await asyncio.wait_for(session._conversion_queue.join(), timeout=1)
    assert converter.resets >= 1
    await session.stop(reason="converted_complete")
    assert converter.closed is True


@pytest.mark.asyncio
async def test_interruption_discards_in_flight_converted_audio(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.voice_conversion.enabled = True
    provider = FakeProvider()
    converter = BlockingVoiceConverter()
    session = CallSession(
        config,
        "converted_interrupt",
        provider_factory=lambda _: provider,
        voice_converter_factory=lambda _: converter,
        store=VoiceEpisodeStore(
            tmp_path, "voice_live_converted_interrupt", "converted_interrupt"
        ),
        consciousness=FakeConsciousness(),
        bridge=FakeBridge(),
        tool_broker=FakeBroker(),
    )
    binary_events: list[bytes] = []

    async def send_json(_: dict[str, Any]) -> None:
        return

    async def send_bytes(value: bytes) -> None:
        binary_events.append(value)

    session.set_send_callbacks(send_json, send_bytes)
    assert await session.start()
    await provider._emit_audio(AudioDelta(b"\x01\x00" * 320, 24000))
    await asyncio.wait_for(converter.started.wait(), timeout=1)
    await provider._emit_interruption(InterruptionEvent("provider"))
    converter.release.set()
    assert session._conversion_queue is not None
    await asyncio.wait_for(session._conversion_queue.join(), timeout=1)

    assert binary_events == []
    assert converter.resets == 1
    await session.stop(reason="interruption_verified")


@pytest.mark.asyncio
async def test_conversion_backpressure_fails_closed(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.voice_conversion.enabled = True
    config.voice_conversion.queue_max_chunks = 4
    provider = FakeProvider()
    converter = BlockingVoiceConverter()
    session = CallSession(
        config,
        "converted_backpressure",
        provider_factory=lambda _: provider,
        voice_converter_factory=lambda _: converter,
        store=VoiceEpisodeStore(
            tmp_path, "voice_live_converted_backpressure", "converted_backpressure"
        ),
        consciousness=FakeConsciousness(),
        bridge=FakeBridge(),
        tool_broker=FakeBroker(),
    )
    json_events: list[dict[str, Any]] = []

    async def send_json(value: dict[str, Any]) -> None:
        json_events.append(value)

    async def send_bytes(_: bytes) -> None:
        return

    session.set_send_callbacks(send_json, send_bytes)
    assert await session.start()
    chunk = AudioDelta(b"\x01\x00" * 320, 24000)
    await provider._emit_audio(chunk)
    await asyncio.wait_for(converter.started.wait(), timeout=1)
    for _ in range(4):
        await provider._emit_audio(chunk)
    await provider._emit_audio(chunk)

    assert session.state is SessionState.FAILED
    assert converter.closed is True
    assert any(event.get("fatal") is True for event in json_events)


@pytest.mark.asyncio
async def test_session_archives_training_tracks_and_transcript_cursors(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    config.observability.persist_audio = True
    config.voice_conversion.enabled = True
    provider = FakeProvider()
    converter = FakeVoiceConverter()
    store = VoiceEpisodeStore(tmp_path, "voice_training", "training")
    session = CallSession(
        config,
        "training",
        provider_factory=lambda _: provider,
        voice_converter_factory=lambda _: converter,
        store=store,
        consciousness=FakeConsciousness(),
        bridge=FakeBridge(),
        tool_broker=FakeBroker(),
    )

    assert await session.start() is True
    user_pcm = b"\x01\x00" * 320
    assistant_pcm = b"\x02\x00" * 480
    await session.handle_audio(pack_audio_frame(1, 16000, user_pcm))
    await provider._emit_audio(
        AudioDelta(assistant_pcm, 24000, response_id="training-response")
    )
    assert session._conversion_queue is not None
    await asyncio.wait_for(session._conversion_queue.join(), timeout=1)
    await provider._emit_transcript(
        TranscriptEvent("assistant", "training text", True, "training-transcript")
    )
    await session.stop(reason="training_complete")

    for name, expected in (
        ("user_input", user_pcm),
        ("assistant_source", assistant_pcm),
        ("assistant_converted", assistant_pcm),
    ):
        with wave.open(str(store.directory / "audio" / f"{name}.wav"), "rb") as audio:
            assert audio.readframes(audio.getnframes()) == expected

    anchor = next(
        record
        for record in store.read_all()
        if record.event == "audio.transcript_anchor"
    )
    assert anchor.payload["provider_event_id"] == "training-transcript"
    assert anchor.payload["cursors"]["user_input"]["samples_enqueued"] == 320
    assert anchor.payload["cursors"]["assistant_source"]["samples_enqueued"] == 480
    assert anchor.payload["cursors"]["assistant_converted"]["samples_enqueued"] == 480
    closed = next(
        record for record in store.read_all() if record.event == "audio.archive.closed"
    )
    assert closed.payload["state"] == "closed"
    assert all(track["sha256_pcm"] for track in closed.payload["tracks"].values())
    manifest = json.loads(
        (store.directory / "audio" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["metadata"]["archive_layer"] == "L0_episode_source"
    assert manifest["metadata"]["canonicalization"] == "not_applied"
    assert manifest["metadata"]["training_eligibility"] == "unreviewed"
    assert session.snapshot()["audio_archive"]["state"] == "closed"


@pytest.mark.asyncio
async def test_session_real_seedvc_stream_when_e2e_environment_is_present(
    tmp_path: Path,
) -> None:
    service_url = os.environ.get("VOICE_CONVERSION_E2E_URL", "")
    source_path = os.environ.get("VOICE_CONVERSION_E2E_WAV", "")
    if not service_url or not source_path:
        pytest.skip("Seed-VC E2E environment is not configured")

    with wave.open(source_path, "rb") as source:
        assert source.getnchannels() == 1 and source.getsampwidth() == 2
        sample_rate = source.getframerate()
        source_pcm = source.readframes(source.getnframes())

    config = make_config(tmp_path)
    config.voice_conversion.enabled = True
    config.voice_conversion.service_url = service_url
    config.voice_conversion.token_env = "SEEDVC_STREAM_TOKEN"
    config.audio.output_sample_rate = 24000
    provider = FakeProvider()
    session = CallSession(
        config,
        "real_seedvc",
        provider_factory=lambda _: provider,
        store=VoiceEpisodeStore(tmp_path, "voice_live_seedvc", "real_seedvc"),
        consciousness=FakeConsciousness(),
        bridge=FakeBridge(),
        tool_broker=FakeBroker(),
    )
    binary_events: list[bytes] = []

    async def send_json(_: dict[str, Any]) -> None:
        return

    async def send_bytes(value: bytes) -> None:
        binary_events.append(value)

    session.set_send_callbacks(send_json, send_bytes)
    assert await session.start()
    assert session._conversion_queue is not None
    await asyncio.wait_for(session._conversion_queue.join(), timeout=5)
    await provider._emit_state(ProviderState.SPEAKING)
    frame_bytes = sample_rate // 10 * 2
    for offset in range(0, len(source_pcm), frame_bytes):
        await provider._emit_audio(
            AudioDelta(
                source_pcm[offset : offset + frame_bytes],
                sample_rate,
                response_id="qwen-real-seedvc",
            )
        )
    await provider._emit_state(ProviderState.LISTENING)
    await asyncio.wait_for(session._conversion_queue.join(), timeout=30)

    converted_pcm = b"".join(unpack_audio_frame(frame).pcm16 for frame in binary_events)
    assert len(converted_pcm) >= 24000 * 2 * 3
    assert converted_pcm != source_pcm
    assert session.snapshot()["voice_conversion_blocks"] >= 10
    await session.stop(reason="real_seedvc_complete")
