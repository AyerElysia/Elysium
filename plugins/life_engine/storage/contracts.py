"""Backend-neutral runtime bundle for life-domain storage consumers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.kernel.storage import (
    AsyncUnitOfWork,
    mysql_storage_health,
    sqlite_storage_health,
)

from .authority import AuthorityRegistry, FileAuthorityRegistry, MySQLAuthorityRegistry
from .models import AuthorityToken, BackendGeneration, BackendKind

if TYPE_CHECKING:
    from .writer_claims import SingletonWriterClaim, SingletonWriterClaimPort


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
    shared_writers: bool = False
    _singleton_writer_claims: SingletonWriterClaimPort | None = None
    _managed_singleton_claims: dict[
        tuple[str, str], tuple[SingletonWriterClaim, int]
    ] = field(default_factory=dict, repr=False)
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

    def unit_of_work(
        self,
        *,
        writer_claim: SingletonWriterClaim | None = None,
    ) -> AsyncUnitOfWork:
        """Create a fenced UoW; no unfenced write session is exposed."""

        if self._closed:
            raise StorageRuntimeClosed("storage runtime is closed")
        if not self.enabled or self.session_factory is None:
            raise StorageRuntimeDisabled("storage runtime is disabled")

        async def _validate_claim(session: AsyncSession) -> None:
            if writer_claim is None:
                return
            if self._singleton_writer_claims is None:
                raise StorageRuntimeDisabled(
                    "storage runtime has no singleton writer claim registry"
                )
            await self._singleton_writer_claims.validate_in_transaction(
                session,
                writer_claim,
            )

        if self._write_fence is not None:

            async def _validate_shared_and_claim(session: AsyncSession) -> None:
                await self._write_fence(session)
                await _validate_claim(session)

            return AsyncUnitOfWork(
                self.session_factory,
                before_commit=_validate_shared_and_claim,
            )
        if self.authority_registry is None or self.authority_token is None:
            raise StorageRuntimeDisabled("storage runtime has no writer authority")

        token = self.authority_token
        registry = self.authority_registry
        if isinstance(registry, FileAuthorityRegistry):
            return AsyncUnitOfWork(
                self.session_factory,
                before_commit=_validate_claim if writer_claim is not None else None,
                scope_factory=lambda: registry.fenced(token),
            )
        if isinstance(registry, MySQLAuthorityRegistry):

            async def _validate_before_commit(session: AsyncSession) -> None:
                connection = await session.connection()
                await registry.validate_in_transaction(connection, token)
                await _validate_claim(session)

            return AsyncUnitOfWork(
                self.session_factory,
                before_commit=_validate_before_commit,
            )
        raise StorageRuntimeError(
            f"unsupported authority registry: {type(registry).__name__}"
        )

    async def bind_singleton_writer_write(
        self,
        session: AsyncSession,
        claim: SingletonWriterClaim,
    ) -> None:
        """Bind one transaction connection for claim-aware domain triggers.

        Domain adapters must call this inside ``unit_of_work(writer_claim=claim)``
        immediately before statements guarded by database triggers, then clear
        the binding in ``finally``. The opaque token never leaves the shared
        claim registry and local backends retain the same validation contract.
        """

        if self._closed:
            raise StorageRuntimeClosed("storage runtime is closed")
        if self._singleton_writer_claims is None:
            raise StorageRuntimeDisabled(
                "storage runtime has no singleton writer claim registry"
            )
        await self._singleton_writer_claims.bind_runtime_state_write(
            session,
            claim,
        )

    async def clear_singleton_writer_write(
        self,
        session: AsyncSession,
    ) -> None:
        """Clear the current transaction connection's trigger binding."""

        if self._singleton_writer_claims is None:
            return
        await self._singleton_writer_claims.clear_runtime_state_write(session)

    async def acquire_singleton_writer(
        self,
        *,
        namespace: str,
        state_key: str,
        owner_instance_id: str,
        lease_seconds: int,
    ) -> SingletonWriterClaim:
        """Acquire and manage one generation-scoped singleton writer claim."""

        if self._closed:
            raise StorageRuntimeClosed("storage runtime is closed")
        if self._singleton_writer_claims is None:
            raise StorageRuntimeDisabled(
                "storage runtime has no singleton writer claim registry"
            )
        key = (str(namespace), str(state_key))
        if key in self._managed_singleton_claims:
            raise StorageRuntimeError(
                f"singleton writer is already managed locally: {key[0]}:{key[1]}"
            )
        claim = await self._singleton_writer_claims.acquire(
            namespace=namespace,
            state_key=state_key,
            owner_instance_id=owner_instance_id,
            lease_seconds=lease_seconds,
        )
        self._managed_singleton_claims[key] = (claim, int(lease_seconds))
        return claim

    async def renew_singleton_writer(
        self,
        claim: SingletonWriterClaim,
        *,
        lease_seconds: int,
    ) -> SingletonWriterClaim:
        """Renew an exact managed claim and replace its local lease snapshot."""

        if self._closed:
            raise StorageRuntimeClosed("storage runtime is closed")
        if self._singleton_writer_claims is None:
            raise StorageRuntimeDisabled(
                "storage runtime has no singleton writer claim registry"
            )
        key = (claim.namespace, claim.state_key)
        managed = self._managed_singleton_claims.get(key)
        if managed is None or managed[0] != claim:
            raise StorageRuntimeError(
                f"singleton writer is not managed locally: {key[0]}:{key[1]}"
            )
        renewed = await self._singleton_writer_claims.renew(
            claim,
            lease_seconds=lease_seconds,
        )
        self._managed_singleton_claims[key] = (renewed, int(lease_seconds))
        return renewed

    async def release_singleton_writer(
        self,
        claim: SingletonWriterClaim,
    ) -> bool:
        """Release an exact managed claim without touching another epoch."""

        if self._singleton_writer_claims is None:
            return False
        key = (claim.namespace, claim.state_key)
        managed = self._managed_singleton_claims.get(key)
        if managed is None or managed[0] != claim:
            return False
        released = await self._singleton_writer_claims.release(claim)
        self._managed_singleton_claims.pop(key, None)
        return released

    async def _renew_managed_singleton_writers(self) -> None:
        if self._singleton_writer_claims is None:
            return
        for key, (claim, lease_seconds) in tuple(
            self._managed_singleton_claims.items()
        ):
            renewed = await self._singleton_writer_claims.renew(
                claim,
                lease_seconds=lease_seconds,
            )
            self._managed_singleton_claims[key] = (renewed, lease_seconds)

    async def _release_managed_singleton_writers(self) -> None:
        if self._singleton_writer_claims is None:
            self._managed_singleton_claims.clear()
            return
        errors: list[BaseException] = []
        for key, (claim, _) in tuple(self._managed_singleton_claims.items()):
            try:
                await self._singleton_writer_claims.release(claim)
            except BaseException as exc:  # noqa: BLE001 - aggregate exact cleanup
                errors.append(exc)
            else:
                self._managed_singleton_claims.pop(key, None)
        if errors:
            raise BaseExceptionGroup(
                "singleton writer claim release failed",
                errors,
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
        """Renew an exclusive lease or revalidate shared MySQL authority."""

        if self._closed:
            raise StorageRuntimeClosed("storage runtime is closed")
        if self.authority_registry is None or self.authority_token is None:
            raise StorageRuntimeDisabled("storage runtime is disabled")
        if self.shared_writers:
            await self.validate_writer()
            renewed = self.authority_token
        else:
            renewed = await self.authority_registry.renew(
                self.authority_token,
                lease_seconds=lease_seconds,
            )
            self.authority_token = renewed
        await self._renew_managed_singleton_writers()
        return renewed

    async def revoke_authority(self) -> int | None:
        """Revoke an exclusive token; shared writers only release local ownership."""

        await self._release_managed_singleton_writers()
        if self.authority_registry is None or self.authority_token is None:
            return None
        if self.shared_writers:
            self.authority_token = None
            return None
        token = self.authority_token
        next_epoch = await self.authority_registry.revoke(token)
        self.authority_token = None
        return next_epoch

    def invalidate_writer(self) -> None:
        """Fail closed locally after lease renewal can no longer be proven."""

        self.authority_token = None

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
            "writer_mode": "shared" if self.shared_writers else "exclusive",
            "managed_singleton_writer_count": len(
                self._managed_singleton_claims
            ),
            "backend_health": backend_health,
            "authority_health": authority_health,
        }

    async def close(self) -> None:
        """Close only the caller-owned SQLAlchemy engine."""

        if self._closed:
            return
        errors: list[BaseException] = []
        try:
            await self._release_managed_singleton_writers()
        except BaseException as exc:  # noqa: BLE001 - dispose must still run
            errors.append(exc)
        self._closed = True
        if self.engine is not None:
            try:
                await self.engine.dispose()
            except BaseException as exc:  # noqa: BLE001 - aggregate cleanup
                errors.append(exc)
        if errors:
            raise BaseExceptionGroup("storage runtime close failed", errors)


__all__ = [
    "StorageBackendRuntime",
    "StorageRuntimeClosed",
    "StorageRuntimeDisabled",
    "StorageRuntimeError",
    "StorageWriterRole",
]
