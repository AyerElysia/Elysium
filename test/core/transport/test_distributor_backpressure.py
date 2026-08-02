"""Lossless inbound distribution backpressure tests."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from src.core.models.stream import StreamContext
from src.core.transport.distribution import distributor


async def test_unread_backpressure_waits_until_capacity_is_released(
    monkeypatch,
) -> None:
    """A full stream must block producers instead of dropping an unread message."""

    monkeypatch.setattr(distributor, "_UNREAD_BACKPRESSURE_POLL_SECONDS", 0.001)
    context = StreamContext(stream_id="stream-a", max_unread_messages=1)
    context.add_unread_message(MagicMock())

    waiter = asyncio.create_task(
        distributor._wait_for_unread_capacity(context, "stream-a")
    )
    await asyncio.sleep(0.01)
    assert waiter.done() is False

    context.unread_messages.clear()
    await asyncio.wait_for(waiter, timeout=0.1)
    assert context.has_unread_capacity is True
