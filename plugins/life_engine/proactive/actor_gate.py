"""Process-local linearization gate for local Presence and proactive writes."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator


class ProactiveActorDecisionGate:
    """Serialize local actor deactivation with proactive SQL commits.

    Selected/MySQL storage locks the Presence row in the same transaction. In
    disabled/local mode Presence and proactive state live in different stores,
    so the service owns this process-wide gate and uses it around both the
    lifecycle transition and the complete proactive transaction commit.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def hold(self, instance_id: str) -> AsyncIterator[None]:
        identity = str(instance_id or "").strip()
        if not identity:
            raise ValueError("proactive actor identity must not be empty")
        async with self._lock:
            yield


__all__ = ["ProactiveActorDecisionGate"]
