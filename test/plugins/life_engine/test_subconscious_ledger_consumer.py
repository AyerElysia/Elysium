"""Subconscious Life Event consumer catch-up tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.agents.definitions import AgentResult
from plugins.life_engine.service.chat_events import build_chat_message_event
from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.service.event_bus import RawEventGapError
from plugins.life_engine.service.subconscious_ingest import (
    SUBCONSCIOUS_INGEST_CONSUMER_ID,
    SubconsciousLedgerGap,
    classify_life_event_for_workset,
)
from src.core.models.message import Message

from .test_service import _make_service


def _message(**kwargs: Any) -> Message:
    payload = {
        "message_id": "m-1",
        "content": "投递失败的正文",
        "sender_id": "u-1",
        "sender_name": "Ayer",
        "platform": "qq",
        "chat_type": "private",
        "stream_id": "stream-1",
    }
    payload.update(kwargs)
    return Message(**payload)


async def test_ledger_only_write_reaches_next_prepare(tmp_path: Any) -> None:
    service = _make_service(tmp_path)
    event = service._event_builder.build_dfc_message_event(
        "只写在账本里",
        stream_id="stream-1",
        platform="qq",
        chat_type="private",
        sender_name="Ayer",
    )
    await service._publish_raw_events([event])
    assert service._pending_events == []

    prepared = await service._prepare_heartbeat_context()

    identities = {
        str(item.occurrence_id or item.event_id)
        for item in service._event_history
    }
    assert event.event_id in identities or any(
        "只写在账本里" in str(item.content) for item in service._event_history
    )
    assert "只写在账本里" in prepared.content
    cursor = await service._get_event_bus().store.consumer_cursor(
        SUBCONSCIOUS_INGEST_CONSUMER_ID
    )
    assert cursor.position > 0


async def test_duplicate_occurrence_is_not_queued_twice(tmp_path: Any) -> None:
    service = _make_service(tmp_path)
    event = service._event_builder.build_dfc_message_event(
        "同一条经历",
        stream_id="stream-1",
    )
    await service._queue_pending_event(event)
    pending_before = list(service._pending_events)
    await service.catch_up_subconscious_ingest()
    assert len(service._pending_events) == len(pending_before)


async def test_bootstrap_high_water_does_not_replay_history_as_new_delta(
    tmp_path: Any,
) -> None:
    service = _make_service(tmp_path)
    old = service._event_builder.build_dfc_message_event("旧经历不应重放")
    await service._publish_raw_events([old])
    service._state.heartbeat_context_cursor = 9

    await service.catch_up_subconscious_ingest()

    assert service._pending_events == []
    assert service._subconscious_ingest_health["bootstrapped"] is True
    cursor = await service._get_event_bus().store.consumer_cursor(
        SUBCONSCIOUS_INGEST_CONSUMER_ID
    )
    assert cursor.position > 0
    assert (cursor.metadata or {}).get("bootstrap") == "high_water"
    prepared = await service._prepare_heartbeat_context()
    assert prepared.selected_event_ids == []


async def test_ledger_gap_fails_closed_without_advancing_cursor(
    tmp_path: Any,
) -> None:
    service = _make_service(tmp_path)

    class _GapStore:
        async def consumer_cursor(self, consumer_id: str) -> Any:
            return SimpleNamespace(
                consumer_id=consumer_id,
                position=2,
                revision=1,
                updated_at="2026-09-03T00:00:00+00:00",
                metadata={"stage": "subconscious_ingest"},
            )

        async def get_consumer_offset(self, consumer_id: str) -> int:
            return 2

        async def commit_consumer_offset(self, *args: object, **kwargs: object) -> int:
            raise AssertionError("gap must not commit")

        async def health_snapshot(self) -> dict[str, Any]:
            return {"latest_position": 9}

        async def read_since(self, position: int, *, limit: int | None = None) -> list:
            raise RawEventGapError(position, 8)

    service._try_life_event_store = lambda: _GapStore()  # type: ignore[method-assign]
    with pytest.raises(SubconsciousLedgerGap):
        await service.catch_up_subconscious_ingest()
    assert service._subconscious_ingest_health["gap"] is True
    assert service._pending_events == []


async def test_delivery_failed_enters_workset_send_requested_does_not(
    tmp_path: Any,
) -> None:
    service = _make_service(tmp_path)
    failed = build_chat_message_event(
        _message(message_id="fail-1"),
        direction="delivered",
        delivery_status="failed",
    )
    requested = build_chat_message_event(
        _message(message_id="req-1", content="请求发送"),
        direction="requested",
    )
    assert classify_life_event_for_workset(failed) == "synthetic_delivery"
    assert classify_life_event_for_workset(requested) == "advance"
    await service._get_event_bus().publish_many([failed, requested])

    await service.catch_up_subconscious_ingest()

    contents = [str(event.content) for event in service._pending_events]
    assert any("投递失败的正文" in text for text in contents)
    assert all("请求发送" not in text for text in contents)


async def test_agent_collect_restore_keeps_result_after_persist_failure(
    tmp_path: Any,
) -> None:
    service = _make_service(tmp_path)
    result = AgentResult(
        agent_type="explore",
        success=True,
        result_text="智能体做完了",
        rounds_used=1,
        duration_ms=12,
    )

    class _Coordinator:
        def __init__(self) -> None:
            self.restored: dict[str, AgentResult] = {}

        def has_pending(self) -> bool:
            return True

        async def collect_results(
            self, timeout_seconds: float = 5.0
        ) -> dict[str, AgentResult]:
            del timeout_seconds
            if self.restored:
                payload = dict(self.restored)
                self.restored.clear()
                return payload
            return {"agent-1": result}

        async def restore_results(self, results: dict[str, AgentResult]) -> None:
            self.restored.update(results)

    coordinator = _Coordinator()
    service.plugin._agent_coordinator = coordinator  # type: ignore[attr-defined]

    async def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("persist failed")

    service._queue_pending_events = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="persist failed"):
        await service._collect_background_agent_results()
    assert coordinator.restored["agent-1"].result_text == "智能体做完了"

    service._queue_pending_events = LifeEngineService._queue_pending_events.__get__(
        service, LifeEngineService
    )
    await service._collect_background_agent_results()
    assert any("智能体做完了" in str(event.content) for event in service._pending_events)


async def test_message_and_activity_order_preserved_through_catch_up(
    tmp_path: Any,
) -> None:
    service = _make_service(tmp_path)
    message = service._event_builder.build_dfc_message_event("先收到消息")
    activity = service._event_builder.build_conscious_model_turn_event(
        activity_id="act-1",
        transport_request_id="req-1",
        stream_id="stream-1",
        source_instance_id="chat_global",
        turn_occurrence_id="turn-1",
        provider_reasoning_content="想了想",
        assistant_message="",
        tool_call_ids=[],
        surface="life_chatter",
    )
    await service._publish_raw_events([message, activity])
    await service.catch_up_subconscious_ingest()
    contents = [str(event.content) for event in service._pending_events]
    message_index = next(
        index for index, text in enumerate(contents) if "先收到消息" in text
    )
    activity_index = next(
        index
        for index, text in enumerate(contents)
        if "想了想" in text or "has_provider_reasoning" in text
    )
    assert message_index < activity_index
