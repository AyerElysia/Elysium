"""Synthetic restart contracts: persistence never prunes unacknowledged history.

Only in-memory stores and pytest temporary local files are used. No service,
model, formal database, or raw-consumer cursor participates in these tests.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.service.event_builder import (
    EventType,
    LifeEngineEvent,
    LifeEngineState,
)
from plugins.life_engine.service.state_manager import StatePersistence, event_to_dict
from plugins.life_engine.service.subconscious_context import SubconsciousContextManager


class _MemoryStore:
    def __init__(self) -> None:
        self.record = None

    async def put_state(self, **kwargs):
        self.record = SimpleNamespace(
            revision=int(kwargs["expected_revision"]) + 1,
            payload=deepcopy(kwargs["payload"]),
        )
        return self.record

    async def get_state(self, namespace, state_key):
        assert (namespace, state_key) == ("life_engine.runtime_context", "global")
        return deepcopy(self.record)


def _event(sequence: int, *, consumed: bool = False) -> LifeEngineEvent:
    return LifeEngineEvent(
        event_id=f"restart-event-{sequence}",
        event_type=EventType.CONSCIOUS_ACTIVITY,
        timestamp="2026-09-06T00:00:00+00:00",
        sequence=sequence,
        source="synthetic-restart",
        source_detail="isolated persistence contract",
        content=f"synthetic visible {sequence}",
        raw_content=f"synthetic complete original {sequence}: \u7231\u8389\n" + "r" * 300,
        source_instance_id="synthetic-heartbeat-instance",
        occurrence_id=f"synthetic-occurrence-{sequence}",
        content_ref=f"synthetic-ref:{sequence}",
        heartbeat_context_consumed=consumed,
    )


@pytest.fixture(params=["selected", "local"])
def persistence_factory(request, tmp_path: Path):
    store = _MemoryStore() if request.param == "selected" else None

    def factory(limit: int) -> StatePersistence:
        return StatePersistence(str(tmp_path), lambda: limit, runtime_store=store)

    return factory


def _serialized(events):
    return [event_to_dict(event) for event in events]


@pytest.mark.parametrize("limit", [0, 1, 100])
async def test_restart_preserves_all_219_history_and_45_pending_events(
    persistence_factory, limit: int,
) -> None:
    history = [_event(sequence) for sequence in range(28480, 28699)]
    pending = [_event(sequence) for sequence in range(28700, 28745)]
    state = LifeEngineState(
        heartbeat_count=6061, event_sequence=28870, heartbeat_context_cursor=28469,
    )
    original_history = _serialized(history)
    original_pending = _serialized(pending)
    writer = persistence_factory(limit)
    await writer.save_runtime_context(state, pending, history)

    for _ in range(2):
        reader = persistence_factory(limit)
        restored = LifeEngineState()
        loaded_pending, loaded_history, _ = await reader.load_runtime_context(
            restored, lambda: pytest.fail("Existing sequences must not be regenerated"),
        )
        assert _serialized(loaded_history) == original_history
        assert _serialized(loaded_pending) == original_pending
        assert restored.history_event_count == 219
        assert restored.pending_event_count == 45
        assert restored.heartbeat_context_cursor == 28469
        assert restored.event_sequence == 28870
        assert restored.heartbeat_count == 6061
        await reader.save_runtime_context(restored, loaded_pending, loaded_history)

    assert _serialized(history) == original_history
    assert _serialized(pending) == original_pending


@pytest.mark.parametrize("link", ["run", "call", "parent", "causation", "legacy"])
async def test_zero_limit_preserves_cross_cursor_cross_queue_causal_group(
    persistence_factory, link: str,
) -> None:
    call = _event(1, consumed=True)
    call.event_type = EventType.TOOL_CALL
    call.tool_name = "synthetic_tool"
    result = _event(2)
    result.event_type = EventType.TOOL_RESULT
    result.tool_name = call.tool_name
    result.tool_success = True
    if link == "run":
        call.heartbeat_run_id = result.heartbeat_run_id = "synthetic-run"
    elif link == "call":
        call.call_id = result.call_id = "synthetic-call"
    elif link == "parent":
        result.parent_event_id = call.event_id
    elif link == "causation":
        result.causation_id = call.event_id
    history = [call, *[_event(sequence, consumed=True) for sequence in range(3, 104)]]
    state = LifeEngineState(event_sequence=103, heartbeat_context_cursor=1)
    await persistence_factory(0).save_runtime_context(state, [result], history)

    restored = LifeEngineState()
    pending, loaded, _ = await persistence_factory(0).load_runtime_context(
        restored, lambda: pytest.fail("Existing sequences must remain stable"),
    )

    assert _serialized(loaded) == _serialized(history)
    assert _serialized(pending) == _serialized([result])
    group = next(
        group for group in SubconsciousContextManager().group_events([*loaded, *pending])
        if call.event_id in group.event_ids
    )
    assert group.event_ids == [call.event_id, result.event_id]
    assert group.closed
    assert restored.heartbeat_context_cursor == 1


async def test_zero_limit_preserves_consumed_history_until_compaction_owner_decides(
    persistence_factory,
) -> None:
    history = [_event(sequence, consumed=True) for sequence in range(1, 8)]
    state = LifeEngineState(event_sequence=7, heartbeat_context_cursor=7)
    await persistence_factory(0).save_runtime_context(state, [], history)

    restored = LifeEngineState()
    pending, loaded, _ = await persistence_factory(0).load_runtime_context(
        restored, lambda: pytest.fail("Existing sequences must remain stable"),
    )

    assert pending == []
    assert _serialized(loaded) == _serialized(history)
    assert restored.heartbeat_context_cursor == 7


async def test_summary_frontier_does_not_authorize_restart_history_pruning(
    persistence_factory,
) -> None:
    history = [_event(7), _event(100, consumed=True)]
    state = LifeEngineState(event_sequence=1, heartbeat_context_cursor=3)
    state.subconscious_summary = {
        "schema_version": 1,
        "covered_from_sequence": 1,
        "covered_through_sequence": 100,
        "entries": [],
        "stats": {},
    }
    await persistence_factory(0).save_runtime_context(state, [], history)

    restored = LifeEngineState()
    _, loaded, _ = await persistence_factory(0).load_runtime_context(
        restored, lambda: pytest.fail("Existing sequences must remain stable"),
    )

    assert _serialized(loaded) == _serialized(history)
    assert restored.event_sequence == 100
    assert restored.heartbeat_context_cursor == 3
    assert restored.subconscious_summary["covered_through_sequence"] == 100


async def test_restart_sorts_without_changing_flags_or_dropping_open_groups(
    persistence_factory,
) -> None:
    below_cursor = _event(1, consumed=False)
    above_cursor = _event(7, consumed=True)
    same_sequence = replace(
        above_cursor, event_id="restart-event-7-z", heartbeat_context_consumed=False,
    )
    open_call = _event(9, consumed=True)
    open_call.event_type = EventType.TOOL_CALL
    open_call.call_id = "synthetic-open-call"
    open_call.tool_args = {"value": ["synthetic", 1], "exact": "  padded\ntext  "}
    history = [open_call, same_sequence, above_cursor, below_cursor]
    pending = [_event(12), _event(11)]
    state = LifeEngineState(event_sequence=12, heartbeat_context_cursor=5)
    await persistence_factory(1).save_runtime_context(state, pending, history)

    restored = LifeEngineState()
    loaded_pending, loaded_history, _ = await persistence_factory(1).load_runtime_context(
        restored, lambda: pytest.fail("Existing sequences must remain stable"),
    )

    assert _serialized(loaded_history) == _serialized(
        [below_cursor, above_cursor, same_sequence, open_call],
    )
    assert _serialized(loaded_pending) == _serialized(list(reversed(pending)))
    assert restored.heartbeat_context_cursor == 5
    assert [event.heartbeat_context_consumed for event in loaded_history] == [
        False, True, False, True,
    ]
