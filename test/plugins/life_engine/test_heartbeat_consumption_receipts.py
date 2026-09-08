"""Synthetic heartbeat consumption receipts: candidates are not commits.

No service constructor/startup, model client, network or formal storage is used.
"""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.life_engine.service import core as service_module
from plugins.life_engine.service.core import HeartbeatModelResult, LifeEngineService
from plugins.life_engine.service.event_builder import (
    EventType,
    LifeEngineEvent,
    LifeEngineState,
)
from plugins.life_engine.service.perception_gateway import PerceptionDeliveryUnverified
from plugins.life_engine.service.subconscious_context import (
    HeartbeatConsumptionReceipt,
    SubconsciousContextManager,
)
from src.kernel.llm.context_delivery import EffectiveContextReceipt


def _event(sequence: int, *, consumed: bool = False) -> LifeEngineEvent:
    return LifeEngineEvent(
        event_id=f"synthetic-event-{sequence}",
        event_type=EventType.MESSAGE,
        timestamp="2026-09-06T00:00:00+00:00",
        sequence=sequence,
        source="synthetic-source",
        source_detail="isolated receipt regression",
        content=f"synthetic event {sequence}",
        heartbeat_context_consumed=consumed,
    )

@pytest.fixture
def service(tmp_path, monkeypatch):
    service = LifeEngineService.__new__(LifeEngineService)
    config = SimpleNamespace(
        settings=SimpleNamespace(
            workspace_path=str(tmp_path),
            heartbeat_timeout_seconds=2,
            log_heartbeat=True,
        ),
        model=SimpleNamespace(task_name="synthetic-heartbeat"),
    )
    service._cfg = lambda: config
    service._state = LifeEngineState()
    service._lock = asyncio.Lock()
    service._heartbeat_run_lock = asyncio.Lock()
    service._event_history = [_event(1)]
    service._pending_events = []
    service._state_dirty = False
    service._multi_writer_bridge = None
    service._opportunity_runtime = None
    service._opportunity_health = {}
    service._learning_scheduler = None
    service._subconscious_context = SubconsciousContextManager(recent_group_count=0)
    service._record_model_reply = AsyncMock()
    service._publish_raw_events = AsyncMock()
    service._collect_background_agent_results = AsyncMock()
    service._collect_background_mission_results = AsyncMock()
    service._event_builder = SimpleNamespace(
        build_heartbeat_event=lambda *_args, **_kwargs: _event(100),
    )

    async def save(**_kwargs):
        service._state_dirty = False

    async def prepare():
        return _prepare(service)

    service._save_runtime_context = AsyncMock(side_effect=save)
    service._prepare_heartbeat_context = prepare
    service._sleep_state_active = False
    service._self_pause_skip_logged = False
    service._stop_event = asyncio.Event()
    service._effective_heartbeat_interval = lambda: 0
    service._in_sleep_window_now = lambda: (False, "synthetic")
    service._self_pause_status = lambda: (False, None, None, None)
    logs = []
    service._synthetic_logs = logs
    monkeypatch.setattr(
        service_module,
        "logger",
        SimpleNamespace(
            info=lambda message: logs.append(str(message)),
            warning=lambda message: logs.append(str(message)),
            debug=lambda message: logs.append(str(message)),
            error=lambda message: logs.append(str(message)),
        ),
    )
    monkeypatch.setattr(service_module, "log_error", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service_module, "log_heartbeat_event", lambda **_kwargs: None)
    return service


def _prepare(service):
    prepared = service._subconscious_context.prepare(
        service._event_history,
        cursor=service._state.heartbeat_context_cursor,
        existing_summary=service._state.subconscious_summary,
    )
    LifeEngineService._seal_subconscious_activity_delivery(prepared)
    return prepared


def _delivery(prepared) -> EffectiveContextReceipt:
    return EffectiveContextReceipt(
        delivery_id=prepared.delivery_id,
        exact_present=True,
        expected_utf8_bytes=prepared.delivery_bytes,
        expected_sha256=prepared.delivery_sha256,
        effective_utf8_bytes=prepared.delivery_bytes,
        effective_sha256=prepared.delivery_sha256,
    )

async def _commit(service, prepared, *, model_reply=""):
    return await service._commit_heartbeat_context(
        prepared,
        model_reply,
        "synthetic-heartbeat-run",
        None,
        _delivery(prepared),
    )


