"""Historical continuation metadata survives real context projection exits.

All event bodies and service state are synthetic; persistence and lifecycle
boundaries are isolated, with no model call or formal runtime data access.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
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
from plugins.life_engine.service.heartbeat_rolling import format_visible_event
from plugins.life_engine.service.historical_redelivery import (
    format_historical_redelivery,
)
from plugins.life_engine.service.state_manager import event_to_dict
from plugins.life_engine.service.subconscious_context import SubconsciousContextManager
from src.kernel.llm.context_delivery import EffectiveContextReceipt


def _event(sequence=1, *, historical=True, **changes):
    values = {
        "event_id": f"historical-event-{sequence}",
        "event_type": EventType.CONSCIOUS_ACTIVITY,
        "timestamp": "2026-09-06T14:21:03.591000+00:00",
        "sequence": sequence,
        "source": "synthetic-source",
        "source_detail": "isolated-redelivery-exit",
        "source_instance_id": "synthetic-instance",
        "content": "synthetic original body " + "x" * 2000,
        "raw_content": "synthetic raw body " + "r" * 8000,
        "occurrence_id": f"synthetic-original-occurrence-{sequence}",
        "content_ref": f"life-event-occurrence:synthetic-original-occurrence-{sequence}",
    }
    if historical:
        values.update(
            redelivery_operation_id="synthetic-recovery",
            redelivery_source_position=262000 + sequence,
        )
    values.update(changes)
    return LifeEngineEvent(**values)


def _metadata(text):
    """The wrapper is one physical line; callers may have their own outer heading."""
    header, body = text.split("] ", 1)
    assert header.startswith("[历史活动重投，非新发生、非工具执行请求；")
    return json.loads(body)


def _bare_service(events):
    service = LifeEngineService.__new__(LifeEngineService)
    service._event_history = events
    service._pending_events = []
    service._state = LifeEngineState()
    service._lock = asyncio.Lock()
    service._subconscious_context = SubconsciousContextManager(
        max_chars=16000, recent_group_count=0
    )
    return service


@pytest.mark.parametrize(
    "kind",
    [
        EventType.CONSCIOUS_ACTIVITY,
        EventType.TOOL_CALL,
        EventType.TOOL_RESULT,
    ],
)
def test_wake_and_salient_exits_keep_the_same_complete_historical_wrapper(kind):
    event = _event(
        event_type=kind,
        tool_name="synthetic-tool",
        tool_args={"x": "old"},
        tool_success=True,
    )
    service = _bare_service([event])
    before = event_to_dict(event)
    expected = (
        service._subconscious_context._render_event(event, include_tool_payloads=False)
        if kind in {EventType.TOOL_CALL, EventType.TOOL_RESULT}
        else format_visible_event(event)
    )

    assert service._build_wake_context_text([event]) == expected
    assert service._format_salient_event(event) == expected
    assert len(expected.encode("utf-8")) <= 512
    assert "执行了" not in expected
    assert _metadata(expected)["occurrence_id"] == event.content_ref
    assert event_to_dict(event) == before


def test_wake_does_not_fold_historical_tools_into_fresh_tool_run():
    fresh_first = _event(1, historical=False, event_type=EventType.TOOL_CALL)
    old = _event(2, event_type=EventType.TOOL_CALL, tool_name="historical-tool")
    fresh_last = _event(3, historical=False, event_type=EventType.TOOL_CALL)
    service = _bare_service([fresh_first, old, fresh_last])

    rendered = service._build_wake_context_text([fresh_first, old, fresh_last])

    assert rendered.count("执行了 1 次工具操作") == 2
    assert "执行了 3 次工具操作" not in rendered
    expected_old = service._subconscious_context._render_event(
        old, include_tool_payloads=False
    )
    assert rendered.count(expected_old) == 1


async def test_real_recent_exit_wraps_once_and_keeps_source_inside_event_byte_cap():
    event = _event(content="small visible", raw_content="small raw")
    service = _bare_service([event])
    before = event_to_dict(event)

    view = await service.get_recent_subconscious_context(group_limit=1, max_bytes=2000)

    assert view.event_ids == (event.event_id,)
    assert view.content.count("[历史活动重投，非新发生、非工具执行请求；") == 1
    wrapper = view.content[view.content.index("[历史活动重投") :]
    assert len(wrapper.encode("utf-8")) <= 512
    metadata = _metadata(wrapper)
    assert metadata["occurrence_id"] == event.content_ref
    assert (
        metadata["raw_sha256"] == hashlib.sha256(event.raw_content.encode()).hexdigest()
    )
    assert "source=synthetic-source" in metadata["excerpt"]
    assert event_to_dict(event) == before


@pytest.mark.parametrize("include_tool_payloads", [True, False])
async def test_recent_outer_budget_omits_whole_historical_group_not_metadata(
    include_tool_payloads,
):
    event = _event(event_type=EventType.TOOL_RESULT, tool_name="inspect")
    service = _bare_service([event])
    full = await service.get_recent_subconscious_context(
        group_limit=1,
        max_bytes=2000,
        include_tool_payloads=include_tool_payloads,
    )
    assert full.event_ids == (event.event_id,)
    too_small = await service.get_recent_subconscious_context(
        group_limit=1,
        max_bytes=full.delivered_bytes - 1,
        include_tool_payloads=include_tool_payloads,
    )

    assert too_small.content == ""
    assert too_small.event_ids == ()
    assert too_small.group_count == 0
    assert too_small.omitted_group_count == 1
    assert not too_small.truncated
    assert service._state.heartbeat_context_cursor == 0


async def test_recent_budget_can_keep_newer_group_without_claiming_omitted_old_group():
    old = _event(1)
    new = _event(2, content="new visible", raw_content="new raw")
    service = _bare_service([new])
    only_new = await service.get_recent_subconscious_context(
        group_limit=2, max_bytes=2000
    )
    service._event_history = [old, new]

    limited = await service.get_recent_subconscious_context(
        group_limit=2,
        max_bytes=only_new.delivered_bytes,
    )

    assert limited.event_ids == (new.event_id,)
    assert limited.content == only_new.content
    assert old.content_ref not in limited.content
    assert limited.omitted_group_count == 1


def test_salient_single_historical_entry_over_budget_keeps_cursor_and_metadata_unseen():
    event = _event(event_type=EventType.AGENT_RESULT)
    service = _bare_service([event])
    config = SimpleNamespace(
        runtime_sync=SimpleNamespace(
            salient_tail_enabled=True,
            salient_tail_max_items=4,
            salient_tail_max_chars=200,
        )
    )
    service._cfg = lambda: config
    assert len(service._format_salient_event(event)) > 200

    body, cursor = service._build_salient_activity_tail(
        [event],
        0,
        current_stream_id="",
        unified_chatter_context=True,
    )

    assert body == ""
    assert cursor == 0
    config.runtime_sync.salient_tail_max_chars = 1000
    body, cursor = service._build_salient_activity_tail(
        [event],
        0,
        current_stream_id="",
        unified_chatter_context=True,
    )
    assert body == format_visible_event(event)
    assert cursor == event.sequence


def test_salient_prefix_omission_keeps_newer_historical_wrapper_complete():
    events = [
        _event(1, event_type=EventType.AGENT_RESULT),
        _event(2, event_type=EventType.TOOL_RESULT, tool_success=False),
    ]
    service = _bare_service(events)
    expected = service._subconscious_context._render_event(
        events[-1], include_tool_payloads=False
    )
    service._cfg = lambda: SimpleNamespace(
        runtime_sync=SimpleNamespace(
            salient_tail_enabled=True,
            salient_tail_max_items=4,
            salient_tail_max_chars=len(expected),
        )
    )

    body, _cursor = service._build_salient_activity_tail(
        events,
        0,
        current_stream_id="",
        unified_chatter_context=True,
    )

    assert body == expected
    assert events[0].content_ref not in body
    assert _metadata(body)["occurrence_id"] == events[-1].content_ref


@asynccontextmanager
async def _isolated_gate(_reason):
    yield


@pytest.fixture
def delivery_service(tmp_path, monkeypatch, request):
    events = []
    for group_id, size in enumerate(getattr(request, "param", (20, 9, 29))):
        for _member in range(size):
            events.append(
                _event(
                    len(events) + 1,
                    heartbeat_run_id=f"synthetic-historical-group-{group_id}",
                    content="synthetic visible source " + "v" * 6000,
                    raw_content="synthetic raw source " + "r" * 9000,
                )
            )
    service = _bare_service(events)
    config = SimpleNamespace(
        settings=SimpleNamespace(workspace_path=str(tmp_path), log_heartbeat=False),
        model=SimpleNamespace(task_name="synthetic-historical-delivery"),
    )
    service._cfg = lambda: config
    service._state_dirty = False
    service._multi_writer_bridge = None
    service._opportunity_runtime = None
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


async def _assert_whole_group_delivery_cycles(
    service,
    *,
    group_sizes,
    max_beats,
    exact_beats=None,
):
    original_events = list(service._event_history)
    source_before = [event_to_dict(event) for event in original_events]
    groups = service._subconscious_context.group_events(original_events)
    assert [len(group.events) for group in groups] == list(group_sizes)
    assert all(group.closed for group in groups)
    assert all(
        len("\n".join(format_visible_event(e) for e in group.events)) <= 16000
        for group in groups
    )
    consumed_ids = set()
    beats = 0

    while len(consumed_ids) < len(original_events):
        prepared = await service._prepare_heartbeat_context()
        selected = set(prepared.selected_event_ids)
        acknowledged = set(prepared.acknowledged_event_ids)
        assert selected == acknowledged
        assert selected
        assert not selected & consumed_ids
        for group in groups:
            identities = set(group.event_ids)
            assert selected & identities in (set(), identities)
        proof = EffectiveContextReceipt(
            delivery_id=prepared.delivery_id,
            exact_present=True,
            expected_utf8_bytes=prepared.delivery_bytes,
            expected_sha256=prepared.delivery_sha256,
            effective_utf8_bytes=prepared.delivery_bytes,
            effective_sha256=prepared.delivery_sha256,
        )
        receipt = await service._commit_heartbeat_context(
            prepared,
            "",
            f"synthetic-redelivery-beat-{beats}",
            None,
            proof,
        )
        assert set(receipt.event_ids) == acknowledged
        consumed_ids.update(acknowledged)
        beats += 1
        assert beats <= max_beats

    if exact_beats is not None:
        assert beats == exact_beats
    assert service._state.heartbeat_context_cursor == sum(group_sizes)
    assert all(event.heartbeat_context_consumed for event in original_events)
    for original, event in zip(source_before, original_events, strict=True):
        restored = event_to_dict(event)
        restored["heartbeat_context_consumed"] = original["heartbeat_context_consumed"]
        assert restored == original
    final = await service._prepare_heartbeat_context()
    assert final.selected_event_ids == final.acknowledged_event_ids == []
    assert final.content == ""
    service._publish_raw_events.assert_not_awaited()


async def test_large_historical_groups_advance_only_whole_groups_over_successive_beats(
    delivery_service,
):
    await _assert_whole_group_delivery_cycles(
        delivery_service,
        group_sizes=(20, 9, 29),
        max_beats=3,
        exact_beats=2,
    )


_SYNTHETIC_112_GROUP_SIZES = (20, 9, 29, *(3,) * 8, *(2,) * 15)


@pytest.mark.parametrize(
    "delivery_service",
    [_SYNTHETIC_112_GROUP_SIZES],
    indirect=True,
    ids=["112-events-26-groups"],
)
async def test_112_historical_events_in_26_groups_commit_without_partial_delivery(
    delivery_service,
):
    assert len(_SYNTHETIC_112_GROUP_SIZES) == 26
    assert sum(_SYNTHETIC_112_GROUP_SIZES) == 112
    await _assert_whole_group_delivery_cycles(
        delivery_service,
        group_sizes=_SYNTHETIC_112_GROUP_SIZES,
        max_beats=6,
    )


@pytest.mark.parametrize("kind", [EventType.TOOL_CALL, EventType.TOOL_RESULT])
def test_cross_stream_historical_tools_keep_private_payload_out_of_both_chatter_exits(
    kind,
):
    secret = "SYNTHETIC-PRIVATE-CROSS-STREAM-PAYLOAD"
    event = _event(
        event_type=kind,
        stream_id="synthetic-source-stream",
        tool_name="synthetic-inspect",
        tool_args={"key": secret},
        tool_success=False,
        content=secret,
        raw_content=secret * 200,
    )
    service = _bare_service([event])
    before = event_to_dict(event)
    if kind == EventType.TOOL_RESULT:
        assert service._is_salient_event(
            event,
            current_stream_id="different-consumer-stream",
            cfg_runtime=SimpleNamespace(),
            unified_chatter_context=False,
        )
    for text in (
        service._build_wake_context_text([event]),
        service._format_salient_event(event),
    ):
        assert secret not in text
        assert text.splitlines() == [text]
        assert len(text.encode("utf-8")) <= 512
        metadata = _metadata(text)
        assert metadata["occurrence_id"] == event.content_ref
        assert metadata["read_with"] == "nucleus_read_event"
        assert (
            metadata["raw_sha256"]
            == hashlib.sha256(event.raw_content.encode("utf-8")).hexdigest()
        )
    assert event_to_dict(event) == before
    assert secret in format_visible_event(event)


_LINE_SEPARATORS = (
    "\n",
    "\r",
    "\r\n",
    "\v",
    "\f",
    "\x1c",
    "\x1d",
    "\x1e",
    "\u0085",
    "\u2028",
    "\u2029",
)


@pytest.mark.parametrize("separator", _LINE_SEPARATORS)
@pytest.mark.parametrize("field", ["body", "timestamp", "occurrence_id"])
def test_historical_metadata_is_one_physical_line_even_with_unicode_line_controls(
    separator,
    field,
):
    event = _event(content="source visible", raw_content="raw authority")
    body = "visible body"
    if field == "body":
        body = f"prefix{separator}suffix"
    else:
        setattr(event, field, f"prefix{separator}suffix")
    before = event_to_dict(event)

    text = format_historical_redelivery(event, body)

    assert text.splitlines() == [text]
    assert len(text.encode("utf-8")) <= 512
    metadata = _metadata(text)
    assert metadata["excerpt"] == body
    assert metadata["occurrence_id"] == f"life-event-occurrence:{event.occurrence_id}"
    assert event_to_dict(event) == before


@pytest.mark.parametrize("keep_historical_line", [False, True])
def test_final_sixty_kib_chatter_suffix_keeps_or_omits_complete_historical_line(
    keep_historical_line,
):
    event = _event(content="prefix\u0085middle\u2028tail\u2029end")
    wrapper = format_visible_event(event)
    assert wrapper.splitlines() == [wrapper]
    section = (wrapper + "\n" + "synthetic later section " * 200).rstrip()
    section_bytes = len(section.encode("utf-8"))
    digest = hashlib.sha256(section.encode("utf-8")).hexdigest()
    omission = f"[section_projection_omitted bytes={section_bytes}; sha256={digest}]"
    reserve = len(omission.encode("utf-8")) + 1
    allowed_line_bytes = len(wrapper.encode("utf-8")) if keep_historical_line else 200
    remaining = allowed_line_bytes + reserve
    hard_budget = service_module.LIFE_CHATTER_PROJECTED_SUFFIX_MAX_BYTES
    header = "H" * (hard_budget - 512 - remaining - 2)

    content, _delivery_id, _marker, _source_bytes, omitted_bytes = (
        LifeEngineService._build_bounded_chatter_suffix(
            header=header,
            sections=[section],
            world_delivery_id="synthetic-world",
        )
    )

    assert len(content.encode("utf-8")) <= hard_budget
    assert omission in content
    assert omitted_bytes > 0
    historical_lines = [
        line for line in content.splitlines() if line.startswith("[历史活动重投")
    ]
    if keep_historical_line:
        assert historical_lines == [wrapper]
        assert _metadata(historical_lines[0])["occurrence_id"] == event.content_ref
    else:
        assert historical_lines == []
        assert event.content_ref not in content
        assert "历史活动重投" not in content
