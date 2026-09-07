"""Pure synthetic UTF-8 and source-integrity contracts for historical delivery."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.life_engine.service.event_builder import EventType, LifeEngineEvent
from plugins.life_engine.service.heartbeat_rolling import format_visible_event
from plugins.life_engine.service.historical_redelivery import (
    HISTORICAL_REDELIVERY_MAX_UTF8_BYTES,
    format_historical_redelivery,
)
from plugins.life_engine.service.subconscious_context import SubconsciousContextManager

OCCURRENCE = "synthetic-occurrence-0123456789abcdef"
REFERENCE = f"life-event-occurrence:{OCCURRENCE}"
TIMESTAMP = "2026-09-06T14:21:03.591000+00:00"


def _event(**changes) -> LifeEngineEvent:
    values = {
        "event_id": "synthetic-source-event",
        "event_type": EventType.CONSCIOUS_ACTIVITY,
        "timestamp": TIMESTAMP,
        "sequence": 1001,
        "source": "synthetic-source",
        "source_detail": "synthetic-surface",
        "content": "synthetic visible source words",
        "raw_content": "synthetic full source words",
        "occurrence_id": OCCURRENCE,
        "content_ref": REFERENCE,
        "redelivery_operation_id": "synthetic-recovery",
        "redelivery_source_position": 123456,
    }
    values.update(changes)
    return LifeEngineEvent(**values)


def _render(event: LifeEngineEvent, renderer: str) -> str:
    if renderer == "visible":
        return format_visible_event(event)
    return SubconsciousContextManager()._render_event(event)


def _assert_raw_fingerprint(text: str, raw: str) -> None:
    metadata = json.loads(text.split("] ", 1)[1])
    assert metadata["raw_sha256"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert metadata["raw_bytes"] == len(raw.encode("utf-8"))


def _assert_historical_metadata(text: str) -> None:
    assert "历史活动重投" in text
    assert "非新发生" in text
    assert "非工具执行请求" in text
    assert TIMESTAMP in text
    assert "123456" in text
    assert REFERENCE in text
    assert "nucleus_read_event" in text
    assert "occurrence_id" in text
    metadata = json.loads(text.split("] ", 1)[1])
    assert metadata["occurrence_id"] == REFERENCE
    assert metadata["read_with"] == "nucleus_read_event"
    assert metadata["delivery"] in {"visible_projection", "excerpt_ref"}


def test_fixed_cap_covers_the_whole_wrapper_not_only_the_excerpt() -> None:
    assert HISTORICAL_REDELIVERY_MAX_UTF8_BYTES == 512
    event = _event(raw_content="complete authority 🌸" * 1000)
    rendered = format_historical_redelivery(event, "visible excerpt 中文🌸" * 1000)
    assert len(rendered.encode("utf-8")) <= 512
    assert "excerpt_ref" in rendered
    _assert_historical_metadata(rendered)
    _assert_raw_fingerprint(rendered, event.raw_content)


@pytest.mark.parametrize("fragment", ["ASCII bytes ", "完整中文片段", "🌸🪷🙂"])
@pytest.mark.parametrize("renderer", ["visible", "subconscious"])
def test_both_renderers_bound_large_historical_events_without_splitting_utf8(
    fragment: str,
    renderer: str,
) -> None:
    raw = "authority-only:" + fragment * 1000
    event = _event(content=fragment * 1000, raw_content=raw)
    before = deepcopy(event)
    rendered = _render(event, renderer)
    assert 0 < len(rendered.encode("utf-8")) <= 512
    assert "\ufffd" not in rendered
    assert rendered.encode("utf-8").decode("utf-8") == rendered
    _assert_historical_metadata(rendered)
    _assert_raw_fingerprint(rendered, raw)
    assert _render(event, renderer) == rendered
    assert event == before


@pytest.mark.parametrize("renderer", ["visible", "subconscious"])
@pytest.mark.parametrize(
    "kind",
    [
        EventType.MESSAGE,
        EventType.HEARTBEAT,
        EventType.CONSCIOUS_ACTIVITY,
        EventType.TOOL_CALL,
        EventType.TOOL_RESULT,
        EventType.AGENT_RESULT,
    ],
)
def test_historical_event_kinds_share_one_whole_projection_byte_cap(
    renderer: str,
    kind: EventType,
) -> None:
    event = _event(
        event_type=kind,
        content="合成可见正文" * 300,
        raw_content="完整权威原文🌸" * 400,
        sender="synthetic-sender",
        tool_name="synthetic-tool",
        tool_args={"synthetic_arg": "x" * 1000},
        tool_success=True,
        call_id="synthetic-call",
    )
    before = deepcopy(event)
    text = _render(event, renderer)
    assert len(text.encode("utf-8")) <= 512
    _assert_historical_metadata(text)
    _assert_raw_fingerprint(text, event.raw_content)
    assert event == before


def test_short_projection_preserves_visible_body_and_hashes_authority_not_display() -> (
    None
):
    body = "  synthetic visible line\n完整片段🌸  "
    raw = "different complete authority " * 30
    event = _event(content="different source presentation", raw_content=raw)
    text = format_historical_redelivery(event, body)
    assert "visible_projection" in text
    assert json.loads(text.split("] ", 1)[1])["excerpt"] == body
    _assert_raw_fingerprint(text, raw)
    assert hashlib.sha256(body.encode("utf-8")).hexdigest() not in text
    assert len(text.encode("utf-8")) <= 512


@pytest.mark.parametrize("raw", [None, "", "  \n", "原始权威🌸"])
def test_only_none_raw_content_falls_back_to_content(raw: str | None) -> None:
    content = "synthetic fallback content"
    event = _event(content=content, raw_content=raw)
    text = format_historical_redelivery(event, "visible text")
    expected = content if raw is None else raw
    _assert_raw_fingerprint(text, expected)
    if raw is not None:
        assert hashlib.sha256(content.encode("utf-8")).hexdigest() not in text


@pytest.mark.parametrize("body", ["", " ", "\n\t  "])
def test_blank_visible_body_does_not_turn_into_acknowledgeable_metadata(
    body: str,
) -> None:
    event = _event(occurrence_id=None, content_ref=None)
    assert format_historical_redelivery(event, body) == body


def test_nonhistorical_helper_returns_exact_body_without_a_new_cap() -> None:
    event = _event(
        occurrence_id=None,
        content_ref=None,
        redelivery_operation_id=None,
        redelivery_source_position=None,
    )
    body = "  ordinary unbounded view🌸\n" * 300
    assert format_historical_redelivery(event, body) == body
    assert len(body.encode("utf-8")) > 512


@pytest.mark.parametrize("renderer", ["visible", "subconscious"])
def test_ordinary_renderers_keep_existing_large_content_unchanged(
    renderer: str,
) -> None:
    body = "synthetic ordinary content " * 40
    event = _event(
        event_type=EventType.HEARTBEAT,
        content=body,
        raw_content=body,
        redelivery_operation_id=None,
        redelivery_source_position=None,
    )
    text = _render(event, renderer)
    expected = (
        body.strip()
        if renderer == "visible"
        else f"- #1001 HEARTBEAT ref={REFERENCE} {body}"
    )
    assert text == expected
    assert len(text.encode("utf-8")) > 512
    assert "历史活动重投" not in text


@pytest.mark.parametrize("identity", [None, "", " \n\t", " leading", "trailing "])
def test_nonblank_redelivery_without_occurrence_fails_even_with_a_content_ref(
    identity: str | None,
) -> None:
    event = _event(occurrence_id=identity, content_ref=REFERENCE)
    before = deepcopy(event)
    with pytest.raises(ValueError) as exc:
        format_historical_redelivery(event, "synthetic-private-body")
    assert "synthetic-private-body" not in str(exc.value)
    assert event.raw_content not in str(exc.value)
    assert event == before


@pytest.mark.parametrize("content_ref", [None, "", "untrusted-reference:other"])
def test_continuation_reference_is_canonical_and_uses_complete_occurrence(
    content_ref,
) -> None:
    event = _event(content_ref=content_ref)
    text = format_historical_redelivery(event, "short view")
    assert REFERENCE in text
    assert "nucleus_read_event" in text
    if content_ref and content_ref != REFERENCE:
        assert content_ref not in text


@pytest.mark.parametrize(
    "changes",
    [
        {"occurrence_id": "i" * 600},
        {"timestamp": "t" * 600},
    ],
)
def test_unfit_metadata_fails_instead_of_truncating_source_identity(changes) -> None:
    event = _event(**changes)
    before = deepcopy(event)
    with pytest.raises(ValueError) as exc:
        format_historical_redelivery(event, "synthetic-private-body")
    assert "synthetic-private-body" not in str(exc.value)
    assert event.raw_content not in str(exc.value)
    assert event == before


@pytest.mark.parametrize("kind", [EventType.TOOL_CALL, EventType.TOOL_RESULT])
def test_redacted_historical_tools_never_recover_private_payload_in_wrapper(
    kind: EventType,
) -> None:
    secret = "SYNTHETIC-PRIVATE-TOOL-PAYLOAD-DO-NOT-REPROJECT"
    event = _event(
        event_type=kind,
        content=secret,
        raw_content=secret * 200,
        tool_name="synthetic-tool",
        tool_args={"key": secret},
        tool_success=True,
        call_id="synthetic-call",
    )
    before = deepcopy(event)
    text = SubconsciousContextManager()._render_event(
        event, include_tool_payloads=False
    )
    assert len(text.encode("utf-8")) <= 512
    assert secret not in text
    _assert_historical_metadata(text)
    _assert_raw_fingerprint(text, event.raw_content)
    assert event == before


@pytest.mark.asyncio
async def test_canonical_projection_reference_is_accepted_by_exact_occurrence_reader() -> (
    None
):
    from plugins.life_engine.tools.event_grep_tools import _read_authoritative_event

    event = _event()
    text = format_historical_redelivery(event, "short view")
    assert REFERENCE in text
    store = SimpleNamespace(get_by_occurrence_id=AsyncMock(return_value=event))
    service = SimpleNamespace(_get_life_event_store=Mock(return_value=store))
    found, frontier = await _read_authoritative_event(service, REFERENCE)
    assert found is event
    assert frontier["occurrence_id"] == OCCURRENCE
    store.get_by_occurrence_id.assert_awaited_once_with(OCCURRENCE)


def test_source_fields_do_not_change_when_only_delivery_sequence_changes() -> None:
    event = _event(content="visible " * 200, raw_content="authority " * 200)
    replay = replace(event, sequence=987654)
    first = format_historical_redelivery(event, "same bounded view")
    second = format_historical_redelivery(replay, "same bounded view")
    assert first == second
    assert replay.occurrence_id == event.occurrence_id
    assert replay.timestamp == event.timestamp
    assert replay.raw_content == event.raw_content


def test_exact_512_byte_visible_boundary_and_one_byte_overflow() -> None:
    event = _event()
    minimal = format_historical_redelivery(event, "x")
    visible_overhead = len(minimal.encode("utf-8")) - 1
    body = "x" * (512 - visible_overhead)
    exact = format_historical_redelivery(event, body)
    assert len(exact.encode("utf-8")) == 512
    metadata = json.loads(exact.split("] ", 1)[1])
    assert metadata["delivery"] == "visible_projection"
    assert metadata["excerpt"] == body

    over = format_historical_redelivery(event, body + "x")
    assert len(over.encode("utf-8")) <= 512
    assert json.loads(over.split("] ", 1)[1])["delivery"] == "excerpt_ref"


@pytest.mark.parametrize("fragment", ['quoted " slash \\\n', "中文🌸\t\x00"])
def test_actual_json_escaping_counts_toward_cap_and_excerpt_remains_exact_prefix(
    fragment: str,
) -> None:
    body = fragment * 300
    event = _event(raw_content="authority remains separate " * 300)
    text = format_historical_redelivery(event, body)
    metadata = json.loads(text.split("] ", 1)[1])
    assert len(text.encode("utf-8")) <= 512
    assert metadata["delivery"] == "excerpt_ref"
    assert 0 < len(metadata["excerpt"]) < len(body)
    assert body.startswith(metadata["excerpt"])
    _assert_raw_fingerprint(text, event.raw_content)