def test_prepared_acknowledgement_candidates_are_not_a_receipt(service):
    prepared = _prepare(service)
    assert prepared.selected_event_ids == ["synthetic-event-1"]
    assert prepared.acknowledged_event_ids == ["synthetic-event-1"]
    assert prepared.consumption_receipt is None


async def test_only_saved_newly_consumed_events_are_in_the_receipt(service):
    already_consumed = _event(2, consumed=True)
    service._event_history.append(already_consumed)
    prepared = _prepare(service)
    prepared.acknowledged_event_ids.extend(
        ["synthetic-event-1", already_consumed.event_id, "synthetic-missing-event"]
    )

    async def save(**kwargs):
        assert kwargs == {"recoverable_on_shared_conflict": False}
        assert prepared.consumption_receipt is None
        service._state_dirty = False

    service._save_runtime_context.side_effect = save
    receipt = await _commit(service, prepared)
    assert receipt is prepared.consumption_receipt
    assert receipt.event_ids == ("synthetic-event-1",)
    assert receipt.heartbeat_run_id == "synthetic-heartbeat-run"
    assert receipt.cursor_before == 0
    assert receipt.cursor_after == prepared.snapshot_high_water
    with pytest.raises(FrozenInstanceError):
        receipt.cursor_after = 999


@pytest.mark.parametrize("failure", ["missing", "nonexact", "wrong_hash"])
async def test_missing_or_invalid_delivery_never_issues_a_receipt(service, failure):
    prepared = _prepare(service)
    prepared.consumption_receipt = HeartbeatConsumptionReceipt("old-run", ("old",), 0, 1)
    delivery = _delivery(prepared)
    if failure == "missing":
        delivery = None
    elif failure == "nonexact":
        delivery = replace(delivery, exact_present=False)
    else:
        delivery = replace(delivery, effective_sha256="different")
    with pytest.raises(PerceptionDeliveryUnverified):
        await service._commit_heartbeat_context(prepared, "", "new-run", None, delivery)
    assert prepared.consumption_receipt is None
    assert service._state.heartbeat_context_cursor == 0
    assert not service._event_history[0].heartbeat_context_consumed
    service._save_runtime_context.assert_not_awaited()


@pytest.mark.parametrize("failure", ["exception", "dirty_return", "cancelled"])
async def test_failed_save_restores_pending_events_and_next_round_can_retry(
    service, failure,
):
    original = service._event_history[0]
    prepared = _prepare(service)
    arrived_history = _event(2)
    arrived_pending = _event(3)
    failed_snapshot = {}

    async def failed_save(**kwargs):
        assert kwargs == {"recoverable_on_shared_conflict": False}
        assert prepared.consumption_receipt is None
        # recent_group_count=0 means the candidate is already compacted away.
        assert original not in service._event_history
        failed_snapshot["cursor"] = service._state.heartbeat_context_cursor
        service._event_history.append(arrived_history)
        service._pending_events.append(arrived_pending)
        if failure == "cancelled":
            raise asyncio.CancelledError("synthetic commit cancellation")
        if failure == "dirty_return":
            service._state_dirty = True
            return
        raise OSError("synthetic rejected commit")

    service._save_runtime_context.side_effect = failed_save
    expected = asyncio.CancelledError if failure == "cancelled" else Exception
    with pytest.raises(expected):
        await _commit(service, prepared)
    assert failed_snapshot["cursor"] == 1
    assert prepared.consumption_receipt is None
    assert service._state.heartbeat_context_cursor == 0
    assert service._state.subconscious_summary == {}
    assert service._state_dirty
    assert not original.heartbeat_context_consumed
    assert not arrived_history.heartbeat_context_consumed
    assert not arrived_pending.heartbeat_context_consumed
    assert [event.event_id for event in service._event_history] == [
        original.event_id, arrived_history.event_id,
    ]
    assert service._pending_events == [arrived_pending]

    # The next prepare must select the failed batch again, not skip its cursor.
    retry = _prepare(service)
    assert set(retry.selected_event_ids) == {original.event_id, arrived_history.event_id}

    async def successful_save(**_kwargs):
        service._state_dirty = False

    service._save_runtime_context.side_effect = successful_save
    receipt = await _commit(service, retry)
    assert set(receipt.event_ids) == {original.event_id, arrived_history.event_id}
    assert receipt.cursor_after == 2
    assert service._pending_events == [arrived_pending]
    assert not arrived_pending.heartbeat_context_consumed


