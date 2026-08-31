"""Deterministic subconscious context domain tests."""

from __future__ import annotations

import json

from plugins.life_engine.service.event_builder import (
    EventBuilder,
    EventType,
    LifeEngineEvent,
)
from plugins.life_engine.service.subconscious_context import (
    SubconsciousContextManager,
    SubconsciousSummary,
    SummaryEntry,
)
from src.core.models.message import Message


def _event(
    sequence: int,
    *,
    event_type: EventType = EventType.MESSAGE,
    content: str | None = None,
    content_type: str = "text",
    tool_name: str | None = None,
    tool_success: bool | None = None,
    heartbeat_run_id: str | None = None,
    call_id: str | None = None,
    parent_event_id: str | None = None,
    causation_id: str | None = None,
) -> LifeEngineEvent:
    return LifeEngineEvent(
        event_id=f"event-{sequence}",
        event_type=event_type,
        timestamp=f"2026-07-19T00:00:{sequence:02d}+00:00",
        sequence=sequence,
        source="life_engine" if event_type != EventType.MESSAGE else "qq",
        source_detail="test source",
        content=content if content is not None else f"content-{sequence}",
        content_type=content_type,
        sender="Ayer" if event_type == EventType.MESSAGE else None,
        chat_type="private" if event_type == EventType.MESSAGE else None,
        heartbeat_run_id=heartbeat_run_id,
        call_id=call_id,
        parent_event_id=parent_event_id,
        causation_id=causation_id,
        tool_name=tool_name,
        tool_args={} if event_type == EventType.TOOL_CALL else None,
        tool_success=tool_success,
    )


def _summary_from_history(events: list[LifeEngineEvent]) -> SubconsciousSummary:
    summary_events = [event for event in events if event.event_type == EventType.SUMMARY]
    assert len(summary_events) == 1
    return SubconsciousSummary.from_json(summary_events[0].content)


def test_builder_uses_one_sequence_per_event_and_links_tool_result() -> None:
    current = 0

    def next_sequence() -> int:
        nonlocal current
        current += 1
        return current

    builder = EventBuilder(next_sequence)
    message_event = builder.build_message_event(
        Message(
            message_id="",
            content="hello",
            processed_plain_text="hello",
            sender_id="u1",
            sender_name="Ayer",
            platform="qq",
            chat_type="private",
            stream_id="stream-a",
        )
    )
    heartbeat_event = builder.build_heartbeat_event(
        "thinking",
        1,
        "life",
        heartbeat_run_id="heartbeat-1",
    )
    call_event = builder.build_tool_call_event(
        "inspect",
        {"path": "a.txt"},
        heartbeat_run_id="heartbeat-1",
        call_id="call-1",
        parent_event_id=heartbeat_event.event_id,
    )
    result_event = builder.build_tool_result_event(
        "inspect",
        "ok",
        True,
        call_event=call_event,
    )
    agent_event = builder.build_agent_result_event(
        "research",
        "done",
        heartbeat_run_id="heartbeat-1",
        causation_id=result_event.event_id,
    )

    assert [
        message_event.sequence,
        heartbeat_event.sequence,
        call_event.sequence,
        result_event.sequence,
        agent_event.sequence,
    ] == [1, 2, 3, 4, 5]
    assert current == 5
    assert message_event.event_id == "msg_1"
    assert result_event.call_id == "call-1"
    assert result_event.parent_event_id == call_event.event_id
    assert result_event.causation_id == call_event.event_id
    assert result_event.heartbeat_run_id == "heartbeat-1"


def test_summary_json_round_trip_preserves_provenance() -> None:
    summary = SubconsciousSummary(
        covered_from_sequence=2,
        covered_through_sequence=9,
        entries=[
            SummaryEntry(
                kind="direct_message",
                text="记住这件事",
                event_ids=["event-2", "event-9"],
                sequences=[2, 9],
                source="qq",
            )
        ],
        stats={"heartbeat_count": 3, "total_events": 8},
    )

    restored = SubconsciousSummary.from_json(summary.to_json())

    assert restored == summary
    assert restored.entries[0].event_id == "event-9"
    assert restored.entries[0].sequence == 9


