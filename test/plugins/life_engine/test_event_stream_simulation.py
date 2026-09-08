"""In-process interaction lab: real services/tools/storage, no external effects.

Both the compatibility SQLite ledger and selected SQL adapter run in temporary
directories. Only network/model dispatch and background scheduling are replaced.
Never call LifeEngineService.start or open the deployment's configuration/data.
No Memory Witness or Experience projection is required or instantiated.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.service.chat_events import build_chat_message_event
from plugins.life_engine.service.event_builder import EventBuilder, EventType
from plugins.life_engine.service.event_handler import (
    LifeEngineMessageCollectorHandler,
    LifeEventCollectionFailed,
)
from plugins.life_engine.service.event_bus import (
    LifeEventBus,
    RawEventGapError,
    RawEventStore,
    legacy_event_from_life_event,
    life_event_from_legacy,
)
from plugins.life_engine.service.subconscious_ingest import (
    SUBCONSCIOUS_INGEST_CONSUMER_ID,
)
from plugins.life_engine.storage.event_contracts import LifeEventConsumerConflict
from plugins.life_engine.tools.file_tools import LifeEngineReadFileTool
from src.core.models.message import Message
from src.core.components.types import EventType as TransportEventType
from src.kernel.event import EventDecision

from .test_life_event_storage_contract import _local_store
from .test_service import _make_service


def _message(identity: str = "message-1", **values: Any) -> Message:
    return Message(
        **{
            "message_id": identity,
            "time": datetime(2026, 9, 5, 8, 0, tzinfo=UTC),
            "content": "  保留前后空白与完整正文。\n" + "星光" * 2000 + "\n ",
            "sender_id": "simulation-user",
            "sender_name": "Test source",
            "platform": "simulation",
            "chat_type": "private",
            "stream_id": "simulation:stream-a",
            **values,
        }
    )


class InteractionLab:
    def __init__(self, root: Path, store: Any) -> None:
        self.root = root
        self.store = store
        self.deferred: list[tuple[Any, Any, Any]] = []
        self.service = self.new_service()

    def new_service(self) -> Any:
        service = _make_service(self.root / "workspace")
        service._event_bus = LifeEventBus(self.store)
        service._schedule_curiosity_review = lambda *_a, **_k: None
        service._schedule_message_persist = lambda event, fact: self.deferred.append(
            (service, event, fact)
        )
        return service

    async def flush(self) -> None:
        while self.deferred:
            service, event, fact = self.deferred.pop(0)
            await service._run_message_persist(event, fact)


@pytest.fixture(params=["compatibility-sqlite", "selected-local-sql"])
async def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: Any):
    original_connect = socket.socket.connect

    def forbid_network(sock: socket.socket, address: Any) -> Any:
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            raise AssertionError("event-stream lab must not use a network")
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", forbid_network)
    if request.param == "compatibility-sqlite":
        yield InteractionLab(tmp_path, RawEventStore(tmp_path / "ledger"))
    else:
        async with _local_store(tmp_path / "selected") as (_, store, _, _):
            yield InteractionLab(tmp_path, store)


async def test_message_retries_share_one_exact_occurrence(lab: InteractionLab) -> None:
    message = _message()
    await asyncio.gather(*(lab.service.record_message(message) for _ in range(4)))
    await lab.flush()
    rows = await lab.store.read_since(0)
    assert len(rows) == 1
    assert rows[0].content == message.content
    assert rows[0].source_sequence == 0
    assert rows[0].event_type == "chat.message.received"
    assert len(lab.service._pending_events) == 1
    restored = legacy_event_from_life_event(rows[0])
    assert restored is not None
    assert restored.event_type is EventType.MESSAGE
    assert restored.source == "simulation"
    assert restored.raw_content == message.content
    assert restored.occurrence_id == lab.service._pending_events[0].occurrence_id
    # A returned row is itself safe to replay, including producer sequence zero.
    assert await lab.store.append(rows[0]) == rows[0]
    assert await lab.store.get_by_occurrence_id(rows[0].occurrence_id) == rows[0]
    assert len(await lab.store.read_since(0)) == 1


async def test_same_provider_id_in_distinct_streams_is_not_deduplicated(
    lab: InteractionLab,
) -> None:
    await lab.service.record_message(_message())
    await lab.service.record_message(_message(stream_id="simulation:stream-b"))
    assert len(await lab.store.read_since(0)) == 2
    assert len(lab.service._pending_events) == 2


async def test_synchronous_checkpoint_setting_uses_the_same_durable_ingress(
    lab: InteractionLab,
) -> None:
    lab.service.plugin.config.settings.message_checkpoint_async = False
    assert lab.service._message_persist_async_enabled() is False
    await lab.service.record_message(_message())
    assert lab.deferred == []
    assert len(await lab.store.read_since(0)) == 1
    restarted = lab.new_service()
    await restarted._load_runtime_context()
    assert len(restarted._pending_events) == 1


async def test_message_retry_does_not_repeat_inbound_runtime_side_effects(
    lab: InteractionLab,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notifications: list[Any] = []
    monkeypatch.setattr(
        lab.service,
        "_schedule_curiosity_review",
        lambda *args: notifications.append(args),
    )
    await lab.service.record_message(_message())
    lab.service._state.self_pause_until = "2027-01-01T00:00:00+00:00"
    await lab.service.record_message(_message())
    assert len(notifications) == 1
    assert lab.service._state.self_pause_until == "2027-01-01T00:00:00+00:00"


async def test_append_must_complete_before_pending_or_background_notification(
    lab: InteractionLab,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    append = lab.store.append

    async def slow_append(event: Any) -> Any:
        entered.set()
        await release.wait()
        return await append(event)

    monkeypatch.setattr(lab.store, "append", slow_append)
    task = asyncio.create_task(lab.service.record_message(_message()))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert lab.service._pending_events == []
        assert lab.deferred == []
        release.set()
        await asyncio.wait_for(task, 3)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert len(await lab.store.read_since(0)) == 1
    assert len(lab.service._pending_events) == 1


async def test_failed_append_is_not_a_successful_message_acceptance(
    lab: InteractionLab,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject(_event: Any) -> Any:
        raise OSError("injected ledger failure")

    monkeypatch.setattr(lab.store, "append", reject)
    with pytest.raises(OSError, match="injected ledger"):
        await lab.service.record_message(_message())
    assert lab.service._pending_events == []
    assert lab.deferred == []
    assert lab.service._state.last_external_message_at is None


async def test_lost_notification_and_restart_recover_from_ledger(
    lab: InteractionLab,
) -> None:
    await lab.service.record_message(_message())
    assert lab.deferred  # Process dies before background checkpoint.
    lab.deferred.clear()
    restarted = lab.new_service()
    await restarted._load_runtime_context()
    assert len(restarted._pending_events) == 1
    assert restarted._pending_events[0].raw_content == _message().content
    await restarted.record_message(_message())
    assert len(restarted._pending_events) == 1
    assert len(await lab.store.read_since(0)) == 1
    await lab.flush()
    again = lab.new_service()
    await again._load_runtime_context()
    assert len(again._pending_events) == 1


async def test_failed_checkpoint_is_retried_even_if_batch_is_already_in_memory(
    lab: InteractionLab,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = lab.service._event_builder.build_direct_message_event("checkpoint evidence")
    await lab.service._publish_raw_events([event])
    save = lab.service._save_runtime_context
    attempts = 0

    async def failing_save(**kwargs: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise OSError("injected checkpoint failure")
        await save(**kwargs)

    monkeypatch.setattr(lab.service, "_save_runtime_context", failing_save)
    for _ in range(2):
        with pytest.raises(OSError, match="injected checkpoint"):
            await lab.service.catch_up_subconscious_ingest()
        assert len(lab.service._pending_events) == 1
        assert await lab.store.get_consumer_offset(SUBCONSCIOUS_INGEST_CONSUMER_ID) == 0
    await lab.service.catch_up_subconscious_ingest()
    assert attempts == 3
    assert await lab.store.get_consumer_offset(SUBCONSCIOUS_INGEST_CONSUMER_ID) > 0
    restarted = lab.new_service()
    await restarted._load_runtime_context()
    assert len(restarted._pending_events) == 1


async def test_consumers_are_independent_and_stale_commit_fails(
    lab: InteractionLab,
) -> None:
    await lab.service.record_message(_message())
    end = (await lab.store.read_tail(1))[0].sequence
    initial = await lab.store.consumer_cursor("simulation:reader-a")
    await lab.store.commit_consumer_cursor(
        initial.consumer_id,
        expected_position=initial.position,
        expected_revision=initial.revision,
        through_position=end,
    )
    assert (await lab.store.consumer_cursor("simulation:reader-b")).position == 0
    with pytest.raises(LifeEventConsumerConflict):
        await lab.store.commit_consumer_cursor(
            initial.consumer_id,
            expected_position=initial.position,
            expected_revision=initial.revision,
            through_position=end,
        )
    with pytest.raises(LifeEventConsumerConflict):
        await lab.store.commit_consumer_cursor(
            "simulation:reader-b",
            expected_position=0,
            expected_revision=0,
            through_position=end + 100,
        )


async def test_real_read_tool_keeps_call_result_and_delivery_causality(
    lab: InteractionLab,
) -> None:
    target = lab.root / "workspace" / "engineering-fixture.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    original = "仿真工程夹具，不是主体记忆。\n" + "🪷" * 100
    target.write_text(original, encoding="utf-8")
    await lab.service.record_message(_message(content="请读取工程夹具"))
    inbound = (await lab.store.read_since(0))[0]
    call = await lab.service.record_tool_call(
        "nucleus_read_file",
        {"path": target.name, "limit": 0},
        call_id="simulation:tool-call",
        causation_id=inbound.occurrence_id,
    )
    tool = LifeEngineReadFileTool(plugin=lab.service.plugin)
    ok, output = await tool.execute(path=target.name, limit=0)
    assert ok is True
    result = await lab.service.record_tool_result(
        "nucleus_read_file",
        output,
        ok,
        call_event=call,
    )
    outbound = _message(
        "reply-1",
        content="已读取",
        extra={
            "conscious_activity_id": result.occurrence_id,
            "tool_call_id": call.call_id,
            "consciousness_instance_id": "simulation:consciousness",
            "provider_receipt": {"message_id": "simulation:receipt"},
        },
    )
    await lab.service.record_send_requested(outbound)
    await lab.service.record_delivery_status(outbound, status="unknown")
    await lab.service.record_message(outbound, direction="sent")
    await lab.flush()
    rows = await lab.store.read_since(0)
    calls = [row for row in rows if row.event_type == "tool_call"]
    results = [row for row in rows if row.event_type == "tool_result"]
    assert len(calls) == len(results) == 1
    assert results[0].causation_id == call.event_id
    assert json.loads(results[0].content)["result"] == output
    assert calls[0].causation_id == inbound.occurrence_id
    assert target.read_text(encoding="utf-8") == original
    chat_types = [row.event_type for row in rows if row.event_type.startswith("chat.")]
    assert chat_types == [
        "chat.message.received",
        "chat.message.send_requested",
        "chat.message.delivery_unknown",
        "chat.message.delivery_confirmed",
    ]
    assert rows[-1].causation_id == result.occurrence_id
    assert rows[-1].metadata["provider_receipt"]["message_id"] == "simulation:receipt"


async def test_legacy_pair_replay_does_not_rewrite_or_add_history(
    lab: InteractionLab,
) -> None:
    message = _message()
    legacy = lab.service._event_builder.build_message_event(message)
    fact = build_chat_message_event(message, direction="received")
    before = await lab.store.append_many([life_event_from_legacy(legacy), fact])
    await lab.service.record_message(message)
    assert await lab.store.read_since(0) == before
    await lab.service.catch_up_subconscious_ingest()
    assert len(lab.service._pending_events) == 1
    # Same identity with changed evidence must never be accepted as replay.
    with pytest.raises((ValueError, RuntimeError)):
        await lab.service.record_message(_message(content="different evidence"))
    assert await lab.store.read_since(0) == before


async def test_occurrence_time_is_not_ingest_order(lab: InteractionLab) -> None:
    later = build_chat_message_event(_message("later"), direction="received")
    earlier = replace(
        build_chat_message_event(_message("earlier"), direction="received"),
        timestamp="2026-01-01T00:00:00+00:00",
    )
    rows = await lab.store.append_many([later, earlier])
    assert rows[0].sequence < rows[1].sequence
    assert rows[0].timestamp > rows[1].timestamp
    assert await lab.store.read_since(0) == rows


async def test_cancel_after_commit_recovers_without_duplicate_occurrence(
    lab: InteractionLab,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    committed = asyncio.Event()
    append = lab.store.append

    async def interrupted_receipt(event: Any) -> Any:
        await append(event)
        committed.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(lab.store, "append", interrupted_receipt)
    task = asyncio.create_task(lab.service.record_message(_message()))
    await asyncio.wait_for(committed.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lab.service._pending_events == []
    monkeypatch.setattr(lab.store, "append", append)
    restarted = lab.new_service()
    await restarted.catch_up_subconscious_ingest()
    await restarted.record_message(_message())
    assert len(await lab.store.read_since(0)) == 1
    assert len(restarted._pending_events) == 1


async def test_publish_freezes_payload_before_waiting_for_ingress_lock(
    lab: InteractionLab,
) -> None:
    bus = lab.service._get_event_bus()
    event = build_chat_message_event(_message(), direction="received")
    await bus._lock.acquire()
    task = asyncio.create_task(bus.publish(event))
    try:
        await asyncio.sleep(0)
        event.content = "mutated after publication began"
        event.metadata["chat"]["sender"]["name"] = "mutated name"
    finally:
        bus._lock.release()
    saved = await task
    assert saved.content == _message().content
    assert saved.metadata["chat"]["sender"]["name"] == "Test source"


async def test_real_transport_handler_does_not_return_success_for_failed_append(
    lab: InteractionLab,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = LifeEngineMessageCollectorHandler(
        SimpleNamespace(
            plugin_name="life_engine",
            service=lab.service,
        )
    )
    decision, _ = await handler.execute(
        TransportEventType.ON_MESSAGE_RECEIVED.value,
        {"message": _message()},
    )
    assert decision is EventDecision.SUCCESS
    assert len(await lab.store.read_since(0)) == 1

    async def reject(_event: Any) -> Any:
        raise OSError("private-payload-must-not-appear")

    monkeypatch.setattr(lab.store, "append", reject)
    with pytest.raises(LifeEventCollectionFailed) as failed:
        await handler.execute(
            TransportEventType.ON_MESSAGE_RECEIVED.value,
            {"message": _message("second-message")},
        )
    assert str(failed.value) == "OSError"
    assert len(lab.service._pending_events) == 1


async def test_local_cursor_upgrade_preserves_old_state(tmp_path: Path) -> None:
    store = RawEventStore(tmp_path)
    row = await store.append(build_chat_message_event(_message(), direction="received"))
    # Build a pre-revision cursor table in this test's temporary database only.
    with sqlite3.connect(store.database_path) as db:
        db.execute("DROP TABLE raw_event_consumer_offsets")
        db.execute(
            "CREATE TABLE raw_event_consumer_offsets (consumer_id TEXT PRIMARY KEY, "
            "ingest_position INTEGER NOT NULL, updated_at TEXT NOT NULL, metadata_json TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO raw_event_consumer_offsets VALUES (?, ?, ?, ?)",
            ("old-reader", row.sequence, "2026-09-05T00:00:00+00:00", '{"old":true}'),
        )
    restarted = RawEventStore(tmp_path)
    cursor = await restarted.consumer_cursor("old-reader")
    assert cursor.position == row.sequence
    assert cursor.metadata == {"old": True}
    assert cursor.revision == 1
    assert await restarted.read_since(0) == [row]


async def test_local_sparse_positions_are_not_invented_history_gaps(
    tmp_path: Path,
) -> None:
    store = RawEventStore(tmp_path)
    await store.health()
    with sqlite3.connect(store.database_path) as db:
        db.execute(
            "INSERT INTO sqlite_sequence(name, seq) VALUES ('raw_life_events', 9)"
        )
    row = await store.append(build_chat_message_event(_message(), direction="received"))
    assert row.sequence == 10
    assert await store.read_since(1) == [row]
    with sqlite3.connect(store.database_path) as db:
        db.execute(
            "INSERT INTO raw_event_store_meta VALUES (?, ?, ?)",
            ("history_floor_position", "5", "2026-09-05T00:00:00+00:00"),
        )
    with pytest.raises(RawEventGapError):
        await store.read_since(1)


async def test_completion_retry_reuses_original_event_after_checkpoint_failure(
    lab: InteractionLab,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = lab.service._event_builder
    first = builder.build_agent_result_event("simulation", "complete evidence")
    first.occurrence_id = "simulation:completed-agent"
    first.content_ref = "life-event-occurrence:simulation:completed-agent"
    save = lab.service._save_runtime_context

    async def fail(**_kwargs: Any) -> None:
        raise OSError("injected completion checkpoint failure")

    monkeypatch.setattr(lab.service, "_save_runtime_context", fail)
    with pytest.raises(OSError, match="completion checkpoint"):
        await lab.service._queue_completed_background_events([first])
    before = await lab.store.read_since(0)
    monkeypatch.setattr(lab.service, "_save_runtime_context", save)
    retry = builder.build_agent_result_event("simulation", "complete evidence")
    retry.occurrence_id, retry.content_ref = first.occurrence_id, first.content_ref
    await lab.service._queue_completed_background_events([retry])
    assert await lab.store.read_since(0) == before
    assert len(lab.service._pending_events) == 1
    changed = builder.build_agent_result_event("simulation", "changed evidence")
    changed.occurrence_id, changed.content_ref = first.occurrence_id, first.content_ref
    with pytest.raises(RuntimeError):
        await lab.service._queue_completed_background_events([changed])


async def test_independent_tool_occurrences_survive_reset_source_counter(
    tmp_path: Path,
) -> None:
    store = RawEventStore(tmp_path)
    first = EventBuilder(lambda: 1).build_tool_call_event(
        "nucleus_read_file", {"path": "a"}
    )
    second = EventBuilder(lambda: 1).build_tool_call_event(
        "nucleus_read_file", {"path": "b"}
    )
    assert first.occurrence_id != second.occurrence_id
    await store.append_many(
        [life_event_from_legacy(first), life_event_from_legacy(second)]
    )
    assert len(await store.read_since(0)) == 2

