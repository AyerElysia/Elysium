"""A summary projection must not move the confirmed frontier across a raw gap."""

from __future__ import annotations

from plugins.life_engine.service.event_builder import EventType, LifeEngineEvent
from plugins.life_engine.service.heartbeat_rolling import (
    format_new_events_text,
    format_visible_event,
)
from plugins.life_engine.service.subconscious_context import (
    SubconsciousContextManager,
    SubconsciousSummary,
    SummaryEntry,
)


def _event(sequence, *, run=None, consumed=False, content=None, **kwargs):
    return LifeEngineEvent(
        event_id=f"frontier-event-{sequence}",
        event_type=kwargs.pop("event_type", EventType.HEARTBEAT),
        timestamp="",
        sequence=sequence,
        source="synthetic-frontier-test",
        source_detail="synthetic only",
        content=f"synthetic visible {sequence}" if content is None else content,
        raw_content=f"synthetic exact raw {sequence}",
        heartbeat_run_id=run,
        heartbeat_context_consumed=consumed,
        **kwargs,
    )


def _interleaved_events():
    return [
        _event(1, run="crossing-confirmed-group", consumed=True),
        *[_event(sequence, consumed=True) for sequence in range(2, 7)],
        _event(7),
        _event(100, run="crossing-confirmed-group", consumed=True),
    ]


def _legacy_summary():
    return SubconsciousSummary(
        covered_from_sequence=1,
        covered_through_sequence=100,
        entries=[
            SummaryEntry(
                kind="direct_message",
                text="synthetic existing note must remain unchanged",
                event_ids=["frontier-event-1", "frontier-event-100"],
                sequences=[1, 100],
                source="synthetic-frontier-test",
            )
        ],
        stats={"total_events": 2},
    )


def _raw_ids(history):
    return {
        event.event_id for event in history if event.event_type != EventType.SUMMARY
    }


def _summary_high_water(history):
    return max(
        (
            SubconsciousSummary.from_json(event.content).covered_through_sequence
            for event in history
            if event.event_type == EventType.SUMMARY
        ),
        default=0,
    )


def test_crossing_consumed_group_cannot_absorb_past_unconsumed_sequence():
    events = _interleaved_events()
    manager = SubconsciousContextManager()

    retained = manager.compact_history(events, cursor=0)

    assert "frontier-event-7" in _raw_ids(retained)
    assert {"frontier-event-1", "frontier-event-100"} <= _raw_ids(retained)
    assert _summary_high_water(retained) < 7
    # Default/legacy preparation must not manufacture a covering summary first.
    prepared = manager.prepare(events, cursor=0)
    assert prepared.selected_event_ids == ["frontier-event-7"]
    assert prepared.acknowledged_event_ids == ["frontier-event-7"]
    assert prepared.updated_summary.covered_through_sequence < 7
    assert prepared.snapshot_high_water == 100


def test_existing_summary_above_cursor_does_not_remove_raw_pending_gap():
    events = _interleaved_events()
    summary = _legacy_summary()
    before_summary = summary.to_dict()
    manager = SubconsciousContextManager()

    retained = manager.compact_history(events, cursor=0, existing_summary=summary)

    assert "frontier-event-7" in _raw_ids(retained)
    assert {"frontier-event-1", "frontier-event-100"} <= _raw_ids(retained)
    assert summary.to_dict() == before_summary
    retained_summary = next(
        SubconsciousSummary.from_json(event.content)
        for event in retained
        if event.event_type == EventType.SUMMARY
    )
    assert retained_summary.to_dict() == before_summary
    # Repeating projection maintenance must not silently lose the same raw gap.
    again = manager.compact_history(retained, cursor=0, existing_summary=summary)
    assert "frontier-event-7" in _raw_ids(again)


def test_visible_delta_uses_real_cursor_not_existing_summary_high_water():
    events = _interleaved_events()
    summary = _legacy_summary()
    manager = SubconsciousContextManager()
    before_raw = [
        (
            event.event_id,
            event.content,
            event.raw_content,
            event.heartbeat_context_consumed,
        )
        for event in events
    ]

    prepared = manager.prepare(
        events, cursor=0, existing_summary=summary, event_renderer=format_visible_event
    )

    assert prepared.selected_event_ids == ["frontier-event-7"]
    assert prepared.acknowledged_event_ids == ["frontier-event-7"]
    assert prepared.content == format_visible_event(events[6])
    assert prepared.target_reached is True
    assert prepared.updated_summary.to_dict() == summary.to_dict()
    assert [
        (
            event.event_id,
            event.content,
            event.raw_content,
            event.heartbeat_context_consumed,
        )
        for event in events
    ] == before_raw


