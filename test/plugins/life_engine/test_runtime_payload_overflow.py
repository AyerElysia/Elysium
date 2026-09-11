"""Persist-time overflow may drop consumed history; unconsumed and pending stay."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from plugins.life_engine.service import state_manager as state_manager_module
from plugins.life_engine.service.event_builder import (
    EventType,
    LifeEngineEvent,
    LifeEngineState,
)
from plugins.life_engine.service.state_manager import (
    StatePersistence,
    _runtime_context_payload,
    _runtime_payload_utf8_size,
    event_to_dict,
    fit_runtime_snapshot_to_storage_limit,
)
from src.kernel.storage import canonical_json


class _LimitedStore:
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.record = None

    async def put_state(self, **kwargs):
        encoded = canonical_json(kwargs["payload"])
        if len(encoded.encode("utf-8")) > self.max_bytes:
            raise ValueError("runtime payload exceeds explicit storage limit")
        self.record = SimpleNamespace(
            revision=int(kwargs["expected_revision"]) + 1,
            payload=deepcopy(kwargs["payload"]),
        )
        return self.record


def _bulky_event(
    sequence: int,
    *,
    consumed: bool = False,
    body: str = "x" * 4000,
) -> LifeEngineEvent:
    return LifeEngineEvent(
        event_id=f"overflow-event-{sequence}",
        event_type=EventType.CONSCIOUS_ACTIVITY,
        timestamp="2026-09-11T00:00:00+00:00",
        sequence=sequence,
        source="synthetic-overflow",
        source_detail="storage overflow fixture",
        content=body,
        raw_content=body + "-raw",
        heartbeat_context_consumed=consumed,
    )


def _event_ids(events: list[LifeEngineEvent]) -> list[str]:
    return [event.event_id for event in events]


def test_fit_does_not_mutate_small_snapshots() -> None:
    history = [_bulky_event(sequence, consumed=True) for sequence in range(1, 4)]
    pending = [_bulky_event(4)]
    state = LifeEngineState(
        event_sequence=4,
        heartbeat_context_cursor=3,
        last_model_reply="short reply",
    )
    original_history = [event_to_dict(event) for event in history]
    original_pending = [event_to_dict(event) for event in pending]
    original_reply = state.last_model_reply

    changed = fit_runtime_snapshot_to_storage_limit(
        state, pending, history, max_bytes=16 * 1024 * 1024
    )

    assert changed is False
    assert [event_to_dict(event) for event in history] == original_history
    assert [event_to_dict(event) for event in pending] == original_pending
    assert state.last_model_reply == original_reply


def test_fit_drops_consumed_history_and_keeps_unconsumed_and_pending() -> None:
    consumed = [
        _bulky_event(sequence, consumed=True) for sequence in range(1, 41)
    ]
    unconsumed = [_bulky_event(sequence) for sequence in range(41, 44)]
    pending = [_bulky_event(sequence) for sequence in range(44, 47)]
    history = [*consumed, *unconsumed]
    state = LifeEngineState(event_sequence=46, heartbeat_context_cursor=40)
    max_bytes = 120_000
    original_pending = [event_to_dict(event) for event in pending]
    original_unconsumed = _event_ids(unconsumed)

    assert (
        _runtime_payload_utf8_size(
            _runtime_context_payload(state, pending, history)
        )
        > max_bytes
    )

    changed = fit_runtime_snapshot_to_storage_limit(
        state, pending, history, max_bytes=max_bytes
    )

    assert changed is True
    assert _event_ids(pending) == [f"overflow-event-{n}" for n in range(44, 47)]
    assert [event_to_dict(event) for event in pending] == original_pending
    for event_id in original_unconsumed:
        assert event_id in _event_ids(history)
    assert len(history) < 40 + 3
    assert (
        _runtime_payload_utf8_size(
            _runtime_context_payload(state, pending, history)
        )
        <= max_bytes
    )


async def test_save_runtime_context_fits_limited_store(tmp_path, monkeypatch) -> None:
    max_bytes = 120_000
    monkeypatch.setattr(state_manager_module, "MAX_RUNTIME_PAYLOAD_BYTES", max_bytes)
    store = _LimitedStore(max_bytes)
    persistence = StatePersistence(str(tmp_path), lambda: 100, runtime_store=store)
    consumed = [
        _bulky_event(sequence, consumed=True) for sequence in range(1, 41)
    ]
    unconsumed = [_bulky_event(sequence) for sequence in range(41, 44)]
    pending = [_bulky_event(sequence) for sequence in range(44, 47)]
    history = [*consumed, *unconsumed]
    state = LifeEngineState(event_sequence=46, heartbeat_context_cursor=40)

    await persistence.save_runtime_context(state, pending, history)

    assert store.record is not None
    payload = store.record.payload
    encoded = canonical_json(payload)
    assert len(encoded.encode("utf-8")) <= max_bytes
    loaded_ids = [item["event_id"] for item in payload["event_history"]]
    pending_ids = [item["event_id"] for item in payload["pending_events"]]
    assert pending_ids == [f"overflow-event-{n}" for n in range(44, 47)]
    for event_id in [f"overflow-event-{n}" for n in range(41, 44)]:
        assert event_id in loaded_ids
