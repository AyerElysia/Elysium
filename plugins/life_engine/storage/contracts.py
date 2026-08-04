"""Backend-neutral runtime bundle for life-domain storage consumers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.kernel.storage import (
    AsyncUnitOfWork,
    mysql_storage_health,
    sqlite_storage_health,
)

from .authority import AuthorityRegistry, FileAuthorityRegistry, MySQLAuthorityRegistry
from .models import AuthorityToken, BackendGeneration, BackendKind


class StorageRuntimeError(RuntimeError):
    """Base class for coherent backend runtime failures."""


class StorageRuntimeDisabled(StorageRuntimeError):
    """Raised when a consumer asks a disabled storage runtime to write."""


class StorageRuntimeClosed(StorageRuntimeError):
    """Raised when a consumer uses a runtime after its owned engine was closed."""


WriteFence = Callable[[AsyncSession], Awaitable[None]]
WriterValidator = Callable[[], Awaitable[None]]


class StorageWriterRole(StrEnum):
    """Limit which write surface one fenced runtime may use."""

    ACTIVE = "active"
    CANDIDATE_COPY = "candidate_copy"


@dataclass(slots=True)
class StorageBackendRuntime:
    """One coherent backend/generation/authority/session bundle.

    A runtime never chooses repositories independently.  Every consumer gets
    the same engine, generation and authority token, and every committing unit
    of work is fenced inside its exact transaction.
    """

    enabled: bool
    backend: BackendKind
    backend_identity: str
    generation: BackendGeneration | None
    authority_registry: AuthorityRegistry | None
    authority_token: AuthorityToken | None
    engine: AsyncEngine | None
    session_factory: async_sessionmaker[AsyncSession] | None
    _write_fence: WriteFence | None = None
    _writer_validator: WriterValidator | None = None
    writer_role: StorageWriterRole = StorageWriterRole.ACTIVE
    writer_epoch: int = 0
    _closed: bool = False

    @classmethod
    def disabled(cls, backend: BackendKind) -> StorageBackendRuntime:
        return cls(
            enabled=False,
            backend=backend,
            backend_identity=f"{backend.value}://disabled",
            generation=None,
            authority_registry=None,
            authority_token=None,
            engine=None,
            session_factory=None,
        )

    def unit_of_work(self) -> AsyncUnitOfWork:
        """Create a fenced UoW; no unfenced write session is exposed."""

        if self._closed:
            raise StorageRuntimeClosed("storage runtime is closed")
        if not self.enabled or self.session_factory is None:
            raise StorageRuntimeDisabled("storage runtime is disabled")

        if self._write_fence is not None:
            return AsyncUnitOfWork(
                self.session_factory,
                before_commit=self._write_fence,
            )
        if self.authority_registry is None or self.authority_token is None:
            raise StorageRuntimeDisabled("storage runtime has no writer authority")

        token = self.authority_token
        registry = self.authority_registry
        if isinstance(registry, FileAuthorityRegistry):
            return AsyncUnitOfWork(
                self.session_factory,
                scope_factory=lambda: registry.fenced(token),
            )
        if isinstance(registry, MySQLAuthorityRegistry):

            async def _validate_before_commit(session: AsyncSession) -> None:
                connection = await session.connection()
                await registry.validate_in_transaction(connection, token)

            return AsyncUnitOfWork(
                self.session_factory,
                before_commit=_validate_before_commit,
            )
        raise StorageRuntimeError(
            f"unsupported authority registry: {type(registry).__name__}"
        )

    async def validate_writer(self) -> None:
        """Validate the exact active or migration writer without guessing mode."""

        if self._closed:
            raise StorageRuntimeClosed("storage runtime is closed")
        if not self.enabled:
            raise StorageRuntimeDisabled("storage runtime is disabled")
        if self._writer_validator is not None:
            await self._writer_validator()
            return
        if self.authority_registry is None or self.authority_token is None:
            raise StorageRuntimeDisabled("storage runtime has no writer authority")
        await self.authority_registry.validate(self.authority_token)

    async def renew_authority(self, *, lease_seconds: int) -> AuthorityToken:
        """Renew the current lease without changing backend or generation."""

        if self._closed:
            raise StorageRuntimeClosed("storage runtime is closed")
        if self.authority_registry is None or self.authority_token is None:
            raise StorageRuntimeDisabled("storage runtime is disabled")
        renewed = await self.authority_registry.renew(
            self.authority_token,
            lease_seconds=lease_seconds,
        )
        self.authority_token = renewed
        return renewed

    async def health(self) -> dict[str, Any]:
        """Return secret-free backend, generation and authority diagnostics."""

        if not self.enabled:
            return {
                "status": "disabled",
                "backend": self.backend.value,
                "backend_identity": self.backend_identity,
                "reason": "selectable storage runtime is not enabled",
            }
        if self._closed or self.engine is None or self.authority_registry is None:
            return {
                "status": "failed",
                "backend": self.backend.value,
                "backend_identity": self.backend_identity,
                "reason": "storage runtime is closed or incomplete",
            }
        if self.backend == BackendKind.LOCAL:
            backend_health = await sqlite_storage_health(
                self.engine,
                backend_identity=self.backend_identity,
            )
        else:
            backend_health = await mysql_storage_health(
                self.engine,
                backend_identity=self.backend_identity,
            )
        authority_health = await self.authority_registry.health()
        statuses = {
            str(backend_health.get("status")),
            str(authority_health.get("status")),
        }
        if "failed" in statuses:
            status = "failed"
        elif statuses != {"healthy"}:
            status = "degraded"
        else:
            status = "healthy"
        return {
            "status": status,
            "backend": self.backend.value,
            "backend_identity": self.backend_identity,
            "generation_id": self.generation.generation_id if self.generation else "",
            "schema_version": self.generation.schema_version if self.generation else 0,
            "backend_health": backend_health,
            "authority_health": authority_health,
        }

    async def close(self) -> None:
        """Close only the caller-owned SQLAlchemy engine."""

        if self._closed:
            return
        self._closed = True
        if self.engine is not None:
            await self.engine.dispose()


__all__ = [
    "StorageBackendRuntime",
    "StorageRuntimeClosed",
    "StorageRuntimeDisabled",
    "StorageRuntimeError",
    "StorageWriterRole",
]