def test_existing_summary_filter_preserves_complete_causal_group_across_cursor():
    call = _event(
        1,
        event_type=EventType.TOOL_CALL,
        consumed=True,
        tool_name="synthetic_inspect",
        tool_args={"query": "synthetic query"},
        call_id="synthetic-frontier-call",
    )
    result = _event(
        7,
        event_type=EventType.TOOL_RESULT,
        tool_name="synthetic_inspect",
        tool_success=True,
        call_id="synthetic-frontier-call",
        parent_event_id=call.event_id,
    )
    manager = SubconsciousContextManager()
    summary = _legacy_summary()

    retained = manager.compact_history(
        [call, result], cursor=1, existing_summary=summary
    )
    assert {call.event_id, result.event_id} <= _raw_ids(retained)
    prepared = manager.prepare(
        retained,
        cursor=1,
        existing_summary=summary,
        event_renderer=format_visible_event,
    )

    assert prepared.selected_event_ids == [call.event_id, result.event_id]
    assert prepared.acknowledged_event_ids == [result.event_id]
    assert prepared.content == format_new_events_text([call, result])
    assert call.heartbeat_context_consumed is True
    assert result.heartbeat_context_consumed is False


def test_preparation_candidates_are_not_confirmation_for_history_compaction():
    events = _interleaved_events()
    manager = SubconsciousContextManager()
    prepared = manager.prepare(events, cursor=0, event_renderer=format_visible_event)

    assert prepared.acknowledged_event_ids == ["frontier-event-7"]
    assert prepared.consumption_receipt is None
    assert events[6].heartbeat_context_consumed is False
    # A failed/cancelled run has no committed flag change; preparation alone
    # cannot let later maintenance treat this gap as confirmed.
    retained = manager.compact_history(events, cursor=0)
    assert "frontier-event-7" in _raw_ids(retained)
    assert _summary_high_water(retained) < 7

    events[6].heartbeat_context_consumed = True
    confirmed = manager.compact_history(events, cursor=100)
    assert _summary_high_water(confirmed) == 100
    assert all(
        event.heartbeat_context_consumed
        for event in confirmed
        if event.event_type != EventType.SUMMARY
    )


def test_safe_confirmed_prefix_still_compacts_before_first_pending_event():
    events = [
        *[_event(sequence, consumed=True) for sequence in range(1, 8)],
        _event(8),
        _event(100, consumed=True),
    ]
    manager = SubconsciousContextManager(recent_group_count=0)

    retained = manager.compact_history(events, cursor=0)

    assert _summary_high_water(retained) == 7
    assert "frontier-event-8" in _raw_ids(retained)
    assert "frontier-event-100" in _raw_ids(retained)


def test_already_confirmed_cursor_remains_authoritative_over_legacy_flags():
    events = [
        _event(1, consumed=False),
        _event(2, consumed=True),
        _event(3, consumed=True),
        _event(4, consumed=False),
    ]
    manager = SubconsciousContextManager(recent_group_count=0)

    retained = manager.compact_history(events, cursor=1)

    assert _summary_high_water(retained) == 3
    assert _raw_ids(retained) == {"frontier-event-4"}


def test_empty_visible_pending_event_still_blocks_the_absorption_frontier():
    events = _interleaved_events()
    events[6].content = ""
    events[6].raw_content = ""
    manager = SubconsciousContextManager()
    summary = _legacy_summary()

    prepared = manager.prepare(
        events, cursor=0, existing_summary=summary, event_renderer=format_visible_event
    )
    retained = manager.compact_history(events, cursor=0, existing_summary=summary)

    assert prepared.content == ""
    assert prepared.acknowledged_event_ids == []
    assert prepared.target_reached is False
    assert "frontier-event-7" in _raw_ids(retained)
    assert events[6].heartbeat_context_consumed is False
