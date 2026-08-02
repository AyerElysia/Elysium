from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from plugins.voice_live.config import VoiceLiveConfig
from plugins.voice_live.protocol import ProviderState, SessionState, pack_audio_frame, unpack_audio_frame
from plugins.voice_live.providers.base import (
    AudioDelta,
    BaseRealtimeProvider,
    InterruptionEvent,
    ToolCallEvent,
    TranscriptEvent,
)
from plugins.voice_live.runtime_store import VoiceEpisodeStore
from plugins.voice_live.session import CallSession


class FakeProvider(BaseRealtimeProvider):
    provider_name = "fake_realtime"

    def __init__(self) -> None:
        super().__init__()
        self.connected: dict[str, Any] | None = None
        self.audio: list[bytes] = []
        self.interrupt_values: list[int | None] = []
        self.tool_results: list[tuple[str, Any]] = []

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


class FakeConsciousness:
    instance_id = "voice_live_test"
    stream_id = "voice_live_test"

    def __init__(self) -> None:
        self.is_active = False
        self.reasons: list[str] = []

    async def activate(self, provider_name: str) -> None:
        assert provider_name == "fake_realtime"
        self.is_active = True

    async def report_state(self, summary: str) -> None:
        assert summary

    async def suspend(self, *, reason: str) -> None:
        self.is_active = False
        self.reasons.append(reason)


class FakeBridge:
    def __init__(self) -> None:
        self.transcripts: list[tuple[str, str, str]] = []

    def build_system_prompt(self) -> str:
        return "independent voice context"

    async def record_transcript(self, role: str, text: str, *, provider_event_id: str = "") -> None:
        self.transcripts.append((role, text, provider_event_id))


class FakeBroker:
    def schemas(self) -> list[dict[str, Any]]:
        return [{"type": "function", "name": "action-think", "parameters": {"type": "object"}}]

    async def execute(self, name: str, arguments_json: str) -> dict[str, Any]:
        return {"name": name, "arguments": arguments_json}


def make_config(tmp_path: Path) -> VoiceLiveConfig:
    config = VoiceLiveConfig()
    config.observability.trace_root = str(tmp_path)
    config.session.require_life_engine = False
    config.session.record_to_life = False
    return config


@pytest.mark.asyncio
async def test_session_runs_audio_interrupt_transcript_tool_and_cleanup(tmp_path: Path) -> None:
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
    assert provider.connected and provider.connected["instructions"] == "independent voice context"
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
    assert any(event.get("type") == "playback.clear" for event in json_events)

    await provider._emit_tool_call(ToolCallEvent("call", "action-think", '{"x":1}'))
    assert provider.tool_results == [
        ("call", {"name": "action-think", "arguments": '{"x":1}'})
    ]
    await session.stop(reason="test_complete")
    assert session.state is SessionState.ENDED
    assert consciousness.reasons == ["test_complete"]
    assert store.load_checkpoint()["state"] == "ended"


@pytest.mark.asyncio
async def test_provider_error_is_fatal_and_suspends_consciousness(tmp_path: Path) -> None:
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
