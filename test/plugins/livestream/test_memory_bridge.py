from __future__ import annotations

import pytest

from plugins.livestream.domain import PlatformEvent
from plugins.livestream.ledger import LedgerRecord, LivestreamLedger
from plugins.livestream.memory_bridge import (
    LivestreamMemoryBridge,
    RunningLifeEventPublisher,
)

pytestmark = pytest.mark.asyncio


class FakePublisher:
    def __init__(self, fail_at: int | None = None) -> None:
        self.records = []
        self.fail_at = fail_at

    async def publish(self, record) -> None:
        if self.fail_at is not None and len(self.records) == self.fail_at:
            raise OSError("injected LifeEngine outage")
        self.records.append(record)


async def _ledger(tmp_path) -> LivestreamLedger:
    ledger = LivestreamLedger(tmp_path / "livestream.sqlite3")
    await ledger.start()
    await ledger.append_platform_event(
        "session-1",
        PlatformEvent(
            kind="danmaku",
            user_name="viewer",
            content="hello",
            event_id="event-1",
            room_id="42",
        ),
    )
    await ledger.append(
        record_id="performance.completed:u1",
        session_id="session-1",
        kind="performance.completed",
        source="test",
        payload={
            "utterance_id": "u1",
            "spoken_text": "actually spoken",
            "completed_chunk_count": 1,
            "detail": "",
        },
    )
    return ledger


async def test_memory_projection_commits_after_complete_batch(tmp_path) -> None:
    ledger = await _ledger(tmp_path)
    publisher = FakePublisher()
    bridge = LivestreamMemoryBridge(
        ledger,
        publisher,
        session_id="session-1",
    )

    assert await bridge.run_once() == 2
    assert await bridge.run_once() == 0
    assert len(publisher.records) == 2
    assert await ledger.get_cursor("session-1", bridge.consumer_name) == 2
    await ledger.stop()


async def test_memory_projection_failure_keeps_cursor_for_idempotent_replay(tmp_path) -> None:
    ledger = await _ledger(tmp_path)
    publisher = FakePublisher(fail_at=1)
    bridge = LivestreamMemoryBridge(
        ledger,
        publisher,
        session_id="session-1",
    )

    with pytest.raises(OSError, match="LifeEngine outage"):
        await bridge.run_once()
    assert await ledger.get_cursor("session-1", bridge.consumer_name) == 0
    await ledger.stop()


async def test_interrupted_partial_audio_is_a_trace_not_forged_complete_speech() -> None:
    event = RunningLifeEventPublisher._to_life_event(
        LedgerRecord(
            sequence=7,
            record_id="performance.interrupted:u1",
            session_id="session-1",
            kind="performance.interrupted",
            occurred_at=1_700_000_000,
            source="test",
            correlation_id="u1",
            causation_id=None,
            payload={
                "spoken_text": "first sentence。",
                "partial_chunk_text": "unfinished sentence。",
                "partial_played_ms": 250,
            },
            payload_sha256="0" * 64,
            recorded_at=1_700_000_001,
        )
    )

    assert event is not None
    assert "实际完整说出" in event.content
    assert "已确认音频播放 250ms" in event.content
    assert "原计划文本" in event.content