async def test_failed_raw_publication_restores_consumption_and_keeps_generated_event(service):
    prepared = _prepare(service)
    service._publish_raw_events.side_effect = OSError("synthetic raw publish failure")
    with pytest.raises(OSError, match="raw publish failure"):
        await _commit(service, prepared, model_reply="synthetic generated reply")
    assert prepared.consumption_receipt is None
    assert service._state.heartbeat_context_cursor == 0
    assert {event.event_id for event in service._event_history} == {
        "synthetic-event-1", "synthetic-event-100",
    }
    assert all(not event.heartbeat_context_consumed for event in service._event_history)
    service._save_runtime_context.assert_not_awaited()


async def test_compaction_failure_restores_consumption_before_any_save(service, monkeypatch):
    prepared = _prepare(service)

    def fail_compaction(*_args, **_kwargs):
        raise ValueError("synthetic compaction failure")

    monkeypatch.setattr(service._subconscious_context, "compact_history", fail_compaction)
    with pytest.raises(ValueError, match="compaction failure"):
        await _commit(service, prepared)
    assert prepared.consumption_receipt is None
    assert service._state.heartbeat_context_cursor == 0
    assert not service._event_history[0].heartbeat_context_consumed
    service._save_runtime_context.assert_not_awaited()


@pytest.mark.parametrize("outcome", ["unresolved", "missing_receipt", "model_failed", "committed"])
async def test_real_round_logs_consumption_only_after_commit(service, outcome):
    prepared = _prepare(service)

    async def prepare():
        return prepared

    async def model(*_args, **_kwargs):
        if outcome == "model_failed":
            raise OSError("synthetic model failure")
        return HeartbeatModelResult(
            text="",
            perception_receipt=None,
            subconscious_receipt=None if outcome == "missing_receipt" else _delivery(prepared),
            compression_unresolved=outcome == "unresolved",
        )

    service._prepare_heartbeat_context = prepare
    service._run_heartbeat_model = model
    original_round = service._run_heartbeat_round

    async def one_round(**kwargs):
        try:
            return await original_round(**kwargs)
        finally:
            service._state.running = False

    service._run_heartbeat_round = one_round
    service._state.running = True
    await asyncio.wait_for(service._heartbeat_loop(), timeout=2)
    consumed_logs = [message for message in service._synthetic_logs if "已消费" in message]
    if outcome == "committed":
        assert len(consumed_logs) == 1, service._synthetic_logs
        assert "已消费 1 条事件" in consumed_logs[0]
        assert prepared.consumption_receipt.event_ids == ("synthetic-event-1",)
    else:
        assert consumed_logs == []
        assert prepared.consumption_receipt is None
        assert service._state.heartbeat_context_cursor == 0
        assert not service._event_history[0].heartbeat_context_consumed
        if outcome == "unresolved":
            assert any(
                "准备了 1 条事件，本拍未消费" in message
                for message in service._synthetic_logs
            ), service._synthetic_logs
        elif outcome == "missing_receipt":
            assert "exact subconscious activity delivery proof" in service._state.last_model_error
        else:
            assert service._state.last_model_error == "synthetic model failure"


async def test_legacy_prepared_candidates_do_not_count_as_committed(service):
    prepared = SimpleNamespace(
        content="synthetic prepared projection",
        selected_event_ids=["one", "two"],
        acknowledged_event_ids=["one", "two"],
    )

    async def one_round(**_kwargs):
        service._state.running = False
        return "", prepared

    service._run_heartbeat_round = one_round
    service._state.running = True
    await asyncio.wait_for(service._heartbeat_loop(), timeout=2)
    assert any("准备了 2 条事件，本拍未消费" in message for message in service._synthetic_logs)
    assert not any("已消费" in message for message in service._synthetic_logs)


async def test_unrepresented_pending_activity_is_not_logged_as_no_new_events(service):
    prepared = SimpleNamespace(
        content="",
        selected_event_ids=[],
        acknowledged_event_ids=[],
        target_reached=False,
        dropped_count=2,
    )

    async def one_round(**_kwargs):
        service._state.running = False
        return "", prepared

    service._run_heartbeat_round = one_round
    service._state.running = True
    await asyncio.wait_for(service._heartbeat_loop(), timeout=2)
    assert any(
        "待处理活动尚未完整投影，本拍未消费" in message
        for message in service._synthetic_logs
    )
    assert not any("无新事件" in message for message in service._synthetic_logs)
    assert not any("已消费" in message for message in service._synthetic_logs)