def test_groups_heartbeat_run_explicit_tools_legacy_pair_and_open_call() -> None:
    manager = SubconsciousContextManager()
    events = [
        _event(8, event_type=EventType.TOOL_CALL, tool_name="open"),
        _event(
            6,
            event_type=EventType.TOOL_RESULT,
            tool_name="legacy",
            tool_success=True,
        ),
        _event(
            3,
            event_type=EventType.TOOL_RESULT,
            tool_name="inspect",
            tool_success=False,
            call_id="call-1",
            parent_event_id="event-2",
        ),
        _event(5, event_type=EventType.TOOL_CALL, tool_name="legacy"),
        _event(
            1,
            event_type=EventType.HEARTBEAT,
            heartbeat_run_id="run-1",
        ),
        _event(
            4,
            event_type=EventType.HEARTBEAT,
            heartbeat_run_id="run-1",
        ),
        _event(
            2,
            event_type=EventType.TOOL_CALL,
            tool_name="inspect",
            heartbeat_run_id="run-1",
            call_id="call-1",
            parent_event_id="event-1",
        ),
    ]

    groups = manager.group_events(events)

    assert [event.sequence for event in groups[0].events] == [1, 2, 3, 4]
    assert groups[0].heartbeat_run_id == "run-1"
    assert groups[0].closed is True
    assert [event.sequence for event in groups[1].events] == [5, 6]
    assert groups[1].closed is True
    assert [event.sequence for event in groups[2].events] == [8]
    assert groups[2].closed is False
    assert groups[2].protected is True


def test_prepare_keeps_causal_group_whole_when_result_is_new_delta() -> None:
    manager = SubconsciousContextManager(max_chars=1000, recent_group_count=0)
    events = [
        _event(
            1,
            event_type=EventType.TOOL_CALL,
            tool_name="inspect",
            call_id="call-1",
        ),
        _event(
            2,
            event_type=EventType.TOOL_RESULT,
            tool_name="inspect",
            tool_success=True,
            call_id="call-1",
            parent_event_id="event-1",
        ),
    ]

    prepared = manager.prepare(events, cursor=1)

    assert prepared.snapshot_high_water == 2
    assert prepared.selected_event_ids == ["event-1", "event-2"]
    assert "#1 TOOL_CALL" in prepared.content
    assert "#2 TOOL_RESULT" in prepared.content
    assert prepared.target_reached is True


def test_prepare_with_no_delta_is_empty_and_does_not_replay_summary() -> None:
    manager = SubconsciousContextManager(max_chars=500)
    summary = SubconsciousSummary(
        covered_from_sequence=1,
        covered_through_sequence=1,
        entries=[
            SummaryEntry(
                kind="direct_message",
                text="old fact",
                event_ids=["event-1"],
                sequences=[1],
            )
        ],
        stats={"total_events": 1},
    )

    prepared = manager.prepare([_event(1)], cursor=1, existing_summary=summary)

    assert prepared.content == ""
    assert prepared.before_chars == 0
    assert prepared.after_chars == 0
    assert prepared.selected_event_ids == []
    assert prepared.summary_event_ids == []
    assert prepared.snapshot_high_water == 1


def test_continuous_compaction_keeps_one_summary_old_and_new_facts_and_dedups() -> None:
    manager = SubconsciousContextManager(recent_group_count=0)
    first_history = manager.compact_history(
        [
            _event(
                1,
                content="旧事实",
                content_type="direct_message",
            ),
            _event(2, event_type=EventType.HEARTBEAT, content="ordinary heartbeat"),
            _event(
                3,
                event_type=EventType.TOOL_RESULT,
                content="success",
                tool_name="inspect",
                tool_success=True,
            ),
        ],
        cursor=3,
        keep_recent_groups=0,
    )
    first_summary = _summary_from_history(first_history)
    assert [entry.text for entry in first_summary.entries] == ["旧事实"]

    second_history = manager.compact_history(
        [
            *first_history,
            _event(
                4,
                content="  旧事实  ",
                content_type="dfc_message",
            ),
            _event(
                5,
                content="新事实",
                content_type="proactive_opportunity",
            ),
            _event(
                6,
                event_type=EventType.AGENT_RESULT,
                content="agent conclusion",
                tool_name="agent:research",
                tool_success=True,
            ),
        ],
        cursor=6,
        keep_recent_groups=0,
    )
    second_summary = _summary_from_history(second_history)

    assert len([e for e in second_history if e.event_type == EventType.SUMMARY]) == 1
    assert [entry.text for entry in second_summary.entries] == [
        "旧事实",
        "新事实",
        "agent conclusion",
    ]
    duplicate = second_summary.entries[0]
    assert duplicate.event_ids == ["event-1", "event-4"]
    assert duplicate.sequences == [1, 4]
    assert second_summary.covered_from_sequence == 1
    assert second_summary.covered_through_sequence == 6
    assert second_summary.stats["heartbeat_count"] == 1
    assert second_summary.stats["tool_success_count"] == 1


