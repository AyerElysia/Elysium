"""Isolated visible-projection contracts for heartbeat prepare and commit.

All event bodies are synthetic. No service startup, model, network, formal
database, runtime file, or shared storage authority is used.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.life_engine.service import core as service_module
from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.service.event_builder import (
    EventType,
    LifeEngineEvent,
    LifeEngineState,
)
from plugins.life_engine.service.heartbeat_rolling import (
    format_new_events_text,
    format_visible_event,
)
from plugins.life_engine.service.perception_gateway import PerceptionDeliveryUnverified
from plugins.life_engine.service.subconscious_context import (
    SubconsciousContextManager,
    SubconsciousSummary,
    SummaryEntry,
)
from src.kernel.llm.context_delivery import EffectiveContextReceipt


def _event(
    sequence: int,
    *,
    content: str | None = None,
    event_type: EventType = EventType.HEARTBEAT,
    run: str | None = None,
    consumed: bool = False,
) -> LifeEngineEvent:
    return LifeEngineEvent(
        event_id=f"visible-event-{sequence}",
        event_type=event_type,
        timestamp="2026-09-06T00:00:00+00:00",
        sequence=sequence,
        source="synthetic-visible-projection",
        source_detail="isolated regression",
        content=content if content is not None else f"visible-{sequence}",
        heartbeat_run_id=run,
        heartbeat_context_consumed=consumed,
        content_ref=f"synthetic-ref:{sequence}",
    )


def _large_raw_group() -> list[LifeEngineEvent]:
    """A complete tool chain with a small existing visible representation."""
    thought = _event(
        1,
        content="visible thought",
        event_type=EventType.CONSCIOUS_ACTIVITY,
        run="synthetic-complete-run",
    )
    thought.raw_content = json.dumps(
        {
            "assistant_message": thought.content,
            "reasoning": "synthetic-original-" + "r" * 1600,
        }
    )
    call = _event(
        2,
        event_type=EventType.TOOL_CALL,
        run="synthetic-complete-run",
    )
    call.tool_name = "inspect"
    call.tool_args = {"key": "value"}
    call.call_id = "synthetic-call"
    call.parent_event_id = thought.event_id
    call.raw_content = json.dumps({"protocol": "c" * 1600})
    result = _event(
        3,
        content="visible result",
        event_type=EventType.TOOL_RESULT,
        run="synthetic-complete-run",
    )
    result.tool_name = "inspect"
    result.tool_success = True
    result.call_id = call.call_id
    result.parent_event_id = call.event_id
    result.raw_content = json.dumps({"protocol": "t" * 1600})
    return [thought, call, result]


def _crossing_consumed_group() -> list[LifeEngineEvent]:
    return [
        _event(1, run="crossing-run", consumed=True),
        *[_event(sequence, consumed=True) for sequence in range(2, 7)],
        _event(7),
        _event(100, run="crossing-run", consumed=True),
    ]


def _raw_ids(events: list[LifeEngineEvent]) -> list[str]:
    return [event.event_id for event in events if event.event_type != EventType.SUMMARY]


def test_visible_budget_admits_whole_group_when_legacy_render_does_not() -> None:
    manager = SubconsciousContextManager(max_chars=120, recent_group_count=0)
    events = _large_raw_group()
    originals = deepcopy(events)
    (group,) = manager.group_events(events)
    expected = format_new_events_text(events)
    assert group.closed
    assert len(manager._render_group(group)) > manager.max_chars
    assert len(expected) < manager.max_chars

    legacy = manager.prepare(events, cursor=0)
    assert legacy.acknowledged_event_ids == []
    visible = manager.prepare(events, cursor=0, event_renderer=format_visible_event)

    assert visible.content == expected
    assert visible.selected_event_ids == _raw_ids(events)
    assert visible.acknowledged_event_ids == _raw_ids(events)
    assert visible.target_reached is True
    assert visible.after_chars == len(expected)
    assert events == originals


@pytest.mark.parametrize("budget", [0, 1, 20])
def test_oversized_visible_group_is_not_partially_selected_or_acknowledged(
    budget,
) -> None:
    events = [
        _event(1, content="short", run="atomic"),
        _event(2, content="large-" + "x" * 200, run="atomic"),
    ]
    manager = SubconsciousContextManager(max_chars=budget, recent_group_count=0)

    prepared = manager.prepare(events, cursor=0, event_renderer=format_visible_event)

    assert prepared.content == ""
    assert prepared.selected_event_ids == []
    assert prepared.acknowledged_event_ids == []
    assert prepared.target_reached is False
    assert all(not event.heartbeat_context_consumed for event in events)


@pytest.mark.parametrize("blank", ["", " ", "\n\t"])
def test_blank_visible_member_keeps_its_entire_closed_group_pending(blank) -> None:
    events = [
        _event(1, content="present", run="with-blank"),
        _event(2, content=blank, run="with-blank"),
    ]
    manager = SubconsciousContextManager(max_chars=100)

    prepared = manager.prepare(
        events, cursor=0, event_renderer=lambda event: event.content
    )

    assert prepared.content == ""
    assert prepared.selected_event_ids == []
    assert prepared.acknowledged_event_ids == []
    assert prepared.target_reached is False


def test_open_tool_group_never_receives_acknowledgement() -> None:
    call = _event(1, event_type=EventType.TOOL_CALL)
    call.tool_name = "synthetic-open-tool"
    call.call_id = "unanswered-call"
    manager = SubconsciousContextManager(max_chars=100)
    assert manager.group_events([call])[0].closed is False

    prepared = manager.prepare([call], cursor=0, event_renderer=format_visible_event)

    assert prepared.acknowledged_event_ids == []
    assert prepared.target_reached is False
    assert not call.heartbeat_context_consumed


def test_cross_cursor_group_selects_old_members_but_only_acks_new_delta() -> None:
    events = [
        _event(1, run="cross-cursor", consumed=True),
        _event(2, run="cross-cursor"),
    ]
    prepared = SubconsciousContextManager(max_chars=100).prepare(
        events,
        cursor=1,
        event_renderer=format_visible_event,
    )

    assert prepared.selected_event_ids == _raw_ids(events)
    assert prepared.acknowledged_event_ids == [events[1].event_id]
    assert prepared.content == format_new_events_text(events)
    assert prepared.target_reached is True


def test_already_consumed_group_member_is_not_acknowledged_again() -> None:
    events = [
        _event(1, run="mixed-consumption", consumed=True),
        _event(2, run="mixed-consumption"),
    ]
    prepared = SubconsciousContextManager(max_chars=100).prepare(
        events,
        cursor=0,
        event_renderer=format_visible_event,
    )

    assert prepared.selected_event_ids == _raw_ids(events)
    assert prepared.acknowledged_event_ids == [events[1].event_id]


def test_interleaved_groups_use_global_sequence_order_and_exact_separator_budget() -> (
    None
):
    events = [
        _event(1, content="a", run="first-group"),
        _event(2, content="b", run="second-group"),
        _event(3, content="c", run="first-group"),
        _event(4, content="d", run="second-group"),
    ]
    manager = SubconsciousContextManager(max_chars=7)

    prepared = manager.prepare(
        list(reversed(events)),
        cursor=0,
        event_renderer=format_visible_event,
    )

    assert prepared.content == "a\nb\nc\nd"
    assert prepared.after_chars == 7
    assert prepared.selected_event_ids == _raw_ids(events)
    assert prepared.acknowledged_event_ids == _raw_ids(events)
    assert prepared.target_reached is True


def test_visible_budget_counts_unicode_characters_not_utf8_bytes() -> None:
    events = [_event(1, content="\u5fc3"), _event(2, content="\u8df3")]
    manager = SubconsciousContextManager(max_chars=3)

    prepared = manager.prepare(events, cursor=0, event_renderer=format_visible_event)

    assert prepared.content == "\u5fc3\n\u8df3"
    assert prepared.after_chars == len(prepared.content) == manager.max_chars
    assert len(prepared.content.encode("utf-8")) > manager.max_chars
    assert prepared.selected_event_ids == _raw_ids(events)
    assert prepared.acknowledged_event_ids == _raw_ids(events)
    assert prepared.target_reached is True


def test_equal_sequence_output_has_stable_event_id_tie_breaking() -> None:
    left = _event(1, content="left")
    right = _event(1, content="right")
    left.event_id = "a-visible-event"
    right.event_id = "z-visible-event"

    prepared = SubconsciousContextManager(max_chars=100).prepare(
        [right, left],
        cursor=0,
        event_renderer=format_visible_event,
    )

    assert prepared.content == "left\nright"
    assert prepared.selected_event_ids == [left.event_id, right.event_id]
    assert prepared.acknowledged_event_ids == [left.event_id, right.event_id]


def test_selection_uses_sequence_not_legacy_semantic_priority(monkeypatch) -> None:
    first = _event(1, content="first")
    later = _event(2, content="later", event_type=EventType.MESSAGE)
    later.content_type = "direct_message"
    manager = SubconsciousContextManager(max_chars=5)

    def forbidden_priority(*_args, **_kwargs):
        raise AssertionError("visible preparation must not rank semantic value")

    monkeypatch.setattr(manager, "_group_priority", forbidden_priority)
    prepared = manager.prepare(
        [later, first],
        cursor=0,
        event_renderer=lambda event: event.content,
    )

    assert prepared.content == "first"
    assert prepared.selected_event_ids == [first.event_id]
    assert prepared.acknowledged_event_ids == [first.event_id]
    assert prepared.target_reached is False


def test_renderer_is_cached_once_per_snapshot_event_and_not_stored_on_manager() -> None:
    manager = SubconsciousContextManager(max_chars=100)
    events = [_event(1, run="pair"), _event(2, run="pair"), _event(3)]
    original_manager_state = deepcopy(vars(manager))
    calls = Counter()

    def first_renderer(event):
        calls[event.event_id] += 1
        assert calls[event.event_id] == 1
        return f"first-{event.sequence}"

    first = manager.prepare(events, cursor=0, event_renderer=first_renderer)
    second_calls = Counter()

    def second_renderer(event):
        second_calls[event.event_id] += 1
        return f"second-{event.sequence}"

    second = manager.prepare(events, cursor=0, event_renderer=second_renderer)

    assert calls == Counter({event.event_id: 1 for event in events})
    assert second_calls == calls
    assert first.content == "first-1\nfirst-2\nfirst-3"
    assert second.content == "second-1\nsecond-2\nsecond-3"
    assert vars(manager) == original_manager_state


def test_renderer_reentry_does_not_replace_outer_call_local_projection() -> None:
    manager = SubconsciousContextManager(max_chars=100)
    nested_results = []

    def renderer(event):
        if event.sequence == 1:
            nested_results.append(
                manager.prepare(
                    [_event(100)],
                    cursor=0,
                    event_renderer=lambda _event: "nested-only",
                )
            )
        return f"outer-{event.sequence}"

    prepared = manager.prepare(
        [_event(1), _event(2)], cursor=0, event_renderer=renderer
    )

    assert nested_results[0].content == "nested-only"
    assert prepared.content == "outer-1\nouter-2"


def test_default_prepare_is_unchanged_after_visible_renderer_call() -> None:
    manager = SubconsciousContextManager(max_chars=120)
    events = _large_raw_group()
    before = manager.prepare(events, cursor=0)

    manager.prepare(events, cursor=0, event_renderer=format_visible_event)
    after = manager.prepare(events, cursor=0)
    explicit_default = manager.prepare(events, cursor=0, event_renderer=None)

    assert after == before
    assert explicit_default == before


def test_visible_branch_does_not_reinject_old_summary_or_recent_protocol_text() -> None:
    old = _event(1, content="synthetic-old-recent", consumed=True)
    current = _event(2, content="only-new-visible")
    summary = SubconsciousSummary(
        entries=[
            SummaryEntry(kind="synthetic", text="synthetic-old-summary"),
        ]
    )

    prepared = SubconsciousContextManager(max_chars=1000, recent_group_count=5).prepare(
        [old, current],
        cursor=1,
        existing_summary=summary,
        event_renderer=format_visible_event,
    )

    assert prepared.content == current.content
    assert prepared.selected_event_ids == [current.event_id]
    assert prepared.acknowledged_event_ids == [current.event_id]
    assert "synthetic-old-summary" not in prepared.content
    assert "synthetic-old-recent" not in prepared.content


@pytest.mark.parametrize("invalid", [None, 7, {}, b"not-text"])
def test_non_string_renderer_result_is_rejected_without_exposing_body(invalid) -> None:
    event = _event(1, content="synthetic-private-body")
    with pytest.raises(TypeError) as error:
        SubconsciousContextManager().prepare(
            [event],
            cursor=0,
            event_renderer=lambda _event: invalid,
        )
    assert event.content not in str(error.value)


@pytest.mark.parametrize("invalid_identity", ["duplicate", "", "   "])
def test_ambiguous_snapshot_identity_is_rejected_before_false_ack(
    invalid_identity,
) -> None:
    first = _event(1, content="synthetic-private-first")
    second = _event(2, content="synthetic-private-second")
    second.event_id = (
        first.event_id if invalid_identity == "duplicate" else invalid_identity
    )

    with pytest.raises(ValueError) as error:
        SubconsciousContextManager().prepare(
            [first, second],
            cursor=0,
            event_renderer=format_visible_event,
        )

    assert first.content not in str(error.value)
    assert second.content not in str(error.value)
    assert not first.heartbeat_context_consumed
    assert not second.heartbeat_context_consumed


def test_visible_prepare_does_not_fold_crossing_consumed_group_over_pending_gap() -> (
    None
):
    events = _crossing_consumed_group()
    prepared = SubconsciousContextManager(
        max_chars=100,
        recent_group_count=5,
    ).prepare(events, cursor=0, event_renderer=format_visible_event)

    assert prepared.selected_event_ids == ["visible-event-7"]
    assert prepared.acknowledged_event_ids == ["visible-event-7"]
    assert prepared.content == "visible-7"
    assert prepared.updated_summary.covered_through_sequence < 7
    assert not next(
        event for event in events if event.sequence == 7
    ).heartbeat_context_consumed


@asynccontextmanager
async def _isolated_gate(_reason):
    yield


@pytest.fixture
def service(tmp_path, monkeypatch):
    """Use real prepare/commit methods with all external boundaries stubbed."""
    service = LifeEngineService.__new__(LifeEngineService)
    config = SimpleNamespace(
        settings=SimpleNamespace(workspace_path=str(tmp_path), log_heartbeat=False),
        model=SimpleNamespace(task_name="synthetic-visible-heartbeat"),
    )
    service._cfg = lambda: config
    service._state = LifeEngineState()
    service._lock = asyncio.Lock()
    service._event_history = _large_raw_group()
    service._pending_events = []
    service._state_dirty = False
    service._multi_writer_bridge = None
    service._opportunity_runtime = None
    service._subconscious_context = SubconsciousContextManager(
        max_chars=120,
        recent_group_count=0,
    )
    service._proactive_actor_gate = SimpleNamespace(hold=_isolated_gate)
    service._consciousness_registry = SimpleNamespace(reconcile_expired=Mock())
    service.save_consciousness_registry_async = AsyncMock()
    service.catch_up_subconscious_ingest = AsyncMock()
    service.drain_pending_events = AsyncMock(return_value=[])
    service._expression_unseen_note = Mock(return_value="")
    service._publish_raw_events = AsyncMock()
    service._record_model_reply = AsyncMock()

    async def save(**_kwargs):
        service._state_dirty = False

    service._save_runtime_context = AsyncMock(side_effect=save)
    monkeypatch.setattr(service_module, "logger", Mock())
    monkeypatch.setattr(service_module, "log_wake_context_injected", Mock())
    return service


def _delivery(prepared) -> EffectiveContextReceipt:
    return EffectiveContextReceipt(
        delivery_id=prepared.delivery_id,
        exact_present=True,
        expected_utf8_bytes=prepared.delivery_bytes,
        expected_sha256=prepared.delivery_sha256,
        effective_utf8_bytes=prepared.delivery_bytes,
        effective_sha256=prepared.delivery_sha256,
    )


async def _commit(service, prepared):
    return await service._commit_heartbeat_context(
        prepared,
        "",
        "synthetic-visible-run",
        None,
        _delivery(prepared),
    )


async def test_real_prepare_and_exact_commit_stop_next_round_replay(service) -> None:
    original_events = list(service._event_history)
    originals = [(event.raw_content, event.content_ref) for event in original_events]
    expected = format_new_events_text(original_events)
    prepared = await service._prepare_heartbeat_context()

    assert prepared.acknowledged_event_ids == _raw_ids(original_events)
    assert prepared.content == (
        f"{prepared.delivery_marker}\n{expected}\n</subconscious_activity_projection>"
    )
    assert prepared.delivery_bytes == len(prepared.content.encode("utf-8"))
    assert (
        prepared.delivery_sha256
        == hashlib.sha256(prepared.content.encode("utf-8")).hexdigest()
    )
    assert prepared.consumption_receipt is None

    receipt = await _commit(service, prepared)

    assert receipt.event_ids == tuple(_raw_ids(original_events))
    assert receipt.cursor_before == 0
    assert receipt.cursor_after == 3
    assert all(event.heartbeat_context_consumed for event in original_events)
    assert [
        (event.raw_content, event.content_ref) for event in original_events
    ] == originals
    service._save_runtime_context.assert_awaited_once_with(
        recoverable_on_shared_conflict=False,
    )

    following = await service._prepare_heartbeat_context()
    assert following.content == ""
    assert following.selected_event_ids == []
    assert following.acknowledged_event_ids == []


@pytest.mark.parametrize(
    "failure", ["missing", "nonexact", "wrong_hash", "wrong_bytes", "wrong_id"]
)
async def test_real_visible_projection_requires_exact_delivery_before_consumption(
    service,
    failure,
) -> None:
    original = list(service._event_history)
    prepared = await service._prepare_heartbeat_context()
    delivery = _delivery(prepared)
    if failure == "missing":
        delivery = None
    elif failure == "nonexact":
        delivery = replace(delivery, exact_present=False)
    elif failure == "wrong_hash":
        delivery = replace(delivery, effective_sha256="synthetic-wrong-hash")
    elif failure == "wrong_bytes":
        delivery = replace(delivery, effective_utf8_bytes=prepared.delivery_bytes + 1)
    else:
        delivery = replace(delivery, delivery_id="synthetic-wrong-id")

    with pytest.raises(PerceptionDeliveryUnverified):
        await service._commit_heartbeat_context(
            prepared,
            "",
            "synthetic-rejected-run",
            None,
            delivery,
        )

    assert prepared.consumption_receipt is None
    assert service._state.heartbeat_context_cursor == 0
    assert all(not event.heartbeat_context_consumed for event in original)
    service._save_runtime_context.assert_not_awaited()
    retry = await service._prepare_heartbeat_context()
    assert retry.selected_event_ids == prepared.selected_event_ids
    assert retry.delivery_sha256 == prepared.delivery_sha256


@pytest.mark.parametrize("failure", ["exception", "dirty_return", "cancelled"])
async def test_visible_commit_failure_rolls_back_and_preserves_arrivals_for_retry(
    service,
    failure,
) -> None:
    original = list(service._event_history)
    prepared = await service._prepare_heartbeat_context()
    arrived_history = _event(4, content="arrived-history")
    arrived_pending = _event(5, content="arrived-pending")

    async def rejected_save(**kwargs):
        assert kwargs == {"recoverable_on_shared_conflict": False}
        assert prepared.consumption_receipt is None
        assert service._state.heartbeat_context_cursor == 3
        service._event_history.append(arrived_history)
        service._pending_events.append(arrived_pending)
        if failure == "cancelled":
            raise asyncio.CancelledError("synthetic visible commit cancellation")
        if failure == "dirty_return":
            service._state_dirty = True
            return
        raise OSError("synthetic visible persistence failure")

    service._save_runtime_context.side_effect = rejected_save
    expected_error = asyncio.CancelledError if failure == "cancelled" else Exception
    with pytest.raises(expected_error):
        await _commit(service, prepared)

    assert prepared.consumption_receipt is None
    assert service._state.heartbeat_context_cursor == 0
    assert service._state.subconscious_summary == {}
    assert service._state_dirty
    assert service._pending_events == [arrived_pending]
    assert _raw_ids(service._event_history) == [
        *_raw_ids(original),
        arrived_history.event_id,
    ]
    assert all(
        not event.heartbeat_context_consumed
        for event in [*original, arrived_history, arrived_pending]
    )

    retry = await service._prepare_heartbeat_context()
    assert set(retry.selected_event_ids) == {
        *_raw_ids(original),
        arrived_history.event_id,
    }

    async def accepted_save(**_kwargs):
        service._state_dirty = False

    service._save_runtime_context.side_effect = accepted_save
    receipt = await _commit(service, retry)
    assert set(receipt.event_ids) == {*_raw_ids(original), arrived_history.event_id}
    assert receipt.cursor_after == 4
    assert not arrived_pending.heartbeat_context_consumed


async def test_oversized_early_group_stays_pending_when_later_group_commits(
    service,
) -> None:
    oversized = _event(1, content="x" * 500)
    later = _event(2, content="later-small")
    service._event_history = [oversized, later]
    prepared = await service._prepare_heartbeat_context()

    assert prepared.selected_event_ids == [later.event_id]
    assert prepared.acknowledged_event_ids == [later.event_id]
    assert prepared.target_reached is False
    receipt = await _commit(service, prepared)

    assert receipt.event_ids == (later.event_id,)
    assert receipt.cursor_after == 0
    assert service._state.heartbeat_context_cursor == 0
    assert not oversized.heartbeat_context_consumed
    assert later.heartbeat_context_consumed
    assert oversized.event_id in _raw_ids(service._event_history)
    following = await service._prepare_heartbeat_context()
    assert following.selected_event_ids == []
    assert following.acknowledged_event_ids == []
    assert following.target_reached is False
