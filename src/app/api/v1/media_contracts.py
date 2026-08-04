"""Stable managed-media boundary shared by API and chat command domains."""

from __future__ import annotations

from typing import Protocol

from src.core.models.media import MediaAttachment


class ManagedMediaFailure(RuntimeError):
    """A safe managed-media failure that callers can map by domain code."""

    def __init__(self, code: str, *, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class ManagedMediaResolver(Protocol):
    """Resolve only authorized, intact managed objects for transport."""

    async def resolve_ready(
        self,
        media_id: str,
        *,
        actor_id: str,
        expected_type: str,
        resource_grants: tuple[str, ...] = (),
    ) -> MediaAttachment: ...


__all__ = ["ManagedMediaFailure", "ManagedMediaResolver"]
