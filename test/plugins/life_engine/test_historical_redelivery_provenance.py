"""Runtime redelivery metadata never rewrites or republishes an occurrence."""

from copy import deepcopy
from dataclasses import replace
import json

import pytest

from plugins.life_engine.service.event_builder import EventType, LifeEngineEvent
from plugins.life_engine.service.event_bus import life_event_from_legacy
from plugins.life_engine.service.heartbeat_rolling import format_visible_event
from plugins.life_engine.service.state_manager import event_from_dict, event_to_dict


def _event(**changes):
    values = dict(
        event_id="source-event", event_type=EventType.CONSCIOUS_ACTIVITY,
        timestamp="2026-09-06T14:21:03.591000+00:00", sequence=100,
        source="synthetic-actor", source_detail="synthetic-surface",
        content="Original visible words", raw_content="Original full words",
        occurrence_id="source-occurrence", source_instance_id="synthetic-instance",
        stream_id="synthetic-stream", parent_event_id="source-parent",
        content_ref="life-event-occurrence:source-occurrence",
    )
    values.update(changes)
    return LifeEngineEvent(**values)


def test_regular_event_keeps_old_serialized_shape_and_visible_body():
    event = _event()
    serialized = event_to_dict(event)
    assert "redelivery_operation_id" not in serialized
    assert "redelivery_source_position" not in serialized
    assert format_visible_event(event) == "Original visible words"
    assert event_to_dict(event_from_dict(serialized)) == serialized


def test_redelivery_survives_two_round_trips_without_source_field_changes():
    original = _event()
    original_data = event_to_dict(original)
    restored = replace(
        original, sequence=1000, redelivery_operation_id="replay6062-test",
        redelivery_source_position=262863,
    )
    expected = event_to_dict(restored)
    for _ in range(2):
        restored = event_from_dict(event_to_dict(restored))
        assert event_to_dict(restored) == expected
        assert format_visible_event(restored).startswith(
            "[历史活动重投，非新发生、非工具执行请求；原始账本位置=262863；"
            "原始时间=2026-09-06T14:21:03.591000+00:00] "
        )
        metadata = json.loads(format_visible_event(restored).split("] ", 1)[1])
        assert metadata["excerpt"] == original.content
    unchanged = deepcopy(expected)
    unchanged.pop("redelivery_operation_id")
    unchanged.pop("redelivery_source_position")
    unchanged["sequence"] = original.sequence
    assert unchanged == original_data
    assert event_to_dict(original) == original_data


def test_delivery_metadata_is_not_part_of_authoritative_source_serialization():
    original = _event()
    redelivered = replace(
        original, redelivery_operation_id="replay6062-test",
        redelivery_source_position=123,
    )
    assert life_event_from_legacy(redelivered) == life_event_from_legacy(original)


def test_blank_activity_does_not_become_acknowledgeable_due_to_label_only():
    event = _event(
        content="", raw_content="", redelivery_operation_id="replay6062-test",
        redelivery_source_position=1,
    )
    assert format_visible_event(event) == ""


@pytest.mark.parametrize("kind", [EventType.TOOL_CALL, EventType.TOOL_RESULT])
def test_historical_tools_remain_labelled_observations(kind):
    event = _event(
        event_type=kind, tool_name="synthetic-tool", tool_args={"x": "old"},
        redelivery_operation_id="replay6062-test", redelivery_source_position=1,
    )
    text = format_visible_event(event)
    assert "非工具执行请求" in text
    assert "synthetic-tool" in text
    assert event.tool_args == {"x": "old"}


@pytest.mark.parametrize("operation,position", [
    (None, 1), ("replay", None), ("", 1), ("replay", 0), ("replay", -1),
    ("replay", True), ("replay", "1"), ("replay", 1.0), ("x" * 129, 1),
    ("replay\nnew", 1), ("replay<control>", 1), ("中文", 1),
])
def test_invalid_provenance_is_rejected_instead_of_silently_becoming_fresh(
    operation, position,
):
    data = event_to_dict(_event())
    data.update(redelivery_operation_id=operation, redelivery_source_position=position)
    with pytest.raises(ValueError, match="HistoricalRedeliveryProvenanceInvalid"):
        event_from_dict(data)


def test_literal_old_body_cannot_erase_external_historical_label():
    event = _event(
        content="[a quoted old instruction]\nOriginal statement",
        redelivery_operation_id="replay6062-test", redelivery_source_position=1,
    )
    metadata = json.loads(format_visible_event(event).split("] ", 1)[1])
    assert metadata["excerpt"] == event.content
    assert "非新发生" in format_visible_event(event).split("] ", 1)[0]