def test_compact_history_sorts_and_keeps_recent_open_and_unconfirmed_groups() -> None:
    manager = SubconsciousContextManager(recent_group_count=1)
    history = manager.compact_history(
        [
            _event(4, content="unconfirmed", content_type="direct_message"),
            _event(3, event_type=EventType.TOOL_CALL, tool_name="open"),
            _event(1, content="fold me", content_type="direct_message"),
            _event(2, event_type=EventType.HEARTBEAT),
        ],
        cursor=3,
    )

    summary = _summary_from_history(history)
    raw_events = [event for event in history if event.event_type != EventType.SUMMARY]
    assert [event.sequence for event in raw_events] == [2, 3, 4]
    assert [entry.text for entry in summary.entries] == ["fold me"]
    groups = manager.group_events(raw_events)
    open_group = next(group for group in groups if group.from_sequence == 3)
    assert open_group.protected is True


def test_prepare_strict_budget_uses_high_value_truncation() -> None:
    manager = SubconsciousContextManager(max_chars=120, recent_group_count=0)
    prepared = manager.prepare(
        [
            _event(
                1,
                content="CRITICAL-" + "x" * 2000,
                content_type="direct_message",
            )
        ],
        cursor=0,
    )

    assert len(prepared.content) <= 120
    assert prepared.after_chars == len(prepared.content)
    assert prepared.before_chars > prepared.after_chars
    assert prepared.target_reached is False
    assert "CRITICAL" in prepared.content
    assert prepared.selected_event_ids == ["event-1"]
    assert prepared.acknowledged_event_ids == []


def test_consumed_events_are_not_replayed_and_do_not_advance_delta() -> None:
    manager = SubconsciousContextManager(max_chars=500)
    consumed = _event(1, content="already handled")
    consumed.heartbeat_context_consumed = True

    prepared = manager.prepare([consumed], cursor=0)

    assert prepared.content == ""
    assert prepared.selected_event_ids == []
    assert prepared.acknowledged_event_ids == []


def test_acknowledges_only_complete_delta_groups() -> None:
    manager = SubconsciousContextManager(max_chars=500, recent_group_count=0)
    call = _event(
        1,
        event_type=EventType.TOOL_CALL,
        tool_name="inspect",
        call_id="call-1",
    )
    result = _event(
        2,
        event_type=EventType.TOOL_RESULT,
        tool_name="inspect",
        tool_success=True,
        call_id="call-1",
        parent_event_id="event-1",
    )

    prepared = manager.prepare([call, result], cursor=0)

    assert prepared.selected_event_ids == ["event-1", "event-2"]
    assert prepared.acknowledged_event_ids == ["event-1", "event-2"]


def test_recent_projection_shares_thoughts_and_tools_without_private_messages() -> None:
    manager = SubconsciousContextManager(recent_group_count=2)
    private_message = _event(1, content="PRIVATE_TRANSCRIPT")
    thought = _event(
        2,
        event_type=EventType.HEARTBEAT,
        content="我刚刚想到要继续看看这件事",
        heartbeat_run_id="run-1",
    )
    call = _event(
        3,
        event_type=EventType.TOOL_CALL,
        tool_name="inspect",
        heartbeat_run_id="run-1",
        call_id="call-1",
        parent_event_id="event-2",
    )
    call.tool_args = {"path": "notes.txt"}
    result = _event(
        4,
        event_type=EventType.TOOL_RESULT,
        content="看完了",
        tool_name="inspect",
        tool_success=True,
        heartbeat_run_id="run-1",
        call_id="call-1",
        parent_event_id="event-3",
    )
    later_thought = _event(
        5,
        event_type=EventType.HEARTBEAT,
        content="现在更清楚了",
        content_type="chatter_inner_monologue",
    )
    later_thought.source = "life_chatter"
    later_thought.source_instance_id = "chat_global"

    projected = manager.project_recent(
        [private_message, thought, call, result, later_thought]
    )

    assert projected.group_count == 2
    assert projected.source_group_count == 2
    assert projected.omitted_group_count == 0
    assert projected.event_ids == ("event-2", "event-3", "event-4", "event-5")
    assert projected.from_sequence == 2
    assert projected.through_sequence == 5
    assert "我刚刚想到要继续看看这件事" in projected.content
    assert "TOOL_CALL inspect" in projected.content
    assert "notes.txt" in projected.content
    assert "TOOL_RESULT inspect success" in projected.content
    assert "现在更清楚了" in projected.content
    assert "source=life_chatter instance=chat_global" in projected.content
    assert "PRIVATE_TRANSCRIPT" not in projected.content
    assert projected.delivered_bytes == len(projected.content.encode("utf-8"))

    witness_view = manager.project_recent(
        [private_message, thought, call, result, later_thought],
        include_tool_payloads=False,
    )
    assert "notes.txt" not in witness_view.content
    assert "看完了" not in witness_view.content
    assert '"argument_keys":["path"]' in witness_view.content
    assert "payload=redacted_for_consumer" in witness_view.content
    assert "redacted-tools-v1" in witness_view.algorithm_version


