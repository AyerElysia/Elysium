from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from plugins.livestream.domain import PlatformEvent
from plugins.livestream.ledger import (
    LedgerNotStartedError,
    LedgerRecordConflictError,
    LivestreamLedger,
)

pytestmark = pytest.mark.asyncio


async def test_start_stop_are_idempotent_and_restart_preserves_history(tmp_path) -> None:
    path = tmp_path / "livestream.sqlite3"
    ledger = LivestreamLedger(path)

    with pytest.raises(LedgerNotStartedError):
        await ledger.read_since(0)

    await ledger.start()
    await ledger.start()
    result = await ledger.append(
        record_id="record-1",
        session_id="session-1",
        kind="test.fact",
        source="test",
        payload={"value": 1},
    )
    await ledger.stop()
    await ledger.stop()

    restarted = LivestreamLedger(path)
    await restarted.start()
    records = await restarted.read_since(0)
    await restarted.stop()

    assert result.inserted is True
    assert [record.record_id for record in records] == ["record-1"]


async def test_latest_record_supports_crash_session_recovery(tmp_path) -> None:
    ledger = LivestreamLedger(tmp_path / "livestream.sqlite3")
    await ledger.start()
    assert await ledger.get_latest_record("session.started") is None
    for index in range(2):
        await ledger.append(
            record_id=f"session-started:{index}",
            session_id=f"session-{index}",
            kind="session.started",
            source="test",
            payload={"index": index},
        )
    latest = await ledger.get_latest_record("session.started")
    await ledger.stop()

    assert latest is not None
    assert latest.session_id == "session-1"


async def test_append_is_idempotent_but_rejects_identity_conflict(tmp_path) -> None:
    ledger = LivestreamLedger(tmp_path / "livestream.sqlite3")
    await ledger.start()
    kwargs = {
        "record_id": "stable-id",
        "session_id": "session-1",
        "kind": "test.fact",
        "source": "test",
        "payload": {"nested": {"b": 2, "a": 1}},
    }

    first = await ledger.append(**kwargs)
    replay = await ledger.append(**kwargs)
    with pytest.raises(LedgerRecordConflictError):
        await ledger.append(**{**kwargs, "payload": {"nested": {"a": 9}}})
    with pytest.raises(LedgerRecordConflictError):
        await ledger.append(**{**kwargs, "source": "another-source"})
    await ledger.stop()

    assert first.inserted is True
    assert replay.inserted is False
    assert replay.sequence == first.sequence


async def test_platform_dedup_ignores_local_receipt_fields_only(tmp_path) -> None:
    ledger = LivestreamLedger(tmp_path / "livestream.sqlite3")
    await ledger.start()
    original = PlatformEvent(
        kind="danmaku",
        user_name="viewer",
        content="hello",
        timestamp=100.0,
        event_id="local-1",
        platform="bilibili",
        room_id="42",
        received_at=101.0,
        dedup_key="native-message-7",
        raw_payload={"id": 7, "message": "hello"},
    )
    replay = replace(original, event_id="local-2", received_at=999.0)

    first = await ledger.append_platform_event("session-1", original)
    duplicate = await ledger.append_platform_event("session-1", replay)
    with pytest.raises(LedgerRecordConflictError):
        await ledger.append_platform_event(
            "session-1",
            replace(replay, content="tampered"),
        )
    records = await ledger.read_since(0)
    await ledger.stop()

    assert first.inserted is True
    assert duplicate == type(duplicate)(sequence=first.sequence, inserted=False)
    assert len(records) == 1
    assert records[0].payload["received_at"] == 101.0


async def test_platform_record_identity_includes_room_and_event_kind(tmp_path) -> None:
    ledger = LivestreamLedger(tmp_path / "livestream.sqlite3")
    await ledger.start()
    for room_id, kind in (("1", "gift"), ("2", "gift"), ("1", "super_chat")):
        await ledger.append_platform_event(
            "session-1",
            PlatformEvent(
                kind=kind,
                user_name="viewer",
                event_id="native-7",
                room_id=room_id,
                dedup_key=f"{kind}:native-7",
            ),
        )
    records = await ledger.read_since(0)
    await ledger.stop()

    assert len(records) == 3
    assert len({record.record_id for record in records}) == 3


async def test_concurrent_appends_are_serialized_without_loss(tmp_path) -> None:
    ledger = LivestreamLedger(tmp_path / "livestream.sqlite3")
    await ledger.start()

    async def append(index: int) -> None:
        await ledger.append(
            record_id=f"record-{index}",
            session_id="session-1",
            kind="test.fact",
            source="test",
            payload={"index": index},
        )

    await asyncio.gather(*(append(index) for index in range(40)))
    records = await ledger.read_since(0, limit=100)
    await ledger.stop()

    assert len(records) == 40
    assert [record.sequence for record in records] == list(range(1, 41))
    assert {record.payload["index"] for record in records} == set(range(40))


async def test_cursor_is_monotonic_and_is_not_advanced_by_reads(tmp_path) -> None:
    ledger = LivestreamLedger(tmp_path / "livestream.sqlite3")
    await ledger.start()
    for index in range(3):
        await ledger.append(
            record_id=f"record-{index}",
            session_id="session-1",
            kind="test.fact",
            source="test",
            payload={"index": index},
        )

    batch = await ledger.read_since(
        await ledger.get_cursor("session-1", "consumer"),
        session_id="session-1",
        kinds={"test.fact"},
    )
    assert await ledger.get_cursor("session-1", "consumer") == 0

    await ledger.commit_cursor("session-1", "consumer", batch[-1].sequence)
    await ledger.commit_cursor("session-1", "consumer", batch[-1].sequence)
    with pytest.raises(ValueError, match="rewind"):
        await ledger.commit_cursor("session-1", "consumer", 1)
    await ledger.stop()

    assert [record.payload["index"] for record in batch] == [0, 1, 2]
