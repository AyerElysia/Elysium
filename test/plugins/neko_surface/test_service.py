from __future__ import annotations

import pytest

from plugins.neko_surface.protocol import SurfaceEvent
from plugins.neko_surface.service import (
    BoundedEventQueue,
    EventDeduplicator,
    token_is_valid,
)


def _state_event(event_id: str, *, sequence: int, priority: int) -> SurfaceEvent:
    return SurfaceEvent.create(
        "state",
        event_id=event_id,
        sequence=sequence,
        origin="neo",
        payload={"value": event_id},
        priority=priority,
    )


@pytest.mark.asyncio
async def test_bounded_queue_sheds_old_low_priority_event() -> None:
    queue = BoundedEventQueue(maxsize=2)
    await queue.put(_state_event("low-old", sequence=1, priority=1))
    await queue.put(_state_event("normal", sequence=2, priority=5))

    result = await queue.put(_state_event("critical", sequence=3, priority=9))

    assert result.enqueued is True
    assert result.dropped_event_id == "low-old"
    assert (await queue.get()).event_id == "normal"
    assert (await queue.get()).event_id == "critical"


@pytest.mark.asyncio
async def test_bounded_queue_rejects_new_low_priority_event() -> None:
    queue = BoundedEventQueue(maxsize=1)
    await queue.put(_state_event("critical", sequence=1, priority=9))

    result = await queue.put(_state_event("low-new", sequence=2, priority=1))

    assert result.enqueued is False
    assert result.dropped_event_id == "low-new"
    assert (await queue.get()).event_id == "critical"


def test_event_deduplicator_survives_reconnect_window() -> None:
    dedupe = EventDeduplicator(capacity=2, ttl=10.0)

    assert dedupe.remember("surface:event-1", now=1.0) is False
    assert dedupe.remember("surface:event-1", now=2.0) is True
    assert dedupe.remember("surface:event-2", now=3.0) is False
    assert dedupe.remember("surface:event-3", now=4.0) is False
    assert dedupe.remember("surface:event-1", now=5.0) is False


def test_surface_token_auth_requires_configured_exact_match() -> None:
    assert token_is_valid("secret", "secret") is True
    assert token_is_valid("wrong", "secret") is False
    assert token_is_valid("", "secret") is False
    assert token_is_valid("secret", "") is False
