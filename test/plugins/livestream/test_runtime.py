from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from plugins.livestream.config import LivestreamConfig
from plugins.livestream.domain import PerformancePlan, PlatformEvent, PlaybackReceipt
from plugins.livestream.performance import AudioPacket
from plugins.livestream.runtime import LivestreamRuntime

pytestmark = pytest.mark.asyncio


class FakeStage:
    primary_client_id = "obs-stage"
    client_count = 1

    def __init__(self) -> None:
        self.played: list[str] = []
        self.controls: list[tuple[str, dict]] = []
        self.interruptions: list[tuple[str, str]] = []

    async def play(self, **kwargs) -> PlaybackReceipt:
        self.played.append(kwargs["text"])
        return PlaybackReceipt(
            playback_id=kwargs["playback_id"],
            utterance_id=kwargs["utterance_id"],
            chunk_id=kwargs["chunk_id"],
            outcome="completed",
            started_at=time.time(),
            ended_at=time.time(),
            played_ms=50,
        )

    async def interrupt(self, utterance_id: str, reason: str) -> None:
        self.interruptions.append((utterance_id, reason))

    async def send_control(self, message_type: str, payload: dict) -> None:
        self.controls.append((message_type, payload))


class FakeAdapter:
    def __init__(self) -> None:
        self.callback = None
        self.health = SimpleNamespace(
            connected=False,
            last_event_at=None,
            last_error="",
        )

    def on_event(self, callback) -> None:
        self.callback = callback

    async def connect(self) -> None:
        self.health.connected = True

    async def disconnect(self) -> None:
        self.health.connected = False

    async def emit(self, event: PlatformEvent) -> None:
        self.health.last_event_at = time.time()
        await self.callback(event)


