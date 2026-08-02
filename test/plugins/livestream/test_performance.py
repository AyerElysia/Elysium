from __future__ import annotations

import time

import httpx
import pytest

from plugins.livestream.domain import PerformancePlan, PlaybackReceipt
from plugins.livestream.ledger import LivestreamLedger
from plugins.livestream.performance import (
    AudioArtifactStore,
    AudioPacket,
    HttpTTSClient,
    PerformanceRuntime,
    PerformanceSettings,
    TTSProtocolError,
    split_speech_text,
)

pytestmark = pytest.mark.asyncio


class FakeSynthesizer:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    async def synthesize(self, text: str) -> AudioPacket:
        self.calls.append(text)
        if self.fail:
            raise RuntimeError("injected TTS failure")
        return AudioPacket(content=f"audio:{text}".encode(), mime_type="audio/wav")


class FakeStage:
    def __init__(self, outcomes: list[str] | None = None) -> None:
        self.calls: list[str] = []
        self.outcomes = list(outcomes or [])
        self.interruptions: list[tuple[str, str]] = []

    async def play(self, **kwargs) -> PlaybackReceipt:
        self.calls.append(kwargs["playback_id"])
        outcome = self.outcomes.pop(0) if self.outcomes else "completed"
        return PlaybackReceipt(
            playback_id=kwargs["playback_id"],
            utterance_id=kwargs["utterance_id"],
            chunk_id=kwargs["chunk_id"],
            outcome=outcome,
            started_at=time.time(),
            ended_at=time.time(),
            played_ms=100 if outcome == "completed" else 20,
        )

    async def interrupt(self, utterance_id: str, reason: str) -> None:
        self.interruptions.append((utterance_id, reason))


async def _planned_ledger(tmp_path, text: str = "First。Second！") -> LivestreamLedger:
    ledger = LivestreamLedger(tmp_path / "livestream.sqlite3")
    await ledger.start()
    plan = PerformancePlan(
        should_speak=True,
        reason="chosen",
        speech_text=text,
    )
    await ledger.append(
        record_id="performance-plan:utterance-1",
        session_id="session-1",
        kind="performance.planned",
        source="test",
        payload={
            "utterance_id": "utterance-1",
            "decision_id": "decision-1",
            "plan": plan.model_dump(mode="json"),
        },
    )
    return ledger


async def test_performance_records_actual_playback_and_spoken_text(tmp_path) -> None:
    ledger = await _planned_ledger(tmp_path)
    synth = FakeSynthesizer()
    stage = FakeStage()
    runtime = PerformanceRuntime(
        ledger,
        synth,
        stage,
        AudioArtifactStore(tmp_path / "audio"),
        session_id="session-1",
    )

    outcome = await runtime.run_once()
    records = await ledger.read_since(0, session_id="session-1")
    completed = next(r for r in records if r.kind == "performance.completed")
    await ledger.stop()

    assert outcome == "performance.completed"
    assert synth.calls == ["First。", "Second！"]
    assert len(stage.calls) == 2
    assert completed.payload["spoken_text"] == "First。Second！"
    assert [r.kind for r in records].count("playback.receipt") == 2


async def test_replay_after_cursor_failure_does_not_resynthesize_or_replay(
    tmp_path, monkeypatch
) -> None:
    ledger = await _planned_ledger(tmp_path, text="Once.")
    synth = FakeSynthesizer()
    stage = FakeStage()
    runtime = PerformanceRuntime(
        ledger,
        synth,
        stage,
        AudioArtifactStore(tmp_path / "audio"),
        session_id="session-1",
    )
    original_commit = ledger.commit_cursor
    failed = False

    async def fail_once(*args, **kwargs) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected cursor failure")
        await original_commit(*args, **kwargs)

    monkeypatch.setattr(ledger, "commit_cursor", fail_once)
    with pytest.raises(OSError, match="injected"):
        await runtime.run_once()
    outcome = await runtime.run_once()
    await ledger.stop()

    assert outcome == "performance.completed"
    assert len(synth.calls) == 1
    assert len(stage.calls) == 1


async def test_interrupted_chunk_does_not_forge_unconfirmed_text(tmp_path) -> None:
    ledger = await _planned_ledger(tmp_path)
    runtime = PerformanceRuntime(
        ledger,
        FakeSynthesizer(),
        FakeStage(["completed", "interrupted"]),
        AudioArtifactStore(tmp_path / "audio"),
        session_id="session-1",
    )

    outcome = await runtime.run_once()
    records = await ledger.read_since(0, session_id="session-1")
    terminal = next(r for r in records if r.kind == "performance.interrupted")
    await ledger.stop()

    assert outcome == "performance.interrupted"
    assert terminal.payload["spoken_text"] == "First。"
    assert terminal.payload["completed_chunk_count"] == 1
    assert terminal.payload["partial_chunk_text"] == "Second！"
    assert terminal.payload["partial_played_ms"] == 20


async def test_tts_failure_is_explicit_and_consumed_without_spin(tmp_path) -> None:
    ledger = await _planned_ledger(tmp_path)
    runtime = PerformanceRuntime(
        ledger,
        FakeSynthesizer(fail=True),
        FakeStage(),
        AudioArtifactStore(tmp_path / "audio"),
        session_id="session-1",
    )

    outcome = await runtime.run_once()
    second = await runtime.run_once()
    records = await ledger.read_since(0, session_id="session-1")
    await ledger.stop()

    assert outcome == "performance.failed"
    assert second is None
    assert [r.kind for r in records].count("performance.failed") == 1


async def test_splitter_uses_only_transport_bounds() -> None:
    settings = PerformanceSettings(max_chunk_chars=4)
    assert split_speech_text("abcdef。xy！", settings) == ["abcd", "ef。", "xy！"]


async def test_tts_stream_is_rejected_before_unbounded_buffering() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "audio/wav"},
            content=b"a" * 16,
        )

    client = HttpTTSClient("http://tts.local/send", max_audio_bytes=10)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(TTSProtocolError, match="exceeds"):
        await client.synthesize("hello")
    await client.stop()