def test_conscious_expression_activity_keeps_complete_arguments_and_outcome() -> None:
    current = 0

    def next_sequence() -> int:
        nonlocal current
        current += 1
        return current

    builder = EventBuilder(next_sequence)
    arguments = {
        "mood": "温柔而认真",
        "decision": "完整接住她的分享",
        "expected_response": "她知道这段经历被认真看见",
        "thought": "先理解，再用自己的话回应，不把思考冒充成外发正文。",
        "content": "我听见了，也会把这件事认真放在心上。",
    }
    call = builder.build_conscious_tool_call_event(
        "action-life_send_text",
        arguments,
        activity_id="activity-1",
        model_turn_activity_id="model-turn-1",
        call_id="call-1",
        stream_id="stream-1",
        source_instance_id="chat-instance-1",
        turn_occurrence_id="turn-1",
    )
    result = builder.build_conscious_tool_result_event(
        "action-life_send_text",
        {"status": "delivered"},
        True,
        activity_id="activity-1",
        call_id="call-1",
        stream_id="stream-1",
        source_instance_id="chat-instance-1",
        turn_occurrence_id="turn-1",
        technical_outcome="delivered",
        delivery_receipt_sha256="a" * 64,
        delivery_message_id="provider-message-1",
        delivery_proof_status="durable",
    )

    projected = SubconsciousContextManager().prepare([call, result], cursor=0)
    raw_call = json.loads(call.raw_content or "{}")
    raw_result = json.loads(result.raw_content or "{}")

    assert raw_call["arguments"] == arguments
    assert raw_call["actor_consciousness_instance_id"] == "chat-instance-1"
    assert raw_result["result"] == {"status": "delivered"}
    assert raw_result["technical_outcome"] == "delivered"
    assert raw_result["delivery_message_id"] == "provider-message-1"
    assert set(projected.acknowledged_event_ids) == {
        "activity-1:chosen",
        "activity-1:result",
    }
    for value in arguments.values():
        assert value in projected.content
    assert "life-event-occurrence:activity-1:chosen" in projected.content


def test_oversized_activity_is_authoritative_but_prompt_uses_exact_ref() -> None:
    current = 0

    def next_sequence() -> int:
        nonlocal current
        current += 1
        return current

    builder = EventBuilder(next_sequence)
    full_thought = "爱莉在认真整理这段经历🌸" * 1200
    call = builder.build_conscious_tool_call_event(
        "action-life_send_text",
        {
            "mood": "认真",
            "decision": "回应",
            "expected_response": "被看见",
            "thought": full_thought,
            "content": "我在。",
        },
        activity_id="activity-large",
        model_turn_activity_id="model-turn-large",
        call_id="call-large",
        stream_id="stream-large",
        source_instance_id="chat-instance-large",
        turn_occurrence_id="turn-large",
    )

    projected = SubconsciousContextManager().project_recent(
        [call],
        max_bytes=4096,
    )

    assert full_thought in (call.raw_content or "")
    assert full_thought not in projected.content
    assert '"delivery":"excerpt_ref"' in projected.content
    assert '"original_bytes":' in projected.content
    assert '"sha256":' in projected.content
    assert "life-event-occurrence:activity-large:chosen" in projected.content
    assert projected.delivered_bytes <= 4096


def test_recent_projection_uses_latest_group_count_and_hard_utf8_budget() -> None:
    manager = SubconsciousContextManager(recent_group_count=1)
    projected = manager.project_recent(
        [
            _event(1, event_type=EventType.HEARTBEAT, content="旧想法"),
            _event(
                2,
                event_type=EventType.HEARTBEAT,
                content="爱莉🌸" * 1000,
            ),
        ],
        max_bytes=512,
    )

    assert projected.group_count == 1
    assert projected.source_group_count == 2
    assert projected.omitted_group_count == 1
    assert projected.event_ids == ("event-2",)
    assert "旧想法" not in projected.content
    assert "爱莉" in projected.content
    assert projected.truncated is True
    assert projected.delivered_bytes <= 512
    assert projected.delivered_bytes == len(projected.content.encode("utf-8"))