class FailOnceDisconnectAdapter(FakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.disconnect_calls = 0

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        if self.disconnect_calls == 1:
            raise OSError("injected platform shutdown failure")
        await super().disconnect()


class FakeDeliberator:
    actor = "same-life-consciousness"

    def ensure_available(self) -> None:
        pass

    async def deliberate(self, events, **kwargs) -> PerformancePlan:
        return PerformancePlan(
            should_speak=True,
            reason="I choose to answer.",
            speech_text=f"heard {events[0].content}",
            addressed_event_ids=[events[0].event_id],
        )


class FakeTTS:
    def __init__(self, *args, **kwargs) -> None:
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def synthesize(self, text: str) -> AudioPacket:
        return AudioPacket(f"audio:{text}".encode(), "audio/wav")


class FakeConsciousness:
    def __init__(self, config, session_id) -> None:
        self.session_id = session_id
        self.active = False
        self.states = []
        self.renew_count = 0

    async def activate(self) -> None:
        self.active = True

    async def report_state(self, state: str) -> None:
        self.states.append(state)

    async def renew(self) -> None:
        self.renew_count += 1

    async def suspend(self, *, reason: str) -> None:
        self.active = False


class FakePublisher:
    def __init__(self) -> None:
        self.records = []

    def require_service(self) -> object:
        return object()

    async def publish(self, record) -> None:
        self.records.append(record)


async def test_manual_runtime_closes_raw_to_spoken_to_memory_loop(tmp_path) -> None:
    config = LivestreamConfig(
        platform={"room_id": "42"},
        storage={
            "ledger_path": str(tmp_path / "ledger.sqlite3"),
            "audio_artifact_path": str(tmp_path / "audio"),
        },
    )
    stage = FakeStage()
    adapter = FakeAdapter()
    publisher = FakePublisher()
    runtime = LivestreamRuntime(
        config,
        stage,
        adapter_factory=lambda _config: adapter,
        tts_factory=FakeTTS,
        deliberator_factory=lambda **kwargs: FakeDeliberator(),
        consciousness_factory=FakeConsciousness,
        publisher_factory=lambda: publisher,
    )

    session_id = await runtime.start()
    event = PlatformEvent(
        kind="danmaku",
        user_name="viewer",
        content="hello",
        event_id="event-1",
        room_id="42",
        dedup_key="DANMU_MSG:native-1",
        raw_payload={"cmd": "DANMU_MSG", "native": 1},
    )
    await adapter.emit(event)
    await adapter.emit(replace(event, event_id="event-replay", received_at=time.time()))
    async with asyncio.timeout(3):
        while not any(r.kind == "performance.completed" for r in publisher.records):
            await asyncio.sleep(0.01)

    assert runtime.state == "running"
    assert session_id == runtime.session_id
    assert stage.played == ["heard hello"]
    assert len(stage.controls) == 1
    assert [record.kind for record in publisher.records] == [
        "platform.event",
        "performance.completed",
    ]
    health = await runtime.health()
    assert health.platform_connected is True
    assert health.primary_stage_connected is True
    assert health.event_backlog == 0
    assert health.performance_backlog == 0

    ledger = runtime._ledger
    assert ledger is not None
    records = await ledger.read_since(0, session_id=session_id)
    assert [record.kind for record in records] == [
        "session.started",
        "platform.event",
        "director.decision",
        "performance.planned",
        "performance.started",
        "tts.synthesized",
        "playback.dispatched",
        "playback.receipt",
        "performance.completed",
    ]
    await runtime.stop()
    assert runtime.state == "stopped"


async def test_runtime_refuses_start_without_stage(tmp_path) -> None:
    config = LivestreamConfig(
        platform={"room_id": "42"},
        storage={
            "ledger_path": str(tmp_path / "ledger.sqlite3"),
            "audio_artifact_path": str(tmp_path / "audio"),
        },
    )
    stage = FakeStage()
    stage.primary_client_id = None
    runtime = LivestreamRuntime(config, stage)

    with pytest.raises(RuntimeError, match="stage"):
        await runtime.start()
    assert runtime.state == "stopped"


async def test_failed_shutdown_retains_resources_for_explicit_retry(tmp_path) -> None:
    config = LivestreamConfig(
        platform={"room_id": "42"},
        storage={
            "ledger_path": str(tmp_path / "ledger.sqlite3"),
            "audio_artifact_path": str(tmp_path / "audio"),
        },
    )
    adapter = FailOnceDisconnectAdapter()
    runtime = LivestreamRuntime(
        config,
        FakeStage(),
        adapter_factory=lambda _config: adapter,
        tts_factory=FakeTTS,
        deliberator_factory=lambda **kwargs: FakeDeliberator(),
        consciousness_factory=FakeConsciousness,
        publisher_factory=FakePublisher,
    )
    await runtime.start()

    with pytest.raises(RuntimeError, match="platform"):
        await runtime.stop(reason="test stop")
    assert runtime.state == "failed"
    with pytest.raises(RuntimeError, match="still owns resources"):
        await runtime.start()

    await runtime.stop(reason="retry stop")
    assert runtime.state == "stopped"
    assert adapter.disconnect_calls == 2


async def test_director_interrupt_respects_current_plan_boundary() -> None:
    stage = FakeStage()
    runtime = LivestreamRuntime(
        LivestreamConfig(platform={"room_id": "42"}),
        stage,
    )
    runtime._performance = SimpleNamespace(
        current_utterance_id="utterance-1",
        current_interruptible=True,
    )

    await runtime._on_director_progress(
        SimpleNamespace(plan=SimpleNamespace(interrupt_current=True))
    )
    assert stage.interruptions == [
        (
            "utterance-1",
            "livestream director chose to interrupt current performance",
        )
    ]

    runtime._performance.current_interruptible = False
    await runtime._on_director_progress(
        SimpleNamespace(plan=SimpleNamespace(interrupt_current=True))
    )
    assert len(stage.interruptions) == 1
    assert await runtime.interrupt() is False


async def test_presence_lease_renews_only_during_manual_session(tmp_path) -> None:
    config = LivestreamConfig(
        platform={"room_id": "42"},
        storage={
            "ledger_path": str(tmp_path / "ledger.sqlite3"),
            "audio_artifact_path": str(tmp_path / "audio"),
        },
    )
    # Shorten the technical interval after config validation for the test clock.
    config.server.presence_lease_seconds = 0.03
    runtime = LivestreamRuntime(
        config,
        FakeStage(),
        adapter_factory=lambda _config: FakeAdapter(),
        tts_factory=FakeTTS,
        deliberator_factory=lambda **kwargs: FakeDeliberator(),
        consciousness_factory=FakeConsciousness,
        publisher_factory=FakePublisher,
    )

    await runtime.start()
    consciousness = runtime._consciousness
    async with asyncio.timeout(1):
        while consciousness.renew_count == 0:
            await asyncio.sleep(0.01)

    await runtime.stop(reason="test stop")
    renewals_after_stop = consciousness.renew_count
    await asyncio.sleep(0.04)
    assert consciousness.renew_count == renewals_after_stop
