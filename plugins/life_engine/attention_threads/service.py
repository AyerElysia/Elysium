"""Consumer-facing AttentionThread service over injected authority ports."""

from __future__ import annotations

from typing import Any

from .contracts import (
    AttentionThreadAuthorityPort,
    AttentionThreadCommand,
    AttentionThreadCommit,
    AttentionThreadEventPage,
    AttentionThreadPage,
    AttentionThreadPageQuery,
    AttentionThreadValueChunk,
    AttentionThreadView,
    InstanceFocus,
    InstanceFocusPort,
)


class AttentionThreadService:
    """Thin lifecycle-neutral facade; injected ports retain all authority."""

    def __init__(
        self,
        authority: AttentionThreadAuthorityPort,
        focus: InstanceFocusPort,
    ) -> None:
        self._authority = authority
        self._focus = focus

    async def decide(
        self,
        command: AttentionThreadCommand,
    ) -> AttentionThreadCommit:
        return await self._authority.decide(command)

    async def get(self, thread_id: str) -> AttentionThreadView | None:
        return await self._authority.get(thread_id)

    async def page(self, query: AttentionThreadPageQuery) -> AttentionThreadPage:
        return await self._authority.page(query)

    async def event_page(
        self,
        thread_id: str,
        *,
        after_position: int = 0,
        limit: int = 100,
    ) -> AttentionThreadEventPage:
        return await self._authority.event_page(
            thread_id,
            after_position=after_position,
            limit=limit,
        )

    async def read_statement_chunk(
        self,
        event_id: str,
        *,
        offset_bytes: int = 0,
        max_bytes: int = 16 * 1024,
    ) -> AttentionThreadValueChunk:
        return await self._authority.read_statement_chunk(
            event_id,
            offset_bytes=offset_bytes,
            max_bytes=max_bytes,
        )

    async def set_focus(self, focus: InstanceFocus) -> InstanceFocus:
        return await self._focus.set_focus(focus)

    async def get_focus(self, instance_id: str) -> InstanceFocus | None:
        return await self._focus.get_focus(instance_id)

    async def clear_focus(
        self,
        instance_id: str,
        *,
        expected_revision: int,
    ) -> None:
        await self._focus.clear_focus(
            instance_id,
            expected_revision=expected_revision,
        )

    async def health_snapshot(self) -> dict[str, Any]:
        return await self._authority.health_snapshot()


__all__ = ["AttentionThreadService"]
