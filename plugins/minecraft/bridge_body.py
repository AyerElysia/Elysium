"""Game-body adapter backed by the authenticated bridge protocol."""

from __future__ import annotations

from .bridge_client import MinecraftBridgeClient
from .embodiment_contracts import ActionCommand, ActionReceipt, WorldObservation


class BridgeBody:
    """Expose one bridge endpoint as an execution-kernel body."""

    def __init__(self, name: str, client: MinecraftBridgeClient) -> None:
        """Bind an explicit body name to a bridge client."""

        if not name.strip():
            raise ValueError("body name must not be empty")
        self._name = name
        self._client = client

    @property
    def name(self) -> str:
        """Return the explicitly configured body name."""

        return self._name

    async def open(self) -> None:
        """Open and authenticate the persistent bridge connection."""

        await self._client.open()

    async def observe(self, after_sequence: int | None = None) -> WorldObservation:
        """Return a fresh structured observation from the body."""

        return await self._client.observe(after_sequence)

    async def act(self, command: ActionCommand) -> ActionReceipt:
        """Execute one correlated command and await its terminal receipt."""

        return await self._client.act(command)

    async def interrupt(self, intent_id: str, reason: str) -> None:
        """Interrupt an intention and ask the bridge to release controls."""

        await self._client.interrupt(intent_id, reason)

    async def close(self) -> None:
        """Release controls and close the persistent bridge."""

        await self._client.close()
